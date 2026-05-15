"""Stage A of the technical dimension must surface as real findings,
independent of the LLM stage B succeeding."""

from __future__ import annotations

import json

from app import claude_client
from app.agents.technical import TechnicalAgent
from app.schema import (
    AuditRequest,
    DimensionConfig,
    Document,
    PR,
    Payload,
    Repo,
    Target,
)


def _req() -> AuditRequest:
    return AuditRequest(
        request_id="r",
        repo=Repo(owner="o", name="n"),
        pr=PR(number=1, base_sha="b", head_sha="h"),
        target=Target(
            name="cips",
            paths=["docs/**"],
            dimensions={"technical": DimensionConfig(enabled=True, severity_gate="warn")},
        ),
        payload=Payload(
            index_head={
                "code_symbols_referenced": [
                    {"symbol": "ghost_fn", "file": "docs/cip-1.md", "line": 3},
                ],
            },
            diff={"by_kind": {}},
            changed_files=["docs/cip-1.md"],
            documents={"docs/cip-1.md": Document(content="a\nb\nc\n")},
            related_code_excerpts={"node/lib.rs": "fn unrelated() {}\n"},
        ),
    )


def test_stage_a_findings_surface_even_when_llm_degrades(monkeypatch):
    """LLM call fails — stage A T001 finding should still appear in the result."""
    monkeypatch.setattr(claude_client, "_runner", lambda c: (2, "", "fail"))
    agent = TechnicalAgent()
    res = agent.run(_req())
    rule_ids = [f.rule_id for f in res.findings]
    assert "T001_symbol_not_found" in rule_ids
    # Status remains degraded because the LLM half failed; stage A counts
    # the deterministic side.
    assert res.status.status == "degraded"


def test_stage_a_and_stage_b_findings_merge_on_success(monkeypatch):
    """When the LLM also returns findings, both surface."""
    def fake(call):
        return 0, json.dumps([
            {
                "rule_id": "T_behaviour_drift",
                "dimension": "technical",
                "severity": "warn",
                "title": "doc claims retry, code doesn't",
                "locations": [{"file": "docs/cip-1.md", "line_start": 2}],
                "message": "m",
                "suggestion": "s",
            }
        ]), ""
    monkeypatch.setattr(claude_client, "_runner", fake)
    res = TechnicalAgent().run(_req())
    rule_ids = [f.rule_id for f in res.findings]
    assert "T001_symbol_not_found" in rule_ids
    assert "T_behaviour_drift" in rule_ids
    assert res.status.status == "ok"


def test_stage_a_finding_is_marked_as_rules_source():
    """Stage A is deterministic; its source must be `rules`, not `semantic`."""
    agent = TechnicalAgent()
    # Skip LLM by giving an empty index — base class won't call the LLM though,
    # so we monkey-patch _runner to make it explicit.
    from unittest import mock
    with mock.patch.object(claude_client, "_runner", return_value=(0, "[]", "")):
        res = agent.run(_req())
    stage_a = [f for f in res.findings if f.rule_id == "T001_symbol_not_found"]
    assert stage_a and stage_a[0].source == "rules"
