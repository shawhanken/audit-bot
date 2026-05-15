"""Security-dimension rules: secret patterns + dangerous shell commands.

Design ref: §5.3. These cover the "layer 2 rules" portion. The first layer
(third-party scanner fusion) is the server's job; the third (semantic LLM
pass) runs on the server side. Findings emitted here feed into the agent
as already-known issues so it won't double-report.
"""

from __future__ import annotations

import re
from typing import Iterable

from common.schema import Finding, Location
from rules.registry import RuleContext, rule


_DIM = "security"


# Conservative high-signal patterns. We intentionally don't try to catch
# every secret shape — that's what Secret Scanning / gitleaks are for.
_SECRET_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "S001_aws_access_key_id",
        "AWS Access Key ID present in a documentation code block",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "S002_anthropic_key",
        "Anthropic API key present in documentation",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    ),
    (
        "S003_openai_key",
        "OpenAI API key present in documentation",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    ),
    (
        "S004_private_key_block",
        "PEM private-key block present in documentation",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "S005_generic_jwt",
        "JWT present in a documentation code block",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    ),
]


_DANGEROUS_CMDS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "S010_rm_rf_root",
        "Example code contains a `rm -rf /` pattern",
        re.compile(r"rm\s+-rf?\s+/\s*(\$|\b|$)"),
    ),
    (
        "S011_chmod_777",
        "Example code contains `chmod 777`",
        re.compile(r"chmod\s+(?:-R\s+)?777\b"),
    ),
    (
        "S012_curl_pipe_bash",
        "Example code contains a `curl | bash` pattern",
        re.compile(r"curl\s+[^\|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"),
    ),
    (
        "S013_disable_auth_for_test",
        "Example code suggests disabling authentication",
        re.compile(r"(?i)(?:disable|turn\s*off|skip)\s+(?:auth|authentication)"),
    ),
]


def _walk_code_blocks(idx: dict) -> Iterable[dict]:
    for cb in idx.get("code_blocks", []) or []:
        yield cb


def _line_for_offset(content: str, offset: int) -> int:
    return content[:offset].count("\n")


@rule("S0xx_secret_in_doc", _DIM)
def secret_in_doc(ctx: RuleContext) -> Iterable[Finding]:
    for cb in _walk_code_blocks(ctx.head_index):
        for rid, title, pat in _SECRET_PATTERNS:
            for m in pat.finditer(cb["content"]):
                line = cb["line_start"] + _line_for_offset(cb["content"], m.start())
                yield Finding(
                    rule_id=rid,
                    source="rules",
                    dimension=_DIM,
                    severity="block",
                    title=title,
                    locations=[Location(file=cb["file"], line_start=line)],
                    evidence=m.group(0)[:64],
                    message=f"The code block at {cb['file']}:{line} contains what looks like a real secret.",
                    suggestion="Replace with a placeholder such as `<YOUR_API_KEY>`.",
                )


@rule("S0xx_dangerous_command_in_doc", _DIM)
def dangerous_command_in_doc(ctx: RuleContext) -> Iterable[Finding]:
    for cb in _walk_code_blocks(ctx.head_index):
        if cb["lang"] not in ("", "bash", "sh", "shell", "console"):
            continue
        for rid, title, pat in _DANGEROUS_CMDS:
            for m in pat.finditer(cb["content"]):
                line = cb["line_start"] + _line_for_offset(cb["content"], m.start())
                yield Finding(
                    rule_id=rid,
                    source="rules",
                    dimension=_DIM,
                    severity="warn",
                    title=title,
                    locations=[Location(file=cb["file"], line_start=line)],
                    evidence=m.group(0)[:80],
                    message=f"A potentially destructive command appears in the code block at {cb['file']}:{line}.",
                    suggestion="Warn the reader explicitly, or replace with a safer equivalent.",
                )
