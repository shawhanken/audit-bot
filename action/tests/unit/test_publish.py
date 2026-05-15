"""Drive publish.sh in DRY_RUN mode and assert the gh calls it would make.

We don't have gh / GITHUB_TOKEN in CI; dry-run writes a JSON log instead,
so we can verify call shapes deterministically.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


_PUBLISH = Path(__file__).resolve().parents[2] / "publish.sh"


def _run_publish(tmp_path, *args, env_extra=None):
    log = tmp_path / "dry.log"
    env = {
        **os.environ,
        "DOC_AUDIT_DRY_RUN": "1",
        "DOC_AUDIT_DRY_RUN_LOG": str(log),
        "GITHUB_REPOSITORY": "octo/repo",
        "GITHUB_HEAD_SHA": "deadbeef",
        "GITHUB_PR_NUMBER": "42",
        "COMMENT_MARKER": "doc-audit-bot",
    }
    if env_extra:
        env.update(env_extra)
    res = subprocess.run(
        ["bash", str(_PUBLISH), *args],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    entries = []
    if log.exists():
        for line in log.read_text().splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries, res.stderr


def test_publish_dry_run_emits_sticky_comment_and_check_runs(tmp_path):
    report_md = tmp_path / "report.md"
    report_md.write_text("<!-- doc-audit-bot -->\n## sample\nbody\n")
    check_runs = tmp_path / "checks.json"
    check_runs.write_text(json.dumps([
        {"name": "Doc Audit / Consistency", "conclusion": "failure",
         "summary": "1 finding", "annotations": []},
        {"name": "Doc Audit / Security", "conclusion": "success",
         "summary": "0 findings", "annotations": []},
    ]))

    entries, _stderr = _run_publish(
        tmp_path, "cips", str(report_md), str(check_runs)
    )

    ops = [e["op"] for e in entries]
    assert "sticky_comment" in ops
    # Marker must be per-target so multiple targets don't overwrite one
    # another's sticky comments in the same PR.
    sticky = next(e for e in entries if e["op"] == "sticky_comment")
    assert sticky["marker"] == "doc-audit-bot:cips"
    check_run_entries = [e for e in entries if e["op"] == "check_run"]
    names = [e["payload"]["name"] for e in check_run_entries]
    assert "Doc Audit / Consistency" in names and "Doc Audit / Security" in names
    a_consistency = next(e for e in check_run_entries if "Consistency" in e["payload"]["name"])
    assert a_consistency["payload"]["head_sha"] == "deadbeef"
    assert a_consistency["payload"]["conclusion"] == "failure"


def test_publish_skips_when_repo_unset(tmp_path):
    report_md = tmp_path / "report.md"
    report_md.write_text("body")
    check_runs = tmp_path / "checks.json"
    check_runs.write_text("[]")
    entries, stderr = _run_publish(
        tmp_path, "cips", str(report_md), str(check_runs),
        env_extra={"GITHUB_REPOSITORY": ""},
    )
    assert entries == []
    assert "GITHUB_REPOSITORY not set" in stderr


def test_publish_skips_check_runs_when_no_head_sha(tmp_path):
    report_md = tmp_path / "report.md"
    report_md.write_text("body")
    check_runs = tmp_path / "checks.json"
    check_runs.write_text(json.dumps([{"name": "x", "conclusion": "success", "summary": "", "annotations": []}]))
    entries, stderr = _run_publish(
        tmp_path, "cips", str(report_md), str(check_runs),
        env_extra={"GITHUB_HEAD_SHA": ""},
    )
    # Sticky comment still attempted; check runs skipped.
    assert any(e["op"] == "sticky_comment" for e in entries)
    assert all(e["op"] != "check_run" for e in entries)
    assert "no head SHA" in stderr


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq required")
def test_jq_is_available_for_dry_log():
    """publish.sh uses jq to build the dry-run log entry for sticky comment."""
    assert shutil.which("jq") is not None
