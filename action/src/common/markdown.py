"""Lightweight markdown scanning utilities.

We deliberately stay regex-based and tolerant: index extraction must keep
going when individual headings or tables are malformed (design §4.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
ANCHOR_RE = re.compile(r"§(\d+(?:\.\d+)*)")
# A *defined* section anchor lives in a numbered heading. Tolerates `**bold**`
# wrappers (`### **1.2 Title**`), optional `§` prefix (`### §1.2`), and any of
# `.`, ` `, `-`, `—`, `*`, `:` as the separator between number and title.
SECTION_HEADING_RE = re.compile(
    r"^#+\s+\*{0,2}(?:§\s*)?(\d+(?:\.\d+)*)[\s.\-:—*]"
)
FENCED_RE = re.compile(r"^```([a-zA-Z0-9_+\-]*)\s*$")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


@dataclass
class CodeBlock:
    file: str
    line_start: int  # line of opening fence
    line_end: int    # line of closing fence
    lang: str
    content: str


def iter_lines(text: str) -> list[str]:
    return text.splitlines()


def extract_code_blocks(file: str, text: str) -> list[CodeBlock]:
    blocks: list[CodeBlock] = []
    lines = iter_lines(text)
    i = 0
    while i < len(lines):
        m = FENCED_RE.match(lines[i])
        if not m:
            i += 1
            continue
        lang = m.group(1) or ""
        start = i + 1  # 1-based line number for opening fence
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            if FENCED_RE.match(lines[j]):
                break
            body.append(lines[j])
            j += 1
        end = j + 1
        blocks.append(
            CodeBlock(
                file=file,
                line_start=start,
                line_end=end,
                lang=lang.lower(),
                content="\n".join(body),
            )
        )
        i = j + 1
    return blocks


def extract_anchors(text: str) -> list[str]:
    """Return ['3.1', '9.2.3', ...] for §3.1, §9.2.3 references in text."""
    return ANCHOR_RE.findall(text)


def extract_section_anchors(text: str) -> list[str]:
    """Section anchors DEFINED in this file (from numbered headings).

    Distinct from `extract_anchors` which returns section *references* in
    prose. The `cip.anchors` index field uses this so R004 can answer "does
    CIP-N actually define §X.Y" rather than "does CIP-N mention §X.Y in prose".
    """
    out: list[str] = []
    for line in iter_lines(text):
        m = SECTION_HEADING_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def extract_headings(text: str) -> list[tuple[int, int, str]]:
    """Return list of (line_no, level, title) tuples."""
    out: list[tuple[int, int, str]] = []
    for i, line in enumerate(iter_lines(text), start=1):
        m = HEADING_RE.match(line)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def extract_links(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for i, line in enumerate(iter_lines(text), start=1):
        for m in LINK_RE.finditer(line):
            out.append((i, m.group(1), m.group(2)))
    return out
