"""POST the audit request to the server; fall back to local LLM on failure.

Design ref: §6.1 dispatch.py, §9 error handling.

Failure modes (in order of severity):
1. Server reachable + healthy → use semantic findings from server response.
2. Server unreachable / 5xx and `anthropic_key_fallback` is configured → run a
   local in-process semantic pass using `claude -p` against the head index.
3. Otherwise → write an empty semantic findings file and a degraded-status
   marker. Aggregate / report still proceed using rules findings only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any

import requests

from common.config import TargetConfig, load_config
from common.schema import dump_findings, finding_from_json


REQUEST_TIMEOUT_S = 300
MAX_RETRIES = 1


def build_request(
    *,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    target: TargetConfig,
    head_index: dict[str, Any],
    diff: dict[str, Any],
    changed_files: list[str],
    rules_findings: list[dict[str, Any]],
    documents: dict[str, dict[str, str]],
    related_code_excerpts: dict[str, str],
    budget_max_usd: float,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "request_id": str(uuid.uuid4()),
        "repo": {
            "owner": repo_owner,
            "name": repo_name,
            "default_branch": "main",
        },
        "pr": {
            "number": pr_number,
            "title": "",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "is_draft": False,
            "is_fork": False,
        },
        "target": {
            "name": target.name,
            "paths": list(target.paths),
            "dimensions": {
                name: {"enabled": c.enabled, "severity_gate": c.severity_gate}
                for name, c in target.dimensions.items()
            },
        },
        "payload": {
            "index_head": head_index,
            "diff": diff,
            "changed_files": list(changed_files),
            "rules_findings": list(rules_findings),
            "documents": documents,
            "related_code_excerpts": related_code_excerpts,
            "related_code_mirrors": list(target.related_code_mirrors or []),
        },
        "budget_hint": {
            "max_usd": budget_max_usd,
            "max_duration_seconds": REQUEST_TIMEOUT_S,
        },
        "client_version": "doc-audit-action@dev",
    }


def post_audit(
    *,
    server_url: str,
    server_token: str,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if server_token:
        headers["Authorization"] = f"Bearer {server_token}"
    # Tolerate both forms of server_url: with or without a trailing /v1 segment.
    base = server_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    audit_url = f"{base}/v1/audit"

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.post(
                audit_url,
                json=request_body,
                headers=headers,
                timeout=REQUEST_TIMEOUT_S,
            )
            if r.status_code >= 500:
                raise RuntimeError(f"server returned {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400:
                # Client error — surface but do not retry.
                return {
                    "status": "failed",
                    "findings_by_dimension": {},
                    "dimension_status": {},
                    "error": f"http {r.status_code}: {r.text[:200]}",
                }
            return r.json()
        except (requests.RequestException, RuntimeError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
            continue
    raise RuntimeError(f"audit POST failed after retries: {last_err}")


def local_fallback(
    *,
    target: TargetConfig,
    head_index_path: str,
    diff_path: str,
    documents: dict[str, dict[str, str]],
    rules_findings_path: str,
    anthropic_key: str,
    workdir: str,
) -> dict[str, Any]:
    """Best-effort local semantic pass when the server is unreachable.

    Shells out to `claude -p` once per enabled dimension. The prompt mirrors
    the server-side prompt skeleton; if `claude` is missing or fails, the
    dimension is recorded as `degraded` and yields zero findings.
    """
    findings_by_dim: dict[str, list[dict[str, Any]]] = {}
    dim_status: dict[str, dict[str, Any]] = {}
    enabled = [name for name, c in target.dimensions.items() if c.enabled]
    for dim in enabled:
        prompt = _fallback_prompt(
            dim=dim,
            head_index_path=head_index_path,
            diff_path=diff_path,
            documents=documents,
            rules_findings_path=rules_findings_path,
            target=target.name,
        )
        try:
            # --bare deliberately omitted: it forces ANTHROPIC_API_KEY auth and
            # ignores OAuth/keychain. We keep the user's interactive login
            # working; --tools "" still blocks tool use.
            res = subprocess.run(
                [
                    "claude", "-p", prompt,
                    "--output-format", "json",
                    "--tools", "",
                ],
                env={**os.environ, "ANTHROPIC_API_KEY": anthropic_key},
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            findings_by_dim[dim] = []
            dim_status[dim] = {"status": "degraded", "reason": f"claude_unavailable: {e}"}
            continue
        if res.returncode != 0:
            findings_by_dim[dim] = []
            dim_status[dim] = {
                "status": "degraded",
                "reason": f"claude_exit_{res.returncode}",
            }
            continue
        # Real `claude -p --output-format json` returns an envelope
        # {"type":"result","is_error":bool,"result":"<answer-string>",...}.
        # Unwrap, treat is_error=true as degraded, then parse the inner string
        # as JSON (tolerant of fences and embedded arrays).
        parsed, parse_error = _parse_claude_output(res.stdout)
        if parse_error:
            findings_by_dim[dim] = []
            dim_status[dim] = {"status": "degraded", "reason": parse_error}
            continue
        if isinstance(parsed, dict):
            parsed = parsed.get("findings") or parsed.get("result") or []
        findings_by_dim[dim] = parsed if isinstance(parsed, list) else []
        dim_status[dim] = {"status": "ok", "fallback": True}
    return {
        "schema_version": "1",
        "request_id": "local-fallback",
        "status": "degraded",
        "findings_by_dimension": findings_by_dim,
        "dimension_status": dim_status,
        "totals": {},
        "remaining_budget_usd": None,
    }


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _parse_claude_output(stdout: str) -> tuple[Any, str | None]:
    """Return (parsed_value, error_reason). Mirrors server/app/claude_client.py
    but kept local because the Action half is intentionally independent.

    error_reason is None on success; otherwise the dimension is degraded.
    """
    stdout = stdout.strip()
    if not stdout:
        return [], None
    # Try envelope first.
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        env = None
    answer_text: str
    if isinstance(env, dict) and env.get("type") == "result" and "result" in env:
        if env.get("is_error"):
            return None, f"envelope_is_error: {str(env.get('result', ''))[:120]}"
        answer_text = str(env.get("result", ""))
    elif env is not None:
        # Caller (or test) passed a bare JSON value — accept it.
        return env, None
    else:
        answer_text = stdout
    answer_text = answer_text.strip()
    if not answer_text:
        return [], None
    try:
        return json.loads(answer_text), None
    except json.JSONDecodeError:
        pass
    m = _FENCED_JSON_RE.search(answer_text)
    if m:
        try:
            return json.loads(m.group(1).strip()), None
        except json.JSONDecodeError:
            pass
    for i, ch in enumerate(answer_text):
        if ch in "[{":
            for j in range(len(answer_text), i, -1):
                try:
                    return json.loads(answer_text[i:j]), None
                except json.JSONDecodeError:
                    continue
    return None, "claude_non_json"


def _fallback_prompt(
    *,
    dim: str,
    head_index_path: str,
    diff_path: str,
    documents: dict[str, dict[str, str]],
    rules_findings_path: str,
    target: str,
) -> str:
    return (
        f"你是文档审计 bot 的 {dim} 维度 agent。\n"
        f"target={target}\n"
        f"已知 rules 发现见 {rules_findings_path}（不要重复报告）。\n"
        f"head index 见 {head_index_path}；diff 见 {diff_path}。\n"
        f"涉及文档：{list(documents.keys())[:20]}。\n"
        "输出严格 JSON 数组，每条形如 {rule_id, source: \"semantic\", dimension, "
        "severity, title, locations: [{file, line_start}], message, suggestion}。\n"
        "若无可靠发现，输出 []。"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--repo-owner", required=True)
    p.add_argument("--repo-name", required=True)
    p.add_argument("--pr-number", type=int, default=0)
    p.add_argument("--base-sha", default="")
    p.add_argument("--head-sha", default="")
    p.add_argument("--head-index", required=True)
    p.add_argument("--diff", required=True)
    p.add_argument("--changed-files", default="")
    p.add_argument("--rules-findings", required=True)
    p.add_argument("--documents-manifest", required=True,
                   help="JSON: {file_path: blob_sha}; file contents read from --repo-root")
    p.add_argument("--repo-root", required=True)
    p.add_argument("--related-code-dir", action="append", default=[])
    p.add_argument("--server-url", default="")
    p.add_argument("--server-token", default="")
    p.add_argument("--anthropic-key-fallback", default="")
    p.add_argument("--out-dir", required=True)
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    tgt = cfg.target_by_name(args.target)
    if tgt is None:
        print(f"error: unknown target {args.target}", file=sys.stderr)
        return 2

    with open(args.head_index, encoding="utf-8") as fp:
        head_index = json.load(fp)
    with open(args.diff, encoding="utf-8") as fp:
        diff = json.load(fp)
    with open(args.rules_findings, encoding="utf-8") as fp:
        rules_findings = json.load(fp)
    with open(args.documents_manifest, encoding="utf-8") as fp:
        manifest = json.load(fp)

    documents: dict[str, dict[str, str]] = {}
    for rel, sha in manifest.items():
        full = os.path.join(args.repo_root, rel)
        try:
            with open(full, encoding="utf-8") as fp:
                documents[rel] = {"content": fp.read(), "sha": sha}
        except OSError:
            continue

    related_code: dict[str, str] = {}
    for d in args.related_code_dir:
        # We collect a small excerpt-per-file budget; design §6.2 notes Action
        # uploads code excerpts rather than the server cloning.
        for root, _dirs, files in os.walk(os.path.join(args.repo_root, d)):
            for fn in files:
                if not fn.endswith((".rs", ".py", ".ts", ".tsx", ".go")):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, args.repo_root)
                try:
                    with open(full, encoding="utf-8") as fp:
                        related_code[rel] = fp.read()[:8_000]
                except OSError:
                    continue
                if len(related_code) >= 200:
                    break
            if len(related_code) >= 200:
                break

    changed: list[str] = []
    if args.changed_files and os.path.exists(args.changed_files):
        with open(args.changed_files, encoding="utf-8") as fp:
            changed = [line.strip() for line in fp if line.strip()]
    elif args.changed_files:
        changed = [s for s in args.changed_files.split(",") if s]

    req = build_request(
        repo_owner=args.repo_owner,
        repo_name=args.repo_name,
        pr_number=args.pr_number,
        base_sha=args.base_sha,
        head_sha=args.head_sha,
        target=tgt,
        head_index=head_index,
        diff=diff,
        changed_files=changed,
        rules_findings=rules_findings,
        documents=documents,
        related_code_excerpts=related_code,
        budget_max_usd=cfg.global_.max_usd_per_run,
    )

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, f"dispatch_request_{tgt.name}.json"), "w", encoding="utf-8") as fp:
        json.dump(req, fp, ensure_ascii=False, indent=2)

    response: dict[str, Any] | None = None
    if args.server_url:
        # Token may be empty when the server runs in open mode (local dev);
        # post_audit will send the bearer header only if a token is given.
        try:
            response = post_audit(
                server_url=args.server_url,
                server_token=args.server_token,
                request_body=req,
            )
        except Exception as e:  # noqa: BLE001 - design §9
            print(f"warn: server unreachable: {e}", file=sys.stderr)
            response = None

    if response is None:
        if args.anthropic_key_fallback:
            response = local_fallback(
                target=tgt,
                head_index_path=args.head_index,
                diff_path=args.diff,
                documents=documents,
                rules_findings_path=args.rules_findings,
                anthropic_key=args.anthropic_key_fallback,
                workdir=args.out_dir,
            )
        else:
            response = {
                "schema_version": "1",
                "request_id": req["request_id"],
                "status": "degraded",
                "findings_by_dimension": {},
                "dimension_status": {
                    name: {"status": "degraded", "reason": "server_unreachable"}
                    for name, c in tgt.dimensions.items()
                    if c.enabled
                },
                "totals": {},
                "remaining_budget_usd": None,
            }

    with open(os.path.join(args.out_dir, f"dispatch_response_{tgt.name}.json"), "w", encoding="utf-8") as fp:
        json.dump(response, fp, ensure_ascii=False, indent=2)

    # Emit per-dimension semantic findings files so aggregate.py can load them.
    for dim, findings in (response.get("findings_by_dimension") or {}).items():
        out_path = os.path.join(args.out_dir, f"findings_semantic_{tgt.name}_{dim}.json")
        normalized = []
        for f in findings:
            f = dict(f)
            f.setdefault("dimension", dim)
            f.setdefault("source", "semantic")
            normalized.append(finding_from_json(f).to_json())
        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump(normalized, fp, ensure_ascii=False, indent=2)

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
