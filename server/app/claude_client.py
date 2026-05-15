"""Thin wrapper around the `claude -p` headless CLI.

The user requested we drive LLM through the `claude` subprocess rather than
the Anthropic SDK directly (design §6.2 normally calls SDK; we deliberately
trade some control for not needing an SDK dependency at all).

`claude -p --output-format json` returns a single envelope of shape::

    {"type": "result", "subtype": "success", "is_error": false,
     "result": "<answer text from the model>",
     "session_id": "...", "total_cost_usd": ..., "usage": {...},
     ...}

The model's actual answer is the `result` STRING, which we then attempt to
parse as JSON (since our agents instruct the model to return a JSON array).

For tests we expose `_runner` as a module-level attribute so tests can
monkey-patch it without subprocess. Tests may return either the envelope
shape or a bare JSON payload — `_extract_json` handles both.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ClaudeCall:
    prompt: str
    timeout_s: int = 180
    extra_env: dict[str, str] | None = None
    # Disable tools by default — agents receive all needed context in the
    # prompt; allowing tools would let prompt-injection in PR docs steer the
    # model to read arbitrary files.
    allowed_tools: str = ""


@dataclass
class ClaudeResult:
    ok: bool
    parsed: Any  # parsed JSON payload (list of findings, or {"findings": [...]})
    raw_stdout: str
    raw_stderr: str
    duration_ms: int
    error: str | None = None
    envelope_is_error: bool | None = None
    cost_usd: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cached_read: int = 0


def _default_runner(call: ClaudeCall) -> tuple[int, str, str]:
    if shutil.which("claude") is None:
        raise FileNotFoundError("`claude` CLI not on PATH")
    env = dict(os.environ)
    if call.extra_env:
        env.update(call.extra_env)
    # NOTE: `--bare` was tried initially for hermetic runs, but it forces
    # Anthropic auth strictly through ANTHROPIC_API_KEY or apiKeyHelper —
    # OAuth / keychain logins are ignored. Without `--bare` we keep the
    # user's interactive login working at the cost of inheriting hooks /
    # plugins / MCP from their settings. `--tools ""` still blocks tool use.
    #
    # Prompt is piped to stdin (omit positional prompt arg) instead of argv:
    # full document corpus easily reaches 100KB, which blows argv+envp's
    # E2BIG limit (~2MB but combined with the runner env we hit it).
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--tools",
        call.allowed_tools,
    ]
    res = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=call.timeout_s,
        input=call.prompt,
    )
    return res.returncode, res.stdout, res.stderr


# Tests monkey-patch this attribute.
_runner: Callable[[ClaudeCall], tuple[int, str, str]] = _default_runner


def invoke(call: ClaudeCall) -> ClaudeResult:
    start = time.monotonic()
    try:
        rc, stdout, stderr = _runner(call)
    except FileNotFoundError as e:
        return ClaudeResult(
            ok=False, parsed=None, raw_stdout="", raw_stderr="",
            duration_ms=int((time.monotonic() - start) * 1000),
            error=str(e),
        )
    except subprocess.TimeoutExpired as e:
        return ClaudeResult(
            ok=False, parsed=None, raw_stdout="", raw_stderr="",
            duration_ms=int((time.monotonic() - start) * 1000),
            error=f"timeout: {e}",
        )
    duration_ms = int((time.monotonic() - start) * 1000)
    if rc != 0:
        return ClaudeResult(
            ok=False, parsed=None, raw_stdout=stdout, raw_stderr=stderr,
            duration_ms=duration_ms,
            error=f"claude exited {rc}",
        )

    envelope = _try_envelope(stdout)
    answer_text: str
    envelope_is_error: bool | None = None
    cost_usd = 0.0
    tokens_input = tokens_output = cached = 0
    if envelope is not None:
        envelope_is_error = bool(envelope.get("is_error"))
        if envelope_is_error:
            return ClaudeResult(
                ok=False, parsed=None, raw_stdout=stdout, raw_stderr=stderr,
                duration_ms=duration_ms,
                envelope_is_error=True,
                error=f"claude envelope is_error: {envelope.get('result', '')[:120]}",
            )
        answer_text = str(envelope.get("result", ""))
        cost_usd = float(envelope.get("total_cost_usd", 0) or 0)
        usage = envelope.get("usage") or {}
        tokens_input = int(usage.get("input_tokens", 0) or 0)
        tokens_output = int(usage.get("output_tokens", 0) or 0)
        cached = int(usage.get("cache_read_input_tokens", 0) or 0)
    else:
        # Test monkey-patches that return bare payloads land here.
        answer_text = stdout

    try:
        parsed = _extract_json(answer_text)
    except ValueError as e:
        return ClaudeResult(
            ok=False, parsed=None, raw_stdout=stdout, raw_stderr=stderr,
            duration_ms=duration_ms,
            envelope_is_error=envelope_is_error,
            cost_usd=cost_usd, tokens_input=tokens_input,
            tokens_output=tokens_output, tokens_cached_read=cached,
            error=f"non-json: {e}",
        )
    return ClaudeResult(
        ok=True, parsed=parsed, raw_stdout=stdout, raw_stderr=stderr,
        duration_ms=duration_ms,
        envelope_is_error=envelope_is_error,
        cost_usd=cost_usd, tokens_input=tokens_input,
        tokens_output=tokens_output, tokens_cached_read=cached,
    )


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _try_envelope(stdout: str) -> dict[str, Any] | None:
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and obj.get("type") == "result" and "result" in obj:
        return obj
    return None


def _extract_json(s: str) -> Any:
    """Parse the model's answer text into JSON.

    Tolerates:
      * a bare JSON value (array or object)
      * a ```json ... ``` fenced block (common when the model ignores the
        'no markdown' instruction)
      * a JSON value embedded in surrounding prose
    """
    s = s.strip()
    if not s:
        return []
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = _FENCED_JSON_RE.search(s)
    if m:
        inner = m.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass
    # Locate the first '[' or '{' and try progressively shorter slices.
    for i, ch in enumerate(s):
        if ch in "[{":
            for j in range(len(s), i, -1):
                try:
                    return json.loads(s[i:j])
                except json.JSONDecodeError:
                    continue
    raise ValueError("no parseable JSON in output")
