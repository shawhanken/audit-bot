"""Technical agent should consult server-side mirrors when configured."""

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


def _setup_mirror(tmp_path, monkeypatch):
    """Build a fake ~/workspace/node + redirect the mirror root."""
    workspace = tmp_path / "ws"
    (workspace / "node" / "src").mkdir(parents=True)
    (workspace / "node" / "src" / "lib.rs").write_text(
        "pub fn ghost_fn() {}\npub fn other() {}\n"
    )
    monkeypatch.setenv("DOC_AUDIT_MIRROR_ROOT", str(workspace))


def _req(mirrors: list[str]) -> AuditRequest:
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
            related_code_excerpts={},
            related_code_mirrors=mirrors,
        ),
    )


def test_mirror_supplies_symbol_so_stage_a_does_not_fire(monkeypatch, tmp_path):
    """Without mirrors, ghost_fn is missing → T001. With mirror=node defining
    it, stage A finds the symbol and emits no T001."""
    _setup_mirror(tmp_path, monkeypatch)

    # Case A: no mirrors → ghost_fn missing → T001 emitted by stage A.
    agent = TechnicalAgent()
    inputs = agent.prepare_inputs(_req(mirrors=[]))
    rule_ids = [f["rule_id"] for f in inputs["stage_a_findings"]]
    assert "T001_symbol_not_found" in rule_ids

    # Case B: mirror node provides ghost_fn → no T001.
    inputs = agent.prepare_inputs(_req(mirrors=["node"]))
    rule_ids = [f["rule_id"] for f in inputs["stage_a_findings"]]
    assert "T001_symbol_not_found" not in rule_ids
    assert "node" in inputs["mirrors_consulted"]


def test_unknown_mirror_silently_skipped(monkeypatch, tmp_path):
    _setup_mirror(tmp_path, monkeypatch)
    agent = TechnicalAgent()
    inputs = agent.prepare_inputs(_req(mirrors=["doesnotexist"]))
    # Should not crash; "mirrors_consulted" simply doesn't include it.
    assert inputs["mirrors_consulted"] == []


def test_full_run_with_mirror(monkeypatch, tmp_path):
    """End-to-end: stage A clean (mirror provides symbol), stage B (LLM)
    returns empty, agent succeeds."""
    _setup_mirror(tmp_path, monkeypatch)
    monkeypatch.setattr(claude_client, "_runner", lambda c: (0, json.dumps([]), ""))
    res = TechnicalAgent().run(_req(mirrors=["node"]))
    assert res.status.status == "ok"
    assert res.findings == []
