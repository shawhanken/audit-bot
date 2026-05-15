"""SQLite-backed audit history + finding dedup ledger.

Design ref: §6.2 Audit History. The full design uses Postgres for
cross-instance state; SQLite is plenty for the skeleton and the schema is
near-identical, so the migration is mechanical.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


@dataclass
class HistoryEntry:
    signature: str
    occurrences: int
    first_seen_pr: int | None
    first_seen_at: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS finding_history (
    repo TEXT NOT NULL,
    signature TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_seen_pr INTEGER,
    first_seen_at TEXT,
    last_seen_at TEXT,
    sample_json TEXT,
    PRIMARY KEY (repo, signature)
);

CREATE TABLE IF NOT EXISTS audit_run (
    request_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pr_number INTEGER,
    target TEXT,
    created_at TEXT,
    status TEXT,
    response_json TEXT
);
"""


class HistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def lookup(self, repo: str, signature: str) -> HistoryEntry | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT signature, occurrences, first_seen_pr, first_seen_at "
                "FROM finding_history WHERE repo=? AND signature=?",
                (repo, signature),
            ).fetchone()
        if row is None:
            return None
        return HistoryEntry(
            signature=row["signature"],
            occurrences=row["occurrences"],
            first_seen_pr=row["first_seen_pr"],
            first_seen_at=row["first_seen_at"],
        )

    def record(self, *, repo: str, signature: str, finding: Any, pr_number: int | None = None) -> None:
        now = _now_iso()
        sample = json.dumps(getattr(finding, "model_dump", lambda: {})(), ensure_ascii=False)
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO finding_history "
                "(repo, signature, occurrences, first_seen_pr, first_seen_at, last_seen_at, sample_json) "
                "VALUES (?, ?, 1, ?, ?, ?, ?)",
                (repo, signature, pr_number, now, now, sample),
            )

    def bump(self, *, repo: str, signature: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE finding_history "
                "SET occurrences = occurrences + 1, last_seen_at = ? "
                "WHERE repo=? AND signature=?",
                (_now_iso(), repo, signature),
            )

    def save_audit_run(self, *, request_id: str, repo: str, pr_number: int, target: str, response: Any) -> None:
        body = response.model_dump_json() if hasattr(response, "model_dump_json") else json.dumps(response)
        status = getattr(response, "status", "unknown")
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO audit_run "
                "(request_id, repo, pr_number, target, created_at, status, response_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (request_id, repo, pr_number, target, _now_iso(), status, body),
            )

    def get_audit_run(self, request_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT request_id, repo, pr_number, target, created_at, status, response_json "
                "FROM audit_run WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "repo": row["repo"],
            "pr_number": row["pr_number"],
            "target": row["target"],
            "created_at": row["created_at"],
            "status": row["status"],
            "response": json.loads(row["response_json"]) if row["response_json"] else None,
        }


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
