"""Merge rules + per-dimension semantic findings, validate, dedup, sort.

Design ref: §6.1 aggregate.py.

Key behaviours:
  1. Validate every semantic finding's `locations[].file` exists in the head
     working tree and `line_start` ≤ file line count (§3.3).
  2. If >30% of a dimension's semantic findings fail validation, that whole
     dimension is downgraded to low-confidence (§4.5).
  3. Cross-dimension dedup: same `(file, line_start)` hit by multiple
     dimensions → keep highest-severity finding, others appear in
     `related_findings`.
  4. Sort by (severity rank, file, line).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Any

from common.schema import Finding, dump_findings, finding_from_json


_SEVERITY_RANK = {"block": 0, "warn": 1, "info": 2}


def _file_line_count(repo_root: str, rel: str) -> int | None:
    full = os.path.join(repo_root, rel)
    if not os.path.exists(full):
        return None
    try:
        with open(full, encoding="utf-8") as fp:
            return sum(1 for _ in fp)
    except OSError:
        return None


def validate_finding(f: Finding, repo_root: str) -> bool:
    if not f.locations:
        return False
    for loc in f.locations:
        n = _file_line_count(repo_root, loc.file)
        if n is None:
            return False
        if loc.line_start < 1 or loc.line_start > n:
            return False
    return True


def load_ignore_list(repo_root: str, ignore_file: str) -> set[str]:
    """Read `.doc-audit-ignore` — one finding_id per line; lines starting with
    `#` are comments, blank lines are skipped (design §4.2 + §6.1).

    Returns an empty set if the file is missing.
    """
    full = os.path.join(repo_root, ignore_file)
    if not os.path.exists(full):
        return set()
    out: set[str] = set()
    with open(full, encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Allow trailing comments after whitespace.
            tok = line.split(None, 1)[0]
            out.add(tok)
    return out


def aggregate(
    *,
    rules_findings: list[Finding],
    semantic_by_dim: dict[str, list[Finding]],
    repo_root: str,
    drop_threshold: float = 0.30,
    ignored_ids: set[str] | None = None,
) -> dict[str, Any]:
    accepted: list[Finding] = list(rules_findings)
    dimension_quality: dict[str, str] = {}
    ignored_ids = ignored_ids or set()
    ignored_count = 0

    for dim, items in semantic_by_dim.items():
        kept: list[Finding] = []
        dropped = 0
        for f in items:
            if validate_finding(f, repo_root):
                kept.append(f)
            else:
                dropped += 1
        total = len(items)
        if total > 0 and dropped / total > drop_threshold:
            dimension_quality[dim] = "low_confidence"
            for k in kept:
                k.confidence = min(k.confidence, 0.5)
                k.agent_meta = {**k.agent_meta, "low_confidence_pass": True}
        accepted.extend(kept)

    # Cross-dim dedup by (file, line_start)
    by_key: dict[tuple[str, int], list[Finding]] = defaultdict(list)
    for f in accepted:
        if f.locations:
            by_key[(f.locations[0].file, f.locations[0].line_start)].append(f)
    deduped: list[Finding] = []
    seen_ids: set[str] = set()
    for findings in by_key.values():
        findings.sort(key=lambda x: _SEVERITY_RANK.get(x.severity, 99))
        primary = findings[0]
        primary.related_findings = [
            other.finding_id for other in findings[1:] if other.finding_id != primary.finding_id
        ]
        if primary.finding_id not in seen_ids:
            deduped.append(primary)
            seen_ids.add(primary.finding_id)

    # Apply the ignore list AFTER dedup so that a finding ignored by id still
    # masks its dedup-merged "related_findings" cluster.
    if ignored_ids:
        kept: list[Finding] = []
        for f in deduped:
            if f.finding_id in ignored_ids:
                ignored_count += 1
                continue
            kept.append(f)
        deduped = kept

    deduped.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(f.severity, 99),
            f.locations[0].file if f.locations else "",
            f.locations[0].line_start if f.locations else 0,
        )
    )
    return {
        "findings": deduped,
        "dimension_quality": dimension_quality,
        "ignored_count": ignored_count,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo-root", required=True)
    p.add_argument("--rules-findings", required=True)
    p.add_argument("--semantic-glob-dir", required=True,
                   help="directory containing findings_semantic_<target>_<dim>.json files")
    p.add_argument("--target", required=True)
    p.add_argument("--ignore-file", default=".doc-audit-ignore",
                   help="path (relative to repo-root) of the ignore-list file")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    with open(args.rules_findings, encoding="utf-8") as fp:
        rules_raw = json.load(fp)
    rules_findings = [finding_from_json(r) for r in rules_raw]

    semantic_by_dim: dict[str, list[Finding]] = {}
    for fn in os.listdir(args.semantic_glob_dir):
        prefix = f"findings_semantic_{args.target}_"
        if not fn.startswith(prefix) or not fn.endswith(".json"):
            continue
        dim = fn[len(prefix):-len(".json")]
        with open(os.path.join(args.semantic_glob_dir, fn), encoding="utf-8") as fp:
            raw = json.load(fp)
        semantic_by_dim[dim] = [finding_from_json(r) for r in raw]

    ignored_ids = load_ignore_list(args.repo_root, args.ignore_file)
    result = aggregate(
        rules_findings=rules_findings,
        semantic_by_dim=semantic_by_dim,
        repo_root=args.repo_root,
        ignored_ids=ignored_ids,
    )
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    dump_findings(result["findings"], args.out)
    meta_path = args.out.replace(".json", "_meta.json")
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump({
            "dimension_quality": result["dimension_quality"],
            "ignored_count": result.get("ignored_count", 0),
            "ignored_ids_loaded": sorted(ignored_ids),
        }, fp, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
