from __future__ import annotations

import aggregate
from common.schema import Finding, Location


def _f(rule_id, file, line, dim="consistency", sev="warn", source="semantic"):
    return Finding(
        rule_id=rule_id,
        source=source,
        dimension=dim,
        severity=sev,
        title=rule_id,
        locations=[Location(file=file, line_start=line)],
        message=rule_id,
    )


def test_invalid_locations_are_dropped(tmp_path):
    (tmp_path / "real.md").write_text("a\nb\nc\n")

    rules_findings = [_f("R001", "real.md", 1, sev="block", source="rules")]
    semantic = {
        "consistency": [
            _f("L1", "real.md", 2),
            _f("L2", "real.md", 99),  # out of range, drop
            _f("L3", "missing.md", 1),  # file absent, drop
        ]
    }
    res = aggregate.aggregate(
        rules_findings=rules_findings,
        semantic_by_dim=semantic,
        repo_root=str(tmp_path),
    )
    ids = [f.rule_id for f in res["findings"]]
    assert "R001" in ids
    assert "L1" in ids
    assert "L2" not in ids
    assert "L3" not in ids


def test_high_drop_rate_marks_low_confidence(tmp_path):
    (tmp_path / "a.md").write_text("x\n")
    semantic = {
        "security": [
            _f("ok", "a.md", 1, dim="security"),
            _f("bad1", "missing.md", 1, dim="security"),
            _f("bad2", "missing.md", 1, dim="security"),
            _f("bad3", "missing.md", 1, dim="security"),
        ]
    }
    res = aggregate.aggregate(
        rules_findings=[],
        semantic_by_dim=semantic,
        repo_root=str(tmp_path),
    )
    assert res["dimension_quality"].get("security") == "low_confidence"
    kept = [f for f in res["findings"] if f.dimension == "security"]
    assert kept and kept[0].confidence <= 0.5


def test_ignore_list_drops_matching_findings(tmp_path):
    (tmp_path / "a.md").write_text("x\n")
    rules_findings = [
        _f("R", "a.md", 1, sev="block", source="rules"),
    ]
    target_id = rules_findings[0].finding_id
    res = aggregate.aggregate(
        rules_findings=rules_findings,
        semantic_by_dim={},
        repo_root=str(tmp_path),
        ignored_ids={target_id},
    )
    assert res["findings"] == []
    assert res["ignored_count"] == 1


def test_load_ignore_list_skips_comments_and_blanks(tmp_path):
    (tmp_path / ".doc-audit-ignore").write_text(
        "# this is a comment\n\n  abc123  trailing-comment-ok\n# another\n"
        "deadbeef\n"
    )
    ids = aggregate.load_ignore_list(str(tmp_path), ".doc-audit-ignore")
    assert ids == {"abc123", "deadbeef"}


def test_load_ignore_list_missing_file_returns_empty(tmp_path):
    assert aggregate.load_ignore_list(str(tmp_path), ".doc-audit-ignore") == set()


def test_cross_dim_dedup_keeps_highest_severity(tmp_path):
    (tmp_path / "a.md").write_text("x\n")
    rules_findings = [_f("R", "a.md", 1, sev="block", source="rules")]
    semantic = {
        "style": [_f("S", "a.md", 1, dim="style", sev="warn")],
    }
    res = aggregate.aggregate(
        rules_findings=rules_findings,
        semantic_by_dim=semantic,
        repo_root=str(tmp_path),
    )
    primary = [f for f in res["findings"] if f.rule_id == "R"]
    assert primary and primary[0].severity == "block"
    assert any("S" not in f.rule_id for f in res["findings"])
    # The warn finding is collapsed into related_findings, not present standalone.
    assert all(f.rule_id != "S" for f in res["findings"])
