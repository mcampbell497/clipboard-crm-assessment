"""Shared test doubles. No test ever talks to the real website or the real CRM."""
from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bellhaven.crm import CRMError, UncertainWrite  # noqa: E402
from bellhaven.store import Store  # noqa: E402

BH = "P_BELLHAVEN"
CEDAR = "P_CEDAR"
HARBOR = "P_HARBOR"
MILL = "P_MILLSTONE"
PARENT_NAMES = {BH: "Bellhaven Senior Living (Parent Account)", CEDAR: "Cedar Trail Communities (Parent Account)",
                HARBOR: "Harborview Care Group (Parent Account)", MILL: "Millstone Health Partners (Parent Account)"}


def acct(account_id, name, street="", city="", state="OH", zip_code="", parent="", rev=0, ar=0, phone="",
         care="Skilled Nursing", status="Active", **extra):
    row = {"account_id": account_id, "name": name, "parent_id": parent, "parent_name": PARENT_NAMES.get(parent, ""),
           "billing_street": street, "billing_city": city, "billing_state": state, "billing_zip": zip_code,
           "care_type": care, "status": status, "phone": phone, "lifetime_revenue": rev, "outstanding_ar": ar,
           "chow_current_account": "", "duplicate_of_account": "", "note": "", "created_by_candidate": False,
           "updated_at": "2026-01-01 00:00:00Z"}
    row.update(extra)
    return row


def parents():
    return [acct(pid, name, care="") for pid, name in PARENT_NAMES.items()]


def fac(slug, name, street, city, state, zip_code, care=("Short-Term Rehabilitation & Nursing",), admin="", phone="",
        source="directory"):
    return {"slug": slug, "name": name, "street": street, "city": city, "state": state, "zip": zip_code,
            "care_offerings": list(care), "administrator": admin, "phone": phone,
            "url": f"http://site/communities/{slug}", "source": source}


class FakeCRM:
    """In-memory CRM with the same method names as CRMClient, plus failure switches."""

    def __init__(self, accounts, contacts=()):
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.contacts = list(contacts)
        self.writes = []            # every POST/PATCH attempted, in order
        self.audit = None
        self.fail_patch = None      # "uncertain" | "reject": next PATCH fails without landing
        self.post_times_out = False  # next POST lands on the server but the client sees a timeout
        self.patch_wipes = False    # simulate a PATCH that unexpectedly blanks another field
        self._next = 1

    # reads
    def get_account(self, account_id):
        row = self.accounts.get(account_id)
        return copy.deepcopy(row) if row else None

    def list_accounts(self, **filters):
        rows = [copy.deepcopy(a) for a in self.accounts.values()]
        if filters.get("zip"):
            rows = [a for a in rows if a["billing_zip"] == filters["zip"]]
        return rows

    def list_contacts(self, **filters):
        return copy.deepcopy(self.contacts)

    # writes
    def create_account(self, body):
        self.writes.append(("POST", "", copy.deepcopy(body)))
        new_id = f"NEW{self._next}"
        self._next += 1
        self.accounts[new_id] = acct(new_id, "", created_by_candidate=True)
        self.accounts[new_id].update(body)
        self.accounts[new_id]["parent_name"] = PARENT_NAMES.get(body.get("parent_id"), "")
        if self.post_times_out:
            self.post_times_out = False
            raise UncertainWrite("POST /accounts: no response (ReadTimeout); outcome unknown")
        return self.get_account(new_id)

    def update_account(self, account_id, body):
        self.writes.append(("PATCH", account_id, copy.deepcopy(body)))
        if self.fail_patch:
            mode, self.fail_patch = self.fail_patch, None
            raise UncertainWrite("PATCH: no response") if mode == "uncertain" else CRMError("PATCH -> 422")
        self.accounts[account_id].update(body)
        if "parent_id" in body:
            self.accounts[account_id]["parent_name"] = PARENT_NAMES.get(body["parent_id"], "")
        if self.patch_wipes:
            self.accounts[account_id]["phone"] = ""
        return self.get_account(account_id)

    def posts(self):
        return [w for w in self.writes if w[0] == "POST"]

    def patches(self):
        return [w for w in self.writes if w[0] == "PATCH"]


# ---- a tiny fake website with the same HTML shape as the real one -------------------------
def _detail_html(f):
    badges = "".join(f'<span class="badge">{c.replace("&", "&amp;")}</span>' for c in f["care_offerings"])
    return (f'<header><a class="logo" href="/">Bellhaven</a><nav><a href="/communities">Our Communities</a></nav></header>'
            f'<div class="wrap"><h1>{f["name"].replace("&", "&amp;")}</h1><dl class="detail">'
            f'<dt>Address</dt><dd>{f["street"]}<br>{f["city"]}, {f["state"]} {f["zip"]}</dd>'
            f'<dt>Care Offerings</dt><dd>{badges}</dd><dt>Administrator</dt><dd>{f["administrator"]}</dd>'
            f'<dt>Phone</dt><dd>{f["phone"]}</dd></dl><p><a href="/communities">Back</a></p></div>')


def make_site(directory, homepage_only=(), per_page=2, claimed=None, listed=None):
    """Return (fetch, requested_paths). Pages past the end repeat the last page, like the real site."""
    total_pages = max(1, math.ceil(len(directory) / per_page))
    claimed = len(directory) + len(homepage_only) if claimed is None else claimed
    listed = len(directory) if listed is None else listed
    by_slug = {f["slug"]: f for f in list(directory) + list(homepage_only)}
    requested = []

    def fetch(path):
        requested.append(path)
        if path == "/":
            links = "".join(f'<a href="/communities/{f["slug"]}">{f["name"]}</a>' for f in homepage_only)
            return f'<nav><a href="/communities">Our Communities</a></nav><p>Today we proudly serve {claimed} communities and growing.</p>{links}'
        if path.startswith("/communities?page="):
            page = min(int(path.split("=")[1]), total_pages)
            chunk = directory[(page - 1) * per_page: page * per_page]
            cards = "".join(f'<div class="card"><h3><a href="/communities/{f["slug"]}">{f["name"]}</a></h3></div>' for f in chunk)
            return f'<p>Page {page} of {total_pages} · {listed} communities listed</p>{cards}<a href="/communities?page={page + 1}">Next</a>'
        return _detail_html(by_slug[path.rsplit("/", 1)[1]])

    return fetch, requested


@pytest.fixture
def store():
    return Store(":memory:")
