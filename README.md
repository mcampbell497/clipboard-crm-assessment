# Bellhaven CRM reconciliation

Keeps CRM parent-company links accurate for Bellhaven Senior Living by comparing the
Bellhaven website with the CRM every day and queueing proposed corrections for a human
to approve.

```
scrape website ──┐
                 ├─► match + classify ─► proposals (SQLite) ─► review app ─► CRM API ─► verify
read CRM (GET) ──┘                          pending          approve / reject   one at a time
```

**Nothing is written to the CRM unless a reviewer clicks Approve on that specific proposal.**
The daily job is read-only.

## Setup

Requires Python 3.9+.

```bash
git clone https://github.com/mcampbell497/clipboard-crm-assessment.git
```

```bash
cd clipboard-crm-assessment
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Then put your CRM token in `.env` as `CRM_API_TOKEN=...`. The file is gitignored, and the token is only
ever read from the environment (never written to logs or the database).
The local review database (`data/review.db`) is created on the first run and is also gitignored.
Optionally set `GITHUB_REPO_URL=https://github.com/<you>/<repo>` to show "View source code" links on the Scraper and Matcher pages.

## Run

Tests (no network, CRM is faked):

```bash
.venv/bin/python -m pytest -q
```

Pipeline (read-only: scrape, read CRM, refresh the review queue):

```bash
.venv/bin/python -m bellhaven.pipeline
```

Review app:

```bash
.venv/bin/streamlit run app.py
```

Opens at http://localhost:8501. The app is light-mode only: `.streamlit/config.toml` sets the theme and hides Streamlit's options menu (so there is no theme picker), and it re-runs automatically when a file is saved. The **Run pipeline (read-only)** button at the top of the
Review queue (and on the Scraper and Matcher pages) does the same read-only refresh.

## Daily schedule

`schedule/crontab.txt` holds the entry (06:30 daily):

```
30 6 * * * cd /path/to/clipboard-crm-assessment && mkdir -p logs && .venv/bin/python -m bellhaven.pipeline >> logs/pipeline.log 2>&1
```

Install with `crontab -e`, replacing `/path/to` with where you cloned the repo. Cron was chosen over GitHub Actions because reviewer decisions
live in a local SQLite file; a hosted runner starts from a clean checkout each day and
would not remember what was already approved or rejected.

## Pages in the app

| Page | Shows |
|---|---|
| **Review queue** | A one-line pipeline status (facilities scraped, CRM accounts read, last successful run in local time, OK/failed indicator) with the read-only Run pipeline button, then every proposal with evidence and Approve / Reject. The only page that writes to the CRM. |
| **Scraper** | What the last successful run pulled from the website: every facility, how it was found (directory or homepage-only), and the coverage check. |
| **Matcher** | How each facility was linked to a CRM account: match method, candidates at the address, the chosen account and why, the result, the reverse check for Bellhaven accounts not on the website, and a small address-normaliser to try. |
| **Audit log** | Every write sent to the CRM, and every pipeline run. |

The Scraper and Matcher pages read a snapshot that the pipeline saves on each successful run; they never scrape or call the CRM themselves.

## Using the review app

Each proposal row shows the facility, what would change, and one status badge: **Judgement call** (the data
does not settle it), **CHOW** (billing history involved), or **High / Medium confidence**. Opening it shows current
vs proposed values, the website listing, the evidence (each fact marked ✅/❌), the CRM accounts involved, and a
Technical details toggle with account IDs and the exact API requests.

| Button | What happens |
|---|---|
| **Approve and apply** | Decision is saved, the account is re-read, safety rules are re-checked on live values, the write is sent, and the result is read back and verified. |
| **Approve as duplicate of …** | Only on "legacy record" items. Reviewer confirms the record is a duplicate instead of just flagging it. |
| **Reject** | Decision is saved. The same proposal is never raised again. |
| **Use this account** | Only on ambiguous items. Writes nothing; records which account is the right one and generates ordinary proposals that still need approval. |
| **Retry** | On failed items. Re-reads the CRM first, so a write that already landed is not repeated. |

## Proposal types

| Type | CRM effect when approved |
|---|---|
| `UPDATE_FIELDS` | PATCH name / address on one account |
| `SET_PARENT` | PATCH `parent_id` (only when the SOP allows a direct re-parent) |
| `CHOW_NEW_SUCCESSOR` | POST a new account under the correct parent, then PATCH only `chow_current_account` on the old one |
| `CHOW_LINK_EXISTING` | PATCH only `chow_current_account` on the old account, pointing at a successor that already exists |
| `CREATE_ACCOUNT` | POST a new account under Bellhaven |
| `MARK_DUPLICATE` | PATCH `duplicate_of_account`, `status=Inactive`, note |
| `FLAG_REVIEW` | PATCH `status=Needs Review`, note |
| `AMBIGUOUS` | No write. A reviewer picks the right account |

## How the key rules work

**Matching** (`bellhaven/matcher.py`). Address decides a match: normalized street plus ZIP,
or street plus city when the ZIP looks mistyped. If the address is unusable (a PO Box), an
exact name plus city is accepted only with a second signal (matching phone, or the site's
administrator being a CRM contact). Name similarity alone never matches.

**No guessing.** If several accounts share an address and nothing separates them, the item
is `AMBIGUOUS` and no survivor is picked. An account at the same address under a different
parent is flagged for review, not assumed to be a duplicate; only same parent + same name +
same address is proposed as a duplicate.

**Change of ownership SOP** (`bellhaven/chow.py`). One function decides:
revenue history AND outstanding AR > 0 → CHOW (old account and its parent are preserved);
otherwise → direct re-parent; missing or unreadable financials → blocked for investigation.
It runs when the proposal is created and again on live values immediately before the write.

**No duplicate successors** (`bellhaven/applier.py`). Before creating an account the
applier searches the live CRM for one at that address under the target parent and adopts
it if found. So a retry after a timeout, or after a failed linking step, never creates a
second successor. If more than one possible successor exists it stops for manual review.

**Safe re-runs** (`bellhaven/store.py`). Every proposal has a fingerprint (action + target +
proposed values) with a UNIQUE constraint. Approved, rejected, and applied proposals are
never inserted again. If the evidence changes, the fingerprint changes, and the new
proposal waits for a fresh decision with the earlier decision shown beside it.

**Every write is checked.** Before: re-read the account; any drift from the proposal's
snapshot cancels the write. After: re-read and confirm the sent fields landed and no other
field changed. All writes are recorded in the app's Audit log tab (method, path, body,
status). Request headers, and therefore the token, are never stored.

## Layout

```
bellhaven/
  config.py      settings from .env
  scraper.py     website -> facilities, with coverage checks
  crm.py         API client (GET retries; POST/PATCH never auto-retried)
  normalize.py   street / name / ZIP normalization
  chow.py        the SOP decision function
  matcher.py     pure function: facilities + accounts -> proposals
  store.py       SQLite: proposals, decisions, audit log, runs, last-run snapshot
  applier.py     approved proposal -> guarded write -> verification
  pipeline.py    daily read-only job
  display.py     review-app helpers: names instead of IDs, row titles, sorting
app.py           Streamlit app: Scraper, Matcher, Review queue, Audit log
.streamlit/      app theme (light only) and settings
tests/           63 tests, all offline
schedule/        crontab entry
docs/            architecture and discovery notes
```
