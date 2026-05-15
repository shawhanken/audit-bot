"""Pydantic models for the audit server.

Design ref: §7.3, §7.4, §7.5. Mirrors the Action-side dataclasses but uses
Pydantic so FastAPI can validate request bodies at the boundary.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Severity = Literal["block", "warn", "info"]
DimensionName = Literal[
    "consistency", "security", "technical", "architecture", "style"
]
SourceKind = Literal["rules", "semantic", "external"]


class Repo(BaseModel):
    owner: str
    name: str
    default_branch: str = "main"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class PR(BaseModel):
    number: int
    title: str = ""
    base_sha: str = ""
    head_sha: str = ""
    is_draft: bool = False
    is_fork: bool = False


class DimensionConfig(BaseModel):
    enabled: bool = False
    severity_gate: Literal["block", "warn", "off"] = "warn"


class Target(BaseModel):
    name: str
    paths: list[str] = Field(default_factory=list)
    dimensions: dict[str, DimensionConfig] = Field(default_factory=dict)


class Document(BaseModel):
    content: str
    sha: str = ""


class Payload(BaseModel):
    index_head: dict[str, Any]
    diff: dict[str, Any] = Field(default_factory=dict)
    changed_files: list[str] = Field(default_factory=list)
    rules_findings: list[dict[str, Any]] = Field(default_factory=list)
    documents: dict[str, Document] = Field(default_factory=dict)
    related_code_excerpts: dict[str, str] = Field(default_factory=dict)
    # Names of server-side repo mirrors to consult during this audit. The
    # action declares these in its config but doesn't upload their contents
    # — the server reads them from its local mirror root. See app/mirror.py
    # for the security model.
    related_code_mirrors: list[str] = Field(default_factory=list)


class BudgetHint(BaseModel):
    max_usd: float = 2.00
    max_duration_seconds: int = 240


class AuditRequest(BaseModel):
    schema_version: str = "1"
    request_id: str
    repo: Repo
    pr: PR
    target: Target
    payload: Payload
    budget_hint: BudgetHint = Field(default_factory=BudgetHint)
    client_version: str = ""


class Location(BaseModel):
    file: str
    line_start: int
    line_end: int | None = None
    anchor: str | None = None


class History(BaseModel):
    historical_occurrences: int = 1
    first_seen_pr: int | None = None
    first_seen_at: str | None = None
    dedup_confidence: float | None = None
    dedup_method: Literal["g_eval", "hash", "none"] = "none"


class Finding(BaseModel):
    finding_id: str = ""
    rule_id: str
    source: SourceKind = "semantic"
    external_provider: str | None = None
    dimension: str
    severity: Severity = "warn"
    title: str
    locations: list[Location] = Field(default_factory=list)
    evidence: str = ""
    message: str = ""
    suggestion: str = ""
    related_findings: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    agent_meta: dict[str, Any] = Field(default_factory=dict)
    history: History = Field(default_factory=History)


class DimensionStatus(BaseModel):
    status: Literal["ok", "degraded", "skipped"] = "ok"
    duration_ms: int = 0
    cost_usd: float = 0.0
    reason: str | None = None


class Totals(BaseModel):
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cached_read: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


class AuditResponse(BaseModel):
    schema_version: str = "1"
    request_id: str
    status: Literal["ok", "partial", "degraded", "failed"] = "ok"
    findings_by_dimension: dict[str, list[Finding]] = Field(default_factory=dict)
    dimension_status: dict[str, DimensionStatus] = Field(default_factory=dict)
    totals: Totals = Field(default_factory=Totals)
    remaining_budget_usd: float | None = None
    view_url: str | None = None
