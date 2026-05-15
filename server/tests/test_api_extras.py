"""Coverage for /metrics, token-to-repo binding, and the breach notifier."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import claude_client
from app.api import build_app
from app.notifier import BreachEvent, LoggingNotifier, Notifier


def _req_body(*, repo=("o", "n"), head_sha="h1"):
    return {
        "schema_version": "1",
        "request_id": "r",
        "repo": {"owner": repo[0], "name": repo[1], "default_branch": "main"},
        "pr": {"number": 1, "title": "", "base_sha": "b", "head_sha": head_sha, "is_draft": False, "is_fork": False},
        "target": {"name": "cips", "paths": ["docs/**"], "dimensions": {
            "consistency": {"enabled": True, "severity_gate": "warn"}
        }},
        "payload": {
            "index_head": {"code_symbols_referenced": []},
            "diff": {"by_kind": {}},
            "changed_files": ["docs/cip-1.md"],
            "rules_findings": [],
            "documents": {"docs/cip-1.md": {"content": "hi", "sha": ""}},
            "related_code_excerpts": {},
        },
        "budget_hint": {"max_usd": 2.0, "max_duration_seconds": 60},
        "client_version": "test",
    }


def test_metrics_endpoint_after_audit(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_client, "_runner", lambda c: (0, json.dumps([]), ""))
    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(app)
    client.post("/v1/audit", json=_req_body())
    r = client.get("/v1/metrics")
    assert r.status_code == 200
    body = r.text
    assert "# TYPE doc_audit_requests_total counter" in body
    assert 'doc_audit_requests_total{repo="o/n",status="ok"} 1' in body
    assert "doc_audit_duration_ms_bucket" in body
    assert "doc_audit_budget_remaining_usd" in body
    assert "doc_audit_dimension_runs_total" in body


def test_token_binding_rejects_wrong_repo(monkeypatch, tmp_path):
    """A token issued for o1/n1 must not be accepted for o2/n2."""
    monkeypatch.setattr(claude_client, "_runner", lambda c: (0, json.dumps([]), ""))
    monkeypatch.delenv("DOC_AUDIT_SERVER_TOKEN", raising=False)
    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    state = app.state.audit_state
    tok = state.tokens.issue(repo="o1/n1", label="testbed")

    client = TestClient(app)
    # Correct repo → OK.
    r = client.post("/v1/audit", json=_req_body(repo=("o1", "n1")),
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text

    # Wrong repo → 403.
    r = client.post("/v1/audit", json=_req_body(repo=("o2", "n2")),
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403, r.text
    assert "bound to o1/n1" in r.text


def test_token_binding_unknown_token_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_client, "_runner", lambda c: (0, json.dumps([]), ""))
    monkeypatch.delenv("DOC_AUDIT_SERVER_TOKEN", raising=False)
    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    state = app.state.audit_state
    state.tokens.issue(repo="o/n")  # ensure store is non-empty

    client = TestClient(app)
    r = client.post("/v1/audit", json=_req_body(),
                    headers={"Authorization": "Bearer made-up-token"})
    assert r.status_code == 401
    assert "unknown token" in r.text


def test_token_revoke(monkeypatch, tmp_path):
    monkeypatch.delenv("DOC_AUDIT_SERVER_TOKEN", raising=False)
    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    state = app.state.audit_state
    tok = state.tokens.issue(repo="o/n")
    assert state.tokens.lookup(tok) is not None
    assert state.tokens.revoke(tok) is True
    assert state.tokens.lookup(tok) is None
    # revoking twice is a no-op.
    assert state.tokens.revoke(tok) is False


class _CapturingNotifier(LoggingNotifier):
    def __init__(self):
        super().__init__()
        self.events: list[BreachEvent] = []

    def notify(self, event: BreachEvent) -> None:
        super().notify(event)
        self.events.append(event)


def test_soft_breach_triggers_notifier(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_client, "_runner", lambda c: (0, json.dumps([]), ""))
    notifier = _CapturingNotifier()
    app = build_app(db_path=str(tmp_path / "db.sqlite3"), notifier=notifier)
    state = app.state.audit_state
    state.budget.set_cap("o/n", 0.06)  # one ok dim costs $0.05 → soft breach at first call

    client = TestClient(app)
    client.post("/v1/audit", json=_req_body(head_sha="sha-A"))
    soft = [e for e in notifier.events if e.kind == "soft"]
    assert len(soft) == 1
    # Dedup: second call in the same month should NOT re-notify.
    client.post("/v1/audit", json=_req_body(head_sha="sha-B"))
    soft = [e for e in notifier.events if e.kind == "soft"]
    assert len(soft) == 1


def test_hard_breach_returns_503_and_notifies(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_client, "_runner", lambda c: (0, json.dumps([]), ""))
    notifier = _CapturingNotifier()
    app = build_app(db_path=str(tmp_path / "db.sqlite3"), notifier=notifier)
    state = app.state.audit_state
    state.budget.set_cap("o/n", 0.0)  # cap=0 → any spend is hard breach
    state.budget.record(
        repo="o/n", request_id="seed", pr_number=0,
        tokens_input=0, tokens_output=0, tokens_cached_read=0,
        cost_usd=0.10, duration_ms=0,
    )

    client = TestClient(app)
    r = client.post("/v1/audit", json=_req_body())
    assert r.status_code == 503
    assert any(e.kind == "hard" for e in notifier.events)
