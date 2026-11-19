"""SQLite persistence: proposals, reviewer decisions, API audit log, run history.

The proposal's fingerprint is UNIQUE. That one constraint is what makes re-runs safe:
a proposal that was already approved, rejected, or applied can never be inserted again.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

PENDING, APPROVED, APPLYING, APPLIED = "pending", "approved", "applying", "applied"
FAILED, REJECTED, SUPERSEDED, RESOLVED = "failed", "rejected", "superseded", "resolved"
DECIDED = {APPROVED, APPLYING, APPLIED, FAILED, REJECTED, RESOLVED}

JSON_COLUMNS = ("before", "proposed", "alt", "warnings", "evidence", "result")

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    target_key TEXT NOT NULL,
    title TEXT NOT NULL,
    before TEXT NOT NULL DEFAULT '{}',
    proposed TEXT NOT NULL DEFAULT '{}',
    alt TEXT,
    chosen TEXT NOT NULL DEFAULT 'primary',
    confidence TEXT NOT NULL DEFAULT 'medium',
    warnings TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT,
    applied_at TEXT,
    result TEXT
);
CREATE TABLE IF NOT EXISTS api_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER,
    at TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    body TEXT,
    status INTEGER,
    response TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    facilities TEXT NOT NULL,
    trace TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resolutions (
    facility_slug TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _row(row) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        for column in JSON_COLUMNS:
            if column in data:
                data[column] = json.loads(data[column]) if data[column] else None
        return data

    # ---- proposals ---------------------------------------------------------
    def sync(self, proposals: List[dict]) -> Dict[str, int]:
        """Merge a fresh matcher run into the queue. Never touches decided proposals."""
        counts = {"new": 0, "still_pending": 0, "reopened": 0, "already_decided": 0, "superseded": 0}
        stamp = now()
        current = set()
        for p in proposals:
            current.add(p["fingerprint"])
            row = self.db.execute("SELECT id, state FROM proposals WHERE fingerprint=?", (p["fingerprint"],)).fetchone()
            fresh = (json.dumps(p["before"]), json.dumps(p["evidence"]), json.dumps(p["warnings"]),
                     json.dumps(p["alt"]) if p.get("alt") else None, p["title"], p["confidence"], stamp)
            if row is None:
                self.db.execute(
                    "INSERT INTO proposals (fingerprint, action, target_id, target_key, proposed, before, evidence, "
                    "warnings, alt, title, confidence, last_seen_at, created_at, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p["fingerprint"], p["action"], p["target_id"], p["target_key"], json.dumps(p["proposed"]),
                     *fresh, stamp, PENDING))
                counts["new"] += 1
            elif row["state"] in (PENDING, SUPERSEDED):
                self.db.execute(
                    "UPDATE proposals SET before=?, evidence=?, warnings=?, alt=?, title=?, confidence=?, "
                    "last_seen_at=?, state=?, result=NULL WHERE id=?", (*fresh, PENDING, row["id"]))
                counts["reopened" if row["state"] == SUPERSEDED else "still_pending"] += 1
            else:
                counts["already_decided"] += 1
        # Pending items the matcher no longer produces are out of date (the CRM or site changed).
        for row in self.db.execute("SELECT id, fingerprint FROM proposals WHERE state=?", (PENDING,)).fetchall():
            if row["fingerprint"] not in current:
                self.db.execute("UPDATE proposals SET state=?, result=? WHERE id=?",
                                (SUPERSEDED, json.dumps({"message": "No longer produced by the latest run", "at": stamp}),
                                 row["id"]))
                counts["superseded"] += 1
        self.db.commit()
        return counts

    def get(self, proposal_id: int) -> Optional[dict]:
        return self._row(self.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone())

    def list(self, state: Optional[str] = None) -> List[dict]:
        if state:
            rows = self.db.execute("SELECT * FROM proposals WHERE state=? ORDER BY action, title", (state,))
        else:
            rows = self.db.execute("SELECT * FROM proposals ORDER BY action, title")
        return [self._row(r) for r in rows.fetchall()]

    def counts(self) -> Dict[str, int]:
        return {r["state"]: r["n"] for r in
                self.db.execute("SELECT state, COUNT(*) AS n FROM proposals GROUP BY state").fetchall()}

    def history_for_target(self, target_key: str, exclude_id: int) -> List[dict]:
        rows = self.db.execute(
            "SELECT * FROM proposals WHERE target_key=? AND id<>? AND state IN ('rejected','applied','failed','resolved') "
            "ORDER BY decided_at", (target_key, exclude_id))
        return [self._row(r) for r in rows.fetchall()]

    def decide(self, proposal_id: int, state: str, note: str = "", chosen: str = "primary") -> None:
        """Record a reviewer decision. Only a pending proposal can be decided."""
        cursor = self.db.execute(
            "UPDATE proposals SET state=?, decided_at=?, decision_note=?, chosen=? WHERE id=? AND state=?",
            (state, now(), note, chosen, proposal_id, PENDING))
        self.db.commit()
        if cursor.rowcount != 1:
            raise ValueError(f"Proposal {proposal_id} is not pending; decision ignored")

    def set_state(self, proposal_id: int, state: str, result: Optional[dict] = None) -> None:
        if result is not None:
            result = dict(result, at=now())
            self.db.execute("UPDATE proposals SET state=?, result=? WHERE id=?", (state, json.dumps(result), proposal_id))
        else:
            self.db.execute("UPDATE proposals SET state=? WHERE id=?", (state, proposal_id))
        if state == APPLIED:
            self.db.execute("UPDATE proposals SET applied_at=? WHERE id=?", (now(), proposal_id))
        self.db.commit()

    def save_progress(self, proposal_id: int, **fields) -> None:
        """Persist partial progress (e.g. a new successor id) before the next API call."""
        row = self.get(proposal_id)
        result = dict(row.get("result") or {}, **fields)
        self.db.execute("UPDATE proposals SET result=? WHERE id=?", (json.dumps(result), proposal_id))
        self.db.commit()

    # ---- reviewer resolutions of ambiguous matches -------------------------
    def set_resolution(self, facility_slug: str, account_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO resolutions VALUES (?,?,?)", (facility_slug, account_id, now()))
        self.db.commit()

    def resolutions(self) -> Dict[str, str]:
        return {r["facility_slug"]: r["account_id"] for r in self.db.execute("SELECT * FROM resolutions").fetchall()}

    # ---- audit -------------------------------------------------------------
    def log_call(self, proposal_id, method, path, body, status, response) -> None:
        self.db.execute("INSERT INTO api_log (proposal_id, at, method, path, body, status, response) VALUES (?,?,?,?,?,?,?)",
                        (proposal_id, now(), method, path, json.dumps(body), status, response))
        self.db.commit()

    def api_log(self, proposal_id: Optional[int] = None) -> List[dict]:
        if proposal_id is None:
            rows = self.db.execute("SELECT * FROM api_log ORDER BY id DESC LIMIT 200")
        else:
            rows = self.db.execute("SELECT * FROM api_log WHERE proposal_id=? ORDER BY id", (proposal_id,))
        return [dict(r) for r in rows.fetchall()]

    def record_run(self, ok: bool, summary: dict) -> None:
        self.db.execute("INSERT INTO runs (at, ok, summary) VALUES (?,?,?)", (now(), int(ok), json.dumps(summary)))
        self.db.commit()

    def last_successful_run(self) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM runs WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row, summary=json.loads(row["summary"])) if row else None

    # ---- what the last successful run saw (read by the Scraper / Matcher pages) ----------
    def save_snapshot(self, facilities: List[dict], trace: List[dict]) -> None:
        self.db.execute("INSERT INTO snapshots (at, facilities, trace) VALUES (?,?,?)",
                        (now(), json.dumps(facilities), json.dumps(trace, default=str)))
        self.db.execute("DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT 10)")
        self.db.commit()

    def last_snapshot(self) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"at": row["at"], "facilities": json.loads(row["facilities"]), "trace": json.loads(row["trace"])}

    def last_run(self) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row, summary=json.loads(row["summary"])) if row else None
