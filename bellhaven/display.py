"""Readable names for the review app.

Presentation only: account IDs are swapped for account / company names so a reviewer sees
"Harborview Care Group" instead of "001FJZYHR7MLFMNPLL". Nothing here changes what a
proposal does; the raw IDs stay available under "Technical details" in the app.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .matcher import (AMBIGUOUS, CHOW_LINK_EXISTING, CHOW_NEW_SUCCESSOR, CREATE_ACCOUNT, FLAG_REVIEW,
                      MARK_DUPLICATE, SET_PARENT, UPDATE_FIELDS)

ACCOUNT_ID = re.compile(r"\b00[0-9A-Z]{16}\b")

FIELD_LABEL = {"parent_id": "parent"}


def short_parent(name) -> str:
    """'Harborview Care Group (Parent Account)' -> 'Harborview Care Group'."""
    return str(name or "").replace(" (Parent Account)", "").strip()


def build_names(proposals: List[dict]) -> Dict[str, str]:
    """account_id -> display name, gathered from every snapshot the review queue already holds."""
    names: Dict[str, str] = {}

    def take(snap):
        if not snap or not snap.get("account_id"):
            return
        names[snap["account_id"]] = snap.get("name") or snap["account_id"]
        if snap.get("parent_id") and snap.get("parent_name"):
            names[snap["parent_id"]] = snap["parent_name"]

    for p in proposals:
        take(p.get("before"))
        for snap in (p.get("evidence") or {}).get("related_accounts") or []:
            take(snap)
    for p in proposals:  # accounts this app created, named after what was created
        result, proposed = p.get("result") or {}, p.get("proposed") or {}
        new_name = (proposed.get("new_account") or {}).get("name") or proposed.get("name")
        for key in ("created_id", "successor_id"):
            if result.get(key) and new_name:
                names.setdefault(result[key], f"{new_name} (new)")
    return {k: short_parent(v) for k, v in names.items()}


def humanize(text, names: Dict[str, str]) -> str:
    """Swap raw account IDs in a sentence for names, without repeating a name already next to the ID."""
    text = str(text).replace(" (Parent Account)", "")

    # "001ABC... (Bellhaven of Owosso)" -> "Bellhaven of Owosso"
    text = re.sub(r"(" + ACCOUNT_ID.pattern + r") \(([^()]*)\)",
                  lambda m: m.group(2) if names.get(m.group(1)) == m.group(2) else m.group(0), text)
    # "Bellhaven of Owosso (001ABC...)" -> "Bellhaven of Owosso"
    text = re.sub(r" \((" + ACCOUNT_ID.pattern + r")\)",
                  lambda m: "" if text[:m.start()].endswith(names.get(m.group(1), "\0")) else m.group(0), text)
    text = ACCOUNT_ID.sub(lambda m: names.get(m.group(0), m.group(0)), text)
    for field, label in FIELD_LABEL.items():
        text = text.replace(f"{field} = ", f"{label} = ")
    return text


# ---------------------------------------------------------------- collapsed-row title
def _related(p: dict) -> List[dict]:
    return (p.get("evidence") or {}).get("related_accounts") or []


def row_title(p: dict, names: Dict[str, str]) -> Tuple[str, str]:
    """(facility name, what would change) for the collapsed proposal row."""
    action, before, proposed = p["action"], p.get("before") or {}, p.get("proposed") or {}
    name = before.get("name", "")
    if action == UPDATE_FIELDS:
        parts = []
        if "name" in proposed:
            parts.append(f"rename to “{proposed['name']}”")
        if "billing_street" in proposed:
            parts.append(f"street → {proposed['billing_street']}")
        if "billing_city" in proposed:
            parts.append(f"city → {proposed['billing_city']}")
        if "billing_zip" in proposed:
            parts.append(f"ZIP {before.get('billing_zip')} → {proposed['billing_zip']}")
        return name, "; ".join(parts)
    if action == SET_PARENT:
        current = short_parent(before.get("parent_name")) or "no parent"
        return name, f"move from {current} to {names.get(proposed['parent_id'], 'Bellhaven')}"
    if action == CHOW_NEW_SUCCESSOR:
        return name, f"ownership change: {short_parent(before.get('parent_name'))} → Bellhaven"
    if action == CHOW_LINK_EXISTING:
        successor = next((a for a in _related(p) if a["account_id"] == proposed["chow_current_account"]), {})
        return name, f"sold to {short_parent(successor.get('parent_name')) or 'another owner'}; link existing account"
    if action == CREATE_ACCOUNT:
        return proposed.get("name", ""), f"new account in {proposed.get('billing_city')}, {proposed.get('billing_state')}"
    if action == MARK_DUPLICATE:
        return name, "duplicate of another record with the same name"
    if action == FLAG_REVIEW:
        title = p.get("title", "")
        if title.startswith("Review legacy record"):
            keep = _related(p)[0]["name"] if _related(p) else "the Bellhaven account"
            return name, f"legacy record at the same address as {keep}"
        if title.startswith("Not on website"):
            return name, "under Bellhaven in the CRM but not on the website"
        return name, "financial fields unreadable; ownership rule cannot be applied"
    if action == AMBIGUOUS:
        facility = (p.get("evidence") or {}).get("facility") or {}
        return facility.get("name", ""), f"{len(_related(p))} candidate CRM accounts at this address"
    return name, humanize(p.get("title", ""), names)


# ---------------------------------------------------------------- sorting
# Risk = how much harm a wrong approval would do: money involved, or hard to undo.
RISK_LEVEL = {
    UPDATE_FIELDS: 0,                                   # one field, easy to put back
    SET_PARENT: 1, FLAG_REVIEW: 1,                      # status / parent change, reversible
    MARK_DUPLICATE: 2, CREATE_ACCOUNT: 2,               # deactivates, or creates a record that cannot be deleted
    CHOW_NEW_SUCCESSOR: 3, CHOW_LINK_EXISTING: 3,       # billing history and money owed
    AMBIGUOUS: 3,                                       # the data does not say which account is right
}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
SORT_OPTIONS = ["Type", "Confidence (highest first)", "Risk (lowest first)"]


def sort_proposals(proposals: List[dict], how: str, names: Dict[str, str]) -> List[dict]:
    def risk(p):
        return RISK_LEVEL.get(p["action"], 1)

    def confidence(p):
        return CONFIDENCE_RANK.get(p["confidence"], 1)

    def facility(p):
        return row_title(p, names)[0].lower()

    if how == "Confidence (highest first)":
        return sorted(proposals, key=lambda p: (confidence(p), risk(p), facility(p)))
    if how == "Risk (lowest first)":
        return sorted(proposals, key=lambda p: (risk(p), confidence(p), facility(p)))
    return list(proposals)
