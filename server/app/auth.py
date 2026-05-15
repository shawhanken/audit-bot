"""Token-to-repo binding.

Design §6.3 specifies tokens issued via a console and bound to a specific
repository. We implement that binding here with a SQLite-backed token
table; the `DOC_AUDIT_SERVER_TOKEN` env var continues to work as a global
override (useful for local dev and the test suite).

Token storage uses sha256 hashes — plain tokens never hit disk.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_token (
    token_sha256 TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_repo ON auth_token(repo);
"""


@dataclass
class TokenBinding:
    repo: str
    label: str | None


class TokenStore:
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

    def issue(self, *, repo: str, label: str | None = None) -> str:
        """Mint a new token bound to `repo`. Returns the plaintext token; only
        its sha256 is stored. The caller must hand the plaintext to the user
        immediately — there is no recovery later."""
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._conn() as c:
            c.execute(
                "INSERT INTO auth_token (token_sha256, repo, label) VALUES (?, ?, ?)",
                (digest, repo, label),
            )
        return token

    def lookup(self, token: str) -> TokenBinding | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._conn() as c:
            row = c.execute(
                "SELECT repo, label FROM auth_token "
                "WHERE token_sha256=? AND revoked_at IS NULL",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        return TokenBinding(repo=row["repo"], label=row["label"])

    def revoke(self, token: str) -> bool:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE auth_token "
                "SET revoked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                "WHERE token_sha256=? AND revoked_at IS NULL",
                (digest,),
            )
            return cur.rowcount > 0
