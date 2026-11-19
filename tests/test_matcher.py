"""Test category 2: matcher behaviour on the traps found in the real data."""
from conftest import BH, CEDAR, HARBOR, MILL, acct, fac, parents

from bellhaven import matcher as M


def run(facilities, accounts, contacts=(), resolutions=None):
    return M.match(facilities, parents() + accounts, list(contacts), resolutions)


def actions(proposals):
    return sorted(p["action"] for p in proposals)


def test_clean_match_with_formatting_differences_proposes_nothing():
    f = fac("saline", "Bellhaven of Saline", "980 W Michigan Ave", "Saline", "MI", "48176")
    a = acct("A1", "Bellhaven of Saline", "980 West Michigan Avenue", "Saline", "MI", "48176", parent=BH)
    assert run([f], [a]) == []


def test_carlisle_pa_is_not_new_carlisle_oh():
    f = fac("carlisle", "Bellhaven of Carlisle", "640 Walnut Bottom Rd", "Carlisle", "PA", "17015")
    f2 = fac("new-carlisle", "Bellhaven of New Carlisle", "875 Elm Street", "New Carlisle", "OH", "45344")
    a = acct("A1", "Bellhaven of New Carlisle", "875 Elm St", "New Carlisle", "OH", "45344", parent=BH)
    out = run([f, f2], [a])
    assert actions(out) == [M.CREATE_ACCOUNT]
    assert out[0]["target_key"] == "site:carlisle" and out[0]["proposed"]["parent_id"] == BH


def test_identical_name_in_another_state_does_not_match():
    f = fac("amberly", "Amberly Manor", "4390 Darrow Rd", "Hudson", "OH", "44236", care=("Assisted Living",))
    a = acct("A1", "Amberly Manor", "918 S Nevada Ave", "Colorado Springs", "CO", "80903", parent="P_OTHER")
    out = run([f], [a])
    assert actions(out) == [M.CREATE_ACCOUNT]
    assert out[0]["target_id"] == ""  # the Colorado account is untouched


def test_po_box_account_matches_on_name_city_and_phone_only_with_second_signal():
    f = fac("ashtabula", "Bellhaven of Ashtabula", "3156 W Prospect Rd", "Ashtabula", "OH", "44004", phone="(260) 440-4975")
    a = acct("A1", "Bellhaven of Ashtabula", "PO Box 517", "Ashtabula", "OH", "44004", parent=BH, phone="(260) 440-4975")
    out = run([f], [a])
    assert actions(out) == [M.UPDATE_FIELDS]
    assert out[0]["proposed"] == {"billing_street": "3156 W Prospect Rd"}
    assert out[0]["confidence"] == "medium"  # PO Box may be a deliberate billing address
    # Same name + city but NO phone/contact corroboration -> not a match, so it is not overwritten.
    a_no_phone = dict(a, phone="(000) 000-0000")
    assert actions(run([f], [a_no_phone])) == [M.CREATE_ACCOUNT, M.FLAG_REVIEW]


def test_zip_typo_is_matched_by_street_and_city():
    f = fac("portsmouth", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45662")
    a = acct("A1", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45626", parent=BH)
    out = run([f], [a])
    assert actions(out) == [M.UPDATE_FIELDS] and out[0]["proposed"] == {"billing_zip": "45662"}


def test_true_duplicate_same_parent_same_name_yields_one_survivor():
    f = fac("owosso", "Bellhaven of Owosso", "1120 W Main St", "Owosso", "MI", "48867", phone="(734) 503-1363",
            admin="Gloria Lambert")
    keep = acct("KEEP", "Bellhaven of Owosso", "1120 W Main St", "Owosso", "MI", "48867", parent=BH, phone="(734) 503-1363")
    lose = acct("LOSE", "Bellhaven of Owosso", "1120 West Main Street", "Owosso", "MI", "48867", parent=BH, phone="(330) 383-5991")
    contacts = [{"account_id": "KEEP", "name": "Gloria Lambert"}]
    out = run([f], [lose, keep], contacts)
    assert actions(out) == [M.MARK_DUPLICATE]
    assert out[0]["target_id"] == "LOSE"
    assert out[0]["proposed"]["duplicate_of_account"] == "KEEP" and out[0]["proposed"]["status"] == "Inactive"


def test_legacy_record_under_other_parent_is_flagged_not_assumed_duplicate():
    f = fac("erie", "Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505", care=("Assisted Living",))
    current = acct("CUR", "Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505", parent=BH, care="Assisted Living")
    legacy = acct("OLD", "Harborview Shores of Erie", "4930 West Lake Road", "Erie", "PA", "16505", parent=HARBOR, care="Assisted Living")
    out = run([f], [current, legacy])
    assert actions(out) == [M.FLAG_REVIEW]
    assert out[0]["target_id"] == "OLD" and out[0]["proposed"]["status"] == "Needs Review"
    assert "duplicate_of_account" not in out[0]["proposed"]
    assert out[0]["alt"]["action"] == M.MARK_DUPLICATE  # only if a reviewer explicitly chooses it
    # A legacy record carrying money can never be offered for deactivation.
    legacy_money = dict(legacy, lifetime_revenue=9000)
    assert run([f], [current, legacy_money])[0]["alt"] is None


def test_three_equal_candidates_are_ambiguous_and_nothing_is_auto_selected():
    f = fac("kettering", "Bellhaven of Kettering", "3313 Wilmington Pike", "Kettering", "OH", "45429")
    cands = [acct("K1", "Kettering Care Centre", "3313 Wilmington Pike", "Kettering", "OH", "45429", parent=HARBOR),
             acct("K2", "Kettering Nursing & Rehabilitation", "3313 Wilmington Pike", "Kettering", "OH", "45429"),
             acct("K3", "Kettering Senior Campus", "3313 Wilmington Pk", "Kettering", "OH", "45429", parent=CEDAR)]
    out = run([f], cands)
    assert actions(out) == [M.AMBIGUOUS]
    assert out[0]["proposed"]["candidates"] == ["K1", "K2", "K3"]
    assert len(out[0]["evidence"]["related_accounts"]) == 3
    # After a reviewer picks K2, ordinary proposals appear; the other two are flagged, not deactivated.
    resolved = run([f], cands, resolutions={"kettering": "K2"})
    assert actions(resolved) == [M.FLAG_REVIEW, M.FLAG_REVIEW, M.SET_PARENT, M.UPDATE_FIELDS]
    assert {p["target_id"] for p in resolved if p["action"] in (M.SET_PARENT, M.UPDATE_FIELDS)} == {"K2"}


def test_wrong_parent_uses_the_sop():
    tiffin = fac("tiffin", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883")
    lima = fac("lima", "Bellhaven Crossings of Lima", "3115 N Cole St", "Lima", "OH", "45801")
    odd = fac("odd", "Bellhaven of Odd", "1 Odd St", "Odd", "OH", "40000")
    a_tiffin = acct("T", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883", parent=CEDAR, rev=84000, ar=12400)
    a_lima = acct("L", "Bellhaven Crossings of Lima", "3115 N Cole St", "Lima", "OH", "45801", parent=HARBOR, rev=47000, ar=0)
    a_odd = acct("O", "Bellhaven of Odd", "1 Odd St", "Odd", "OH", "40000", parent=CEDAR, rev=None, ar=50)
    out = {p["target_id"]: p for p in run([tiffin, lima, odd], [a_tiffin, a_lima, a_odd])}
    assert out["T"]["action"] == M.CHOW_NEW_SUCCESSOR
    assert out["T"]["proposed"]["new_account"]["parent_id"] == BH
    assert "parent_id" not in out["T"]["proposed"]  # never a direct parent change on the old account
    assert out["L"]["action"] == M.SET_PARENT and out["L"]["proposed"] == {"parent_id": BH}
    assert out["O"]["action"] == M.FLAG_REVIEW  # unreadable financials -> investigate, never treated as zero


def test_chow_old_account_gets_no_field_edits_and_is_ignored_once_linked():
    f = fac("tiffin", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883")
    old = acct("T", "Old Name Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883", parent=CEDAR, rev=84000, ar=12400)
    assert actions(run([f], [old])) == [M.CHOW_NEW_SUCCESSOR]  # no UPDATE_FIELDS on the preserved account
    linked = dict(old, chow_current_account="NEW1")
    new = acct("NEW1", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883", parent=BH)
    assert run([f], [linked, new]) == []  # converged: nothing proposed after the CHOW is applied


def test_sold_facility_links_to_existing_successor_instead_of_creating_one():
    old = acct("S", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870", parent=BH, rev=130000, ar=5200)
    successor = acct("M", "Millstone Care of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870", parent=MILL)
    out = run([], [old, successor])
    assert actions(out) == [M.CHOW_LINK_EXISTING]
    assert out[0]["target_id"] == "S" and out[0]["proposed"] == {"chow_current_account": "M"}
    # Two possible successors -> stop and flag for manual review, no link proposed.
    second = acct("M2", "Other Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870", parent=CEDAR)
    out = run([], [old, successor, second])
    assert actions(out) == [M.FLAG_REVIEW] and "Multiple possible successors" in out[0]["proposed"]["note"]


def test_bellhaven_account_missing_from_site_is_flagged_not_deactivated():
    alliance = acct("A", "Bellhaven Care Center of Alliance", "1785 Freshley Ave", "Alliance", "OH", "44601", parent=BH)
    out = run([], [alliance])
    assert actions(out) == [M.FLAG_REVIEW]
    assert out[0]["proposed"]["status"] == "Needs Review"
    assert run([], [dict(alliance, status="Needs Review")]) == []  # already flagged -> converged


def test_fingerprints_are_stable_and_change_with_the_proposed_values():
    f = fac("portsmouth", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45662")
    a = acct("A1", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45626", parent=BH)
    first, second = run([f], [a])[0], run([f], [dict(a, phone="changed")])[0]
    assert first["fingerprint"] == second["fingerprint"]
    moved = run([dict(f, zip="45663")], [a])[0]
    assert moved["fingerprint"] != first["fingerprint"]


def test_trace_records_how_each_facility_matched_without_changing_proposals():
    site = [fac("saline", "Bellhaven of Saline", "980 W Michigan Ave", "Saline", "MI", "48176"),
            fac("portsmouth", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45662"),
            fac("kettering", "Bellhaven of Kettering", "3313 Wilmington Pike", "Kettering", "OH", "45429"),
            fac("batavia", "Bellhaven of Batavia", "2000 Hospital Dr", "Batavia", "OH", "45103")]
    accounts = [acct("A1", "Bellhaven of Saline", "980 West Michigan Avenue", "Saline", "MI", "48176", parent=BH),
                acct("A2", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45626", parent=BH),
                acct("K1", "Kettering Care Centre", "3313 Wilmington Pike", "Kettering", "OH", "45429", parent=HARBOR),
                acct("K2", "Kettering Nursing", "3313 Wilmington Pike", "Kettering", "OH", "45429"),
                acct("S", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870", parent=BH, rev=130000, ar=5200),
                acct("M", "Millstone Care of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870", parent=MILL)]
    trace = []
    with_trace = M.match(site, parents() + accounts, [], None, trace=trace)
    assert [p["fingerprint"] for p in with_trace] == [p["fingerprint"] for p in run(site, accounts)]
    rows = {r.get("slug") or r["account_id"]: r for r in trace}
    assert rows["saline"]["method"] == "Street + ZIP" and rows["saline"]["account_id"] == "A1" and rows["saline"]["actions"] == []
    assert rows["portsmouth"]["method"] == "Street + city (ZIP differs)" and rows["portsmouth"]["actions"] == [M.UPDATE_FIELDS]
    assert rows["kettering"]["account_id"] == "" and len(rows["kettering"]["candidates"]) == 2
    assert rows["kettering"]["actions"] == [M.AMBIGUOUS]
    assert rows["batavia"]["method"] == "No CRM account at this address" and rows["batavia"]["actions"] == [M.CREATE_ACCOUNT]
    assert rows["S"]["kind"] == "not_on_site" and rows["S"]["actions"] == [M.CHOW_LINK_EXISTING]
