"""Cross-PR semantic dedup of findings.

Design ref: §6.2 Dedup Agent. The full design uses Haiku + log-prob
confidence; for the skeleton we do hash-based dedup and stub the semantic
side behind a feature flag. When real LLM is wired in, only this module
needs to grow.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

from .schema import Finding, History
from .storage import HistoryStore


def _signature(f: Finding) -> str:
    loc = f.locations[0] if f.locations else None
    payload = "|".join([
        f.rule_id,
        f.dimension,
        loc.file if loc else "<none>",
        str(loc.line_start if loc else 0),
        hashlib.sha1(f.message.encode("utf-8")).hexdigest()[:12],
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def annotate_history(findings: Iterable[Finding], *, repo: str, store: HistoryStore) -> list[Finding]:
    out: list[Finding] = []
    for f in findings:
        sig = _signature(f)
        prior = store.lookup(repo, sig)
        if prior is None:
            f.history = History(
                historical_occurrences=1,
                first_seen_pr=prior.first_seen_pr if prior else None,
                first_seen_at=prior.first_seen_at if prior else None,
                dedup_method="hash",
            )
            store.record(repo=repo, signature=sig, finding=f)
        else:
            f.history = History(
                historical_occurrences=prior.occurrences + 1,
                first_seen_pr=prior.first_seen_pr,
                first_seen_at=prior.first_seen_at,
                dedup_method="hash",
            )
            store.bump(repo=repo, signature=sig)
        out.append(f)
    return out
