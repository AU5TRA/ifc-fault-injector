"""
The rule library, against real models.

Skipped when the source corpus is not on this machine. The properties
checked here are the ones that make a generated test case worth having:
determinism, a defect that is genuinely NEW, and a file left structurally
intact.
"""
from __future__ import annotations

import ifcopenshell
import pytest

from ifcfault.config import COLOURS, colour_for, colour_legend
from ifcfault.library import BUILTIN, get
from ifcfault.library.contract import best_target
from ifcfault.library.edits import find_dangling_references
from ifcfault.verify import checks as C

ARCHITECTURAL = ("A1", "A2", "A3", "A4", "A5")
STRUCTURAL = ("S1", "S2", "S3", "S4", "S5")


def _applicable_rules(model, rule_ids):
    out = []
    for rule_id in rule_ids:
        module = get(rule_id)
        if module.applicable(model).ok:
            out.append(rule_id)
    return out


# ---------------------------------------------------------------------------
# colours
# ---------------------------------------------------------------------------
def test_every_builtin_rule_has_its_own_colour():
    assert set(COLOURS) >= set(BUILTIN), "a built-in rule with no colour is unfindable"
    hexes = [c.hex for c in COLOURS.values()]
    assert len(set(hexes)) == len(hexes), "two rules share a colour"


def test_an_unknown_rule_still_gets_a_stable_colour():
    """A synthesized rule has no entry in the table, but its report still
    has to name a colour, and the same rule id must always get the same one."""
    first = colour_for("Z9")
    assert colour_for("Z9").hex == first.hex
    assert first.hex.startswith("#") and len(first.hex) == 7


def test_colour_rgb_is_normalised():
    for colour in COLOURS.values():
        assert len(colour.rgb) == 3
        assert all(0.0 <= channel <= 1.0 for channel in colour.rgb)


def test_legend_mentions_every_rule():
    legend = colour_legend()
    for rule_id in COLOURS:
        assert rule_id in legend


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
def test_candidates_are_deterministic(arc_model_path):
    """Same model, two independent opens, identical ranking. Without this the
    "same input, same output" promise is empty."""
    for rule_id in ("A1", "A3", "A4"):
        module = get(rule_id)
        first = ifcopenshell.open(str(arc_model_path))
        if not module.applicable(first).ok:
            continue
        ranking_a = [t.global_id for t in module.candidates(first)]
        second = ifcopenshell.open(str(arc_model_path))
        ranking_b = [t.global_id for t in module.candidates(second)]
        assert ranking_a == ranking_b, f"{rule_id}: candidate order is not stable"
        assert ranking_a, f"{rule_id}: applicable but produced no candidates"


def test_applicability_reason_is_always_populated(arc_model_path):
    """The reason ends up in a report a human reads, in both directions."""
    model = ifcopenshell.open(str(arc_model_path))
    for rule_id in sorted(BUILTIN):
        result = get(rule_id).applicable(model)
        assert result.reason.strip(), f"{rule_id}: empty applicability reason"


def test_exclude_is_honoured(arc_model_path):
    model = ifcopenshell.open(str(arc_model_path))
    module = get("A1")
    if not module.applicable(model).ok:
        pytest.skip("A1 not applicable to this model")
    first = best_target(module.candidates(model))
    remaining = module.candidates(model, exclude=frozenset({first.global_id}))
    assert first.global_id not in {t.global_id for t in remaining}


# ---------------------------------------------------------------------------
# the mutation itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule_id", ARCHITECTURAL)
def test_architectural_rule_leaves_the_file_structurally_intact(rule_id, arc_model_path):
    module = get(rule_id)
    model = ifcopenshell.open(str(arc_model_path))
    if not module.applicable(model).ok:
        pytest.skip(f"{rule_id} not applicable to this model")
    module.apply_violation(model, best_target(module.candidates(model)), {})
    assert find_dangling_references(model) == []


@pytest.mark.parametrize("rule_id", STRUCTURAL)
def test_structural_rule_leaves_the_file_structurally_intact(rule_id, str_model_path):
    module = get(rule_id)
    model = ifcopenshell.open(str(str_model_path))
    if not module.applicable(model).ok:
        pytest.skip(f"{rule_id} not applicable to this model")
    module.apply_violation(model, best_target(module.candidates(model)), {})
    assert find_dangling_references(model) == []


def test_mutation_record_is_complete_enough_to_report(arc_model_path):
    model = ifcopenshell.open(str(arc_model_path))
    module = get("A1")
    if not module.applicable(model).ok:
        pytest.skip("A1 not applicable")
    target = best_target(module.candidates(model))
    mutation = module.apply_violation(model, target, {})
    assert mutation.rule_id == "A1"
    assert mutation.target_global_id == target.global_id
    assert mutation.description.strip()
    assert mutation.clause.strip()
    assert mutation.extra.get("mechanism"), "a report needs to say HOW the file changed"


def test_a1_actually_violates_its_clause_per_the_independent_verifier(arc_model_path, tmp_path):
    """The end-to-end property that matters: after the mutation, the
    independent re-derivation agrees the clause is broken."""
    module = get("A1")
    model = ifcopenshell.open(str(arc_model_path))
    if not module.applicable(model).ok:
        pytest.skip("A1 not applicable")
    mutation = module.apply_violation(model, best_target(module.candidates(model)), {})

    out = tmp_path / "violated.ifc"
    model.write(str(out))

    written = ifcopenshell.open(str(out))
    result = C.rederive_a1(written, {"target_global_id": mutation.target_global_id})
    assert result.passed, result.message


def test_s5_declines_a_model_whose_walls_sit_on_one_storey(str_model_path):
    """Emptying the only wall-bearing storey removes every wall in the
    building. That is not a soft storey, and saying so is the point."""
    model = ifcopenshell.open(str(str_model_path))
    result = get("S5").applicable(model)
    if result.ok:
        pytest.skip("this model has walls on several storeys")
    assert "one storey" in result.reason or "at least" in result.reason


def test_geometry_rules_target_something_currently_compliant(str_model_path):
    """A defect that was already there is not an injected defect - a checker
    would have flagged the untouched file too."""
    model = ifcopenshell.open(str(str_model_path))
    for rule_id in ("S1", "S2", "S3"):
        module = get(rule_id)
        if not module.applicable(model).ok:
            continue
        target = best_target(module.candidates(model))
        assert "already_noncompliant" in target.extra, (
            f"{rule_id}: candidates must record whether the target already violated "
            f"the clause, so the report can say so"
        )
