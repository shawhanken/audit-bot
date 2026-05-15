"""Decorator-based rule registry.

A rule is a function `fn(ctx: RuleContext) -> Iterable[Finding]`. New rules
land by adding one function in the right module — no pipeline changes
(design §4.3, §6.1).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from common.schema import Finding


@dataclass
class RuleContext:
    base_index: dict[str, Any]
    head_index: dict[str, Any]
    diff: dict[str, Any]
    changed_files: list[str]
    repo_root: str
    target_name: str

    # The runner records which dimensions are enabled so rules can skip work
    # when their dimension is disabled in config.
    enabled_dimensions: set[str] = field(default_factory=set)


RuleFn = Callable[[RuleContext], Iterable[Finding]]


_REGISTRY: list[tuple[str, str, RuleFn]] = []  # (rule_id, dimension, fn)


def rule(rule_id: str, dimension: str) -> Callable[[RuleFn], RuleFn]:
    def deco(fn: RuleFn) -> RuleFn:
        _REGISTRY.append((rule_id, dimension, fn))
        return fn
    return deco


def all_rules() -> list[tuple[str, str, RuleFn]]:
    return list(_REGISTRY)


def load_builtin_rule_modules() -> None:
    """Trigger module import side-effects so decorators register their rules."""
    for mod in ("rules.consistency", "rules.security", "rules.style"):
        importlib.import_module(mod)
