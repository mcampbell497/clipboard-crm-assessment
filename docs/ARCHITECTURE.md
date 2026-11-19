# Bellhaven CRM reconciliation: discovery findings and proposed architecture

Status: **implemented.** Discovery was read-only (website GETs, CRM GETs) on 2026-11-18.
Sections 2 to 9 are the original plan. Where the build differs, this section wins.

## 0. Changes made after the architecture review

- **No arbitrary survivor.** The tie-break chain in section 3 step 3 was dropped. A survivor is
  chosen only when it is the sole account under Bellhaven, the sole account at the address, or
  uniquely backed by phone / administrator contact / exact name. Otherwise the facility becomes
  an `AMBIGUOUS` item (Kettering) showing every candidate; the reviewer picks one, which writes
  nothing and generates ordinary proposals that still need approval.
- **Conservative duplicates.** `MARK_DUPLICATE` is proposed only for same parent + same name +
  same address (Owosso). Accounts at the same address under another parent (Erie, Port Clinton,
  Monroe x2) get `FLAG_REVIEW`; the reviewer may instead confirm "duplicate" explicitly. A record
  with revenue or AR is never offered for deactivation.
- **Sandusky** is `CHOW_LINK_EXISTING`, re-verified at apply time: same address, successor live
  under a different parent, and exactly one candidate, else it stops for manual review.
- **Write format is still undocumented.** Every write is followed by a full before/after
  comparison of the account, so an unexpected side effect (for example a PATCH that blanks other
  fields) fails loudly on the first proposal approved.
- **Changed evidence after a rejection** creates a new pending proposal and shows the earlier
  decision next to it; it never applies on the strength of an old decision.
- **Found by the first real run:** a third Bellhaven account missing from the website,
  *Bellhaven of Coldwater* (no revenue, no other account at the address) → `FLAG_REVIEW`.
- First real run: 35 facilities, 121 accounts, 28 proposals (10 field updates, 3 direct
  re-parents, 2 CHOW successors, 1 CHOW link, 4 creates, 1 duplicate, 6 review flags, 1 ambiguous).
- `chow.py` holds only the SOP decision; the CHOW apply sequence lives in `applier.py`.

## 1. What the system does

```
scrape website ──┐
                 ├─► match + classify ─► proposals (SQLite) ─► Streamlit review ─► apply via API ─► verify
fetch CRM ───────┘        (pure function)     pending             approve/reject     (only on approve)
```

The daily job runs the left half only (scrape, fetch, match, store proposals). Writes happen
only from the review app, one approved proposal at a time.

## 2. Discovery findings

### Website
- Directory: `/communities?page=N`, 3 pages, 16 + 16 + 2 = **34** facilities.
- Homepage says 35. The 35th is **Bellhaven Meadows of Findlay**, linked only from the homepage
  "New this year" notice. Its detail page exists and is not in the directory.
- **Pagination trap:** a page past the end (`?page=4`) returns the last page again with HTTP 200.
  A "loop until empty" scraper never terminates. Stop on "Page X of Y" / missing Next link.
- No sitemap, robots.txt, or JSON endpoint. Detail pages are the only source of street and ZIP.
- Detail page fields: name, street, city/state/ZIP, care offerings (1 or 2), administrator, phone.
- `/about` names two acquisitions: **Harborview Care Group (2025)** and "select communities"
  from **Cedar Trail (2026)**. Both exist as parent accounts in the CRM.
- Coverage check to build in: scraped count must equal the count claimed on the homepage
  (35) and directory count (34) + homepage-only links. A mismatch fails the run loudly.

### CRM API
- Auth: `Authorization: Bearer <token>`; 401 without it. `GET /me` validates the token.
- `GET /accounts` filters: `q, city, state, zip, street, parent_id, page, page_size`.
  Response `{data, page, page_size, total}`. `page_size` is capped at 200.
- `GET/PATCH /accounts/{id}`, `POST /accounts`, plus the same for `/contacts`.
- **The OpenAPI spec documents no request bodies or response schemas.** Field names below
  come from real responses. POST/PATCH body rules are an unknown (section 8).
- Account fields: `account_id, name, parent_id, parent_name, billing_street, billing_city,
  billing_state, billing_zip, care_type, status, phone, lifetime_revenue, outstanding_ar,
  chow_current_account, duplicate_of_account, note, created_by_candidate, updated_at`.
- 121 accounts, 67 contacts. Financials are ints on every row today (no nulls yet).

### How the parent is represented
- A parent is an ordinary account named `"<Company> (Parent Account)"` with empty address.
- Bellhaven parent: `0015QAPLGS3FVYEEEM`. Resolve it at runtime by exact name, fail if not exactly one.
- Other parents: Juniper Point (30 children), Stonebridge (25), Harborview (5), Cedar Trail (5),
  Millstone (1). 20 facility accounts have no parent.
- 29 accounts sit under Bellhaven today. The website has 35 facilities.

### Real data, by case (all 35 facilities accounted for)

| Case | Count | Facilities |
|---|---|---|
| Clean match, no change | 12 | Maplewood, New Carlisle, Wooster, Akron, Saline, Goshen, Meadville, Defiance, Toledo, Marion, Kalamazoo, Altoona |
| Match, name out of date (minor) | 4 | Grove City, Sycamore Ridge, Arbors Dayton, Ashland |
| Match, legacy pre-rebrand name under Bellhaven | 3 | Riverbend Manor → Chagrin Falls, Sunny Acres → Willow Creek, Chesterton Senior Commons → Chesterton |
| Match, bad address | 2 | Ashtabula (PO Box), Portsmouth (ZIP 45626 vs 45662) |
| Match + duplicate/legacy accounts at same address | 4 | Owosso (2 under Bellhaven), Port Clinton, Erie (Harborview copies), Monroe (Harborview + Cedar Trail copies) |
| Wrong parent, **direct re-parent** allowed | 3 | Lima (Harborview, rev 47k, AR 0), Findlay (no parent, rev 22k, AR 0), Zanesville (Cedar Trail, rev 0, also needs rename) |
| Wrong parent, **CHOW required** | 2 | Tiffin (Cedar Trail, rev 84k, AR 12.4k), Marietta (Cedar Trail, rev 51.25k, AR 3.8k) |
| Three candidate accounts, none under Bellhaven | 1 | Kettering (Harborview, Cedar Trail, and unparented copies; all rev 0, no contacts) |
| No CRM account | 4 | Batavia, Union Square (New Albany), Carlisle PA, Amberly Manor (Hudson OH) |

CRM accounts under Bellhaven that are not on the website:

- **Bellhaven of Sandusky** (rev 130k, AR 5.2k). A second account, *Millstone Care of Sandusky*,
  sits at the same address under Millstone Health Partners. Reads as: sold to Millstone.
  SOP applies (revenue + AR), and the successor **already exists**, so the right move is to set
  `chow_current_account` to the existing Millstone account, not create another.
- **Bellhaven Care Center of Alliance** (rev 0, AR 0). No other account at that address, no
  evidence of sale. Flag `Needs Review` with a note; do not mark Inactive on absence alone.

Name traps that fuzzy matching alone gets wrong:
- "Bellhaven of Carlisle" (PA) vs CRM "Bellhaven of New Carlisle" (OH).
- "Amberly Manor" (Hudson OH) vs CRM "Amberly Manor" in Colorado Springs under Juniper Point. Identical name, unrelated facility.
- "Bellhaven at Union Square" vs CRM "Union Square Senior Living" (same city, different street, Juniper Point).
- "Bellhaven of X" names all score > 0.8 against each other.

Useful corroborating evidence found: site phone equals CRM phone on about half the matches,
and the site's administrator name appears as a CRM contact on 20 of 35 (including on
legacy-named accounts, and on exactly one of the two Owosso duplicates).

## 3. Matching

Address first, name as supporting evidence only.

1. **Normalize.** Street: lowercase, strip punctuation, abbreviate suffixes and directionals
   (`Street→st`, `Northwest→nw`, `Pike/Pk→pike`). ZIP: first 5 digits. Name: lowercase, strip
   punctuation, `&→and`, `centre→center`, `rehab→rehabilitation`.
2. **Find candidates per facility**, in tiers:
   - A: normalized street + ZIP equal.
   - B: normalized street + city + state equal (catches the Portsmouth ZIP typo).
   - C: no street match, but exact normalized name + city + state, plus phone or administrator
     match (catches the Ashtabula PO Box). Name similarity never matches on its own.
3. **Pick one survivor per facility** when several accounts share the address, by a fixed
   order: already under Bellhaven → phone matches site → site administrator is a contact →
   has revenue history → has contacts → exact (unabbreviated) street match → lowest account id.
   The evidence panel shows which rule decided it. If only the last two rules separate the
   candidates (Kettering), the proposal is marked **low confidence**.
4. **One-to-one check.** Each account may be claimed by at most one facility and vice versa;
   a collision raises an "ambiguous" proposal instead of guessing.
5. **Classify** survivor vs site into proposals (below). Non-survivors at the address become
   duplicate proposals.
6. **Reverse pass.** Every account under Bellhaven not claimed by a facility: if another
   account exists at its address under a different parent → ownership-change proposal;
   otherwise → `Needs Review` proposal.

Care type is compared through a small map (`Short-Term Rehabilitation & Nursing ↔ Skilled
Nursing`, `Memory Support ↔ Memory Care`). No mismatches exist today, so it is evidence only.

## 4. Proposal types

| Type | CRM effect on approval |
|---|---|
| `UPDATE_FIELDS` | PATCH name and/or address fields on one account |
| `SET_PARENT` | PATCH `parent_id` (only when the SOP allows direct re-parenting) |
| `CHOW_NEW_SUCCESSOR` | POST new account under correct parent, then PATCH old `chow_current_account` |
| `CHOW_LINK_EXISTING` | PATCH old `chow_current_account` to an existing successor (Sandusky) |
| `CREATE_ACCOUNT` | POST new account under Bellhaven |
| `MARK_DUPLICATE` | PATCH `duplicate_of_account`, `status=Inactive`, note |
| `FLAG_REVIEW` | PATCH `status=Needs Review`, note |

Expected first run: roughly 9 field updates, 4 direct re-parents, 2 CHOW successors, 1 CHOW
link, 4 creates, 7 duplicates, 1 review flag.

## 5. Change of ownership (the SOP)

One function, `parent_change_strategy(account) -> DIRECT | CHOW | BLOCKED`, used in two places:
the matcher (to choose the proposal type) and the applier (re-run on **live** values right
before writing).

| lifetime_revenue | outstanding_ar | Result |
|---|---|---|
| > 0 | > 0 | CHOW |
| 0 | any number | DIRECT |
| any number | 0 | DIRECT |
| missing / non-numeric / negative | any | BLOCKED (needs investigation) |
| any | missing / non-numeric / negative | BLOCKED |

CHOW apply sequence, built so a retry never creates a second successor:

1. Re-GET the old account. If `chow_current_account` is already set → verify and mark applied.
2. Look for an existing successor: GET accounts filtered by street + ZIP under the target
   parent, excluding the old account. Also check the successor id saved locally from a
   previous attempt. If found, adopt it.
3. Otherwise POST the new account. Save the returned id to SQLite immediately, before step 4.
4. PATCH old account: `chow_current_account = <new id>` only. No other field is sent.
5. Re-GET both. Assert old `parent_id`, name, and status are unchanged and the link is set.

The API has no transactions. If the POST succeeds and the PATCH fails (or the POST times out
after the server created the row), the proposal stays in `applying` with the successor id
recorded, and "Retry" resumes at step 2, which finds the account instead of creating another.

## 6. Approvals, idempotency, and safe writes

SQLite (`data/review.db`, gitignored), three tables:

- `proposals`: `fingerprint` (unique), type, target account id / facility slug, before values,
  proposed values, evidence JSON, confidence, state, decided_at, reviewer note.
- `apply_log`: one row per API call made (method, path, body, status code, response id). Audit trail.
- `runs`: timestamp, counts scraped / fetched / proposed / skipped.

**Fingerprint** = hash of (type, target, canonical proposed values). `INSERT OR IGNORE` means:
- a pending proposal is not duplicated by the next run;
- an approved or rejected proposal is never proposed again;
- if the evidence changes (site publishes a different name), the fingerprint differs and it is
  proposed fresh.

After an approval lands, the CRM also matches the site, so the matcher stops generating it at
all. The two mechanisms back each other up.

**State machine:** `pending → approved → applying → applied | failed`, or `pending → rejected`,
or `pending → superseded` (CRM changed underneath it).

**Before every write:** re-GET the account and compare with the "before" snapshot stored on the
proposal. Any drift → no write, proposal marked `superseded`, next run re-evaluates.
**After every write:** re-GET and assert the fields landed. Only then mark `applied`.

The token is read from `.env` at startup, sent only in the Authorization header, and never
logged; `apply_log` stores bodies but not headers.

## 7. Project structure

```
bellhaven/
  config.py       env loading, fail fast if token missing
  scraper.py      website → list[Facility], with coverage checks
  crm.py          thin API client (get/list/create/update), timeouts, no retries on POST
  normalize.py    street / name / ZIP normalization
  matcher.py      pure: (facilities, accounts, contacts) → proposals
  chow.py         parent_change_strategy + CHOW apply sequence
  store.py        SQLite schema and queries
  applier.py      approved proposal → guarded API calls → verify
  pipeline.py     CLI entry: `python -m bellhaven.pipeline` (never writes to CRM)
app.py            Streamlit review UI
tests/            fixtures + pytest
schedule/crontab.txt
README.md, WRITEUP.md, requirements.txt, .env.example
```

Stack: Python, `requests`, `beautifulsoup4`, `streamlit`, `pytest`, stdlib `sqlite3`. No LLM calls.

**Schedule: cron, not GitHub Actions.** The review decisions live in a local SQLite file. A
hosted runner starts from a clean checkout each day, so it would have no memory of decisions
without extra plumbing (committing the DB or an artifact cache). A crontab line on the machine
that hosts the review app is simpler and honest. A commented GitHub Actions file can be included
to show the alternative and its trade-off.

## 8. Minimum tests

1. Normalization: `980 West Michigan Avenue` = `980 W Michigan Ave`; `Pk` = `Pike`; ZIP+4.
2. Matcher traps on fixtures: Carlisle PA does not match New Carlisle OH; Amberly Manor (OH)
   does not match Amberly Manor (CO); PO Box Ashtabula does match; Owosso yields one survivor
   and one duplicate.
3. SOP decision table, every row in section 5, including missing and non-numeric values.
4. CHOW apply against a fake client: POST then failed PATCH then retry → exactly one POST;
   old account's parent never appears in any PATCH body.
5. Applier guards: unapproved proposal → zero API calls; drifted account → zero writes.
6. Idempotency: run the pipeline twice on the same inputs → second run inserts zero proposals;
   rejected proposal is not re-inserted.
7. Scraper: saved HTML fixtures; pagination stops at page 3; Findlay is included; count is 35.

## 9. Unknowns to resolve before or during implementation

1. **POST/PATCH body rules are undocumented.** Required fields on create, whether PATCH is
   partial, whether `status` is validated, whether `parent_name` is derived from `parent_id`.
   Only a write can answer this. Plan: first approved write is a single low-risk one, inspected
   closely, before approving the rest.
2. **No sandbox reset is documented.** Treat every write as permanent.
3. **Kettering survivor.** Three equal candidates. The rule above picks deterministically, but
   it is a judgment call to defend in the writeup.
4. **Legacy copies at the same address (Erie, Port Clinton, Monroe, Kettering).** Treated as
   duplicates of the Bellhaven account. They could also be read as historical predecessor
   records; none has revenue, so the duplicate treatment costs nothing.
5. **Sandusky.** Linking to the existing Millstone account vs. only flagging for review.
6. **Alliance.** `Needs Review` vs `Inactive`.
7. **Phones and contacts.** Several matched accounts have a different phone from the site, and
   15 site administrators are not CRM contacts. Out of scope for v1 (evidence only); good
   "what I'd build next" material.
8. Rate limits: none observed in about 15 requests; client will still pace itself.
