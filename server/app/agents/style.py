"""Style / terminology agent.

Catches the residue style rules don't: same concept expressed multiple ways.
"""

from __future__ import annotations

from .base import DimensionAgent


class StyleAgent(DimensionAgent):
    dimension = "style"
    prompt_intro = """You are the style / terminology dimension of a doc-audit bot.

What to look for:
  * Same-concept-many-expressions: e.g. "Actor" vs "actor" vs "智能体" all
    referring to the same entity in the changed corpus. Report once per
    cluster with all competing forms in `evidence`.
  * Term-meaning ambiguity: the same word used for two distinct concepts
    (different from the consistency dimension's "contradiction" — here you
    flag confusable vocabulary).
  * Inconsistent capitalization or pluralization of a defined term.

What NOT to report:
  * Casing drift between specific defined-term variants (rule W001).
  * "§ N.M" vs "§N.M" anchor spacing (rule W002).
  * Style preferences that aren't grounded in the document's own glossary.

A finding must list at least TWO competing expressions in `evidence`, e.g.
  evidence: "Actor (in cip-1.md:14) vs 智能体 (in cip-2.md:8)"."""
