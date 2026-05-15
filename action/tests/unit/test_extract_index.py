from __future__ import annotations

import os

import extract_index


def test_extracts_opcodes_addresses_terms_codeblocks(tmp_path):
    docs = tmp_path / "docs" / "cips"
    docs.mkdir(parents=True)
    cip = docs / "cip-29.md"
    cip.write_text(
        """\
# CIP-29: Foo

Status: Draft

This CIP adds opcode 0x42 to the registry. See CIP-5 §3.1 for context.

**fee_payer** — the account paying for the transaction.

```bash
echo hello
rm -rf /tmp/foo
```

- BLOCK_CYCLES_TARGET = 10000000

`module::sub::function` is the entry point.
""",
        encoding="utf-8",
    )

    idx = extract_index.build_index(
        repo_root=str(tmp_path),
        target_name="cips",
        target_paths=["docs/cips/**"],
        git_ref="HEAD",
    )

    assert idx["target"] == "cips"
    assert any(o["id"] == 0x42 for o in idx["opcodes"])
    cip_ids = [c["id"] for c in idx["cips"]]
    assert 29 in cip_ids
    assert any(t["term"].lower() == "fee_payer" for t in idx["terms"])
    assert any(cb["lang"] == "bash" for cb in idx["code_blocks"])
    assert any(c["name"] == "BLOCK_CYCLES_TARGET" for c in idx["constants"])
    assert any("module::sub::function" in s["symbol"] for s in idx["code_symbols_referenced"])


def test_handles_malformed_files_without_crashing(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "cip-1.md").write_bytes(b"\xff\xfe not utf-8")
    (docs / "cip-7.md").write_text("# CIP-7: Test\nStatus: Final\n", encoding="utf-8")

    idx = extract_index.build_index(
        repo_root=str(tmp_path),
        target_name="t",
        target_paths=["docs/**"],
    )
    cip_ids = {c["id"] for c in idx["cips"]}
    assert 7 in cip_ids
