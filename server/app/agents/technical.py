"""Technical-feasibility agent (stage B of the two-stage approach).

Stage A — exact code-index lookup — runs before this agent (see
`code_index.py`). The agent only handles the residue: API return shape
semantics, behaviour claims vs code logic, ambiguous technical statements.
"""

from __future__ import annotations

from typing import Any

from ..code_index import CodeIndex, lookup_symbols
from ..schema import AuditRequest, DimensionStatus, Finding, Location
from .base import AgentRunResult, DimensionAgent, _coerce_findings  # noqa: F401 (re-export safety)


class TechnicalAgent(DimensionAgent):
    dimension = "technical"
    prompt_intro = """You are the technical-feasibility dimension of a doc-audit bot.

Stage A (exact symbol-index lookup) has ALREADY produced findings for:
  * symbols the doc references that don't exist in the code (T001)
  * constant values where doc and code disagree (T002)
  * paths the doc claims exist but don't (T003)
  * function signatures the doc misstates (T004)
These appear in stage_a_findings — DO NOT repeat them.

What to look for (require semantic judgement that stage A can't make):
  * API return shape: the doc says "returns a list of X", but the code returns
    a dict / a tuple / Option<...>.
  * Behavioural claims: doc says "this function retries on failure", but the
    code has no retry logic.
  * Ordering / atomicity claims contradicted by code structure.
  * Performance / complexity claims that the code obviously violates
    (e.g. "O(1)" on a function with a nested loop over user input).

Hard requirements:
  * Each finding MUST cite at least one entry from `code_index_hits` to ground
    the claim. Put the citation in `evidence`.
  * If the document is genuinely vague ("fast", "efficient", "handles errors
    gracefully") without making a checkable claim — do NOT report it.
  * No findings about code style or organization. This is feasibility only."""

    def prepare_inputs(self, req: AuditRequest) -> dict[str, Any]:
        # Build code index from BOTH the action-uploaded excerpts AND any
        # server-side mirrors the consumer config declared. Without mirrors
        # this falls back to excerpts-only (the original behaviour).
        from ..mirror import default_mirror
        mirror_files: list = []
        wanted = list(req.payload.related_code_mirrors or [])
        if wanted:
            mirror = default_mirror(wanted)
            for name in wanted:
                if mirror.has(name):
                    mirror_files.extend(mirror.iter_files(name))
        idx = CodeIndex(req.payload.related_code_excerpts, mirror_files=mirror_files)
        symbols = [
            s["symbol"]
            for s in (req.payload.index_head.get("code_symbols_referenced") or [])
            if isinstance(s, dict) and "symbol" in s
        ]
        hits = lookup_symbols(idx, symbols)
        return {
            "stage_a_findings": _stage_a_findings(req.payload.index_head, hits),
            "code_index_hits": hits,
            "mirrors_consulted": [m for m in wanted if any(f.mirror == m for f in mirror_files)],
        }

    def run(self, req: AuditRequest) -> AgentRunResult:
        """Two-stage path: stage A (deterministic code-index lookup) ALWAYS
        emits findings; stage B (LLM) runs on top and gets stage A as context
        so it doesn't duplicate.

        If the dimension is disabled, both stages skip. If the LLM degrades,
        stage A findings still surface — they're the high-confidence signal
        and shouldn't be lost.
        """
        if self.dimension not in req.target.dimensions or not req.target.dimensions[self.dimension].enabled:
            return AgentRunResult(
                status=DimensionStatus(status="skipped", reason="dimension_disabled")
            )

        inputs = self.prepare_inputs(req)
        stage_a = list(_materialize_stage_a(inputs["stage_a_findings"]))

        # Stage B: standard LLM call via the base-class machinery.
        prompt = self.build_prompt(req, inputs)
        from .. import claude_client
        call = claude_client.ClaudeCall(prompt=prompt, timeout_s=self.timeout_s)
        res = claude_client.invoke(call)
        if not res.ok:
            # Stage A is still valuable on its own.
            return AgentRunResult(
                findings=stage_a,
                status=DimensionStatus(
                    status="degraded" if stage_a else "degraded",
                    duration_ms=res.duration_ms,
                    reason=res.error or "unknown",
                ),
            )
        raw = res.parsed
        if isinstance(raw, dict):
            raw = raw.get("findings", []) or raw.get("result", [])
        if not isinstance(raw, list):
            return AgentRunResult(
                findings=stage_a,
                status=DimensionStatus(
                    status="degraded",
                    duration_ms=res.duration_ms,
                    reason="output_not_array",
                ),
            )
        stage_b = list(_coerce_findings(raw, dimension=self.dimension))
        return AgentRunResult(
            findings=stage_a + stage_b,
            status=DimensionStatus(status="ok", duration_ms=res.duration_ms),
        )


def _materialize_stage_a(raw_findings: list[dict[str, Any]]):
    for f in raw_findings:
        locs = [
            Location(
                file=l.get("file", ""),
                line_start=int(l.get("line_start", l.get("line", 0)) or 0),
            )
            for l in (f.get("locations") or [])
        ]
        yield Finding(
            rule_id=f.get("rule_id", "T0xx_stage_a"),
            source="rules",  # stage A is deterministic — not "semantic"
            dimension="technical",
            severity=f.get("severity", "warn"),
            title=f.get("title", f.get("rule_id", "stage A")),
            locations=locs,
            message=f.get("message", ""),
            suggestion=f.get("suggestion", ""),
            confidence=1.0,
            agent_meta={"stage": "A"},
        )


def _stage_a_findings(head_index: dict[str, Any], hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize T001/T003 findings deterministically from the index hits."""
    out: list[dict[str, Any]] = []
    refs = head_index.get("code_symbols_referenced") or []
    hit_by_symbol = {h["symbol"]: h for h in hits if h.get("found")}
    for r in refs:
        sym = r.get("symbol")
        if not sym:
            continue
        if sym not in hit_by_symbol and r.get("kind") != "path":
            out.append({
                "rule_id": "T001_symbol_not_found",
                "dimension": "technical",
                "severity": "block",
                "title": f"代码符号 `{sym}` 在 related_code 中找不到",
                "locations": [{"file": r.get("file", ""), "line_start": r.get("line", 0)}],
                "message": f"文档引用 `{sym}`，但 related_code 中找不到匹配。",
                "suggestion": "确认 related_code 配置是否覆盖该符号所在目录；或更正文档中的符号名。",
            })
        elif sym not in hit_by_symbol and r.get("kind") == "path":
            out.append({
                "rule_id": "T003_path_not_found",
                "dimension": "technical",
                "severity": "warn",
                "title": f"路径 `{sym}` 不存在",
                "locations": [{"file": r.get("file", ""), "line_start": r.get("line", 0)}],
                "message": f"文档引用路径 `{sym}`，但 related_code 中无此文件。",
                "suggestion": "确认路径是否被删除/重命名。",
            })
    return out
