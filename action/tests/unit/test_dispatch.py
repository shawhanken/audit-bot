from __future__ import annotations

import json
from unittest import mock

import dispatch
from common.config import DimensionConfig, TargetConfig


def _tgt():
    return TargetConfig(
        name="cips",
        paths=["docs/cips/**"],
        dimensions={
            "consistency": DimensionConfig(enabled=True, severity_gate="block"),
            "security": DimensionConfig(enabled=True, severity_gate="warn"),
        },
    )


def test_post_audit_success_returns_response():
    body = {"foo": "bar"}
    fake_response = mock.Mock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"status": "ok", "findings_by_dimension": {}}
    with mock.patch("dispatch.requests.post", return_value=fake_response) as p:
        r = dispatch.post_audit(server_url="http://x", server_token="t", request_body=body)
    p.assert_called_once()
    assert r["status"] == "ok"


def test_post_audit_5xx_raises_after_retries():
    fake_response = mock.Mock()
    fake_response.status_code = 503
    fake_response.text = "down"
    with mock.patch("dispatch.requests.post", return_value=fake_response):
        try:
            dispatch.post_audit(server_url="http://x", server_token="t", request_body={})
        except RuntimeError as e:
            assert "audit POST failed" in str(e)
            return
        raise AssertionError("expected RuntimeError")


def test_local_fallback_handles_missing_claude():
    with mock.patch("dispatch.subprocess.run", side_effect=FileNotFoundError("claude")):
        resp = dispatch.local_fallback(
            target=_tgt(),
            head_index_path="/dev/null",
            diff_path="/dev/null",
            documents={},
            rules_findings_path="/dev/null",
            anthropic_key="dummy",
            workdir="/tmp",
        )
    # Both enabled dimensions should be marked degraded.
    assert resp["dimension_status"]["consistency"]["status"] == "degraded"
    assert resp["dimension_status"]["security"]["status"] == "degraded"
    assert resp["findings_by_dimension"]["consistency"] == []


def test_local_fallback_parses_bare_json_findings():
    """Test path: a bare JSON array as stdout (no claude envelope)."""
    fake = mock.Mock()
    fake.returncode = 0
    fake.stdout = json.dumps([
        {
            "rule_id": "X1",
            "dimension": "consistency",
            "severity": "warn",
            "title": "x",
            "locations": [{"file": "a.md", "line_start": 1}],
            "message": "...",
        }
    ])
    fake.stderr = ""
    with mock.patch("dispatch.subprocess.run", return_value=fake):
        resp = dispatch.local_fallback(
            target=_tgt(),
            head_index_path="/dev/null",
            diff_path="/dev/null",
            documents={},
            rules_findings_path="/dev/null",
            anthropic_key="dummy",
            workdir="/tmp",
        )
    assert resp["findings_by_dimension"]["consistency"][0]["rule_id"] == "X1"


def test_local_fallback_unwraps_claude_envelope():
    """Real `claude -p` returns an envelope wrapping the answer as a string."""
    fake = mock.Mock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps([
            {
                "rule_id": "Y1",
                "dimension": "security",
                "severity": "warn",
                "title": "y",
                "locations": [{"file": "a.md", "line_start": 1}],
                "message": "...",
            }
        ]),
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    fake.stderr = ""
    with mock.patch("dispatch.subprocess.run", return_value=fake):
        resp = dispatch.local_fallback(
            target=_tgt(),
            head_index_path="/dev/null", diff_path="/dev/null",
            documents={}, rules_findings_path="/dev/null",
            anthropic_key="dummy", workdir="/tmp",
        )
    # Both dimensions get the same parsed payload because we mock subprocess
    # globally; we only assert security parsed cleanly here.
    assert resp["findings_by_dimension"]["security"][0]["rule_id"] == "Y1"


def test_local_fallback_envelope_is_error_marks_degraded():
    """envelope.is_error=true (e.g. 'Not logged in') => degraded, not crash."""
    fake = mock.Mock()
    fake.returncode = 0
    fake.stdout = json.dumps({
        "type": "result",
        "is_error": True,
        "result": "Not logged in · Please run /login",
    })
    fake.stderr = ""
    with mock.patch("dispatch.subprocess.run", return_value=fake):
        resp = dispatch.local_fallback(
            target=_tgt(),
            head_index_path="/dev/null", diff_path="/dev/null",
            documents={}, rules_findings_path="/dev/null",
            anthropic_key="dummy", workdir="/tmp",
        )
    for dim in ("consistency", "security"):
        assert resp["dimension_status"][dim]["status"] == "degraded"
        assert "envelope_is_error" in resp["dimension_status"][dim]["reason"]


def test_parse_claude_output_handles_fenced_json():
    """The model often wraps its JSON in ```json fences."""
    text = '{"type":"result","is_error":false,"result":"```json\\n[{\\"x\\":1}]\\n```"}'
    parsed, err = dispatch._parse_claude_output(text)
    assert err is None
    assert parsed == [{"x": 1}]
