"""Extract a structured index from a target's markdown corpus.

Design ref: §4.1, §6.1, §7.2.

Per-target output schema (extends §5.2 from the CIP Bot design):

    {
      "ref": "<git ref>",
      "target": "<target name>",
      "opcodes":   [...],
      "addresses": [...],
      "errors":    [...],
      "cips":      [...],
      "xrefs":     [...],
      "terms":     [...],
      "code_blocks":              [...],   # used by security dimension
      "code_symbols_referenced":  [...],   # used by technical dimension
      "constants":               [...]     # used by R009 constant_value_mismatch
    }

We intentionally degrade gracefully: a parse error in any single file is
logged and skipped, never fatal (design §6).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import traceback
from typing import Any

from common import markdown as md


# --- Resource patterns --------------------------------------------------------

OPCODE_RE = re.compile(
    r"\bopcode\s+0x([0-9A-Fa-f]{1,4})\b|\bSYS_([A-Z_0-9]+)\s*=\s*0x([0-9A-Fa-f]{1,4})\b"
)
ADDRESS_RE = re.compile(r"\b(0x[0-9A-Fa-f]{2,40})\s*[:\-]\s*([A-Za-z_][A-Za-z0-9_-]+)")
ERROR_RE = re.compile(r"\b(ERR_[A-Z][A-Z0-9_]+)\b")
CIP_FRONTMATTER_RE = re.compile(
    r"^CIP[-\s]?(\d+)\s*[:\-]\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
STATUS_RE = re.compile(
    r"^\s*\*?\*?status\*?\*?\s*[:\-]\s*([A-Za-z]+)", re.MULTILINE | re.IGNORECASE
)
XREF_RE = re.compile(r"CIP-(\d+)\s*(§\d+(?:\.\d+)*)?")
CONSTANT_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]{2,})\s*=\s*([0-9]+(?:_[0-9]+)*|0x[0-9A-Fa-f]+)\b"
)
# Rust/Python/Go-ish dotted or scoped symbol referenced in docs as backticked code.
SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:::|\.)[A-Za-z_][A-Za-z0-9_:.]*)`")
PATH_RE = re.compile(r"`((?:[\w.\-]+/)+[\w.\-]+)`")
TERM_DEF_RE = re.compile(r"\*\*([A-Za-z][A-Za-z0-9 _-]{2,30})\*\*\s*[—\-:]")


def _list_target_files(target_paths: list[str], repo_root: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for pattern in target_paths:
        full = os.path.join(repo_root, pattern)
        for path in glob.glob(full, recursive=True):
            if not path.endswith((".md", ".mdx")) or not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, repo_root)
            if rel in seen:
                continue
            seen.add(rel)
            out.append(rel)
    return sorted(out)


def _safe_read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fp:
            return fp.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"warn: cannot read {path}: {e}", file=sys.stderr)
        return ""


def _parse_file(rel_path: str, text: str) -> dict[str, list[dict[str, Any]]]:
    """Extract every resource from one file. Resilient to malformed sections."""
    out: dict[str, list[dict[str, Any]]] = {
        "opcodes": [],
        "addresses": [],
        "errors": [],
        "cips": [],
        "xrefs": [],
        "terms": [],
        "code_blocks": [],
        "code_symbols_referenced": [],
        "constants": [],
    }
    if not text:
        return out

    lines = text.splitlines()

    # CIP header (filename-based heuristic + first-heading scan)
    name_match = re.match(r"cip-(\d+)", os.path.basename(rel_path), re.IGNORECASE)
    if name_match:
        cip_id = int(name_match.group(1))
        status_m = STATUS_RE.search(text)
        title = ""
        for line in lines[:20]:
            mh = md.HEADING_RE.match(line)
            if mh:
                title = mh.group(2)
                break
        anchors = sorted(set(md.extract_section_anchors(text)))
        out["cips"].append(
            {
                "id": cip_id,
                "title": title,
                "status": status_m.group(1).capitalize() if status_m else "Unknown",
                "file": rel_path,
                "anchors": anchors,
            }
        )

    for i, line in enumerate(lines, start=1):
        for m in OPCODE_RE.finditer(line):
            hex_val = m.group(1) or m.group(3)
            name = m.group(2) or ""
            try:
                opcode_id = int(hex_val, 16)
            except ValueError:
                continue
            out["opcodes"].append(
                {
                    "id": opcode_id,
                    "name": name,
                    "file": rel_path,
                    "line": i,
                    "cip_refs": [],
                }
            )
        for m in ADDRESS_RE.finditer(line):
            out["addresses"].append(
                {"id": m.group(1).lower(), "name": m.group(2), "file": rel_path, "line": i}
            )
        for m in ERROR_RE.finditer(line):
            out["errors"].append({"code": m.group(1), "file": rel_path, "line": i})
        for m in XREF_RE.finditer(line):
            cip_n = m.group(1)
            anchor = m.group(2) or ""
            out["xrefs"].append(
                {
                    "from": rel_path,
                    "to": f"CIP-{cip_n}{(' ' + anchor) if anchor else ''}",
                    "file": rel_path,
                    "line": i,
                }
            )
        for m in CONSTANT_RE.finditer(line):
            out["constants"].append(
                {
                    "name": m.group(1),
                    "value": m.group(2),
                    "file": rel_path,
                    "line": i,
                }
            )
        for m in SYMBOL_RE.finditer(line):
            out["code_symbols_referenced"].append(
                {"symbol": m.group(1), "file": rel_path, "line": i}
            )
        for m in PATH_RE.finditer(line):
            path_val = m.group(1)
            if "/" in path_val and "." in path_val.rsplit("/", 1)[-1]:
                out["code_symbols_referenced"].append(
                    {"symbol": path_val, "kind": "path", "file": rel_path, "line": i}
                )

    # First-definition terms
    for m in TERM_DEF_RE.finditer(text):
        term = m.group(1).strip()
        line_no = text[: m.start()].count("\n") + 1
        out["terms"].append({"term": term, "definition_file": rel_path, "line": line_no})

    # Code blocks (security dimension consumes these)
    for cb in md.extract_code_blocks(rel_path, text):
        out["code_blocks"].append(
            {
                "file": cb.file,
                "line_start": cb.line_start,
                "line_end": cb.line_end,
                "lang": cb.lang,
                "content": cb.content,
            }
        )

    return out


def build_index(
    *,
    repo_root: str,
    target_name: str,
    target_paths: list[str],
    git_ref: str = "HEAD",
) -> dict[str, Any]:
    files = _list_target_files(target_paths, repo_root)
    aggregate: dict[str, list[Any]] = {
        "opcodes": [],
        "addresses": [],
        "errors": [],
        "cips": [],
        "xrefs": [],
        "terms": [],
        "code_blocks": [],
        "code_symbols_referenced": [],
        "constants": [],
    }
    parsed_files: list[str] = []
    for rel in files:
        full = os.path.join(repo_root, rel)
        try:
            txt = _safe_read(full)
            per = _parse_file(rel, txt)
            for k, v in per.items():
                aggregate[k].extend(v)
            parsed_files.append(rel)
        except Exception:  # noqa: BLE001 - design says warn and continue
            print(f"warn: failed to parse {rel}:", file=sys.stderr)
            traceback.print_exc()
            continue

    return {
        "schema_version": "1",
        "ref": git_ref,
        "target": target_name,
        "files_parsed": parsed_files,
        **aggregate,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo-root", required=True)
    p.add_argument("--target-name", required=True)
    p.add_argument("--target-path", action="append", required=True,
                   help="repeat for multiple glob patterns")
    p.add_argument("--ref", default="HEAD")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    idx = build_index(
        repo_root=args.repo_root,
        target_name=args.target_name,
        target_paths=args.target_path,
        git_ref=args.ref,
    )
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(idx, fp, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
