"""Consistency-dimension rules (R001-R009).

Inherits the rule IDs and severity from the CIP Bot design §4.3 and the
doc-audit-service design §5.2.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Iterable

from common.schema import Finding, Location
from rules.registry import RuleContext, rule


_DIM = "consistency"


@rule("R001_opcode_collision", _DIM)
def opcode_collision(ctx: RuleContext) -> Iterable[Finding]:
    """Detect opcode-id collisions.

    Fires in two cases:
      A) `diff.added` contains an opcode whose id already exists in `base`
         (the classic "new opcode collides with existing" case).
      B) `head` contains the same opcode id in multiple distinct files, AND
         at least one of those files is in `changed_files` (i.e. the PR
         introduced the second occurrence). Case B catches collisions that
         (A) misses because the resource-level diff is keyed on opcode id
         and therefore collapses multi-file occurrences of the same id.
    """
    # --- case A: diff.added vs base -------------------------------------------
    added = ctx.diff["by_kind"].get("opcodes", {}).get("added", [])
    base_by_id: dict[int, list[dict]] = defaultdict(list)
    for o in ctx.base_index.get("opcodes", []) or []:
        base_by_id[o["id"]].append(o)
    yielded: set[tuple[int, str, str]] = set()
    for new in added:
        for other in base_by_id.get(new["id"], []):
            key = (new["id"], new["file"], other["file"])
            if key in yielded:
                continue
            yielded.add(key)
            yield _opcode_collision_finding(new, other)

    # --- case B: within-head, PR-introduced ----------------------------------
    head_by_id: dict[int, list[dict]] = defaultdict(list)
    for o in ctx.head_index.get("opcodes", []) or []:
        head_by_id[o["id"]].append(o)
    changed = set(ctx.changed_files)
    for opcode_id, occs in head_by_id.items():
        files = {o["file"] for o in occs}
        if len(files) < 2:
            continue
        if not (files & changed):
            continue  # the collision was already there before this PR
        # Pair every changed-file occurrence with every existing-file occurrence.
        in_pr = [o for o in occs if o["file"] in changed]
        elsewhere = [o for o in occs if o["file"] not in changed]
        for new in in_pr:
            for other in elsewhere:
                key = (opcode_id, new["file"], other["file"])
                if key in yielded:
                    continue
                yielded.add(key)
                yield _opcode_collision_finding(new, other)


def _opcode_collision_finding(new: dict, other: dict) -> Finding:
    return Finding(
        rule_id="R001_opcode_collision",
        source="rules",
        dimension=_DIM,
        severity="block",
        title=f"Opcode 0x{new['id']:02X} collides with existing assignment",
        locations=[
            Location(file=new["file"], line_start=new["line"]),
            Location(file=other["file"], line_start=other["line"]),
        ],
        message=(
            f"HEAD introduces opcode 0x{new['id']:02X} "
            f"({new.get('name') or 'anonymous'}), but "
            f"{other['file']}:{other['line']} already assigns the same opcode "
            f"({other.get('name') or 'anonymous'})."
        ),
        suggestion="Pick the next free slot per the Whitepaper §9.2 opcode registry.",
    )


@rule("R002_address_collision", _DIM)
def address_collision(ctx: RuleContext) -> Iterable[Finding]:
    added = ctx.diff["by_kind"].get("addresses", {}).get("added", [])
    base_by_id: dict[str, dict] = {
        a["id"]: a for a in ctx.base_index.get("addresses", []) or []
    }
    for new in added:
        other = base_by_id.get(new["id"])
        if other:
            yield Finding(
                rule_id="R002_address_collision",
                source="rules",
                dimension=_DIM,
                severity="block",
                title=f"System actor address {new['id']} collides",
                locations=[
                    Location(file=new["file"], line_start=new["line"]),
                    Location(file=other["file"], line_start=other["line"]),
                ],
                message=(
                    f"HEAD assigns {new['id']} to {new.get('name')}, but "
                    f"{other['file']} already assigns it to {other.get('name')}."
                ),
                suggestion="Pick an unassigned address.",
            )


@rule("R003_opcode_without_wp_update", _DIM)
def opcode_without_wp_update(ctx: RuleContext) -> Iterable[Finding]:
    """新增/修改 opcode 时必须同步白皮书 §9.2 所在文件。"""
    opcode_change = (
        ctx.diff["by_kind"].get("opcodes", {}).get("added")
        or ctx.diff["by_kind"].get("opcodes", {}).get("modified")
    )
    if not opcode_change:
        return
    # Heuristic: any changed file under a whitepaper-like path counts.
    whitepaper_touched = any(
        "whitepaper" in cf.lower() or "wp" in os.path.basename(cf).lower()
        for cf in ctx.changed_files
    )
    if whitepaper_touched:
        return
    sample = opcode_change[0]
    sample_loc = sample.get("after") or sample
    yield Finding(
        rule_id="R003_opcode_without_wp_update",
        source="rules",
        dimension=_DIM,
        severity="block",
        title="Opcode changed without updating Whitepaper §9.2",
        locations=[Location(file=sample_loc["file"], line_start=sample_loc["line"])],
        message=(
            "This PR touches the opcode list, but no whitepaper file is in "
            "changed_files. Per Whitepaper §9.2's trailing note, opcode "
            "changes must update the registry in the same PR."
        ),
        suggestion="Update the Whitepaper §9.2 opcode table in the same PR.",
    )


@rule("R004_dangling_xref", _DIM)
def dangling_xref(ctx: RuleContext) -> Iterable[Finding]:
    head_cips = {c["id"]: c for c in ctx.head_index.get("cips", []) or []}
    for x in ctx.head_index.get("xrefs", []) or []:
        target = x.get("to", "")
        if not target.startswith("CIP-"):
            continue
        cip_num_str, *rest = target[4:].split(" ", 1)
        try:
            cip_num = int(cip_num_str)
        except ValueError:
            continue
        cip = head_cips.get(cip_num)
        if cip is None:
            yield Finding(
                rule_id="R004_dangling_xref",
                source="rules",
                dimension=_DIM,
                severity="block",
                title=f"Dangling reference: {target}",
                locations=[Location(file=x["file"], line_start=x["line"])],
                message=f"{x['file']}:{x['line']} references {target}, but that CIP does not exist in HEAD.",
                suggestion="Fix the CIP number or land the missing CIP.",
            )
            continue
        if rest:
            # xref stores anchors as "§9.7"; the index strips the § and stores
            # bare numbers like "9.7". Normalize both sides before comparing.
            anchor = rest[0].strip().lstrip("§").strip()
            existing = {a.lstrip("§").strip() for a in (cip.get("anchors") or [])}
            if anchor and anchor not in existing:
                yield Finding(
                    rule_id="R004_dangling_xref",
                    source="rules",
                    dimension=_DIM,
                    severity="block",
                    title=f"Dangling anchor: {target}",
                    locations=[Location(file=x["file"], line_start=x["line"])],
                    message=f"Anchor §{anchor} in {target} is not defined in CIP-{cip_num}.",
                    suggestion=f"Use an actual section anchor defined in CIP-{cip_num}.",
                )


@rule("R005_cip_number_collision", _DIM)
def cip_number_collision(ctx: RuleContext) -> Iterable[Finding]:
    """Same CIP id claimed by ≥2 files. Language variants (`-zh`, `-en`, `-cn`)
    of the same base file name are treated as one — they're translations,
    not collisions."""
    grouped: dict[int, list[dict]] = defaultdict(list)
    for c in ctx.head_index.get("cips", []) or []:
        grouped[c["id"]].append(c)
    for cip_id, items in grouped.items():
        # Collapse files that differ only in a trailing language tag.
        base_names: set[str] = set()
        for i in items:
            fn = os.path.basename(i["file"])
            base = re.sub(r"[-_](zh|cn|en)(?=\.\w+$)", "", fn, flags=re.IGNORECASE)
            base_names.add(base)
        if len(base_names) <= 1:
            continue
        yield Finding(
            rule_id="R005_cip_number_collision",
            source="rules",
            dimension=_DIM,
            severity="block",
            title=f"CIP number {cip_id} claimed by multiple files",
            locations=[Location(file=i["file"], line_start=1) for i in items],
            message=", ".join(i["file"] for i in items) + f" all claim CIP-{cip_id}.",
            suggestion="Renumber the conflicting CIPs. Translations should share the same base name with `-zh`/`-en` suffix.",
        )


@rule("R006_status_regression", _DIM)
def status_regression(ctx: RuleContext) -> Iterable[Finding]:
    rank = {"Draft": 0, "Review": 1, "LastCall": 2, "Final": 3, "Withdrawn": 4}
    base = {c["id"]: c for c in ctx.base_index.get("cips", []) or []}
    for h in ctx.head_index.get("cips", []) or []:
        b = base.get(h["id"])
        if not b:
            continue
        b_rank = rank.get(b.get("status", ""), -1)
        h_rank = rank.get(h.get("status", ""), -1)
        if b_rank > 0 and h_rank >= 0 and h_rank < b_rank and h.get("status") != "Withdrawn":
            yield Finding(
                rule_id="R006_status_regression",
                source="rules",
                dimension=_DIM,
                severity="block",
                title=f"CIP-{h['id']} status regression: {b['status']} → {h['status']}",
                locations=[Location(file=h["file"], line_start=1)],
                message=f"{h['file']} moves CIP-{h['id']} from {b['status']} back to {h['status']}.",
                suggestion="Only regressing to Withdrawn is allowed; other moves need an explicit rationale in the PR description.",
            )


@rule("R007_terminology_drift", _DIM)
def terminology_drift(ctx: RuleContext) -> Iterable[Finding]:
    """术语在不同文档首次定义不一致（同名 term 出现在多个 definition_file）。"""
    by_term: dict[str, list[dict]] = defaultdict(list)
    for t in ctx.head_index.get("terms", []) or []:
        by_term[t["term"].lower()].append(t)
    for term, defs in by_term.items():
        files = {d["definition_file"] for d in defs}
        if len(files) > 1:
            yield Finding(
                rule_id="R007_terminology_drift",
                source="rules",
                dimension=_DIM,
                severity="warn",
                title=f"Term '{term}' defined in multiple documents",
                locations=[
                    Location(file=d["definition_file"], line_start=d["line"]) for d in defs
                ],
                message=f"Term {term!r} appears as a first definition in {len(files)} documents.",
                suggestion="Define it once in the glossary; have other documents reference it.",
            )


@rule("R008_link_rot", _DIM)
def link_rot(ctx: RuleContext) -> Iterable[Finding]:
    """检查 HEAD 索引中的相对链接是否能在仓库内解析。

    CIP-N 形式的交叉引用由 R004 处理；这里只看 markdown `[text](path)` 形式的
    相对路径链接。绝对 URL 不在此规则范围（留给 style 维度）。
    """
    head_files = {os.path.normpath(f) for f in ctx.head_index.get("files_parsed", [])}
    for rel in ctx.head_index.get("files_parsed", []) or []:
        full = os.path.join(ctx.repo_root, rel)
        try:
            with open(full, encoding="utf-8") as fp:
                text = fp.read()
        except OSError:
            continue
        from common.markdown import extract_links
        for line, _label, url in extract_links(text):
            if "://" in url or url.startswith("#") or url.startswith("mailto:"):
                continue
            target = os.path.normpath(os.path.join(os.path.dirname(rel), url.split("#", 1)[0]))
            if not target or target in head_files:
                continue
            if os.path.exists(os.path.join(ctx.repo_root, target)):
                continue
            yield Finding(
                rule_id="R008_link_rot",
                source="rules",
                dimension=_DIM,
                severity="warn",
                title=f"Broken relative link: {url}",
                locations=[Location(file=rel, line_start=line)],
                message=f"{rel}:{line} points to relative path {url}, which does not exist.",
                suggestion="Fix the link or remove it.",
            )


@rule("R009_constant_value_mismatch", _DIM)
def constant_value_mismatch(ctx: RuleContext) -> Iterable[Finding]:
    by_name: dict[str, list[dict]] = defaultdict(list)
    for c in ctx.head_index.get("constants", []) or []:
        by_name[c["name"]].append(c)
    for name, items in by_name.items():
        values = {i["value"] for i in items}
        if len(values) > 1:
            yield Finding(
                rule_id="R009_constant_value_mismatch",
                source="rules",
                dimension=_DIM,
                severity="block",
                title=f"Constant {name} has conflicting values across documents",
                locations=[Location(file=i["file"], line_start=i["line"]) for i in items],
                message=f"{name} appears with values {sorted(values)} in different documents.",
                suggestion="Pick the canonical value and align the other documents.",
            )
