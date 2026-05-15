from __future__ import annotations

from app.code_index import CodeIndex, lookup_symbols


def test_code_index_finds_rust_symbols():
    excerpts = {
        "node/src/lib.rs": (
            "pub fn set_basefee(x: u64) {}\n"
            "const BLOCK_CYCLES_TARGET: u64 = 10_000_000;\n"
            "struct ExecutionEngine;\n"
        ),
    }
    idx = CodeIndex(excerpts)
    sym = idx.get_symbol("set_basefee")
    assert sym and sym.kind == "fn" and sym.file.endswith("lib.rs")
    assert idx.get_constant_value("BLOCK_CYCLES_TARGET") == "10_000_000"
    assert idx.get_symbol("ExecutionEngine").kind == "struct"


def test_code_index_finds_python_symbols():
    excerpts = {
        "x/y.py": "MAX_DEPTH = 32\n\ndef do_thing():\n    pass\n\nclass Foo:\n    pass\n",
    }
    idx = CodeIndex(excerpts)
    assert idx.get_constant_value("MAX_DEPTH") == "32"
    assert idx.get_symbol("do_thing").kind == "def"
    assert idx.get_symbol("Foo").kind == "class"


def test_lookup_symbols_marks_missing():
    idx = CodeIndex({"f.rs": "fn alpha() {}"})
    hits = lookup_symbols(idx, ["alpha", "beta", "Module::Sub::alpha"])
    by_sym = {h["symbol"]: h for h in hits}
    assert by_sym["alpha"]["found"]
    assert not by_sym["beta"]["found"]
    # Scoped lookup resolves to the leaf identifier.
    assert by_sym["Module::Sub::alpha"]["found"]
