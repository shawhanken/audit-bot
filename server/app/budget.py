"""Per-repo monthly budget ledger (SQLite-backed).

Design ref: §6.2 Budget Ledger. Soft cap (80%) triggers a notification (we
stub the notifier), hard cap (100%) makes the server return 503 for new
requests.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    request_id TEXT NOT NULL,
    pr_number INTEGER,
    timestamp TEXT NOT NULL,
    tokens_input INTEGER DEFAULT 0,
    tokens_output INTEGER DEFAULT 0,
    tokens_cached_read INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    duration_ms INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_budget_repo_ts ON budget_entry(repo, timestamp);

CREATE TABLE IF NOT EXISTS budget_config (
    repo TEXT PRIMARY KEY,
    monthly_cap_usd REAL NOT NULL DEFAULT 50.0
);
"""


@dataclass
class BudgetState:
    repo: str
    spent_usd: float
    cap_usd: float
    remaining_usd: float
    soft_breach: bool
    hard_breach: bool


class BudgetLedger:
    def __init__(self, db_path: str, default_monthly_cap_usd: float = 50.0):
        self.db_path = db_path
        self.default_cap = default_monthly_cap_usd
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

    def record(
        self,
        *,
        repo: str,
        request_id: str,
        pr_number: int,
        tokens_input: int,
        tokens_output: int,
        tokens_cached_read: int,
        cost_usd: float,
        duration_ms: int,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO budget_entry "
                "(repo, request_id, pr_number, timestamp, tokens_input, tokens_output, "
                "tokens_cached_read, cost_usd, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo, request_id, pr_number, _now_iso(),
                    tokens_input, tokens_output, tokens_cached_read,
                    cost_usd, duration_ms,
                ),
            )

    def state(self, repo: str) -> BudgetState:
        month_prefix = time.strftime("%Y-%m", time.gmtime())
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total "
                "FROM budget_entry "
                "WHERE repo=? AND timestamp LIKE ?",
                (repo, f"{month_prefix}%"),
            ).fetchone()
            spent = float(row["total"] or 0)
            cap_row = c.execute(
                "SELECT monthly_cap_usd FROM budget_config WHERE repo=?",
                (repo,),
            ).fetchone()
        cap = float(cap_row["monthly_cap_usd"]) if cap_row else self.default_cap
        remaining = max(cap - spent, 0.0)
        return BudgetState(
            repo=repo,
            spent_usd=spent,
            cap_usd=cap,
            remaining_usd=remaining,
            soft_breach=spent >= cap * 0.8,
            hard_breach=spent >= cap,
        )

    def set_cap(self, repo: str, cap_usd: float) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO budget_config (repo, monthly_cap_usd) VALUES (?, ?) "
                "ON CONFLICT(repo) DO UPDATE SET monthly_cap_usd=excluded.monthly_cap_usd",
                (repo, cap_usd),
            )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
