"""Apply ONE approved proposal to the CRM, with guards before and verification after.

Order of operations for every proposal:
  1. Refuse unless a reviewer approved it (no API call at all otherwise).
  2. Re-read the live account. If the change is already in place, just verify it
     (this is how a retry after an uncertain write stays safe).
  3. If anything on the account drifted since the proposal was made -> stop, no write.
  4. Re-check business rules on LIVE values (the CHOW SOP, successor uniqueness).
  5. Write.
  6. Re-read and confirm: proposed fields landed AND no other field changed.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from . import store as S
from .chow import CHOW, DIRECT, parent_change_strategy, parse_money
from .crm import CRMError, UncertainWrite
from .matcher import (CHOW_LINK_EXISTING, CHOW_NEW_SUCCESSOR, CREATE_ACCOUNT, FLAG_REVIEW, MARK_DUPLICATE,
                      SET_PARENT, SNAPSHOT_FIELDS, UPDATE_FIELDS)
from .normalize import norm_street, norm_zip


class NotApproved(RuntimeError):
    pass


class Blocked(RuntimeError):
    """A safety rule stopped the write. Needs a human."""


class Superseded(RuntimeError):
    """The CRM changed since the proposal was created. Re-run the pipeline."""


def _same_address(a: dict, b: dict) -> bool:
    return (norm_street(a.get("billing_street")) == norm_street(b.get("billing_street"))
            and norm_zip(a.get("billing_zip")) == norm_zip(b.get("billing_zip")))


def _is_history(account: dict) -> bool:
    return bool(account.get("chow_current_account") or account.get("duplicate_of_account"))


class Applier:
    def __init__(self, client, store: S.Store):
        self.client = client
        self.store = store

    # ------------------------------------------------------------------ entry point
    def apply(self, proposal_id: int) -> dict:
        proposal = self.store.get(proposal_id)
        if proposal is None or proposal["state"] not in (S.APPROVED, S.FAILED, S.APPLYING):
            raise NotApproved(f"Proposal {proposal_id} has not been approved by a reviewer; nothing was sent")

        use_alt = proposal["chosen"] == "alt" and proposal.get("alt")
        action = proposal["alt"]["action"] if use_alt else proposal["action"]
        proposed = proposal["alt"]["proposed"] if use_alt else proposal["proposed"]

        self.client.audit = lambda **call: self.store.log_call(proposal_id, **call)
        self.store.set_state(proposal_id, S.APPLYING)
        try:
            if action in (UPDATE_FIELDS, SET_PARENT, MARK_DUPLICATE, FLAG_REVIEW, CHOW_LINK_EXISTING):
                checks = self._apply_patch(proposal, action, proposed)
            elif action == CHOW_NEW_SUCCESSOR:
                checks = self._apply_chow_new(proposal, proposed)
            elif action == CREATE_ACCOUNT:
                checks = self._apply_create(proposal, proposed)
            else:
                raise Blocked(f"Action {action} does not write to the CRM")
        except Superseded as exc:
            return self._finish(proposal_id, S.SUPERSEDED, False, f"Not applied, CRM changed: {exc}")
        except UncertainWrite as exc:
            return self._finish(proposal_id, S.FAILED, False,
                                f"Outcome unknown: {exc}. Retry is safe: it re-reads the CRM first and will not repeat a write that landed.")
        except (Blocked, CRMError) as exc:
            return self._finish(proposal_id, S.FAILED, False, f"Not applied: {exc}")
        return self._finish(proposal_id, S.APPLIED, True, "Applied and verified against the live CRM", checks)

    def _finish(self, proposal_id, state, verified, message, checks: Optional[List[str]] = None) -> dict:
        previous = (self.store.get(proposal_id) or {}).get("result") or {}
        result = {"message": message, "verified": verified, "checks": checks or []}
        if previous.get("successor_id"):
            result["successor_id"] = previous["successor_id"]
        if previous.get("created_id"):
            result["created_id"] = previous["created_id"]
        self.store.set_state(proposal_id, state, result)
        return dict(result, state=state)

    # ------------------------------------------------------------------ shared guards
    def _live(self, account_id: str) -> dict:
        account = self.client.get_account(account_id)
        if account is None:
            raise Blocked(f"Account {account_id} no longer exists in the CRM")
        return account

    @staticmethod
    def _drift(before: dict, live: dict) -> List[str]:
        return [f"{f}: was {before.get(f)!r}, now {live.get(f)!r}" for f in SNAPSHOT_FIELDS
                if f in before and before.get(f) != live.get(f)]

    def _patch_and_verify(self, account_id: str, live: dict, patch: dict) -> List[str]:
        self.client.update_account(account_id, patch)
        after = self._live(account_id)
        missed = [f"{k}: expected {v!r}, CRM has {after.get(k)!r}" for k, v in patch.items() if after.get(k) != v]
        if missed:
            raise Blocked("Write did not land as expected: " + "; ".join(missed))
        ignore = set(patch) | ({"parent_name"} if "parent_id" in patch else set())
        side_effects = [f"{f}: {live.get(f)!r} -> {after.get(f)!r}" for f in SNAPSHOT_FIELDS
                        if f not in ignore and live.get(f) != after.get(f)]
        if side_effects:
            raise Blocked("UNEXPECTED side effects on fields that were not sent: " + "; ".join(side_effects)
                          + ". Stop and inspect this account before approving anything else.")
        return [f"{k} = {after.get(k)!r}" for k in patch] + ["No other field on the account changed"]

    def _accounts_at(self, reference: dict) -> List[dict]:
        """Live accounts at the same normalized street + ZIP, ignoring resolved history records."""
        rows = self.client.list_accounts(zip=norm_zip(reference.get("billing_zip")))
        return [a for a in rows if _same_address(a, reference) and not _is_history(a)]

    # ------------------------------------------------------------------ PATCH-style actions
    def _apply_patch(self, proposal: dict, action: str, patch: dict) -> List[str]:
        account_id = proposal["target_id"]
        live = self._live(account_id)

        if all(live.get(k) == v for k, v in patch.items()):
            return ["Already in place in the live CRM (nothing sent)"] + [f"{k} = {live.get(k)!r}" for k in patch]

        drift = self._drift(proposal["before"], live)
        if drift:
            raise Superseded("; ".join(drift))

        if action == SET_PARENT:
            strategy, reason = parent_change_strategy(live)
            if strategy != DIRECT:
                raise Blocked(f"SOP forbids a direct re-parent on live values. {reason}")
            parent = self.client.get_account(patch["parent_id"])
            if parent is None or parent.get("parent_id"):
                raise Blocked("Target parent account is missing or is not a top-level parent")

        if action == MARK_DUPLICATE:
            survivor = self.client.get_account(patch["duplicate_of_account"])
            if survivor is None or survivor["account_id"] == account_id or _is_history(survivor):
                raise Blocked("Surviving account is missing or is itself a duplicate/CHOW record")
            if survivor.get("status") != "Active":
                raise Blocked(f"Surviving account status is {survivor.get('status')!r}, expected Active")
            if parse_money(live.get("lifetime_revenue")) != 0 or parse_money(live.get("outstanding_ar")) != 0:
                raise Blocked("Account has revenue or AR (or unreadable financials); it cannot be deactivated as a duplicate")

        if action == CHOW_LINK_EXISTING:
            strategy, reason = parent_change_strategy(live)
            if strategy != CHOW:
                raise Superseded(f"SOP no longer requires CHOW on live values. {reason}")
            successor = self.client.get_account(patch["chow_current_account"])
            if successor is None:
                raise Blocked("Successor account no longer exists")
            if not _same_address(successor, live):
                raise Blocked("Successor is no longer at the same address as this account")
            if not successor.get("parent_id") or successor.get("parent_id") == live.get("parent_id") or _is_history(successor):
                raise Blocked("Successor is not a live account under a different parent")
            others = [a for a in self._accounts_at(live)
                      if a["account_id"] != account_id and a.get("parent_id") != live.get("parent_id")]
            if [a["account_id"] for a in others] != [successor["account_id"]]:
                raise Blocked(f"{len(others)} possible successors at this address; manual review required")

        return self._patch_and_verify(account_id, live, patch)

    # ------------------------------------------------------------------ create helpers
    def _create_or_adopt(self, proposal_id: int, body: dict, exclude_id: str = "") -> Tuple[dict, str]:
        """Create the account unless one already exists at this address under this parent.

        Looking first is what prevents a duplicate when an earlier POST timed out but
        actually succeeded, or when a retry happens after a half-finished CHOW.
        """
        existing = [a for a in self._accounts_at(body)
                    if a["account_id"] != exclude_id and a.get("parent_id") == body["parent_id"]]
        if len(existing) > 1:
            raise Blocked(f"{len(existing)} accounts already exist at this address under the target parent; manual review required")
        if existing:
            self.store.save_progress(proposal_id, created_id=existing[0]["account_id"])
            return existing[0], f"Existing account {existing[0]['account_id']} found at this address under the target parent; adopted, none created"

        response = self.client.create_account(body)
        new_id = response.get("account_id") if isinstance(response, dict) else None
        if not new_id:
            raise UncertainWrite("POST returned no account_id")
        self.store.save_progress(proposal_id, created_id=new_id)  # persisted BEFORE any further call
        created = self._live(new_id)
        wrong = {k: f"{k}: expected {v!r}, CRM has {created.get(k)!r}" for k, v in body.items() if created.get(k) != v}
        critical = [msg for k, msg in wrong.items() if k in ("name", "parent_id") or k.startswith("billing_")]
        if critical:
            raise Blocked(f"Account {new_id} was created but key fields differ: " + "; ".join(critical))
        if wrong:
            return created, f"Created account {new_id}; minor fields stored differently: " + "; ".join(wrong.values())
        return created, f"Created account {new_id} and verified every field"

    def _apply_create(self, proposal: dict, body: dict) -> List[str]:
        elsewhere = [a for a in self._accounts_at(body) if a.get("parent_id") != body["parent_id"]]
        if elsewhere:
            raise Superseded(f"An account now exists at this address under another parent ({elsewhere[0]['account_id']})")
        created, how = self._create_or_adopt(proposal["id"], body)
        return [how, f"New account id: {created['account_id']}", f"parent_id = {created.get('parent_id')!r}"]

    # ------------------------------------------------------------------ CHOW with a new successor
    def _apply_chow_new(self, proposal: dict, proposed: dict) -> List[str]:
        old_id = proposal["target_id"]
        body = proposed["new_account"]
        live = self._live(old_id)
        checks = []

        if live.get("chow_current_account"):
            linked = self._live(live["chow_current_account"])
            if linked.get("parent_id") == body["parent_id"] and _same_address(linked, body):
                return [f"Already linked to successor {linked['account_id']} (nothing sent)"]
            raise Blocked(f"Old account already has chow_current_account={live['chow_current_account']!r}, "
                          "which is not the expected successor")

        drift = self._drift(proposal["before"], live)
        if drift:
            raise Superseded("; ".join(drift))

        strategy, reason = parent_change_strategy(live)
        if strategy == DIRECT:
            raise Superseded(f"Financials changed; SOP now allows a direct re-parent. {reason}")
        if strategy != CHOW:
            raise Blocked(reason)
        checks.append(f"SOP re-checked on live values: {reason}")

        successor, how = self._create_or_adopt(proposal["id"], body, exclude_id=old_id)
        self.store.save_progress(proposal["id"], successor_id=successor["account_id"])
        checks.append(how)

        # The ONLY field sent to the old account is the link. Its parent is never in the request.
        checks += self._patch_and_verify(old_id, live, {"chow_current_account": successor["account_id"]})
        after = self._live(old_id)
        if after.get("parent_id") != proposal["before"].get("parent_id"):
            raise Blocked("Old account's parent changed. This must never happen; inspect immediately")
        checks.append(f"Old account parent unchanged: {after.get('parent_name')!r}")
        checks.append(f"Successor {successor['account_id']} is under parent {successor.get('parent_id')!r}")
        return checks
