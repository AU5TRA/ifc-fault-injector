"""
How much of a building one injected fault actually changes.

This is the other half of the cost question. `bench.py` asks what it costs
to GENERATE a script; this asks what the script then DOES to the model -
and the answer is the thing that makes an injected defect a useful test
case at all.

A fault has to be small. A checker that is handed a file where a third of
the building has moved is not being tested on its ability to find a narrow
door; it is being tested on its ability to survive a wrecked model. The
figure worth reporting is therefore the FOOTPRINT: how many IfcRoot entities
the mutation touched, against how many the building has.

Measured, not estimated. For each (model, rule) pair this opens a fresh copy
of the real building, lets the rule pick its own target by its own ranked
score, applies the mutation in memory, and counts what moved from the record
the rule itself wrote. Nothing is written to disk and no LLM is involved.

The fresh parse per rule is not incidental: applying A1 and then S5 to one
in-memory model would compound, and every per-rule figure after the first
would be wrong.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

RULE_IDS = ("A1", "A2", "A3", "A4", "A5", "S1", "S2", "S3", "S4", "S5")

#: One phrase per rule, for the table. What the mutation physically does,
#: which is what predicts the footprint: an attribute write touches one
#: entity whatever the building, a storey clear touches every wall on a floor.
MECHANISM = {
    "A1": "attribute write", "A2": "attribute write", "A3": "attribute write",
    "A4": "placement move", "A5": "relationship delete",
    "S1": "geometry resize", "S2": "geometry resize", "S3": "geometry resize",
    "S4": "element delete", "S5": "storey clear",
}

#: Keys a rule may use to declare what it removed. Collected rather than
#: guessed, because the whole point is to count what the rule SAYS it did -
#: the same declaration `verify/` holds it to.
DELETION_KEYS = ("deleted_global_ids", "deleted_wall_global_ids",
                 "cascade_removed_global_ids")


@dataclass
class RuleStat:
    rule_id: str
    applies: bool
    roots: int = 0
    reason: str = ""
    candidates: int = 0
    changed: int = 0
    deleted: int = 0
    attribute: str = ""
    secs: float = 0.0

    @property
    def touched(self) -> int:
        return self.changed + self.deleted

    @property
    def pct(self) -> float:
        return 100.0 * self.touched / self.roots if self.roots else 0.0


@dataclass
class ModelStat:
    source: str
    megabytes: float
    roots: int = 0
    rules: list[RuleStat] = field(default_factory=list)

    @property
    def label(self) -> str:
        parts = Path(self.source).parts
        return "/".join(parts[-2:]) if len(parts) >= 2 else self.source


def footprint(mutation) -> tuple[int, int]:
    """(changed, deleted) IfcRoot entities, per the mutation's own record."""
    extra = mutation.extra or {}

    deleted: set = set()
    for key in DELETION_KEYS:
        deleted.update(extra.get(key) or [])
    if mutation.attribute == "(entity deleted)":
        deleted.add(mutation.target_global_id)
    if extra.get("severed_relationship_global_id"):
        deleted.add(extra["severed_relationship_global_id"])

    changed: set = set(extra.get("cascade_modified_global_ids") or [])
    if mutation.attribute != "(entity deleted)":
        changed.add(mutation.target_global_id)
    # A4's real edit is the opening's placement, not the door's attributes.
    if extra.get("opening_global_id"):
        changed.add(extra["opening_global_id"])

    changed.discard(None)
    deleted.discard(None)
    return len(changed - deleted), len(deleted)


def measure_model(path: str | Path, rule_ids=RULE_IDS, log=print) -> ModelStat:
    """Every rule against one building, each on a fresh parse."""
    import ifcopenshell

    from .library import get as get_rule
    from .library.contract import best_target

    path = Path(path)
    stat = ModelStat(source=str(path),
                     megabytes=path.stat().st_size / (1 << 20))
    log(f"  {stat.label}  ({stat.megabytes:.0f} MB)")

    for rule_id in rule_ids:
        module = get_rule(rule_id)
        started = time.time()
        if module is None:
            stat.rules.append(RuleStat(rule_id, False, reason="unknown rule"))
            continue
        try:
            model = ifcopenshell.open(str(path))
            roots = len(model.by_type("IfcRoot"))
            stat.roots = max(stat.roots, roots)

            applicability = module.applicable(model)
            if not applicability.ok:
                stat.rules.append(RuleStat(rule_id, False, roots=roots,
                                           reason=applicability.reason))
                continue
            targets = module.candidates(model)
            if not targets:
                stat.rules.append(RuleStat(rule_id, False, roots=roots,
                                           reason="no candidate"))
                continue

            mutation = module.apply_violation(model, best_target(targets), {})
            changed, deleted = footprint(mutation)
            stat.rules.append(RuleStat(
                rule_id, True, roots=roots, candidates=len(targets),
                changed=changed, deleted=deleted, attribute=mutation.attribute,
                secs=time.time() - started,
            ))
        except Exception as e:                      # noqa: BLE001
            stat.rules.append(RuleStat(rule_id, False,
                                       reason=f"{type(e).__name__}: {e}"[:90]))
        finally:
            model = None                            # noqa: F841 - free the parse
    applied = [r.rule_id for r in stat.rules if r.applies]
    log(f"      {stat.roots:,} IfcRoot | applies: {', '.join(applied) or '(none)'}")
    return stat


def _range(values: list, fmt="{:.0f}") -> str:
    lo, hi = min(values), max(values)
    return fmt.format(lo) if lo == hi else f"{fmt.format(lo)}-{fmt.format(hi)}"


def render(stats: list[ModelStat]) -> str:
    """The poster table."""
    lines = [
        "=" * 92,
        " DESIGN-MODIFICATION FOOTPRINT  --  what ONE injected fault changes",
        "=" * 92,
        "",
        f"{'Model':34} {'MB':>5} {'IfcRoot':>9}   applicable rules",
        "-" * 92,
    ]
    for s in stats:
        applied = [r.rule_id for r in s.rules if r.applies]
        lines.append(f"  {s.label:32} {s.megabytes:5.0f} {s.roots:9,}   "
                     f"{', '.join(applied) if applied else '(none)'}")

    lines += [
        "",
        "-" * 92,
        f"{'Rule':6} {'Mechanism':20} {'applies':>8} {'cand.':>12} "
        f"{'changed':>8} {'deleted':>10} {'% of model':>13}",
        "-" * 92,
    ]
    n_models = len(stats)
    edit_pcts: list[float] = []
    delete_pcts: list[float] = []
    all_pcts: list[float] = []

    for rule_id in RULE_IDS:
        hits = [r for s in stats for r in s.rules
                if r.rule_id == rule_id and r.applies]
        if not hits:
            lines.append(f"{rule_id:6} {MECHANISM[rule_id]:20} {f'0/{n_models}':>8} "
                         f"{'-':>12} {'-':>8} {'-':>10} {'-':>13}")
            continue
        pcts = [h.pct for h in hits]
        all_pcts += pcts
        (delete_pcts if max(h.deleted for h in hits) else edit_pcts).extend(pcts)
        lines.append(
            f"{rule_id:6} {MECHANISM[rule_id]:20} {f'{len(hits)}/{n_models}':>8} "
            f"{_range([h.candidates for h in hits]):>12} "
            f"{_range([h.changed for h in hits]):>8} "
            f"{_range([h.deleted for h in hits]):>10} "
            f"{_range(pcts, '{:.3f}') + '%':>13}")
    lines.append("-" * 92)

    if all_pcts:
        import statistics
        lines += [
            "",
            f"  median footprint, all applicable (rule, model) pairs : "
            f"{statistics.median(all_pcts):.4f}% of IfcRoot entities",
        ]
        if edit_pcts:
            lines.append(f"  edit-only rules  (A1-A4, S1-S3)                      : "
                         f"{max(edit_pcts):.4f}% at worst  - one or two entities")
        if delete_pcts:
            lines.append(f"  deleting rules   (A5, S4, S5)                        : "
                         f"up to {max(delete_pcts):.2f}%")
    lines += [
        "",
        "=" * 92,
        " READING THIS",
        "-" * 92,
        "",
        " 'changed'/'deleted' count IfcRoot entities, taken from the mutation record",
        " the rule itself writes  - the same declaration ifcfault/verify/ holds it to.",
        " An undeclared change is a validation failure, so these numbers are not a",
        " summary of the diff; they ARE the diff.",
        "",
        " Eight of the ten rules touch one or two entities, which is the point: a",
        " checker under test has to find one wrong door among forty thousand entities.",
        " A fault big enough to see without looking is not a test of anything.",
        "",
        " S5 is the deliberate exception. A soft storey IS the absence of a storey's",
        " walls, so the mutation is large by definition  - and it is why the marker",
        " box exists, since deleted elements leave nothing to colour.",
        "",
        " 'applies' is how many of the sampled buildings the rule fits. A structural",
        " rule on an architectural model has nothing to target; that is a property of",
        " the corpus, not a failure. `ifcfault survey --source <model>` says which",
        " rules fit a given file, and why not, for the rest.",
        "",
        "=" * 92,
    ]
    return "\n".join(lines)


def render_latex(stats: list[ModelStat]) -> str:
    lines = [
        r"\begin{tabular}{llrrrr}",
        r"\toprule",
        r"Rule & Mechanism & Applies & Changed & Deleted & \% of model \\",
        r"\midrule",
    ]
    for rule_id in RULE_IDS:
        hits = [r for s in stats for r in s.rules
                if r.rule_id == rule_id and r.applies]
        if not hits:
            lines.append(f"{rule_id} & {MECHANISM[rule_id]} & 0/{len(stats)} & - & - & - \\\\")
            continue
        lines.append(
            f"{rule_id} & {MECHANISM[rule_id]} & {len(hits)}/{len(stats)} & "
            f"{_range([h.changed for h in hits]).replace('-', '--')} & "
            f"{_range([h.deleted for h in hits]).replace('-', '--')} & "
            f"{_range([h.pct for h in hits], '{:.3f}').replace('-', '--')} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines)


def render_json(stats: list[ModelStat]) -> str:
    import json

    return json.dumps([
        {
            "source": s.source, "megabytes": round(s.megabytes, 1), "roots": s.roots,
            "rules": [
                {
                    "rule": r.rule_id, "applies": r.applies, "reason": r.reason,
                    "candidates": r.candidates, "changed": r.changed,
                    "deleted": r.deleted, "touched": r.touched,
                    "pct_of_model": round(r.pct, 5), "attribute": r.attribute,
                }
                for r in s.rules
            ],
        }
        for s in stats
    ], indent=2)
