from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import claude_client
from app.api import build_app


def _req_body(head_sha="h1", dims=None):
    dims = dims or {"consistency": {"enabled": True, "severity_gate": "warn"}}
    return {
        "schema_version": "1",
        "request_id": "req-001",
        "repo": {"owner": "o", "name": "n", "default_branch": "main"},
        "pr": {"number": 1, "title": "", "base_sha": "b", "head_sha": head_sha, "is_draft": False, "is_fork": False},
        "target": {"name": "cips", "paths": ["docs/**"], "dimensions": dims},
        "payload": {
            "index_head": {"code_symbols_referenced": []},
            "diff": {"by_kind": {}},
            "changed_files": ["docs/cip-1.md"],
            "rules_findings": [],
            "documents": {"docs/cip-1.md": {"content": "hello", "sha": ""}},
            "related_code_excerpts": {},
        },
        "budget_hint": {"max_usd": 2.0, "max_duration_seconds": 60},
        "client_version": "test",
    }


def test_health_endpoint(tmp_path):
    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(app)
    r = client.get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_audit_endpoint_runs_orchestrator(monkeypatch, tmp_path):
    def fake_runner(call):
        return 0, json.dumps([
            {
                "rule_id": "X",
                "dimension": "consistency",
                "severity": "warn",
                "title": "t",
                "locations": [{"file": "docs/cip-1.md", "line_start": 1}],
                "message": "m",
            }
        ]), ""
    monkeypatch.setattr(claude_client, "_runner", fake_runner)

    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(app)
    r = client.post("/v1/audit", json=_req_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["findings_by_dimension"]["consistency"][0]["rule_id"] == "X"


def test_audit_endpoint_negative_cache_by_diff_hash(monkeypatch, tmp_path):
    """Same diff body via different head_sha should hit the negative cache."""
    call_count = {"n": 0}

    def fake_runner(call):
        call_count["n"] += 1
        return 0, json.dumps([]), ""
    monkeypatch.setattr(claude_client, "_runner", fake_runner)

    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(app)
    r1 = client.post("/v1/audit", json=_req_body(head_sha="sha-X"))
    r2 = client.post("/v1/audit", json=_req_body(head_sha="sha-Y"))
    assert r1.status_code == 200 and r2.status_code == 200
    assert call_count["n"] == 1  # sha-Y served from negative cache


def test_audit_endpoint_caches_repeated_requests(monkeypatch, tmp_path):
    call_count = {"n": 0}

    def fake_runner(call):
        call_count["n"] += 1
        return 0, json.dumps([]), ""
    monkeypatch.setattr(claude_client, "_runner", fake_runner)

    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(app)
    r1 = client.post("/v1/audit", json=_req_body(head_sha="sha-A"))
    r2 = client.post("/v1/audit", json=_req_body(head_sha="sha-A"))
    assert r1.status_code == 200 and r2.status_code == 200
    assert call_count["n"] == 1  # second request served from cache


def test_budget_endpoint(tmp_path):
    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(app)
    r = client.get("/v1/budget/o/n")
    assert r.status_code == 200
    body = r.json()
    assert body["repo"] == "o/n" and body["remaining_usd"] >= 0


def test_audit_endpoint_requires_token_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("DOC_AUDIT_SERVER_TOKEN", "secret")
    app = build_app(db_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(app)
    r = client.post("/v1/audit", json=_req_body())
    assert r.status_code == 401
    r = client.post("/v1/audit", json=_req_body(), headers={"Authorization": "Bearer secret"})
    # Without monkeypatching the claude runner this will degrade rather than crash.
    assert r.status_code == 200
