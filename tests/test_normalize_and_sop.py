"""Test categories 1 (normalization) and 3 (the CHOW SOP decision table)."""
import pytest

from bellhaven.chow import BLOCKED, CHOW, DIRECT, parent_change_strategy
from bellhaven.normalize import is_po_box, map_care, norm_name, norm_street, norm_zip


@pytest.mark.parametrize("a,b", [
    ("980 West Michigan Avenue", "980 W Michigan Ave"),
    ("4850 Northwest Sylvania Avenue", "4850 NW Sylvania Ave"),
    ("199 Barks Road West", "199 Barks Rd W"),
    ("3313 Wilmington Pk", "3313 Wilmington Pike"),
    ("1125 Logan Boulevard", "1125 Logan Blvd."),
    ("45 St Lawrence Dr", "45 St. Lawrence Drive"),
])
def test_equivalent_streets_normalize_equal(a, b):
    assert norm_street(a) == norm_street(b)


def test_different_streets_stay_different():
    assert norm_street("240 Market St") != norm_street("118 Union Square Dr")
    assert norm_street("PO Box 517") != norm_street("3156 W Prospect Rd")


def test_zip_and_names():
    assert norm_zip("45662-1234") == "45662"
    assert norm_zip("") == "" and norm_zip(None) == ""
    assert norm_name("Bellhaven Rehab and Nursing of Grove City") == norm_name("Bellhaven Rehabilitation & Nursing of Grove City")
    assert norm_name("Bellhaven Health Care Center of Ashland") == norm_name("Bellhaven Healthcare Centre of Ashland")
    assert norm_name("Bellhaven of Carlisle") != norm_name("Bellhaven of New Carlisle")
    assert is_po_box("PO Box 517") and is_po_box("P.O. Box 9") and not is_po_box("12 Post Rd")
    assert map_care("Short-Term Rehabilitation & Nursing") == "Skilled Nursing"
    assert map_care("Memory Support") == "Memory Care"


@pytest.mark.parametrize("revenue,ar,expected", [
    (84000, 12400, CHOW),      # revenue history AND AR > 0  -> preserve the old account
    (47000, 0, DIRECT),        # revenue but nothing owed     -> re-parent directly
    (0, 0, DIRECT),            # no history at all
    (0, 500, DIRECT),          # AR without revenue history: SOP needs BOTH for CHOW
    (0.0, 0.0, DIRECT),
    ("51250", "3800", CHOW),   # numeric strings are readable
    ("$1,200", "0", DIRECT),
    (None, 100, BLOCKED),      # unknown is never treated as zero
    (100, None, BLOCKED),
    ("", 0, BLOCKED),
    ("n/a", 0, BLOCKED),
    (100, "unknown", BLOCKED),
    (-5, 10, BLOCKED),
    (True, 10, BLOCKED),
])
def test_sop_decision_table(revenue, ar, expected):
    strategy, reason = parent_change_strategy({"lifetime_revenue": revenue, "outstanding_ar": ar})
    assert strategy == expected
    assert reason


def test_missing_keys_block():
    assert parent_change_strategy({})[0] == BLOCKED
