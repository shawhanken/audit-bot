"""Render the aggregated audit report.

Design ref: §6.1 report.py, §8.

This module is intentionally output-format-focused. Posting to GitHub
(`gh pr comment`, Checks API, SARIF upload) is the calling shell's job; we
just produce the files. The action's entrypoint.sh wires these into the
appropriate GitHub commands.

Outputs (per design §7.1):
  - report.md         Sticky PR comment body
  - check_runs.json   List of Check Run conclusions to emit
  - doc-audit.sarif   SARIF (optional, when --sarif is passed)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Any

from common.config import TargetConfig, load_config
from common.schema import Finding, load_findings


_SEVERITY_RANK = {"block": 0, "warn": 1, "info": 2}
_SEVERITY_EMOJI = {"block": "🔴", "warn": "🟡", "info": "🔵"}
# Dimension labels are user-facing (Check Run names + sticky comment
# section headers). Kept English so they render correctly across all
# GitHub UIs and don't clash with branch-protection rule strings.
_DIM_LABEL = {
    "consistency": "Consistency",
    "security": "Security",
    "technical": "Technical Feasibility",
    "architecture": "Architecture",
    "style": "Style / Terminology",
}

# GitHub Issue Comments hard-cap at 65536 chars. We aim well below to leave
# room for the sticky marker and the trailing operations footer, and to keep
# the rendered comment scannable rather than a wall of 700+ findings.
_MAX_COMMENT_CHARS = 60_000
_MAX_FINDINGS_PER_DIM = 25


def _by_dim(findings: list[Finding]) -> dict[str, list[Finding]]:
    out: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        out[f.dimension].append(f)
    return out


def render_markdown(
    *,
    findings: list[Finding],
    target: TargetConfig,
    dimension_quality: dict[str, str],
    comment_marker: str = "doc-audit-bot",
    view_url: str | None = None,
) -> str:
    counts = defaultdict(int)
    for f in findings:
        counts[f.severity] += 1

    lines = [
        # Per-target marker so multiple targets in one PR each get their own
        # sticky comment instead of overwriting each other. publish.sh greps
        # for this same marker shape.
        f"<!-- {comment_marker}:{target.name} -->",
        "## 📋 Doc Audit Report",
        "",
        f"**target:** `{target.name}`  ",
        f"**Summary:** {counts.get('block', 0)} block, {counts.get('warn', 0)} warn, {counts.get('info', 0)} info",
        "",
    ]
    if view_url:
        lines.append(f"[Full audit log]({view_url})")
        lines.append("")

    grouped = _by_dim(findings)
    for dim_name, conf in target.dimensions.items():
        if not conf.enabled:
            continue
        bucket = grouped.get(dim_name, [])
        block_n = sum(1 for f in bucket if f.severity == "block")
        warn_n = sum(1 for f in bucket if f.severity == "warn")
        emoji = "✅" if not bucket else ("🔴" if block_n else "🟡")
        label = _DIM_LABEL.get(dim_name, dim_name)
        quality_suffix = ""
        if dimension_quality.get(dim_name) == "low_confidence":
            quality_suffix = "  ⚠️ low confidence (>30% of semantic findings failed location validation)"
        lines.append(f"<details><summary>{emoji} {label} — {block_n} block, {warn_n} warn{quality_suffix}</summary>")
        lines.append("")
        if not bucket:
            lines.append("No findings.")
        # Show worst-first; cap per-dim to avoid drowning the reader and
        # blowing the GitHub comment size limit.
        bucket_sorted = sorted(bucket, key=lambda f: _SEVERITY_RANK.get(f.severity, 99))
        shown = bucket_sorted[:_MAX_FINDINGS_PER_DIM]
        hidden = len(bucket_sorted) - len(shown)
        for f in shown:
            lines.append(_render_finding(f))
        if hidden > 0:
            lines.append(f"_…{hidden} more not shown — see the full SARIF artifact / audit log._")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.extend([
        "---",
        "**Actions:**",
        "- `/audit rerun` — re-run the full audit",
        "- `/audit rerun <dim>` — re-run a single dimension",
        "- `/audit ignore <finding-id>` — permanently ignore one finding",
        "- `/audit explain <finding-id>` — ask the bot to elaborate",
    ])
    body = "\n".join(lines)
    # Belt-and-suspenders: even with the per-dim cap, prose / evidence text
    # can push us past GitHub's hard limit on issue comments. Truncate with a
    # clear footer if so.
    if len(body) > _MAX_COMMENT_CHARS:
        cut = _MAX_COMMENT_CHARS - 200
        body = body[:cut].rstrip() + "\n\n_(comment exceeded GitHub's size limit; truncated — see the full audit artifact for all findings.)_"
    return body


def _render_finding(f: Finding) -> str:
    sev = _SEVERITY_EMOJI.get(f.severity, "•") + f" `{f.severity}`"
    locs = " ".join(f"`{l.file}:{l.line_start}`" for l in f.locations)
    body = [f"### {f.rule_id} — {f.title} — {sev}"]
    if locs:
        body.append(f"**Location:** {locs}")
    if f.history.historical_occurrences > 1:
        body.append(
            f"⚠️ Occurrence #{f.history.historical_occurrences}"
            + (f" (first seen in PR #{f.history.first_seen_pr})" if f.history.first_seen_pr else "")
        )
    if f.evidence:
        body.append(f"**Evidence:** `{f.evidence}`")
    if f.message:
        body.append(f"**Detail:** {f.message}")
    if f.suggestion:
        body.append(f"**Suggestion:** {f.suggestion}")
    body.append(f"<sub>finding_id: `{f.finding_id}` · source: {f.source}</sub>")
    return "\n".join(body)


def compute_check_runs(
    *,
    findings: list[Finding],
    target: TargetConfig,
) -> list[dict[str, Any]]:
    """One Check Run per enabled dimension (design §8.2)."""
    out: list[dict[str, Any]] = []
    grouped = _by_dim(findings)
    for dim_name, conf in target.dimensions.items():
        if not conf.enabled:
            continue
        bucket = grouped.get(dim_name, [])
        conclusion = "success"
        if conf.severity_gate == "block":
            if any(f.severity == "block" for f in bucket):
                conclusion = "failure"
        # severity_gate == "warn" → never block, always success/neutral.
        title = f"Doc Audit / {_DIM_LABEL.get(dim_name, dim_name)}"
        annotations = []
        for f in bucket[:50]:  # design §6.1: 50 cap per check
            loc = f.locations[0] if f.locations else None
            if loc is None:
                continue
            annotations.append({
                "path": loc.file,
                "start_line": loc.line_start,
                "end_line": loc.line_end or loc.line_start,
                "annotation_level": {
                    "block": "failure",
                    "warn": "warning",
                    "info": "notice",
                }.get(f.severity, "notice"),
                "title": f"{f.rule_id}: {f.title}",
                "message": f.message[:1000] if f.message else f.title,
            })
        out.append({
            "name": title,
            "conclusion": conclusion,
            "summary": f"{len(bucket)} finding(s)",
            "annotations": annotations,
        })
    return out


def render_sarif(findings: list[Finding]) -> dict[str, Any]:
    rules_seen: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for f in findings:
        if f.rule_id not in rules_seen:
            rules_seen[f.rule_id] = {
                "id": f.rule_id,
                "shortDescription": {"text": f.title},
                "fullDescription": {"text": f.message or f.title},
                "defaultConfiguration": {
                    "level": {
                        "block": "error",
                        "warn": "warning",
                        "info": "note",
                    }.get(f.severity, "note"),
                },
            }
        loc = f.locations[0] if f.locations else None
        results.append({
            "ruleId": f.rule_id,
            "level": {
                "block": "error",
                "warn": "warning",
                "info": "note",
            }.get(f.severity, "note"),
            "message": {"text": f.message or f.title},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": loc.file},
                        "region": {"startLine": loc.line_start},
                    }
                }
            ] if loc else [],
        })
    return {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "doc-audit",
                        "informationUri": "https://example.invalid/doc-audit",
                        "rules": list(rules_seen.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--findings", required=True)
    p.add_argument("--meta", default="")
    p.add_argument("--config", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--out-md", required=True)
    p.add_argument("--out-check-runs", default="")
    p.add_argument("--out-sarif", default="")
    p.add_argument("--view-url", default="")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    tgt = cfg.target_by_name(args.target)
    if tgt is None:
        print(f"error: unknown target {args.target}", file=sys.stderr)
        return 2

    findings = load_findings(args.findings)
    dim_quality: dict[str, str] = {}
    if args.meta and os.path.exists(args.meta):
        with open(args.meta, encoding="utf-8") as fp:
            dim_quality = (json.load(fp) or {}).get("dimension_quality", {})

    md = render_markdown(
        findings=findings,
        target=tgt,
        dimension_quality=dim_quality,
        comment_marker=cfg.global_.comment_marker,
        view_url=args.view_url or None,
    )
    os.makedirs(os.path.dirname(args.out_md), exist_ok=True)
    with open(args.out_md, "w", encoding="utf-8") as fp:
        fp.write(md)

    if args.out_check_runs:
        runs = compute_check_runs(findings=findings, target=tgt)
        with open(args.out_check_runs, "w", encoding="utf-8") as fp:
            json.dump(runs, fp, ensure_ascii=False, indent=2)

    if args.out_sarif:
        with open(args.out_sarif, "w", encoding="utf-8") as fp:
            json.dump(render_sarif(findings), fp, ensure_ascii=False, indent=2)

    # Exit code reflects whether any Check Run gates would fail.
    blocking = any(
        f.severity == "block"
        and tgt.dimensions.get(f.dimension)
        and tgt.dimensions[f.dimension].severity_gate == "block"
        for f in findings
    )
    return 1 if blocking else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
