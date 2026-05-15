"""Load and validate .github/doc-audit.yml.

Design ref: §4.2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


VALID_DIMENSIONS = (
    "consistency",
    "security",
    "technical",
    "architecture",
    "style",
)
VALID_GATES = ("block", "warn", "off")


@dataclass
class DimensionConfig:
    enabled: bool = False
    severity_gate: str = "warn"


@dataclass
class TargetConfig:
    name: str
    paths: list[str]
    glossary: str | None = None
    related_code: list[str] = field(default_factory=list)
    # Names of server-side repo mirrors to consult (e.g. ["node", "runner",
    # "cbfs"]). Server resolves these against its local mirror root; the
    # action just forwards the names. Empty list = excerpts-only (legacy).
    related_code_mirrors: list[str] = field(default_factory=list)
    dimensions: dict[str, DimensionConfig] = field(default_factory=dict)


@dataclass
class GlobalConfig:
    max_usd_per_run: float = 2.00
    comment_marker: str = "doc-audit-bot"
    ignore_file: str = ".doc-audit-ignore"


@dataclass
class AuditConfig:
    targets: list[TargetConfig]
    global_: GlobalConfig

    def target_by_name(self, name: str) -> TargetConfig | None:
        return next((t for t in self.targets if t.name == name), None)


def _parse_dimension(raw: dict[str, Any]) -> DimensionConfig:
    enabled = bool(raw.get("enabled", False))
    gate = raw.get("severity_gate", "warn")
    if gate not in VALID_GATES:
        raise ValueError(f"invalid severity_gate {gate!r}")
    if gate == "off":
        enabled = False
    return DimensionConfig(enabled=enabled, severity_gate=gate)


def parse_config(raw: dict[str, Any]) -> AuditConfig:
    targets_raw = raw.get("targets", []) or []
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ValueError("config.targets must be a non-empty list")
    targets: list[TargetConfig] = []
    for t in targets_raw:
        name = t.get("name")
        if not name:
            raise ValueError("target.name is required")
        dims: dict[str, DimensionConfig] = {}
        for dim_name, dim_raw in (t.get("dimensions") or {}).items():
            if dim_name not in VALID_DIMENSIONS:
                raise ValueError(f"unknown dimension {dim_name!r}")
            dims[dim_name] = _parse_dimension(dim_raw or {})
        targets.append(
            TargetConfig(
                name=name,
                paths=list(t.get("paths") or []),
                glossary=t.get("glossary"),
                related_code=list(t.get("related_code") or []),
                related_code_mirrors=list(t.get("related_code_mirrors") or []),
                dimensions=dims,
            )
        )
    g_raw = raw.get("global", {}) or {}
    g = GlobalConfig(
        max_usd_per_run=float(g_raw.get("max_usd_per_run", 2.00)),
        comment_marker=g_raw.get("comment_marker", "doc-audit-bot"),
        ignore_file=g_raw.get("ignore_file", ".doc-audit-ignore"),
    )
    return AuditConfig(targets=targets, global_=g)


def load_config(path: str) -> AuditConfig:
    if not os.path.exists(path):
        raise FileNotFoundError(f"audit config not found: {path}")
    with open(path, encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}
    return parse_config(raw)
