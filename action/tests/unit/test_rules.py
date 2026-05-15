from __future__ import annotations

from common.config import DimensionConfig, TargetConfig
from rules.registry import RuleContext, all_rules, load_builtin_rule_modules


def _make_ctx(**overrides):
    defaults = dict(
        base_index={
            "opcodes": [], "addresses": [], "errors": [],
            "cips": [], "xrefs": [], "terms": [], "code_blocks": [],
            "code_symbols_referenced": [], "constants": [],
            "files_parsed": [],
        },
        head_index={
            "opcodes": [], "addresses": [], "errors": [],
            "cips": [], "xrefs": [], "terms": [], "code_blocks": [],
            "code_symbols_referenced": [], "constants": [],
            "files_parsed": [],
        },
        diff={"by_kind": {
            "opcodes": {"added": [], "removed": [], "modified": []},
            "addresses": {"added": [], "removed": [], "modified": []},
            "errors": {"added": [], "removed": [], "modified": []},
            "cips": {"added": [], "removed": [], "modified": []},
            "xrefs": {"added": [], "removed": [], "modified": []},
            "terms": {"added": [], "removed": [], "modified": []},
            "constants": {"added": [], "removed": [], "modified": []},
            "code_symbols_referenced": {"added": [], "removed": [], "modified": []},
        }},
        changed_files=[],
        repo_root="/tmp",
        target_name="t",
        enabled_dimensions={"consistency", "security", "style"},
    )
    defaults.update(overrides)
    return RuleContext(**defaults)


def setup_module(_module):
    load_builtin_rule_modules()


def _run_rule(rule_id: str, ctx: RuleContext):
    for rid, _, fn in all_rules():
        if rid == rule_id:
            return list(fn(ctx))
    raise AssertionError(f"rule {rule_id} not registered")


def test_r001_opcode_collision_fires_on_added_duplicate():
    base_op = {"id": 0x42, "name": "EXISTING", "file": "wp.md", "line": 100}
    new_op = {"id": 0x42, "name": "NEW", "file": "cip-29.md", "line": 50}
    ctx = _make_ctx(
        base_index={**_make_ctx().base_index, "opcodes": [base_op]},
        head_index={**_make_ctx().head_index, "opcodes": [base_op, new_op]},
        diff={"by_kind": {
            **_make_ctx().diff["by_kind"],
            "opcodes": {"added": [new_op], "removed": [], "modified": []},
        }},
    )
    found = _run_rule("R001_opcode_collision", ctx)
    assert len(found) == 1
    assert found[0].severity == "block"
    assert any(l.file == "cip-29.md" for l in found[0].locations)


def test_r001_opcode_collision_no_fire_when_no_dup():
    new_op = {"id": 0x99, "name": "NEW", "file": "cip-29.md", "line": 50}
    ctx = _make_ctx(
        diff={"by_kind": {
            **_make_ctx().diff["by_kind"],
            "opcodes": {"added": [new_op], "removed": [], "modified": []},
        }},
    )
    assert _run_rule("R001_opcode_collision", ctx) == []


def test_r001_opcode_collision_within_head_when_pr_introduces_duplicate():
    """Fires when the same opcode id exists in two different HEAD files and one
    of them is in changed_files — the resource-level diff would collapse this
    because keys are opcode id only, so we need the within-HEAD pass."""
    existing = {"id": 0x42, "name": "", "file": "refs/cips/cip-5.md", "line": 10}
    pr_added = {"id": 0x42, "name": "", "file": "refs/cips/cip-29.md", "line": 5}
    base_opcodes = [existing]
    head_opcodes = [existing, pr_added]
    ctx = _make_ctx(
        base_index={**_make_ctx().base_index, "opcodes": base_opcodes},
        head_index={**_make_ctx().head_index, "opcodes": head_opcodes},
        # diff.added is empty (key (0x42,) exists in base, so it dedups out).
        diff={"by_kind": {
            **_make_ctx().diff["by_kind"],
            "opcodes": {"added": [], "removed": [], "modified": []},
        }},
        changed_files=["refs/cips/cip-29.md"],
    )
    findings = _run_rule("R001_opcode_collision", ctx)
    assert len(findings) == 1, findings
    files = {l.file for l in findings[0].locations}
    assert files == {"refs/cips/cip-5.md", "refs/cips/cip-29.md"}


def test_r001_opcode_collision_no_fire_when_dup_predates_pr():
    """If both occurrences of an opcode id already exist in base and the PR
    didn't touch either file, this is pre-existing state — don't punish the PR."""
    a = {"id": 0x42, "name": "", "file": "a.md", "line": 1}
    b = {"id": 0x42, "name": "", "file": "b.md", "line": 1}
    ctx = _make_ctx(
        base_index={**_make_ctx().base_index, "opcodes": [a, b]},
        head_index={**_make_ctx().head_index, "opcodes": [a, b]},
        diff={"by_kind": {
            **_make_ctx().diff["by_kind"],
            "opcodes": {"added": [], "removed": [], "modified": []},
        }},
        changed_files=["docs/unrelated.md"],
    )
    assert _run_rule("R001_opcode_collision", ctx) == []


def test_r003_requires_whitepaper_update_when_opcode_changes():
    new_op = {"id": 1, "name": "X", "file": "cip-29.md", "line": 1}
    ctx_missing = _make_ctx(
        diff={"by_kind": {
            **_make_ctx().diff["by_kind"],
            "opcodes": {"added": [new_op], "removed": [], "modified": []},
        }},
        changed_files=["cip-29.md"],
    )
    assert len(_run_rule("R003_opcode_without_wp_update", ctx_missing)) == 1
    ctx_ok = _make_ctx(
        diff=ctx_missing.diff,
        changed_files=["cip-29.md", "whitepaper/wp.md"],
    )
    assert _run_rule("R003_opcode_without_wp_update", ctx_ok) == []


def test_r004_does_not_fire_when_anchor_exists_with_section_prefix():
    """Index stores anchors as bare numbers (`3.1`); xrefs include the § prefix
    (`§3.1`). R004 must normalize before comparing — bug from real-repo run."""
    ctx = _make_ctx(
        head_index={
            **_make_ctx().head_index,
            "cips": [{"id": 5, "title": "T", "status": "Final", "file": "c.md", "anchors": ["3.1", "9.2"]}],
            "xrefs": [{"from": "x", "to": "CIP-5 §3.1", "file": "x.md", "line": 1}],
        },
    )
    assert _run_rule("R004_dangling_xref", ctx) == []


def test_r004_dangling_xref_when_target_cip_missing():
    ctx = _make_ctx(
        head_index={
            **_make_ctx().head_index,
            "cips": [{"id": 5, "title": "T", "status": "Final", "file": "c5.md", "anchors": []}],
            "xrefs": [{"from": "x", "to": "CIP-99 §1.1", "file": "x.md", "line": 1}],
        },
    )
    findings = _run_rule("R004_dangling_xref", ctx)
    assert any(f.title.startswith("Dangling reference") for f in findings)


def test_r005_cip_number_collision():
    ctx = _make_ctx(
        head_index={
            **_make_ctx().head_index,
            "cips": [
                {"id": 7, "title": "A", "status": "Draft", "file": "a.md", "anchors": []},
                {"id": 7, "title": "B", "status": "Draft", "file": "b.md", "anchors": []},
            ],
        },
    )
    assert len(_run_rule("R005_cip_number_collision", ctx)) == 1


def test_r005_ignores_language_variants_of_same_cip():
    """`cip-28-foo.md` and `cip-28-foo-zh.md` are translations, not collisions."""
    ctx = _make_ctx(
        head_index={
            **_make_ctx().head_index,
            "cips": [
                {"id": 28, "title": "A", "status": "Draft", "file": "cips/cip-28-banking.md", "anchors": []},
                {"id": 28, "title": "A", "status": "Draft", "file": "cips/cip-28-banking-zh.md", "anchors": []},
            ],
        },
    )
    assert _run_rule("R005_cip_number_collision", ctx) == []


def test_r006_status_regression():
    base = {"id": 5, "title": "T", "status": "Final", "file": "x.md", "anchors": []}
    head = {**base, "status": "Draft"}
    ctx = _make_ctx(
        base_index={**_make_ctx().base_index, "cips": [base]},
        head_index={**_make_ctx().head_index, "cips": [head]},
    )
    assert len(_run_rule("R006_status_regression", ctx)) == 1


def test_r009_constant_value_mismatch():
    ctx = _make_ctx(
        head_index={
            **_make_ctx().head_index,
            "constants": [
                {"name": "X", "value": "1", "file": "a.md", "line": 1},
                {"name": "X", "value": "2", "file": "b.md", "line": 1},
            ],
        },
    )
    assert len(_run_rule("R009_constant_value_mismatch", ctx)) == 1


def test_security_secret_block_in_code_block():
    cb = {
        "file": "doc.md",
        "line_start": 10,
        "line_end": 12,
        "lang": "bash",
        "content": "export KEY=AKIA1234567890ABCDEF\n",
    }
    ctx = _make_ctx(
        head_index={**_make_ctx().head_index, "code_blocks": [cb]},
    )
    findings = _run_rule("S0xx_secret_in_doc", ctx)
    assert findings and findings[0].severity == "block"


def test_security_dangerous_command_curl_pipe_bash():
    cb = {
        "file": "doc.md",
        "line_start": 10,
        "line_end": 12,
        "lang": "bash",
        "content": "curl https://example.com/install.sh | sudo bash\n",
    }
    ctx = _make_ctx(
        head_index={**_make_ctx().head_index, "code_blocks": [cb]},
    )
    findings = _run_rule("S0xx_dangerous_command_in_doc", ctx)
    assert findings and findings[0].severity == "warn"
