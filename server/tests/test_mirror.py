"""RepoMirror correctness + security tests."""

from __future__ import annotations

import os

from app.mirror import RepoMirror, default_mirror, MAX_FILE_BYTES


def _setup_workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "node" / "src").mkdir(parents=True)
    (root / "node" / "src" / "lib.rs").write_text(
        "pub fn set_basefee(x: u64) {}\nconst N: u64 = 10;\n"
    )
    (root / "runner" / "app.py").mkdir(parents=False, exist_ok=True) if False else None
    (root / "runner").mkdir(exist_ok=True)
    (root / "runner" / "app.py").write_text("def run_job(): pass\nMAX = 64\n")
    (root / "secrets").mkdir()
    (root / "secrets" / "key.txt").write_text("nuclear codes")
    return root


def test_has_only_returns_allowed_dirs(tmp_path):
    root = _setup_workspace(tmp_path)
    mirror = RepoMirror(root=str(root), available={"node", "runner"})
    assert mirror.has("node")
    assert mirror.has("runner")
    # Not in allowlist even though dir exists
    assert not mirror.has("secrets")
    # Not on disk
    assert not mirror.has("missing")


def test_read_resolves_inside_mirror(tmp_path):
    root = _setup_workspace(tmp_path)
    mirror = RepoMirror(root=str(root), available={"node"})
    txt = mirror.read("node", "src/lib.rs")
    assert txt is not None and "set_basefee" in txt


def test_read_rejects_path_traversal(tmp_path):
    root = _setup_workspace(tmp_path)
    mirror = RepoMirror(root=str(root), available={"node"})
    # Try to escape via ..
    assert mirror.read("node", "../secrets/key.txt") is None
    # Try absolute path
    assert mirror.read("node", "/etc/passwd") is None


def test_read_rejects_disallowed_mirror(tmp_path):
    """Even though `secrets/` exists, it's not in `available` → no read."""
    root = _setup_workspace(tmp_path)
    mirror = RepoMirror(root=str(root), available={"node"})
    assert mirror.read("secrets", "key.txt") is None


def test_iter_files_picks_only_code_suffixes(tmp_path):
    root = _setup_workspace(tmp_path)
    # Drop a non-code file in node/ to confirm it's filtered.
    (root / "node" / "README.md").write_text("not code")
    mirror = RepoMirror(root=str(root), available={"node"})
    files = mirror.iter_files("node")
    paths = sorted(f.rel_path for f in files)
    assert paths == ["src/lib.rs"]


def test_iter_files_skips_build_dirs(tmp_path):
    root = _setup_workspace(tmp_path)
    target_dir = root / "node" / "target" / "debug"
    target_dir.mkdir(parents=True)
    (target_dir / "huge.rs").write_text("// generated")
    mirror = RepoMirror(root=str(root), available={"node"})
    files = mirror.iter_files("node")
    assert not any("target/" in f.rel_path for f in files)


def test_read_truncates_oversize_file(tmp_path):
    root = tmp_path / "ws"
    (root / "node").mkdir(parents=True)
    big = "x" * (MAX_FILE_BYTES + 1000)
    (root / "node" / "huge.rs").write_text(big)
    mirror = RepoMirror(root=str(root), available={"node"})
    txt = mirror.read("node", "huge.rs")
    assert txt is not None
    assert "(truncated)" in txt
    assert len(txt) <= MAX_FILE_BYTES + 50


def test_default_mirror_uses_env(monkeypatch, tmp_path):
    root = _setup_workspace(tmp_path)
    monkeypatch.setenv("DOC_AUDIT_MIRROR_ROOT", str(root))
    m = default_mirror({"node"})
    assert m.has("node")
