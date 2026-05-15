"""Budget-breach notifier hook.

Design §6.2 specifies: soft cap (80%) → email/Slack notification, hard cap
(100%) → 503. The hook lives here so real backends (SES, Slack webhook,
PagerDuty) can be wired in without touching the endpoint logic. For v1 we
ship a logging notifier and a once-per-repo-per-month dedup so we don't
spam.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol


log = logging.getLogger(__name__)


@dataclass
class BreachEvent:
    repo: str
    spent_usd: float
    cap_usd: float
    kind: str  # "soft" | "hard"


class Notifier(Protocol):
    def notify(self, event: BreachEvent) -> None: ...


class LoggingNotifier:
    """Default notifier: structured log line, deduped per (repo, kind, month).

    Real deployments swap this for a Slack / SES / PagerDuty implementation;
    the orchestrator only knows the Protocol.
    """

    def __init__(self) -> None:
        self._sent: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    def notify(self, event: BreachEvent) -> None:
        month = time.strftime("%Y-%m", time.gmtime())
        key = (event.repo, event.kind, month)
        with self._lock:
            if key in self._sent:
                return
            self._sent.add(key)
        log.warning(
            "budget_breach %s",
            json.dumps({
                "repo": event.repo,
                "kind": event.kind,
                "spent_usd": event.spent_usd,
                "cap_usd": event.cap_usd,
                "month": month,
            }),
        )

    # Test helper: clear the dedup memory.
    def reset(self) -> None:
        with self._lock:
            self._sent.clear()
