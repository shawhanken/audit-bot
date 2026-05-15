"""Lightweight in-memory code symbol index.

Design ref: §6.2 Code Index. The full design uses tree-sitter + Postgres
for cross-PR persistence. For the skeleton we keep an in-memory index built
on-demand from the `related_code_excerpts` the Action uploads, so the server
keeps its 'no GitHub credentials' boundary.

This is intentionally regex-based: real implementation would use
tree-sitter (Rust/Python/TS/Go), but for v1 a regex pass covers the
demo path (symbol existence, constant value, file path checks).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_RUST_FN_RE = re.compile(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_RUST_STRUCT_RE = re.compile(r"\b(?:struct|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_RUST_CONST_RE = re.compile(
    r"\bconst\s+([A-Z][A-Z0-9_]*)\s*:\s*[^=]+=\s*([^;]+);"
)
_PY_DEF_RE = re.compile(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_PY_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_PY_CONST_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]+)\s*=\s*([^\n#]+)", re.MULTILINE)


@dataclass
class Symbol:
    name: str
    kind: str  # "fn" | "struct" | "const" | "class" | "def"
    file: str
    line: int
    value: str | None = None


class CodeIndex:
    """Build symbol index lazily from two sources:
      * `excerpts` — files the action uploaded in the request (small)
      * `mirror_files` — files the server-side RepoMirror produced (potentially
        much larger; one repo's worth of code).

    Both flow through the same regex scanners. When a symbol appears in both
    sources, the mirror version wins (it's the canonical code, not the
    excerpt the action happened to grab).
    """

    def __init__(
        self,
        excerpts: dict[str, str],
        mirror_files: list | None = None,
    ):
        self.excerpts = excerpts
        # Make file lookup uniform by giving mirror files a path of the
        # form "<mirror>/<rel_path>".
        self._mirror_files: dict[str, str] = {}
        for mf in mirror_files or []:
            self._mirror_files[f"{mf.mirror}/{mf.rel_path}"] = mf.content
        self._symbols: dict[str, list[Symbol]] = {}
        self._built = False

    def _build(self) -> None:
        if self._built:
            return
        # Scan excerpts first, mirrors second — so mirrors override on
        # `get_symbol` (we keep the first hit, then mirrors append).
        for path, content in self.excerpts.items():
            self._scan_one(path, content)
        for path, content in self._mirror_files.items():
            self._scan_one(path, content)
        self._built = True

    def _scan_one(self, path: str, content: str) -> None:
        if path.endswith(".rs"):
            self._scan_rust(path, content)
        elif path.endswith(".py"):
            self._scan_python(path, content)
        else:
            # Treat as opaque text — only `xxx = N` constants are useful.
            self._scan_python(path, content)

    def _scan_rust(self, path: str, content: str) -> None:
        for m in _RUST_FN_RE.finditer(content):
            self._add(Symbol(m.group(1), "fn", path, _line_of(content, m.start())))
        for m in _RUST_STRUCT_RE.finditer(content):
            self._add(Symbol(m.group(1), "struct", path, _line_of(content, m.start())))
        for m in _RUST_CONST_RE.finditer(content):
            self._add(Symbol(
                m.group(1), "const", path, _line_of(content, m.start()),
                value=m.group(2).strip(),
            ))

    def _scan_python(self, path: str, content: str) -> None:
        for m in _PY_DEF_RE.finditer(content):
            self._add(Symbol(m.group(1), "def", path, _line_of(content, m.start())))
        for m in _PY_CLASS_RE.finditer(content):
            self._add(Symbol(m.group(1), "class", path, _line_of(content, m.start())))
        for m in _PY_CONST_RE.finditer(content):
            self._add(Symbol(
                m.group(1), "const", path, _line_of(content, m.start()),
                value=m.group(2).strip(),
            ))

    def _add(self, sym: Symbol) -> None:
        self._symbols.setdefault(sym.name, []).append(sym)

    # --- query interface ----------------------------------------------------

    def get_symbol(self, name: str) -> Symbol | None:
        self._build()
        return next(iter(self._symbols.get(name, [])), None)

    def get_constant_value(self, name: str) -> str | None:
        s = self.get_symbol(name)
        return s.value if s and s.kind == "const" else None

    def file_exists(self, path: str) -> bool:
        return path in self.excerpts or path in self._mirror_files

    def all_symbols(self) -> dict[str, list[Symbol]]:
        self._build()
        return self._symbols


def _line_of(text: str, offset: int) -> int:
    return text[:offset].count("\n") + 1


def lookup_symbols(index: CodeIndex, symbols: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in symbols:
        # Accept both bare names and `Module::Sub::fn` style.
        leaf = name.split("::")[-1].split(".")[-1]
        sym = index.get_symbol(leaf) or index.get_symbol(name)
        if sym is None:
            if index.file_exists(name):
                out.append({"symbol": name, "found": True, "kind": "path", "file": name})
            else:
                out.append({"symbol": name, "found": False})
        else:
            out.append({
                "symbol": name,
                "found": True,
                "kind": sym.kind,
                "file": sym.file,
                "line": sym.line,
                "value": sym.value,
            })
    return out
