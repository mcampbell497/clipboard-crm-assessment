"""Test categories 6 (idempotent re-runs) and 7 (scraper coverage)."""
import pytest
from conftest import BH, FakeCRM, acct, fac, make_site, parents

from bellhaven import matcher as M
from bellhaven import store as S
from bellhaven.applier import Applier
from bellhaven.pipeline import run
from bellhaven.scraper import ScrapeError, scrape

SITE = [
    fac("saline", "Bellhaven of Saline", "980 W Michigan Ave", "Saline", "MI", "48176"),
    fac("portsmouth", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45662"),
    fac("grove-city", "Bellhaven Rehabilitation & Nursing of Grove City", "3985 Broadway", "Grove City", "OH", "43123"),
    fac("batavia", "Bellhaven of Batavia", "2000 Hospital Dr", "Batavia", "OH", "45103"),
    fac("erie", "Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505", care=("Assisted Living", "Memory Support")),
]
FINDLAY = fac("findlay", "Bellhaven Meadows of Findlay", "1800 N Blanchard St", "Findlay", "OH", "45840", source="homepage")


def crm_accounts():
    return parents() + [
        acct("A1", "Bellhaven of Saline", "980 West Michigan Avenue", "Saline", "MI", "48176", parent=BH),
        acct("A2", "Bellhaven of Portsmouth", "2222 Gallia St", "Portsmouth", "OH", "45626", parent=BH),
        acct("A3", "Bellhaven Rehab and Nursing of Grove City", "3985 Broadway", "Grove City", "OH", "43123", parent=BH),
        acct("A5", "Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505", parent=BH, care="Assisted Living"),
        acct("A6", "Bellhaven Meadows of Findlay", "1800 N Blanchard St", "Findlay", "OH", "45840", rev=22000),
    ]


# ---------------------------------------------------------------- scraper
def test_scraper_walks_all_pages_stops_and_includes_homepage_only_facility():
    fetch, requested = make_site(SITE, homepage_only=[FINDLAY], per_page=2)
    facilities = scrape(fetch)
    assert [f.slug for f in facilities] == ["saline", "portsmouth", "grove-city", "batavia", "erie", "findlay"]
    assert facilities[-1].source == "homepage"
    # 5 facilities at 2 per page = 3 pages; page 4 (which would repeat page 3 forever) is never requested.
    assert [p for p in requested if "page=" in p] == ["/communities?page=1", "/communities?page=2", "/communities?page=3"]


def test_scraper_parses_detail_fields():
    fetch, _ = make_site(SITE, homepage_only=[FINDLAY])
    erie = next(f for f in scrape(fetch) if f.slug == "erie")
    assert (erie.name, erie.street, erie.city, erie.state, erie.zip) == \
        ("Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505")
    assert erie.care_offerings == ["Assisted Living", "Memory Support"]
    grove = next(f for f in scrape(fetch) if f.slug == "grove-city")
    assert grove.name == "Bellhaven Rehabilitation & Nursing of Grove City"


def test_scraper_fails_loudly_when_coverage_does_not_match_the_site_claims():
    fetch, _ = make_site(SITE, claimed=6)             # homepage says 6, only 5 reachable
    with pytest.raises(ScrapeError, match="claims 6"):
        scrape(fetch)
    fetch, _ = make_site(SITE, listed=9)              # directory header disagrees with its own cards
    with pytest.raises(ScrapeError, match="Directory says 9"):
        scrape(fetch)


def test_failed_scrape_changes_nothing(store):
    crm = FakeCRM(crm_accounts())
    fetch, _ = make_site(SITE, claimed=99)
    with pytest.raises(ScrapeError):
        run(store, crm, fetch)
    assert store.list() == [] and crm.writes == []


# ---------------------------------------------------------------- idempotent re-runs
def test_pipeline_never_writes_and_second_run_adds_nothing(store):
    crm = FakeCRM(crm_accounts())
    fetch, _ = make_site(SITE, homepage_only=[FINDLAY])
    first = run(store, crm, fetch)
    assert first["facilities_scraped"] == 6
    assert first["by_action"] == {M.UPDATE_FIELDS: 2, M.CREATE_ACCOUNT: 1, M.SET_PARENT: 1}
    assert first["queue"]["new"] == 4
    second = run(store, crm, fetch)
    assert second["queue"]["new"] == 0 and second["queue"]["still_pending"] == 4
    assert len(store.list()) == 4
    assert crm.writes == []                           # the pipeline itself never writes


def test_decided_proposals_are_not_reproposed_and_applied_ones_converge(store):
    crm = FakeCRM(crm_accounts())
    fetch, _ = make_site(SITE, homepage_only=[FINDLAY])
    run(store, crm, fetch)
    by_target = {p["target_key"]: p for p in store.list()}
    store.decide(by_target["A3"]["id"], S.REJECTED, "keep the short name")
    store.decide(by_target["A2"]["id"], S.APPROVED)
    assert Applier(crm, store).apply(by_target["A2"]["id"])["state"] == S.APPLIED

    again = run(store, crm, fetch)
    assert again["queue"]["new"] == 0
    assert again["queue"]["already_decided"] == 1     # the rejected rename was generated again and ignored
    states = {p["target_key"]: p["state"] for p in store.list()}
    assert states["A3"] == S.REJECTED and states["A2"] == S.APPLIED
    assert len(store.list()) == 4                     # no duplicates of anything
    assert len(crm.writes) == 1                       # the one approved write, never repeated


def test_changed_evidence_after_a_rejection_needs_a_fresh_decision(store):
    crm = FakeCRM(crm_accounts())
    fetch, _ = make_site(SITE, homepage_only=[FINDLAY])
    run(store, crm, fetch)
    rename = next(p for p in store.list() if p["target_key"] == "A3")
    store.decide(rename["id"], S.REJECTED)

    renamed_site = [dict(f, name="Bellhaven Grove City Health Campus") if f["slug"] == "grove-city" else f for f in SITE]
    fetch2, _ = make_site(renamed_site, homepage_only=[FINDLAY])
    result = run(store, crm, fetch2)
    assert result["queue"]["new"] == 1
    a3 = [p for p in store.list() if p["target_key"] == "A3"]
    assert sorted(p["state"] for p in a3) == [S.PENDING, S.REJECTED]   # old rejection kept, new one awaits review
    fresh = next(p for p in a3 if p["state"] == S.PENDING)
    assert [h["state"] for h in store.history_for_target("A3", fresh["id"])] == [S.REJECTED]
    assert crm.writes == []                           # nothing was applied on the strength of the old decision


def test_pending_proposal_is_superseded_when_the_crm_is_fixed_elsewhere(store):
    crm = FakeCRM(crm_accounts())
    fetch, _ = make_site(SITE, homepage_only=[FINDLAY])
    run(store, crm, fetch)
    crm.accounts["A2"]["billing_zip"] = "45662"       # someone corrects the ZIP by hand
    result = run(store, crm, fetch)
    assert result["queue"]["superseded"] == 1
    assert next(p for p in store.list() if p["target_key"] == "A2")["state"] == S.SUPERSEDED


def test_pipeline_saves_what_it_scraped_and_how_it_matched(store):
    crm = FakeCRM(crm_accounts())
    fetch, _ = make_site(SITE, homepage_only=[FINDLAY])
    assert store.last_snapshot() is None and store.last_successful_run() is None
    run(store, crm, fetch)
    snap = store.last_snapshot()
    assert [f["slug"] for f in snap["facilities"]][-1] == "findlay"
    assert len([r for r in snap["trace"] if r["kind"] == "facility"]) == 6
    assert store.last_successful_run()["summary"]["facilities_scraped"] == 6
    assert crm.writes == []
    # a failed run is recorded but does not replace the last successful one
    bad, _ = make_site(SITE, claimed=99)
    with pytest.raises(ScrapeError):
        run(store, crm, bad)
    store.record_run(False, {"error": "coverage"})
    assert store.last_successful_run()["summary"]["facilities_scraped"] == 6 and store.last_run()["ok"] == 0
