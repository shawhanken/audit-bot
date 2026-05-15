"""Tests for the markdown helper module — especially the split between
`extract_anchors` (refs in prose) and `extract_section_anchors` (sections
defined by this file)."""

from __future__ import annotations

from common import markdown as md


def test_extract_anchors_finds_prose_references():
    text = "See §3.1 for details. Compare with §9.2.3 below."
    assert md.extract_anchors(text) == ["3.1", "9.2.3"]


def test_extract_section_anchors_from_simple_headings():
    text = "## 1 Foo\n\n### 1.1 Bar\n\n#### 1.1.2 Baz\n"
    assert md.extract_section_anchors(text) == ["1", "1.1", "1.1.2"]


def test_extract_section_anchors_handles_bold_wrapper():
    """Real CIP files use `### **1. Title**` style."""
    text = "### **1. Motivation**\n#### **2.1 Tier 1**\n"
    assert md.extract_section_anchors(text) == ["1", "2.1"]


def test_extract_section_anchors_handles_section_prefix():
    """Some CIPs write `## §9.2 Foo` with the section symbol."""
    text = "## §9.2 Opcode registry\n### §9.2.1 Reserved\n"
    assert md.extract_section_anchors(text) == ["9.2", "9.2.1"]


def test_extract_section_anchors_ignores_unnumbered_headings():
    """`### Abstract` is not a numbered section."""
    text = "### Abstract\n## Motivation\n### 3.1 Real Section\n"
    assert md.extract_section_anchors(text) == ["3.1"]


def test_anchors_and_section_anchors_are_distinct():
    """A doc may reference §9.2 in prose but only define §3.1 itself."""
    text = "## 3.1 My section\n\nWe build on §9.2 from CIP-X.\n"
    assert md.extract_anchors(text) == ["9.2"]
    assert md.extract_section_anchors(text) == ["3.1"]
