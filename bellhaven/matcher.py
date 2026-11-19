"""Match website facilities to CRM accounts and turn the differences into proposals.

This module is a pure function: data in, proposals out. It makes no network calls and
never writes anything, which is what makes it easy to test.

Principles:
- Address decides a match. Name similarity is supporting evidence only, never a match on
  its own ("Bellhaven of Carlisle" PA is not "Bellhaven of New Carlisle" OH).
- When the evidence cannot separate candidates, we do not guess. We raise an AMBIGUOUS
  item for a human to resolve.
- Same address does not automatically mean duplicate. Only same parent + same name + same
  address is proposed as a duplicate. Other look-alikes are flagged for review.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Dict, List, Optional

from .chow import BLOCKED, CHOW, DIRECT, parent_change_strategy
from .config import BELLHAVEN_PARENT_NAME
from .normalize import (is_po_box, map_care, name_similarity, norm_city, norm_name, norm_phone,
                        norm_street, norm_zip)

# Proposal actions
UPDATE_FIELDS = "UPDATE_FIELDS"
SET_PARENT = "SET_PARENT"
CHOW_NEW_SUCCESSOR = "CHOW_NEW_SUCCESSOR"
CHOW_LINK_EXISTING = "CHOW_LINK_EXISTING"
CREATE_ACCOUNT = "CREATE_ACCOUNT"
MARK_DUPLICATE = "MARK_DUPLICATE"
FLAG_REVIEW = "FLAG_REVIEW"
AMBIGUOUS = "AMBIGUOUS"  # never writes; a reviewer picks the right account

NOTE_TAG = "[Bellhaven reconciliation]"

# Fields captured in the "before" snapshot. The applier refuses to write if any drifted.
SNAPSHOT_FIELDS = [
    "name", "parent_id", "parent_name", "billing_street", "billing_city", "billing_state",
    "billing_zip", "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar",
    "chow_current_account", "duplicate_of_account", "note",
]


class MatchError(RuntimeError):
    pass


def snapshot(account: dict) -> dict:
    return {"account_id": account["account_id"], **{f: account.get(f) for f in SNAPSHOT_FIELDS}}


def fingerprint(action: str, target_key: str, proposed: dict) -> str:
    """Stable identity of a proposal: same action + same target + same values = same id."""
    blob = json.dumps([action, target_key, proposed], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def find_parent(accounts: List[dict], name: str = BELLHAVEN_PARENT_NAME) -> dict:
    hits = [a for a in accounts if a.get("name") == name and not a.get("parent_id")]
    if len(hits) != 1:
        raise MatchError(f"Expected exactly one parent account named {name!r}, found {len(hits)}")
    return hits[0]


def _addr_key(street, zip_code) -> tuple:
    return (norm_street(street), norm_zip(zip_code))


def _append_note(existing, addition: str) -> str:
    existing = (existing or "").strip()
    return f"{existing} | {addition}" if existing else addition


def _candidate_evidence(facility: dict, account: dict, contacts_by_account: dict, bellhaven_id: str) -> dict:
    """Score-free evidence: a list of facts a reviewer can check, plus flags the rules use."""
    same_street = norm_street(account.get("billing_street")) == norm_street(facility["street"])
    same_zip = norm_zip(account.get("billing_zip")) == facility["zip"]
    same_city = (norm_city(account.get("billing_city")) == norm_city(facility["city"])
                 and (account.get("billing_state") or "").upper() == facility["state"])
    phone = bool(norm_phone(facility.get("phone"))) and norm_phone(account.get("phone")) == norm_phone(facility.get("phone"))
    admin = bool(facility.get("administrator")) and any(
        norm_name(c.get("name")) == norm_name(facility["administrator"])
        for c in contacts_by_account.get(account["account_id"], []))
    similarity = name_similarity(account.get("name"), facility["name"])
    name_exact = norm_name(account.get("name")) == norm_name(facility["name"])
    site_care = {map_care(o) for o in facility.get("care_offerings", [])}
    care = bool(account.get("care_type")) and account.get("care_type") in site_care

    facts = []
    facts.append(("Y " if same_street else "N ") + f"Street: CRM '{account.get('billing_street')}' vs site '{facility['street']}'")
    facts.append(("Y " if same_zip else "N ") + f"ZIP: CRM '{account.get('billing_zip')}' vs site '{facility['zip']}'")
    facts.append(("Y " if same_city else "N ") + f"City/state: CRM '{account.get('billing_city')}, {account.get('billing_state')}'")
    facts.append(("Y " if name_exact else "~ ") + f"Name similarity {similarity:.2f}: CRM '{account.get('name')}'")
    facts.append(("Y " if phone else "N ") + f"Phone: CRM '{account.get('phone')}' vs site '{facility.get('phone')}'")
    facts.append(("Y " if admin else "N ") + f"Site administrator '{facility.get('administrator')}' is a CRM contact on this account")
    facts.append(("Y " if care else "N ") + f"Care type: CRM '{account.get('care_type')}' vs site {facility.get('care_offerings')}")
    return {
        "account_id": account["account_id"], "facts": facts, "same_street": same_street,
        "same_zip": same_zip, "same_city": same_city, "phone": phone, "admin": admin,
        "name_exact": name_exact, "name_similarity": similarity, "care": care,
        "under_bellhaven": account.get("parent_id") == bellhaven_id,
    }


def match(facilities: List[dict], accounts: List[dict], contacts: List[dict],
          resolutions: Optional[Dict[str, str]] = None, trace: Optional[List[dict]] = None) -> List[dict]:
    """Return the list of proposals implied by comparing the website to the CRM.

    resolutions: {facility_slug: account_id} choices a reviewer made on AMBIGUOUS items.
    trace: optional list; if given, one row per facility (and per unexplained Bellhaven account)
           is appended describing how it was matched. It only records decisions, never changes them.
    """
    resolutions = resolutions or {}
    bellhaven = find_parent(accounts)
    bh_id = bellhaven["account_id"]
    by_id = {a["account_id"]: a for a in accounts}
    parent_ids = {a["parent_id"] for a in accounts if a.get("parent_id")}

    contacts_by_account = defaultdict(list)
    for contact in contacts:
        contacts_by_account[contact.get("account_id")].append(contact)

    # Pool of facility accounts we reason about. Excluded: parent-company accounts, and
    # records already resolved by a CHOW link or duplicate link (they are history now).
    pool = [a for a in accounts
            if a["account_id"] not in parent_ids
            and not a.get("name", "").endswith("(Parent Account)")
            and not a.get("chow_current_account")
            and not a.get("duplicate_of_account")]

    proposals: List[dict] = []
    claimed: Dict[str, str] = {}  # account_id -> facility slug that touched it

    def add(action, target, target_key, title, proposed, confidence, reasons, facility=None,
            related=None, warnings=None, alt=None):
        proposals.append({
            "fingerprint": fingerprint(action, target_key, proposed),
            "action": action,
            "target_id": target["account_id"] if target else "",
            "target_key": target_key,
            "title": title,
            "before": snapshot(target) if target else {},
            "proposed": proposed,
            "alt": alt,
            "confidence": confidence,
            "warnings": warnings or [],
            "evidence": {
                "reasons": reasons,
                "facility": facility,
                "related_accounts": [dict(snapshot(a), contacts=len(contacts_by_account.get(a["account_id"], [])))
                                     for a in (related or [])],
            },
        })

    # ------------------------------------------------------------------ forward pass
    facility_candidates = {}
    for fac in facilities:
        street, zip_code = norm_street(fac["street"]), fac["zip"]
        cands = []
        for acc in pool:
            if norm_street(acc.get("billing_street")) != street or not street:
                continue
            same_zip = norm_zip(acc.get("billing_zip")) == zip_code
            same_city = (norm_city(acc.get("billing_city")) == norm_city(fac["city"])
                         and (acc.get("billing_state") or "").upper() == fac["state"])
            if same_zip or same_city:  # tier A (street+ZIP) or tier B (street+city, catches ZIP typos)
                cands.append(acc)
        tier = "address"
        if not cands:
            # Tier C: address is unusable (e.g. a PO Box). Requires exact name + same city
            # + a second independent signal (phone or administrator). Name alone never matches.
            for acc in pool:
                ev = _candidate_evidence(fac, acc, contacts_by_account, bh_id)
                if ev["name_exact"] and ev["same_city"] and (ev["phone"] or ev["admin"]):
                    cands.append(acc)
            tier = "name+city+phone/contact"
        facility_candidates[fac["slug"]] = (cands, tier)

    # One-to-one guard: an account may only be a candidate for one facility.
    seen = defaultdict(list)
    for slug, (cands, _) in facility_candidates.items():
        for acc in cands:
            seen[acc["account_id"]].append(slug)
    contested = {aid for aid, slugs in seen.items() if len(slugs) > 1}

    survivor_of: Dict[str, dict] = {}  # for the trace only
    for fac in facilities:
        cands, tier = facility_candidates[fac["slug"]]
        site_key = "site:" + fac["slug"]
        label = f"{fac['name']} ({fac['city']}, {fac['state']})"
        for acc in cands:
            claimed[acc["account_id"]] = fac["slug"]

        if not cands:
            care = next((map_care(o) for o in fac["care_offerings"] if map_care(o)), "")
            body = {"name": fac["name"], "parent_id": bh_id, "billing_street": fac["street"],
                    "billing_city": fac["city"], "billing_state": fac["state"], "billing_zip": fac["zip"],
                    "care_type": care, "phone": fac["phone"], "status": "Active"}
            lookalikes = [a for a in pool if a["account_id"] not in claimed and (
                name_similarity(a.get("name"), fac["name"]) >= 0.85
                or (norm_city(a.get("billing_city")) == norm_city(fac["city"]) and a.get("billing_state") == fac["state"]))]
            reasons = [f"On the website ({fac['source']}) at {fac['street']}, {fac['city']}, {fac['state']} {fac['zip']}",
                       "No CRM account at this street address, and no account with this name + city backed by phone or contact"]
            if lookalikes:
                reasons.append("Look-alike accounts were checked and rejected because their address differs (listed below)")
            add(CREATE_ACCOUNT, None, site_key, f"Create account: {label}", body, "high", reasons,
                facility=fac, related=lookalikes[:5])
            continue

        evidence = {a["account_id"]: _candidate_evidence(fac, a, contacts_by_account, bh_id) for a in cands}

        if any(a["account_id"] in contested for a in cands):
            add(AMBIGUOUS, None, site_key, f"Ambiguous: {label}",
                {"candidates": sorted(a["account_id"] for a in cands), "problem": "account matches more than one facility"},
                "low", ["A candidate account also matches another website facility; needs a human decision"],
                facility=fac, related=cands)
            continue

        # ---- choose the surviving account
        survivor, why = None, ""
        resolved_id = resolutions.get(fac["slug"])
        if resolved_id and resolved_id in evidence:
            survivor, why = by_id[resolved_id], "Chosen by a reviewer on the ambiguous-match screen"
        else:
            under_bh = [a for a in cands if a.get("parent_id") == bh_id]
            group = under_bh or cands
            if len(group) == 1:
                survivor = group[0]
                why = ("Only account at this address under the Bellhaven parent" if under_bh
                       else "Only CRM account at this address")
            else:
                def strength(a):
                    e = evidence[a["account_id"]]
                    return 2 * e["phone"] + 2 * e["admin"] + 1 * e["name_exact"]
                ranked = sorted(group, key=strength, reverse=True)
                if strength(ranked[0]) > 0 and strength(ranked[0]) > strength(ranked[1]):
                    survivor = ranked[0]
                    why = "Several accounts at this address; this one is backed by phone / administrator / exact name"

        if survivor is None:
            add(AMBIGUOUS, None, site_key, f"Ambiguous: {label} has {len(cands)} candidate accounts",
                {"candidates": sorted(a["account_id"] for a in cands)}, "low",
                [f"{len(cands)} CRM accounts share this address and no evidence (phone, administrator contact, "
                 "exact name, Bellhaven parent) separates them",
                 "No survivor is selected automatically. Pick the correct account below, or reject"],
                facility=fac, related=cands)
            continue

        ev = evidence[survivor["account_id"]]
        corroborated = ev["phone"] or ev["admin"] or ev["name_similarity"] >= 0.8
        confidence = "high" if (ev["same_street"] and corroborated) or tier != "address" else "medium"
        if resolved_id:
            confidence = "medium"
        base_reasons = [f"Matched by {tier}: {why}"] + ev["facts"]
        survivor_of[fac["slug"]] = {"account": survivor, "why": why, "confidence": confidence}
        others = [a for a in cands if a is not survivor]

        # ---- parent
        on_chow_path = False
        if survivor.get("parent_id") != bh_id:
            strategy, strategy_reason = parent_change_strategy(survivor)
            current_parent = survivor.get("parent_name") or "(no parent)"
            if strategy == CHOW:
                on_chow_path = True
                care = survivor.get("care_type") or next((map_care(o) for o in fac["care_offerings"] if map_care(o)), "")
                new_account = {"name": fac["name"], "parent_id": bh_id, "billing_street": fac["street"],
                               "billing_city": fac["city"], "billing_state": fac["state"], "billing_zip": fac["zip"],
                               "care_type": care, "phone": fac["phone"], "status": "Active",
                               "note": f"{NOTE_TAG} CHOW successor of account {survivor['account_id']} "
                                       f"(previously under {current_parent})."}
                add(CHOW_NEW_SUCCESSOR, survivor, survivor["account_id"],
                    f"CHOW: {survivor['name']} moves {current_parent} -> Bellhaven (new successor account)",
                    {"new_account": new_account, "link_old_account_via": "chow_current_account"},
                    confidence, base_reasons + [f"SOP: {strategy_reason}"], facility=fac, related=[survivor, bellhaven],
                    warnings=["CHOW: this account has revenue history AND outstanding AR. Its parent will NOT be changed. "
                              "A new account is created under Bellhaven and linked from the old one."])
            elif strategy == DIRECT:
                add(SET_PARENT, survivor, survivor["account_id"],
                    f"Re-parent: {survivor['name']} from {current_parent} to Bellhaven",
                    {"parent_id": bh_id}, confidence,
                    base_reasons + [f"SOP: {strategy_reason}", "Facility is listed on the Bellhaven website"],
                    facility=fac, related=[survivor, bellhaven])
            else:
                add(FLAG_REVIEW, survivor, survivor["account_id"],
                    f"Needs investigation: {survivor['name']} (financial fields unreadable)",
                    {"status": "Needs Review",
                     "note": _append_note(survivor.get("note"),
                                          f"{NOTE_TAG} Listed on Bellhaven website but parent is {current_parent}. "
                                          "Financial fields missing/unreadable, so the CHOW SOP could not be evaluated.")},
                    "low", base_reasons + [f"SOP: {strategy_reason}"], facility=fac, related=[survivor],
                    warnings=["Parent change BLOCKED until lifetime_revenue and outstanding_ar are confirmed."])

        # ---- field corrections (never on an account that must be preserved for CHOW)
        if not on_chow_path:
            changes, notes, field_conf = {}, [], confidence
            if (survivor.get("name") or "").strip() != fac["name"]:
                changes["name"] = fac["name"]
                notes.append(f"Website name is '{fac['name']}'")
            if norm_street(survivor.get("billing_street")) != norm_street(fac["street"]):
                changes["billing_street"] = fac["street"]
                if is_po_box(survivor.get("billing_street")):
                    field_conf = "medium"
                    notes.append("CRM street is a PO Box; it may be a deliberate billing address, so check before approving")
            if not ev["same_city"]:
                changes["billing_city"], changes["billing_state"] = fac["city"], fac["state"]
            if norm_zip(survivor.get("billing_zip")) != fac["zip"]:
                changes["billing_zip"] = fac["zip"]
                notes.append(f"ZIP differs while street and city match (CRM {survivor.get('billing_zip')} vs site {fac['zip']}): likely a typo")
            if changes:
                add(UPDATE_FIELDS, survivor, survivor["account_id"],
                    f"Update {', '.join(sorted(changes))}: {survivor['name']}",
                    changes, field_conf, base_reasons + notes, facility=fac, related=[survivor])

        # ---- the other accounts at this address
        for other in others:
            other_parent = other.get("parent_name") or "(no parent)"
            has_money = other.get("lifetime_revenue") not in (0, 0.0) or other.get("outstanding_ar") not in (0, 0.0)
            dup_patch = {"duplicate_of_account": survivor["account_id"], "status": "Inactive",
                         "note": _append_note(other.get("note"),
                                              f"{NOTE_TAG} Duplicate of {survivor['account_id']} ({survivor['name']}): "
                                              "same facility at the same address.")}
            facts = [f"Same address as {survivor['name']} ({survivor['account_id']}), the account matched to the website",
                     f"This account: parent {other_parent}, revenue {other.get('lifetime_revenue')}, "
                     f"AR {other.get('outstanding_ar')}, contacts {len(contacts_by_account.get(other['account_id'], []))}"]
            true_duplicate = (other.get("parent_id") == survivor.get("parent_id")
                              and norm_name(other.get("name")) == norm_name(survivor.get("name")))
            if true_duplicate and not has_money:
                add(MARK_DUPLICATE, other, other["account_id"],
                    f"Duplicate: {other['name']} ({other['account_id']}) duplicates {survivor['account_id']}",
                    dup_patch, "high",
                    facts + ["Same parent, same name, and same address as the surviving account",
                             f"Survivor chosen because: {why}"] + evidence[other["account_id"]]["facts"],
                    facility=fac, related=[survivor, other])
            else:
                if other.get("status") == "Needs Review":
                    continue  # already flagged; nothing more to propose
                flag_patch = {"status": "Needs Review",
                              "note": _append_note(other.get("note"),
                                                   f"{NOTE_TAG} Same address as {survivor['account_id']} ({survivor['name']}) "
                                                   f"but under {other_parent}. Possible legacy record from a prior owner or a "
                                                   "duplicate; verify before deactivating.")}
                alt = None if has_money else {
                    "label": f"Approve as duplicate of {survivor['account_id']} (sets Inactive)",
                    "action": MARK_DUPLICATE, "proposed": dup_patch}
                add(FLAG_REVIEW, other, other["account_id"],
                    f"Review legacy record: {other['name']} shares an address with {survivor['name']}",
                    flag_patch, "medium",
                    facts + ["Different parent and/or name, so this is NOT assumed to be a duplicate. It may be a "
                             "legitimate historical ownership record",
                             "Default action only flags it. A reviewer who confirms it is a duplicate can choose that instead"],
                    facility=fac, related=[survivor, other],
                    warnings=(["This record has revenue or AR history. It can only be flagged, not deactivated."]
                              if has_money else []),
                    alt=alt)

    # ------------------------------------------------------------------ reverse pass
    # Accounts under Bellhaven that no website facility explains.
    by_addr = defaultdict(list)
    for acc in pool:
        if acc.get("billing_street"):
            by_addr[_addr_key(acc.get("billing_street"), acc.get("billing_zip"))].append(acc)

    for acc in pool:
        if acc.get("parent_id") != bh_id or acc["account_id"] in claimed:
            continue
        if acc.get("status") == "Needs Review":
            continue
        same_place = [o for o in by_addr.get(_addr_key(acc.get("billing_street"), acc.get("billing_zip")), [])
                      if o is not acc and o.get("parent_id") != bh_id]
        strategy, strategy_reason = parent_change_strategy(acc)
        absent = "Under the Bellhaven parent in the CRM but not listed on the Bellhaven website"
        if len(same_place) == 1 and strategy == CHOW and same_place[0].get("parent_id") \
                and same_place[0].get("status") == "Active":
            successor = same_place[0]
            checks = [
                absent,
                f"Exactly one other CRM account exists at the same address: {successor['name']} "
                f"({successor['account_id']}) under {successor.get('parent_name')}",
                f"Same normalized street + ZIP: '{acc.get('billing_street')}' / '{successor.get('billing_street')}'",
                ("Y " if acc.get("care_type") == successor.get("care_type") else "N ")
                + f"Care type: '{acc.get('care_type')}' vs '{successor.get('care_type')}'",
                f"SOP: {strategy_reason}",
                "A successor account already exists, so none is created. Only the link is set",
            ]
            add(CHOW_LINK_EXISTING, acc, acc["account_id"],
                f"CHOW link: {acc['name']} -> existing successor {successor['name']}",
                {"chow_current_account": successor["account_id"]},
                "medium" if acc.get("care_type") == successor.get("care_type") else "low",
                checks, related=[acc, successor],
                warnings=["CHOW: this account has revenue history AND outstanding AR. It stays under Bellhaven untouched; "
                          "only chow_current_account is set, pointing at the existing account under the new owner.",
                          "Ownership change is inferred from the website and the second account, not from a sale record."])
            continue
        if len(same_place) > 1:
            detail = (f"{len(same_place)} other accounts share this address under different parents. Multiple possible "
                      "successors, so no CHOW link is proposed.")
        elif len(same_place) == 1:
            detail = (f"Another account at this address: {same_place[0]['name']} ({same_place[0]['account_id']}) under "
                      f"{same_place[0].get('parent_name') or '(no parent)'}. Possible ownership change. SOP: {strategy_reason}.")
        else:
            detail = "No other account at this address and no evidence of a sale. Absence from the website alone does not prove closure."
        add(FLAG_REVIEW, acc, acc["account_id"], f"Not on website: {acc['name']}",
            {"status": "Needs Review", "note": _append_note(acc.get("note"), f"{NOTE_TAG} {absent}. {detail}")},
            "medium", [absent, detail, f"Revenue {acc.get('lifetime_revenue')}, AR {acc.get('outstanding_ar')}"],
            related=[acc] + same_place)

    if trace is not None:
        trace.extend(_build_trace(facilities, facility_candidates, contested, survivor_of, proposals))
    return proposals


def _build_trace(facilities, facility_candidates, contested, survivor_of, proposals) -> List[dict]:
    """Describe, per facility, how the matcher reached its result (for display; no effect on proposals)."""
    rows = []
    for fac in facilities:
        cands, tier = facility_candidates[fac["slug"]]
        if not cands:
            method = "No CRM account at this address"
        elif tier != "address":
            method = "Name + city + phone/contact (address unusable)"
        elif any(norm_zip(a.get("billing_zip")) == fac["zip"] for a in cands):
            method = "Street + ZIP"
        else:
            method = "Street + city (ZIP differs)"
        chosen = survivor_of.get(fac["slug"], {})
        account = chosen.get("account") or {}
        rows.append({
            "kind": "facility", "slug": fac["slug"], "facility": fac["name"], "city": fac["city"],
            "state": fac["state"], "zip": fac["zip"], "source": fac.get("source", ""), "method": method,
            "candidates": [{"account_id": a["account_id"], "name": a.get("name"), "parent_name": a.get("parent_name") or "",
                            "billing_street": a.get("billing_street"), "billing_zip": a.get("billing_zip")} for a in cands],
            "contested": any(a["account_id"] in contested for a in cands),
            "account_id": account.get("account_id", ""), "account_name": account.get("name", ""),
            "parent_name": account.get("parent_name") or "", "why": chosen.get("why", ""),
            "confidence": chosen.get("confidence", ""),
            "actions": [p["action"] for p in proposals if (p["evidence"].get("facility") or {}).get("slug") == fac["slug"]],
        })
    for p in proposals:  # reverse pass: Bellhaven accounts no website facility explains
        if p["evidence"].get("facility") is None:
            before = p["before"]
            rows.append({
                "kind": "not_on_site", "account_id": p["target_id"], "account_name": before.get("name"),
                "address": f"{before.get('billing_street')}, {before.get('billing_city')}, {before.get('billing_state')} "
                           f"{before.get('billing_zip')}",
                "revenue": before.get("lifetime_revenue"), "ar": before.get("outstanding_ar"),
                "actions": [p["action"]], "detail": p["evidence"]["reasons"][1] if len(p["evidence"]["reasons"]) > 1 else "",
            })
    return rows
