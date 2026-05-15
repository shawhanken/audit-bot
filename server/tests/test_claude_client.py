from __future__ import annotations

import json
import os
import shutil

import pytest

from app import claude_client


def test_invoke_returns_parsed_json(monkeypatch):
    """Bare-list runner output (test-only path) is parsed directly."""
    def fake_runner(call):
        return 0, json.dumps([{"rule_id": "X", "dimension": "consistency"}]), ""
    monkeypatch.setattr(claude_client, "_runner", fake_runner)
    res = claude_client.invoke(claude_client.ClaudeCall(prompt="hello"))
    assert res.ok
    assert isinstance(res.parsed, list) and res.parsed[0]["rule_id"] == "X"


def test_invoke_unwraps_claude_envelope(monkeypatch):
    """Real `claude -p --output-format json` envelopes get unwrapped before JSON parsing."""
    envelope = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps([{"rule_id": "Y", "dimension": "security"}]),
        "total_cost_usd": 0.042,
        "usage": {
            "input_tokens": 123,
            "output_tokens": 45,
            "cache_read_input_tokens": 6,
        },
    })
    monkeypatch.setattr(claude_client, "_runner", lambda c: (0, envelope, ""))
    res = claude_client.invoke(claude_client.ClaudeCall(prompt=""))
    assert res.ok
    assert res.parsed[0]["rule_id"] == "Y"
    assert res.cost_usd == 0.042
    assert res.tokens_input == 123 and res.tokens_output == 45
    assert res.tokens_cached_read == 6


def test_invoke_envelope_is_error_marks_failure(monkeypatch):
    """An envelope with `is_error: true` (e.g. 'Not logged in') is treated as failure."""
    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "result": "Not logged in · Please run /login",
    })
    monkeypatch.setattr(claude_client, "_runner", lambda c: (0, envelope, ""))
    res = claude_client.invoke(claude_client.ClaudeCall(prompt=""))
    assert not res.ok
    assert res.envelope_is_error is True
    assert "Not logged in" in res.error


def test_extract_json_unwraps_fenced_block():
    s = "Here is the answer:\n```json\n[{\"a\": 1}]\n```\nthanks!"
    assert claude_client._extract_json(s) == [{"a": 1}]


def test_extract_json_finds_embedded_array():
    s = "prelude text [{\"rule_id\": \"Z\"}] trailing"
    assert claude_client._extract_json(s) == [{"rule_id": "Z"}]


def test_invoke_non_zero_exit_is_degraded(monkeypatch):
    monkeypatch.setattr(claude_client, "_runner", lambda c: (2, "", "boom"))
    res = claude_client.invoke(claude_client.ClaudeCall(prompt=""))
    assert not res.ok and "exited 2" in res.error


def test_invoke_non_json_output_returns_error(monkeypatch):
    """When neither envelope nor model output yields parseable JSON, mark error."""
    monkeypatch.setattr(claude_client, "_runner", lambda c: (0, "totally not json", ""))
    res = claude_client.invoke(claude_client.ClaudeCall(prompt=""))
    assert not res.ok and "non-json" in res.error


def test_invoke_missing_binary(monkeypatch):
    def boom(_):
        raise FileNotFoundError("claude")
    monkeypatch.setattr(claude_client, "_runner", boom)
    res = claude_client.invoke(claude_client.ClaudeCall(prompt=""))
    assert not res.ok and "claude" in res.error


# --- Real-CLI smoke test ----------------------------------------------------
# Gated behind RUN_REAL_CLAUDE=1 so CI doesn't spend tokens or fail on auth.
@pytest.mark.skipif(
    os.environ.get("RUN_REAL_CLAUDE") != "1" or shutil.which("claude") is None,
    reason="RUN_REAL_CLAUDE=1 not set or claude CLI missing",
)
def test_real_cli_smoke():
    res = claude_client.invoke(claude_client.ClaudeCall(
        prompt=(
            "Respond with ONLY this JSON array (no prose, no markdown fence): "
            "[{\"rule_id\":\"smoke\",\"dimension\":\"consistency\","
            "\"severity\":\"info\",\"title\":\"hi\","
            "\"locations\":[{\"file\":\"x\",\"line_start\":1}],"
            "\"message\":\"m\",\"suggestion\":\"s\"}]"
        ),
        timeout_s=60,
    ))
    assert res.ok, res.error
    assert isinstance(res.parsed, list)
    assert res.parsed[0]["rule_id"] == "smoke"
