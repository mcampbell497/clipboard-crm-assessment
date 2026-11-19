"""Scrape every Bellhaven community from the public website.

Three things this has to get right:
1. The directory is paginated, and a page number past the end returns the LAST page again
   (HTTP 200), so we stop on the "Page X of Y" text instead of looping until empty.
2. Some communities are linked from the homepage but missing from the directory.
3. The homepage states how many communities exist. If we scraped a different number we
   stop the whole run: a partial scrape would make healthy CRM accounts look stale.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

from bs4 import BeautifulSoup

MAX_PAGES = 50  # hard stop in case the site's pagination ever misbehaves


class ScrapeError(RuntimeError):
    pass


@dataclass
class Facility:
    slug: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: List[str] = field(default_factory=list)
    administrator: str = ""
    phone: str = ""
    url: str = ""
    source: str = "directory"  # or "homepage" when only linked from the homepage

    def to_dict(self) -> dict:
        return asdict(self)


def make_fetcher(site_base: str, session=None) -> Callable[[str], str]:
    import requests

    session = session or requests.Session()

    def fetch(path: str) -> str:
        response = session.get(site_base + path, timeout=20)
        response.raise_for_status()
        return response.text

    return fetch


def _community_slugs(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    slugs = []
    for link in soup.find_all("a", href=True):
        match = re.fullmatch(r"/communities/([a-z0-9-]+)/?", link["href"])
        if match and match.group(1) not in slugs:
            slugs.append(match.group(1))
    return slugs


def parse_directory_page(html: str) -> dict:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    pages = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", text)
    listed = re.search(r"(\d+)\s+communities\s+listed", text)
    return {
        "slugs": _community_slugs(html),
        "page": int(pages.group(1)) if pages else None,
        "total_pages": int(pages.group(2)) if pages else None,
        "listed_count": int(listed.group(1)) if listed else None,
    }


def parse_homepage(html: str) -> dict:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    claimed = re.search(r"(\d+)\s+communities", text)
    return {"slugs": _community_slugs(html), "claimed_count": int(claimed.group(1)) if claimed else None}


def parse_detail(html: str, slug: str, site_base: str = "") -> Facility:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(".wrap h1") or soup.find("h1")
    if heading is None:
        raise ScrapeError(f"{slug}: no heading on detail page")
    fields = {}
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            fields[dt.get_text(strip=True).lower()] = dd
    if "address" not in fields:
        raise ScrapeError(f"{slug}: no address on detail page")
    lines = [line.strip() for line in fields["address"].get_text("\n").split("\n") if line.strip()]
    locality = re.fullmatch(r"(.+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?", lines[-1]) if len(lines) >= 2 else None
    if not locality:
        raise ScrapeError(f"{slug}: could not parse address {lines!r}")
    offerings = [b.get_text(strip=True) for b in fields["care offerings"].select(".badge")] if "care offerings" in fields else []
    return Facility(
        slug=slug,
        name=" ".join(heading.get_text(" ", strip=True).split()),
        street=" ".join(lines[:-1]),
        city=locality.group(1),
        state=locality.group(2),
        zip=locality.group(3),
        care_offerings=offerings,
        administrator=fields["administrator"].get_text(strip=True) if "administrator" in fields else "",
        phone=fields["phone"].get_text(strip=True) if "phone" in fields else "",
        url=f"{site_base}/communities/{slug}",
    )


def scrape(fetch: Callable[[str], str], site_base: str = "") -> List[Facility]:
    """Return every facility, or raise ScrapeError if coverage cannot be verified."""
    directory_slugs: List[str] = []
    listed_count: Optional[int] = None
    page = 1
    while True:
        info = parse_directory_page(fetch(f"/communities?page={page}"))
        if info["page"] is None or info["total_pages"] is None:
            raise ScrapeError(f"Directory page {page} has no 'Page X of Y' marker; layout changed?")
        if info["page"] != page:
            raise ScrapeError(f"Asked for page {page} but the site returned page {info['page']}")
        listed_count = info["listed_count"]
        for slug in info["slugs"]:
            if slug not in directory_slugs:
                directory_slugs.append(slug)
        if page >= info["total_pages"]:
            break
        page += 1
        if page > MAX_PAGES:
            raise ScrapeError("Pagination did not terminate")

    home = parse_homepage(fetch("/"))
    homepage_only = [s for s in home["slugs"] if s not in directory_slugs]

    # Coverage checks: fail loudly rather than reconcile against a partial list.
    if listed_count is not None and listed_count != len(directory_slugs):
        raise ScrapeError(f"Directory says {listed_count} communities but {len(directory_slugs)} were found")
    total = len(directory_slugs) + len(homepage_only)
    if home["claimed_count"] is not None and home["claimed_count"] != total:
        raise ScrapeError(
            f"Homepage claims {home['claimed_count']} communities but {total} were found "
            f"({len(directory_slugs)} in directory + {len(homepage_only)} homepage-only)"
        )

    facilities = []
    for slug in directory_slugs + homepage_only:
        facility = parse_detail(fetch(f"/communities/{slug}"), slug, site_base)
        facility.source = "directory" if slug in directory_slugs else "homepage"
        facilities.append(facility)
    return facilities
