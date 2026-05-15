from __future__ import annotations

import diff


def test_diff_adds_removes_modifies():
    base = {
        "ref": "base",
        "opcodes": [{"id": 1, "name": "A", "file": "a.md", "line": 1}],
        "addresses": [],
        "errors": [],
        "cips": [{"id": 5, "title": "Old", "status": "Draft", "file": "c.md", "anchors": []}],
        "xrefs": [],
        "terms": [],
        "code_symbols_referenced": [],
        "constants": [],
    }
    head = {
        "ref": "head",
        "opcodes": [
            {"id": 1, "name": "A_renamed", "file": "a.md", "line": 1},
            {"id": 2, "name": "B", "file": "a.md", "line": 5},
        ],
        "addresses": [],
        "errors": [],
        "cips": [],
        "xrefs": [],
        "terms": [],
        "code_symbols_referenced": [],
        "constants": [],
    }
    d = diff.compute_diff(base, head, ["a.md"])
    op_diff = d["by_kind"]["opcodes"]
    assert any(o["id"] == 2 for o in op_diff["added"])
    assert any(m["after"]["name"] == "A_renamed" for m in op_diff["modified"])
    cip_diff = d["by_kind"]["cips"]
    assert any(c["id"] == 5 for c in cip_diff["removed"])
