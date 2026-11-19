"""The change-of-ownership (CHOW) SOP, as one small function.

SOP: when an account needs to move to a different parent, check its billing history first.
  - revenue history AND outstanding AR > 0  -> CHOW: do NOT touch the old account's parent.
        Create (or identify) a successor under the correct parent and set
        chow_current_account on the OLD account to the successor's id.
  - no revenue history OR no outstanding AR -> DIRECT: re-parent the existing account.
  - a financial field is missing or unreadable -> BLOCKED: a human investigates. We never
        treat "unknown" as zero, because that would wrongly allow a direct re-parent.

This function is called twice: by the matcher when it builds a proposal, and again by the
applier on LIVE values immediately before any write.
"""
from __future__ import annotations

from typing import Optional, Tuple

DIRECT = "DIRECT"
CHOW = "CHOW"
BLOCKED = "BLOCKED"


def parse_money(value) -> Optional[float]:
    """Return a non-negative number, or None when the value cannot be trusted."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        try:
            number = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    if number != number or number < 0:  # NaN or negative
        return None
    return number


def parent_change_strategy(account: dict) -> Tuple[str, str]:
    """Return (strategy, plain-English reason) for moving this account to a new parent."""
    revenue = parse_money(account.get("lifetime_revenue"))
    ar = parse_money(account.get("outstanding_ar"))
    if revenue is None or ar is None:
        return BLOCKED, (
            f"Financial fields are missing or unreadable (lifetime_revenue={account.get('lifetime_revenue')!r}, "
            f"outstanding_ar={account.get('outstanding_ar')!r}); needs investigation before any parent change"
        )
    if revenue > 0 and ar > 0:
        return CHOW, (
            f"Revenue history (${revenue:,.0f}) AND outstanding AR (${ar:,.0f}): billing needs the old account "
            "preserved, so its parent must not change"
        )
    if revenue == 0:
        return DIRECT, f"No revenue history (AR ${ar:,.0f}): direct re-parent allowed"
    return DIRECT, f"Revenue history (${revenue:,.0f}) but no outstanding AR: direct re-parent allowed"
