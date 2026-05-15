"""Read-only mirror of sibling source repos.

The server is colocated with `~/workspace/{node, runner, cbfs, ...}` so it
can audit CIPs against current code without pulling code excerpts into
every HTTP request. The operator keeps mirrors fresh via `bin/pull-mirrors.sh`
(or just `git -C <mirror> pull`); RepoMirror is a thin read accessor.

Constraints:
  * **read-only**: never writes, never modifies the working tree, never
    touches the staging area.
  * **path-safe**: callers can only resolve paths *inside* the configured
    mirror root. Any `..` or absolute-path escape is rejected.
  * **size-bounded**: per-file and per-request total caps so a buggy
    technical-agent prompt can't pull 500MB into RAM.

Mirror discovery is plain-filesystem: any directory directly under
`MIRROR_ROOT` whose name appears in `related_code_mirrors` is mirrored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable


# Conservative caps. These match the kind of context a technical-feasibility
# agent actually benefits from — the LLM is going to summarize, not memorize.
MAX_FILE_BYTES = 64_000
MAX_TOTAL_BYTES = 4_000_000
ALLOWED_SUFFIXES = (".rs", ".py", ".ts", ".tsx", ".go", ".js", ".sol", ".sql")


@dataclass
class MirrorFile:
    mirror: str           # e.g. "node"
    rel_path: str         # path relative to the mirror root
    content: str          # truncated to MAX_FILE_BYTES


class RepoMirror:
    """Resolve and read files from named source-repo mirrors.

    `root` is the parent directory holding all mirrors as immediate
    subdirectories (default: `$HOME/workspace`). `available` is the subset
    of subdirectory names the operator allows audits to read from — typically
    set via env / config to avoid an accidental directory enumeration of the
    entire workspace.
    """

    def __init__(self, root: str, available: Iterable[str]):
        self.root = os.path.abspath(root)
        self.available: set[str] = set(available)

    def has(self, name: str) -> bool:
        if name not in self.available:
            return False
        return os.path.isdir(os.path.join(self.root, name))

    def _resolve(self, mirror: str, rel: str) -> str | None:
        """Return absolute path iff `rel` is inside the named mirror; else None.

        Guards against `..` traversal and absolute-path requests.
        """
        if not self.has(mirror):
            return None
        if os.path.isabs(rel):
            return None
        base = os.path.join(self.root, mirror)
        candidate = os.path.abspath(os.path.join(base, rel))
        if not candidate.startswith(base + os.sep) and candidate != base:
            return None
        return candidate

    def read(self, mirror: str, rel: str) -> str | None:
        full = self._resolve(mirror, rel)
        if full is None or not os.path.isfile(full):
            return None
        try:
            with open(full, "rb") as fp:
                raw = fp.read(MAX_FILE_BYTES + 1)
        except OSError:
            return None
        try:
            text = raw.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return None
        if len(raw) > MAX_FILE_BYTES:
            text = text[:MAX_FILE_BYTES] + "\n... (truncated)"
        return text

    def iter_files(self, mirror: str, *, suffixes: tuple[str, ...] = ALLOWED_SUFFIXES) -> list[MirrorFile]:
        """Walk `mirror` returning code files up to MAX_TOTAL_BYTES."""
        out: list[MirrorFile] = []
        total = 0
        base = self._resolve(mirror, "")
        if base is None or not os.path.isdir(base):
            return out
        for dirpath, dirnames, filenames in os.walk(base):
            # Skip hidden/version-control + build dirs.
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".")
                and d not in ("target", "node_modules", "__pycache__", "dist", "build")
            ]
            for fn in filenames:
                if not fn.endswith(suffixes):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base)
                try:
                    with open(full, "rb") as fp:
                        raw = fp.read(MAX_FILE_BYTES + 1)
                except OSError:
                    continue
                try:
                    text = raw.decode("utf-8", errors="replace")
                except UnicodeDecodeError:
                    continue
                if len(raw) > MAX_FILE_BYTES:
                    text = text[:MAX_FILE_BYTES] + "\n... (truncated)"
                out.append(MirrorFile(mirror=mirror, rel_path=rel, content=text))
                total += len(text)
                if total >= MAX_TOTAL_BYTES:
                    return out
        return out


def default_mirror(allowed: Iterable[str]) -> RepoMirror:
    """Build a RepoMirror from env: DOC_AUDIT_MIRROR_ROOT or ~/workspace."""
    root = os.environ.get("DOC_AUDIT_MIRROR_ROOT") or os.path.expanduser("~/workspace")
    return RepoMirror(root=root, available=allowed)
