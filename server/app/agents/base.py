"""Dimension agent base + protocol.

Design ref: §6.2 Dimension Agents. Each agent runs `claude -p` (per user
choice) with a dimension-specific prompt. Agents must:

  1. Output strict §7.5 finding schema (we validate after).
  2. Include verifiable `locations[]`.
  3. Not duplicate rules-pass findings.
  4. Only report findings within PR change scope.

The base class handles prompt assembly, subprocess invocation, JSON
validation, and degradation. Subclasses override `dimension`, `prompt_intro`,
and (optionally) `prepare_inputs` if they need to inject extra context.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import claude_client
from ..schema import (
    AuditRequest,
    DimensionStatus,
    Finding,
    Location,
)


log = logging.getLogger(__name__)


OUTPUT_INSTRUCTIONS = """=== output instructions ===
Return ONE JSON array. No prose before or after. No markdown fence. No explanation.
Each element must be an object with EXACTLY these fields:

  {
    "rule_id":   "<short-stable-id, e.g. C_default_drift>",
    "dimension": "<one of: consistency|security|technical|architecture|style>",
    "severity":  "block" | "warn" | "info",
    "title":     "<<=80 chars>",
    "locations": [{"file": "<exact path from documents_in_scope>", "line_start": <1-based int>}],
    "message":   "<one or two sentences: what is wrong and where>",
    "suggestion":"<one sentence: how to fix>"
  }

Hard rules — if you cannot satisfy one, OMIT the finding entirely:
  1. locations[].file MUST be a key listed in documents_in_scope.
  2. locations[].line_start MUST be a real line number in that file.
  3. Do NOT report anything already in rules_findings_already_reported (match by topic, not literal text).
  4. Do NOT report findings outside the changed_files set unless explaining a downstream impact.
  5. If you have nothing to report, return [].
"""


@dataclass
class AgentRunResult:
    findings: list[Finding] = field(default_factory=list)
    status: DimensionStatus = field(default_factory=DimensionStatus)


class DimensionAgent:
    dimension: str = ""
    prompt_intro: str = ""

    # Subclasses can lower the timeout budget.
    timeout_s: int = 180

    def build_prompt(self, req: AuditRequest, extra_context: dict[str, Any]) -> str:
        body = {
            "target": req.target.name,
            "changed_files": req.payload.changed_files,
            "diff_summary": _diff_summary(req.payload.diff),
            "rules_findings_already_reported": req.payload.rules_findings,
            "documents_in_scope": list(req.payload.documents.keys()),
            **extra_context,
        }
        # We embed the documents themselves as a separate fenced section so the
        # model isn't tempted to evaluate them as JSON instructions.
        docs_block = "\n".join(
            f"--- file: {p}\n{d.content[:20_000]}\n"
            for p, d in list(req.payload.documents.items())[:50]
        )
        return (
            f"{self.prompt_intro}\n\n"
            f"=== task context (JSON) ===\n{json.dumps(body, ensure_ascii=False)}\n\n"
            f"=== document corpus (truncated) ===\n{docs_block}\n\n"
            f"{OUTPUT_INSTRUCTIONS}"
        )

    def prepare_inputs(self, req: AuditRequest) -> dict[str, Any]:
        """Subclasses may override to inject dimension-specific context."""
        return {}

    def run(self, req: AuditRequest) -> AgentRunResult:
        if self.dimension not in req.target.dimensions or not req.target.dimensions[self.dimension].enabled:
            return AgentRunResult(
                status=DimensionStatus(status="skipped", reason="dimension_disabled")
            )

        prompt = self.build_prompt(req, self.prepare_inputs(req))
        call = claude_client.ClaudeCall(
            prompt=prompt,
            timeout_s=self.timeout_s,
        )
        res = claude_client.invoke(call)
        if not res.ok:
            return AgentRunResult(
                status=DimensionStatus(
                    status="degraded",
                    duration_ms=res.duration_ms,
                    reason=res.error or "unknown",
                )
            )
        raw = res.parsed
        if isinstance(raw, dict):
            raw = raw.get("findings", []) or raw.get("result", [])
        if not isinstance(raw, list):
            return AgentRunResult(
                status=DimensionStatus(
                    status="degraded",
                    duration_ms=res.duration_ms,
                    reason="output_not_array",
                )
            )
        findings = list(_coerce_findings(raw, dimension=self.dimension))
        return AgentRunResult(
            findings=findings,
            status=DimensionStatus(
                status="ok",
                duration_ms=res.duration_ms,
            ),
        )


def _diff_summary(diff: dict[str, Any]) -> dict[str, Any]:
    by_kind = diff.get("by_kind", {}) or {}
    return {
        kind: {
            "added": len(v.get("added", []) or []),
            "removed": len(v.get("removed", []) or []),
            "modified": len(v.get("modified", []) or []),
        }
        for kind, v in by_kind.items()
    }


def _coerce_findings(raw: list[dict[str, Any]], *, dimension: str) -> Iterable[Finding]:
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            locs_raw = item.get("locations") or []
            locs = []
            for l in locs_raw:
                if not isinstance(l, dict):
                    continue
                line_start = l.get("line_start", l.get("line", 0))
                locs.append(Location(
                    file=l.get("file", ""),
                    line_start=int(line_start) if line_start else 0,
                    line_end=l.get("line_end"),
                    anchor=l.get("anchor"),
                ))
            yield Finding(
                rule_id=item.get("rule_id") or f"X_{dimension}_unspecified",
                source="semantic",
                dimension=item.get("dimension") or dimension,
                severity=item.get("severity", "warn"),
                title=item.get("title", item.get("rule_id", "untitled")),
                locations=locs,
                evidence=item.get("evidence", ""),
                message=item.get("message", ""),
                suggestion=item.get("suggestion", ""),
                confidence=float(item.get("confidence", 0.8)),
                agent_meta=item.get("agent_meta") or {},
            )
        except Exception as e:  # noqa: BLE001 - design §9: drop malformed, keep going
            log.warning("dropping malformed finding: %s", e)
            continue
