"""
The three-function contract every rule implements  - hand-written or
LLM-synthesized, the shape is identical.

A rule module is a plain module (no class, no ABC) exposing:

    RULE_ID   : str    e.g. "A1"
    CLAUSE    : str    the code clause, quoted well enough to cite
    DOMAIN    : str    "architectural" | "structural"
    ELEMENT   : str    the IFC type it targets, for reporting

    def applicable(model) -> Applicability
    def candidates(model, exclude=frozenset()) -> list[ScoredTarget]
    def apply_violation(model, target, params) -> Mutation

Plain module functions (rather than a class hierarchy) because every rule
gets source-extracted and inlined into a standalone script  - a flat module
inlines cleanly, a class with an ABC base does not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Applicability:
    """Cheap, read-only answer to 'can this rule act on this model at all?'"""

    ok: bool
    reason: str  # always populated, in both directions  - this goes in the report


@dataclass(frozen=True)
class ScoredTarget:
    """One candidate the rule could act on, ranked so selection is deterministic.

    `score` is higher-is-better; ties break on `global_id` so the winner
    never depends on dict/iteration order.
    """

    global_id: str
    score: float
    justification: str
    element_ids: tuple[int, ...] = field(default_factory=tuple)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Mutation:
    """What apply_violation() actually did. This is the record the .txt
    report is built from, so every field is written to be read by a human."""

    rule_id: str
    element_type: str
    target_global_id: str
    attribute: str
    before: Any
    after: Any
    clause: str
    description: str
    extra: dict[str, Any] = field(default_factory=dict)


def best_target(targets: list[ScoredTarget]) -> ScoredTarget:
    """The one deterministic way to pick a winner from a candidate list."""
    if not targets:
        raise RuntimeError("no candidates to choose from")
    return sorted(targets, key=lambda t: (-t.score, t.global_id))[0]
