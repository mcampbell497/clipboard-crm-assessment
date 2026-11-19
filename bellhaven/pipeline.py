"""Daily job: scrape the website, read the CRM, match, and queue proposals for review.

    python -m bellhaven.pipeline

This NEVER writes to the CRM. It only issues GET requests and updates the local SQLite
review queue. Writes happen exclusively from the review app after a human approves.
"""
from __future__ import annotations

import sys
from collections import Counter

from .config import load_settings
from .crm import CRMClient, CRMError
from .matcher import MatchError, match
from .scraper import ScrapeError, make_fetcher, scrape
from .store import Store


def run(store: Store, client, fetch, site_base: str = "") -> dict:
    """One read-only reconciliation pass. Returns a summary dict; raises on any failure."""
    facilities = [f.to_dict() for f in scrape(fetch, site_base)]
    accounts = client.list_accounts()
    contacts = client.list_contacts()
    if not accounts:
        raise CRMError("CRM returned zero accounts; refusing to reconcile against an empty CRM")
    trace: list = []
    proposals = match(facilities, accounts, contacts, store.resolutions(), trace=trace)
    queue = store.sync(proposals)
    store.save_snapshot(facilities, trace)
    summary = {
        "facilities_scraped": len(facilities),
        "crm_accounts": len(accounts),
        "crm_contacts": len(contacts),
        "proposals_generated": len(proposals),
        "by_action": dict(Counter(p["action"] for p in proposals)),
        "queue": queue,
    }
    store.record_run(True, summary)
    return summary


def main() -> int:
    settings = load_settings()
    store = Store(settings.db_path)
    client = CRMClient(settings.api_base, settings.token)
    try:
        summary = run(store, client, make_fetcher(settings.site_base), settings.site_base)
    except (ScrapeError, CRMError, MatchError) as exc:
        store.record_run(False, {"error": str(exc)})
        print(f"RUN FAILED (no proposals changed): {exc}", file=sys.stderr)
        return 1
    print(f"Scraped {summary['facilities_scraped']} facilities; read {summary['crm_accounts']} CRM accounts.")
    print(f"Matcher produced {summary['proposals_generated']} proposals: {summary['by_action']}")
    q = summary["queue"]
    print(f"Queue: {q['new']} new, {q['still_pending']} still pending, {q['already_decided']} already decided "
          f"(not re-proposed), {q['reopened']} reopened, {q['superseded']} superseded.")
    print("No CRM writes were made. Review with: streamlit run app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
