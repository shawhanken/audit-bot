from __future__ import annotations

import asyncio
import json

import pytest

from app import claude_client, orchestrator
from app.schema import (
    AuditRequest,
    DimensionConfig,
    Document,
    PR,
    Payload,
    Repo,
    Target,
)


def _req(dims: dict[str, bool]) -> AuditRequest:
    return AuditRequest(
        request_id="r1",
        repo=Repo(owner="o", name="n"),
        pr=PR(number=1, base_sha="b", head_sha="h"),
        target=Target(
            name="cips",
            paths=["docs/**"],
            dimensions={
                name: DimensionConfig(enabled=enabled, severity_gate="warn")
                for name, enabled in dims.items()
            },
        ),
        payload=Payload(
            index_head={
                "code_symbols_referenced": [],
            },
            diff={"by_kind": {}},
            documents={"docs/x.md": Document(content="hi")},
        ),
    )


def test_orchestrate_runs_only_enabled_dims(monkeypatch):
    def fake(call):
        return 0, json.dumps([{
            "rule_id": "X",
            "dimension": "consistency",
            "severity": "warn",
            "title": "t",
            "locations": [{"file": "docs/x.md", "line_start": 1}],
            "message": "m",
        }]), ""
    monkeypatch.setattr(claude_client, "_runner", fake)
    req = _req({"consistency": True, "security": False})
    resp = asyncio.run(orchestrator.orchestrate(req))
    assert "consistency" in resp.findings_by_dimension
    assert "security" not in resp.findings_by_dimension
    assert resp.findings_by_dimension["consistency"][0].rule_id == "X"


def test_orchestrate_marks_dim_degraded_when_subprocess_fails(monkeypatch):
    def fake(call):
        return 2, "", "boom"
    monkeypatch.setattr(claude_client, "_runner", fake)
    req = _req({"consistency": True})
    resp = asyncio.run(orchestrator.orchestrate(req))
    assert resp.dimension_status["consistency"].status == "degraded"


def test_orchestrate_independent_dim_failure_does_not_block_other(monkeypatch):
    def fake(call):
        # Key on the prompt_intro phrase (unique per agent) so we don't
        # accidentally match the dimension list inside the shared output
        # instructions block.
        if "security dimension of a doc-audit bot" in call.prompt.lower():
            return 1, "", ""
        return 0, json.dumps([]), ""
    monkeypatch.setattr(claude_client, "_runner", fake)
    req = _req({"consistency": True, "security": True})
    resp = asyncio.run(orchestrator.orchestrate(req))
    assert resp.dimension_status["consistency"].status == "ok"
    assert resp.dimension_status["security"].status == "degraded"
    assert resp.status == "partial"


def test_orchestrate_no_enabled_dims_returns_ok():
    req = _req({"consistency": False, "security": False})
    resp = asyncio.run(orchestrator.orchestrate(req))
    assert resp.status == "ok"
    assert resp.findings_by_dimension == {}
