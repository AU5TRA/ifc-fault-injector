"""
Turning `--rule A1:2,S1` into an ordered list of faults to inject.

One script can carry more than one violation. That is a different thing from
one rule that happens to touch many elements - S5 already deletes every wall
on a storey and calls it one fault - so the vocabulary is kept straight
throughout:

    fault     one clause violated once, at one chosen target. What a reader
              of the report counts.
    rule      the code that knows how to produce a fault of one kind. A rule
              can appear in a plan more than once.
    plan      the ordered list of faults a single emitted script injects.

Order matters and is preserved exactly as written. Injection is sequential
and each fault sees the model the previous ones left behind, so a plan is a
recipe, not a set. Two plans with the same rules in a different order are
different plans and may pick different targets.

Repeats are the reason `exclude` exists on every rule's `candidates()`. A
plan of `A1:3` narrows three DIFFERENT doors, because each fault adds its
target to the exclusion set before the next one chooses. Without that, all
three would re-select the same highest-scoring door and the script would
claim three faults while injecting one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: A rule id as written on the command line: letters then digits (A1, S5),
#: which is what both the built-in rules and the synthesized ones use.
RULE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

#: Beyond this a plan stops being a test case and becomes a different model.
#: Not a technical limit  - it is here so a typo like `A1:500` fails loudly
#: instead of running for an hour.
MAX_FAULTS = 25


class PlanError(ValueError):
    """A rule spec that cannot be turned into a plan."""


@dataclass(frozen=True)
class Fault:
    """One entry in a plan.

    `slot` is the 1-based position in the whole plan and is what the report
    and the record count by. `occurrence` is the 1-based position among
    faults of the SAME rule, which is what distinguishes the second A1 from
    the first when both appear.
    """

    rule_id: str
    slot: int
    occurrence: int
    #: How many faults of this rule the plan contains in total, so a report
    #: can say "A1 (2 of 3)" without recounting.
    of: int

    @property
    def label(self) -> str:
        """How this fault is named in reports: `A1` alone, or `A1#2` when the
        plan injects the same rule more than once."""
        return self.rule_id if self.of == 1 else f"{self.rule_id}#{self.occurrence}"


def parse_rule_spec(spec) -> list[str]:
    """`--rule` as written -> a flat, ordered list of rule ids.

    Accepts a single string, or the list argparse builds from a repeated
    flag. Within a string, faults are comma-separated and `ID:N` repeats a
    rule N times in place:

        "A1"            -> ["A1"]
        "A1,A2,S1"      -> ["A1", "A2", "S1"]
        "A1:3"          -> ["A1", "A1", "A1"]
        "A1:2,S1"       -> ["A1", "A1", "S1"]
        ["A1", "S1:2"]  -> ["A1", "S1", "S1"]

    Ids are upper-cased, since the library registry is keyed that way and
    `--rule a1` is an obvious thing to type.
    """
    if isinstance(spec, str):
        chunks = [spec]
    else:
        chunks = list(spec or [])

    out: list[str] = []
    for chunk in chunks:
        for piece in str(chunk).split(","):
            piece = piece.strip()
            if not piece:
                continue
            rule_id, count = _split_count(piece)
            out.extend([rule_id] * count)

    if not out:
        raise PlanError("no rule given. Pass --rule A1, or --rule A1,S1 for several.")
    if len(out) > MAX_FAULTS:
        raise PlanError(
            f"{len(out)} faults requested but the limit is {MAX_FAULTS}. That is almost "
            f"certainly a typo  - `A1:3` means three A1 faults, not a threshold."
        )
    return out


def _split_count(piece: str) -> tuple[str, int]:
    rule_id, _, raw_count = piece.partition(":")
    rule_id = rule_id.strip().upper()

    if not RULE_ID_RE.match(rule_id):
        raise PlanError(
            f"'{rule_id or piece}' is not a rule id. Expected something like A1 or S5, "
            f"optionally with a repeat count: A1:3."
        )
    if not raw_count:
        return rule_id, 1

    raw_count = raw_count.strip()
    if not raw_count.isdigit():
        raise PlanError(
            f"'{piece}': the repeat count after ':' must be a whole number, got "
            f"'{raw_count}'."
        )
    count = int(raw_count)
    if count < 1:
        raise PlanError(f"'{piece}': a repeat count of {count} injects nothing.")
    return rule_id, count


def build_plan(rule_ids: list[str]) -> list[Fault]:
    """An ordered list of rule ids -> the Faults an emitted script injects."""
    totals: dict[str, int] = {}
    for rule_id in rule_ids:
        totals[rule_id] = totals.get(rule_id, 0) + 1

    seen: dict[str, int] = {}
    plan: list[Fault] = []
    for slot, rule_id in enumerate(rule_ids, start=1):
        seen[rule_id] = seen.get(rule_id, 0) + 1
        plan.append(Fault(rule_id=rule_id, slot=slot,
                          occurrence=seen[rule_id], of=totals[rule_id]))
    return plan


def distinct_rule_ids(plan: list[Fault]) -> list[str]:
    """The rules a plan uses, each once, in first-appearance order.

    Inlining is per RULE, not per fault: a plan with three A1 faults carries
    ONE copy of A1's code and calls it three times with a growing exclusion
    set.
    """
    out: list[str] = []
    for fault in plan:
        if fault.rule_id not in out:
            out.append(fault.rule_id)
    return out


def plan_stem_fragment(plan: list[Fault]) -> str:
    """The part of an output filename that names the plan.

    A single fault keeps the bare rule id, so every path a single-rule
    emission produced before multi-fault existed is byte-for-byte the same.
    Beyond that the rules are joined, with a repeat count rather than a
    repeated id:

        [A1]             -> "A1"
        [A1, S1]         -> "A1_S1"
        [A1, A1, A1]     -> "A1x3"
        [A1, A1, S1]     -> "A1x2_S1"
    """
    parts = []
    for rule_id in distinct_rule_ids(plan):
        count = sum(1 for f in plan if f.rule_id == rule_id)
        parts.append(rule_id if count == 1 else f"{rule_id}x{count}")
    return "_".join(parts)


def describe_plan(plan: list[Fault]) -> str:
    """One line, for logs and the `--help`-ish confirmation at emit time."""
    if len(plan) == 1:
        return f"1 fault: {plan[0].rule_id}"
    return f"{len(plan)} faults: " + ", ".join(f.label for f in plan)
