"""Run all registered rules for a target and emit findings_rules_<target>.json.

Design ref: §4.3, §6.1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from common.config import AuditConfig, TargetConfig, load_config
from common.schema import Finding, dump_findings
from rules.registry import RuleContext, all_rules, load_builtin_rule_modules


def run_rules(
    *,
    base_index: dict[str, Any],
    head_index: dict[str, Any],
    diff: dict[str, Any],
    changed_files: list[str],
    repo_root: str,
    target: TargetConfig,
) -> list[Finding]:
    load_builtin_rule_modules()
    enabled = {name for name, c in target.dimensions.items() if c.enabled}
    ctx = RuleContext(
        base_index=base_index,
        head_index=head_index,
        diff=diff,
        changed_files=changed_files,
        repo_root=repo_root,
        target_name=target.name,
        enabled_dimensions=enabled,
    )
    findings: list[Finding] = []
    for rid, dim, fn in all_rules():
        if dim not in enabled:
            continue
        try:
            for f in fn(ctx):
                findings.append(f)
        except Exception as e:  # noqa: BLE001 - design: warn and continue
            print(f"warn: rule {rid} failed: {e}", file=sys.stderr)
    return findings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo-root", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--base-index", required=True)
    p.add_argument("--head-index", required=True)
    p.add_argument("--diff", required=True)
    p.add_argument("--changed-files", default="")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    cfg: AuditConfig = load_config(args.config)
    tgt = cfg.target_by_name(args.target)
    if tgt is None:
        print(f"error: target {args.target!r} not in config", file=sys.stderr)
        return 2

    with open(args.base_index, encoding="utf-8") as fp:
        base = json.load(fp)
    with open(args.head_index, encoding="utf-8") as fp:
        head = json.load(fp)
    with open(args.diff, encoding="utf-8") as fp:
        diff = json.load(fp)

    changed: list[str] = []
    if args.changed_files and os.path.exists(args.changed_files):
        with open(args.changed_files, encoding="utf-8") as fp:
            changed = [line.strip() for line in fp if line.strip()]
    elif args.changed_files:
        changed = [s for s in args.changed_files.split(",") if s]

    findings = run_rules(
        base_index=base,
        head_index=head,
        diff=diff,
        changed_files=changed,
        repo_root=args.repo_root,
        target=tgt,
    )
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    dump_findings(findings, args.out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
