"""Schema types shared across Action-side scripts.

Design ref: 2026-05-15-doc-audit-service-design.md §7.
Both the Action and the Server marshal these as plain JSON; this module
provides typed constructors + (de)serialization helpers, not validation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


SCHEMA_VERSION = "1"

Severity = str  # "block" | "warn" | "info"
Dimension = str  # "consistency" | "security" | "technical" | "architecture" | "style"
Source = str    # "rules" | "semantic" | "external"


@dataclass
class Location:
    file: str
    line_start: int
    line_end: int | None = None
    anchor: str | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"file": self.file, "line_start": self.line_start}
        if self.line_end is not None:
            d["line_end"] = self.line_end
        if self.anchor is not None:
            d["anchor"] = self.anchor
        return d


@dataclass
class History:
    historical_occurrences: int = 1
    first_seen_pr: int | None = None
    first_seen_at: str | None = None
    dedup_confidence: float | None = None
    dedup_method: str = "none"  # "hash" | "g_eval" | "none"

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Finding:
    rule_id: str
    source: Source
    dimension: Dimension
    severity: Severity
    title: str
    locations: list[Location]
    message: str = ""
    evidence: str = ""
    suggestion: str = ""
    confidence: float = 1.0
    external_provider: str | None = None
    related_findings: list[str] = field(default_factory=list)
    agent_meta: dict[str, Any] = field(default_factory=dict)
    history: History = field(default_factory=History)
    finding_id: str = ""

    def __post_init__(self) -> None:
        if not self.finding_id:
            self.finding_id = compute_finding_id(self)

    def to_json(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "source": self.source,
            "external_provider": self.external_provider,
            "dimension": self.dimension,
            "severity": self.severity,
            "title": self.title,
            "locations": [loc.to_json() for loc in self.locations],
            "evidence": self.evidence,
            "message": self.message,
            "suggestion": self.suggestion,
            "related_findings": list(self.related_findings),
            "confidence": self.confidence,
            "agent_meta": dict(self.agent_meta),
            "history": self.history.to_json(),
        }


def compute_finding_id(f: Finding) -> str:
    """Stable hash per design §7.5: rule_id + dimension + first location + msg hash."""
    first = f.locations[0] if f.locations else Location(file="<none>", line_start=0)
    payload = "|".join(
        [
            f.rule_id,
            f.dimension,
            first.file,
            str(first.line_start),
            hashlib.sha1(f.message.encode("utf-8")).hexdigest()[:12],
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def finding_from_json(raw: dict[str, Any]) -> Finding:
    locs = [
        Location(
            file=l["file"],
            line_start=l.get("line_start", l.get("line", 0)),
            line_end=l.get("line_end"),
            anchor=l.get("anchor"),
        )
        for l in raw.get("locations", [])
    ]
    hist_raw = raw.get("history", {}) or {}
    history = History(
        historical_occurrences=hist_raw.get("historical_occurrences", 1),
        first_seen_pr=hist_raw.get("first_seen_pr"),
        first_seen_at=hist_raw.get("first_seen_at"),
        dedup_confidence=hist_raw.get("dedup_confidence"),
        dedup_method=hist_raw.get("dedup_method", "none"),
    )
    return Finding(
        rule_id=raw["rule_id"],
        source=raw.get("source", "rules"),
        dimension=raw.get("dimension", "consistency"),
        severity=raw.get("severity", "warn"),
        title=raw.get("title", raw["rule_id"]),
        locations=locs,
        message=raw.get("message", ""),
        evidence=raw.get("evidence", ""),
        suggestion=raw.get("suggestion", ""),
        confidence=raw.get("confidence", 1.0),
        external_provider=raw.get("external_provider"),
        related_findings=list(raw.get("related_findings", [])),
        agent_meta=dict(raw.get("agent_meta", {})),
        history=history,
        finding_id=raw.get("finding_id", ""),
    )


def dump_findings(findings: Iterable[Finding], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fp:
        json.dump([f.to_json() for f in findings], fp, ensure_ascii=False, indent=2)


def load_findings(path: str) -> list[Finding]:
    with open(path, encoding="utf-8") as fp:
        return [finding_from_json(r) for r in json.load(fp)]
