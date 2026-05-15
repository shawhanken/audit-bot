"""Resource-level diff between base and head index.json.

Design ref: §4.2, §6.1. Output schema:

    {
      "target": "...",
      "base_ref": "...",
      "head_ref": "...",
      "changed_files": ["..."],
      "by_kind": {
        "opcodes":   {"added": [...], "removed": [...], "modified": [...]},
        "addresses": {...},
        ...
      }
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


_KEY_BY_KIND: dict[str, tuple[str, ...]] = {
    "opcodes": ("id",),
    "addresses": ("id",),
    "errors": ("code",),
    "cips": ("id",),
    "xrefs": ("from", "to"),
    "terms": ("term",),
    "constants": ("name",),
    "code_symbols_referenced": ("symbol",),
}


def _key(item: dict[str, Any], fields: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(item.get(f) for f in fields)


def diff_kind(
    base_items: list[dict[str, Any]],
    head_items: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> dict[str, list[dict[str, Any]]]:
    base_map = {_key(i, fields): i for i in base_items}
    head_map = {_key(i, fields): i for i in head_items}

    added = [head_map[k] for k in head_map.keys() - base_map.keys()]
    removed = [base_map[k] for k in base_map.keys() - head_map.keys()]
    modified: list[dict[str, Any]] = []
    for k in base_map.keys() & head_map.keys():
        b = base_map[k]
        h = head_map[k]
        # Compare ignoring file/line location-only changes.
        b_sig = {kk: vv for kk, vv in b.items() if kk not in ("file", "line")}
        h_sig = {kk: vv for kk, vv in h.items() if kk not in ("file", "line")}
        if b_sig != h_sig:
            modified.append({"before": b, "after": h})
    return {"added": added, "removed": removed, "modified": modified}


def compute_diff(
    base_index: dict[str, Any],
    head_index: dict[str, Any],
    changed_files: list[str],
) -> dict[str, Any]:
    by_kind: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for kind, fields in _KEY_BY_KIND.items():
        by_kind[kind] = diff_kind(
            base_index.get(kind, []) or [],
            head_index.get(kind, []) or [],
            fields,
        )
    return {
        "schema_version": "1",
        "target": head_index.get("target") or base_index.get("target"),
        "base_ref": base_index.get("ref"),
        "head_ref": head_index.get("ref"),
        "changed_files": list(changed_files),
        "by_kind": by_kind,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--changed-files", default="",
                   help="newline-separated list of changed files (file path or '-')")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    with open(args.base, encoding="utf-8") as fp:
        base = json.load(fp)
    with open(args.head, encoding="utf-8") as fp:
        head = json.load(fp)

    changed: list[str] = []
    if args.changed_files == "-":
        changed = [line.strip() for line in sys.stdin if line.strip()]
    elif args.changed_files:
        if os.path.exists(args.changed_files):
            with open(args.changed_files, encoding="utf-8") as fp:
                changed = [line.strip() for line in fp if line.strip()]
        else:
            changed = [s for s in args.changed_files.split(",") if s]

    out = compute_diff(base, head, changed)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
