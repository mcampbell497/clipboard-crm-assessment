"""Test categories 4 (CHOW retries) and 5 (approval and drift guards). All CRM calls are faked."""
import pytest
from conftest import BH, CEDAR, HARBOR, MILL, FakeCRM, acct, fac, parents

from bellhaven import matcher as M
from bellhaven import store as S
from bellhaven.applier import Applier, NotApproved

TIFFIN = fac("tiffin", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883")


def setup(store, facilities, accounts, contacts=()):
    crm = FakeCRM(parents() + accounts, contacts)
    store.sync(M.match(facilities, crm.list_accounts(), list(contacts)))
    return crm, Applier(crm, store)


def only(store, action):
    rows = [p for p in store.list() if p["action"] == action]
    assert len(rows) == 1
    return rows[0]


def chow_case(store):
    old = acct("T", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883", parent=CEDAR, rev=84000, ar=12400)
    crm, applier = setup(store, [TIFFIN], [old])
    return crm, applier, only(store, M.CHOW_NEW_SUCCESSOR)


# ---------------------------------------------------------------- approval guard
def test_unapproved_proposal_sends_nothing(store):
    crm, applier, p = chow_case(store)
    with pytest.raises(NotApproved):
        applier.apply(p["id"])
    assert crm.writes == []
    assert store.get(p["id"])["state"] == S.PENDING


def test_rejected_proposal_can_never_be_applied(store):
    crm, applier, p = chow_case(store)
    store.decide(p["id"], S.REJECTED, "not convinced")
    with pytest.raises(NotApproved):
        applier.apply(p["id"])
    with pytest.raises(ValueError):
        store.decide(p["id"], S.APPROVED)  # a decided proposal cannot be re-decided
    assert crm.writes == []


# ---------------------------------------------------------------- CHOW
def test_chow_happy_path_preserves_old_account(store):
    crm, applier, p = chow_case(store)
    store.decide(p["id"], S.APPROVED)
    result = applier.apply(p["id"])
    assert result["state"] == S.APPLIED and result["verified"]
    assert len(crm.posts()) == 1
    old = crm.accounts["T"]
    new_id = old["chow_current_account"]
    assert new_id and crm.accounts[new_id]["parent_id"] == BH
    assert old["parent_id"] == CEDAR and old["status"] == "Active" and old["name"] == "Bellhaven of Tiffin"
    # The old account was only ever sent the link field.
    assert [w[2] for w in crm.patches()] == [{"chow_current_account": new_id}]
    assert all("parent_id" not in body for _, _, body in crm.patches())


def test_chow_retry_after_failed_link_does_not_create_second_successor(store):
    crm, applier, p = chow_case(store)
    store.decide(p["id"], S.APPROVED)
    crm.fail_patch = "uncertain"                      # POST succeeds, linking PATCH is lost
    first = applier.apply(p["id"])
    assert first["state"] == S.FAILED and len(crm.posts()) == 1
    assert store.get(p["id"])["result"]["successor_id"] == "NEW1"   # progress saved before the PATCH
    second = applier.apply(p["id"])                   # reviewer clicks Retry
    assert second["state"] == S.APPLIED
    assert len(crm.posts()) == 1                      # exactly one successor ever created
    assert crm.accounts["T"]["chow_current_account"] == "NEW1"


def test_chow_retry_after_post_timeout_adopts_the_account_that_did_land(store):
    crm, applier, p = chow_case(store)
    store.decide(p["id"], S.APPROVED)
    crm.post_times_out = True                         # server created it, client saw a timeout
    assert applier.apply(p["id"])["state"] == S.FAILED
    assert applier.apply(p["id"])["state"] == S.APPLIED
    assert len(crm.posts()) == 1
    bellhaven_tiffins = [a for a in crm.accounts.values() if a["parent_id"] == BH and a["billing_city"] == "Tiffin"]
    assert len(bellhaven_tiffins) == 1


def test_chow_third_apply_is_a_no_op(store):
    crm, applier, p = chow_case(store)
    store.decide(p["id"], S.APPROVED)
    applier.apply(p["id"])
    store.set_state(p["id"], S.FAILED)                # pretend the UI lost the result
    writes_before = len(crm.writes)
    assert applier.apply(p["id"])["state"] == S.APPLIED
    assert len(crm.writes) == writes_before


def test_chow_stops_when_multiple_possible_successors_exist(store):
    crm, applier, p = chow_case(store)
    store.decide(p["id"], S.APPROVED)
    for i in (1, 2):                                  # two accounts appear under Bellhaven at the address
        crm.accounts[f"X{i}"] = acct(f"X{i}", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883", parent=BH)
    result = applier.apply(p["id"])
    assert result["state"] == S.FAILED and "manual review" in result["message"]
    assert crm.writes == []


def test_financials_are_revalidated_on_live_values_before_a_direct_reparent(store):
    lima = fac("lima", "Bellhaven Crossings of Lima", "3115 N Cole St", "Lima", "OH", "45801")
    a = acct("L", "Bellhaven Crossings of Lima", "3115 N Cole St", "Lima", "OH", "45801", parent=HARBOR, rev=47000, ar=0)
    crm, applier = setup(store, [lima], [a])
    p = only(store, M.SET_PARENT)
    store.decide(p["id"], S.APPROVED)
    crm.accounts["L"]["outstanding_ar"] = 900         # an invoice appears after the proposal was made
    result = applier.apply(p["id"])
    assert result["state"] == S.SUPERSEDED            # drift detected, nothing written
    assert crm.writes == [] and crm.accounts["L"]["parent_id"] == HARBOR


def test_direct_reparent_is_blocked_by_sop_even_if_snapshot_matches(store):
    lima = fac("lima", "Bellhaven Crossings of Lima", "3115 N Cole St", "Lima", "OH", "45801")
    a = acct("L", "Bellhaven Crossings of Lima", "3115 N Cole St", "Lima", "OH", "45801", parent=HARBOR, rev=47000, ar=0)
    crm, applier = setup(store, [lima], [a])
    p = only(store, M.SET_PARENT)
    store.decide(p["id"], S.APPROVED)
    # Tamper with BOTH the live value and the stored snapshot: the SOP check itself must still stop it.
    crm.accounts["L"]["outstanding_ar"] = 900
    store.db.execute("UPDATE proposals SET before=json_set(before, '$.outstanding_ar', 900) WHERE id=?", (p["id"],))
    result = applier.apply(p["id"])
    assert result["state"] == S.FAILED and "SOP forbids" in result["message"]
    assert crm.writes == []


# ---------------------------------------------------------------- other guards
def test_drifted_account_is_not_written(store):
    f = fac("portsmouth", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45662")
    a = acct("A1", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45626", parent=BH)
    crm, applier = setup(store, [f], [a])
    p = only(store, M.UPDATE_FIELDS)
    store.decide(p["id"], S.APPROVED)
    crm.accounts["A1"]["name"] = "Someone renamed this"
    assert applier.apply(p["id"])["state"] == S.SUPERSEDED
    assert crm.writes == []


def test_patch_is_verified_and_side_effects_are_caught(store):
    f = fac("portsmouth", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45662")
    a = acct("A1", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45626", parent=BH, phone="555")
    crm, applier = setup(store, [f], [a])
    p = only(store, M.UPDATE_FIELDS)
    store.decide(p["id"], S.APPROVED)
    crm.patch_wipes = True                            # the API unexpectedly blanks another field
    result = applier.apply(p["id"])
    assert result["state"] == S.FAILED and "UNEXPECTED side effects" in result["message"]


def test_patch_happy_path_and_idempotent_reapply(store):
    f = fac("portsmouth", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45662")
    a = acct("A1", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45626", parent=BH)
    crm, applier = setup(store, [f], [a])
    p = only(store, M.UPDATE_FIELDS)
    store.decide(p["id"], S.APPROVED)
    assert applier.apply(p["id"])["state"] == S.APPLIED
    assert crm.accounts["A1"]["billing_zip"] == "45662" and len(crm.writes) == 1
    store.set_state(p["id"], S.FAILED)
    assert applier.apply(p["id"])["state"] == S.APPLIED and len(crm.writes) == 1  # already in place -> no second write


def test_create_does_not_duplicate_after_a_timeout(store):
    f = fac("batavia", "Bellhaven of Batavia", "2000 Hospital Dr", "Batavia", "OH", "45103")
    crm, applier = setup(store, [f], [])
    p = only(store, M.CREATE_ACCOUNT)
    store.decide(p["id"], S.APPROVED)
    crm.post_times_out = True
    assert applier.apply(p["id"])["state"] == S.FAILED
    assert applier.apply(p["id"])["state"] == S.APPLIED
    assert len(crm.posts()) == 1


def test_chow_link_existing_sets_only_the_link_and_rechecks_successor(store):
    old = acct("S", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870", parent=BH, rev=130000, ar=5200)
    successor = acct("M", "Millstone Care of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870", parent=MILL)
    crm, applier = setup(store, [], [old, successor])
    p = only(store, M.CHOW_LINK_EXISTING)
    store.decide(p["id"], S.APPROVED)
    assert applier.apply(p["id"])["state"] == S.APPLIED
    assert crm.posts() == [] and crm.patches() == [("PATCH", "S", {"chow_current_account": "M"})]
    assert crm.accounts["S"]["parent_id"] == BH


def test_chow_link_blocked_when_a_second_candidate_appears(store):
    old = acct("S", "Bellhaven of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870", parent=BH, rev=130000, ar=5200)
    successor = acct("M", "Millstone Care of Sandusky", "2715 Columbus Ave", "Sandusky", "OH", "44870", parent=MILL)
    crm, applier = setup(store, [], [old, successor])
    p = only(store, M.CHOW_LINK_EXISTING)
    store.decide(p["id"], S.APPROVED)
    crm.accounts["M2"] = acct("M2", "Another Sandusky", "2715 Columbus Avenue", "Sandusky", "OH", "44870", parent=CEDAR)
    result = applier.apply(p["id"])
    assert result["state"] == S.FAILED and "manual review" in result["message"]
    assert crm.writes == []


def test_reviewer_can_choose_duplicate_for_a_legacy_record(store):
    f = fac("erie", "Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505", care=("Assisted Living",))
    current = acct("CUR", "Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505", parent=BH, care="Assisted Living")
    legacy = acct("OLD", "Harborview Shores of Erie", "4930 West Lake Road", "Erie", "PA", "16505", parent=HARBOR, care="Assisted Living")
    crm, applier = setup(store, [f], [current, legacy])
    p = only(store, M.FLAG_REVIEW)
    store.decide(p["id"], S.APPROVED, "confirmed duplicate", chosen="alt")
    assert applier.apply(p["id"])["state"] == S.APPLIED
    assert crm.accounts["OLD"]["duplicate_of_account"] == "CUR" and crm.accounts["OLD"]["status"] == "Inactive"
    assert crm.accounts["OLD"]["parent_id"] == HARBOR


def test_api_calls_are_audited_without_credentials(store):
    crm, applier, p = chow_case(store)
    store.decide(p["id"], S.APPROVED)
    applier.apply(p["id"])
    # The applier wires the client's audit hook to the store; the real client calls it on every write.
    crm.audit(method="PATCH", path="/accounts/T", body={"x": 1}, status=200, response="ok")
    log = store.api_log(p["id"])
    assert len(log) == 1 and set(log[0]) == {"id", "proposal_id", "at", "method", "path", "body", "status", "response"}
    assert "authorization" not in str(log).lower() and "bearer" not in str(log).lower()
