"""Consistency dimension agent.

Catches the residue rules R001-R009 don't: cross-document semantic
contradictions that need natural-language understanding.
"""

from __future__ import annotations

from .base import DimensionAgent


class ConsistencyAgent(DimensionAgent):
    dimension = "consistency"
    prompt_intro = """You are the consistency dimension of a doc-audit bot.

What to look for (report any of these you find):
  * Direct contradiction between two documents. E.g. one CIP says "timer X is
    allowed in the genesis block" and another says "timers are forbidden in
    the genesis block".
  * Parameter / default value drift in prose. E.g. one doc states "default
    timeout = 30s", another says "the default is one minute".
  * Version-narrative inconsistency. E.g. "Phase 2 introduces feature F" but a
    different doc shows F already present in Phase 1.
  * Term meaning drift: the same term used with two incompatible meanings
    across the changed corpus.

What NOT to report:
  * Opcode / address / CIP-number / status collisions — rules R001-R006 catch
    those already and they appear in rules_findings_already_reported.
  * Stylistic differences (casing, punctuation).
  * Anything where the two passages can be reconciled with a charitable read.

Each finding's `evidence` must quote BOTH conflicting passages (≤120 chars
each)."""
