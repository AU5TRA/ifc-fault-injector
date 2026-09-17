"""
Multi-fault emission: the plan parser, rule namespacing, and assembly.

Everything here runs with no API key and no source model. What it cannot
cover is the generated harness itself, which needs both  - so these tests
pin the machinery the harness is handed, and the shape it is asked for.
"""
from __future__ import annotations

import ast

import pytest

from ifcfault import codegen
from ifcfault.library import get as get_rule
from ifcfault.plan import (
    MAX_FAULTS, PlanError, build_plan, describe_plan, distinct_rule_ids,
    parse_rule_spec, plan_stem_fragment,
)
from ifcfault.safety import check_source

ALL_RULES = ("A1", "A2", "A3", "A4", "A5", "S1", "S2", "S3", "S4", "S5")

#: Stands in for what the model returns, so assembly can be exercised
#: without an API key. Deliberately minimal: the point is the wiring around
#: it, not what it computes.
STUB_HARNESS = '''
def _mark_violation(model, mutation, target, source_path, fault):
    return {"label": fault["label"], "painted_items": 0, "warnings": []}


def _report_top(ctx):
    return "INPUT MODEL INJECTED VIOLATION WHERE TO FIND IT"


def _report_bottom(ctx):
    return "HOW TO SPOT IT IN A VIEWER OUTPUT FILES SELF-CHECKS COLOUR LEGEND"


def main(argv=None):
    used = set()
    for fault in FAULTS:
        fault["applicable"](None)
        targets = fault["candidates"](None, exclude=frozenset(used))
        chosen = best_target(targets)
        used.add(chosen.global_id)
        fault["apply_violation"](None, chosen, {})
    return 0
'''


def build(spec):
    """spec -> (plan, rules, assembled script)."""
    import pathlib

    plan = build_plan(parse_rule_spec(spec))
    rules = {r: codegen.extract_rule(get_rule(r), suffix=f"_{r}")
             for r in distinct_rule_ids(plan)}
    script = codegen.assemble_multi(
        plan=plan, rules=rules, harness_source=STUB_HARNESS,
        source_ifc=pathlib.Path("m.ifc"),
        output_stem="t_" + plan_stem_fragment(plan),
        model_name="stub", harness_origin="test",
    )
    return plan, rules, script


# ---------------------------------------------------------------------------
# the plan parser
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("spec,expected", [
    ("A1", ["A1"]),
    ("A1,A2,S1", ["A1", "A2", "S1"]),
    ("A1:3", ["A1", "A1", "A1"]),
    ("A1:2,S1", ["A1", "A1", "S1"]),
    ("a1,s5", ["A1", "S5"]),
    (" A1 , S1 ", ["A1", "S1"]),
    (["A1", "S1:2"], ["A1", "S1", "S1"]),
])
def test_rule_specs_parse(spec, expected):
    assert parse_rule_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "   ", "A1:0", "A1:x", "1A", "A1:-1", f"A1:{MAX_FAULTS + 1}"])
def test_bad_rule_specs_are_rejected(spec):
    with pytest.raises(PlanError):
        parse_rule_spec(spec)


def test_plan_order_is_preserved_exactly():
    """Injection is sequential and each fault sees what the last one left, so
    a plan is a recipe, not a set."""
    assert parse_rule_spec("S1,A1") == ["S1", "A1"]
    assert parse_rule_spec("A1,S1") == ["A1", "S1"]


def test_repeats_are_labelled_so_a_report_can_tell_them_apart():
    plan = build_plan(parse_rule_spec("A1:2,S1"))
    assert [f.label for f in plan] == ["A1#1", "A1#2", "S1"]
    assert [f.slot for f in plan] == [1, 2, 3]
    # A rule used once keeps its bare id - no "#1" noise on the common case.
    assert build_plan(["A1"])[0].label == "A1"


def test_single_rule_stem_is_unchanged():
    """Every path a single-rule emission produced before multi-fault existed
    has to stay byte-for-byte the same."""
    assert plan_stem_fragment(build_plan(["A1"])) == "A1"


@pytest.mark.parametrize("spec,stem", [
    ("A1", "A1"),
    ("A1,S1", "A1_S1"),
    ("A1:3", "A1x3"),
    ("A1:2,S1", "A1x2_S1"),
])
def test_plan_stem_fragments(spec, stem):
    assert plan_stem_fragment(build_plan(parse_rule_spec(spec))) == stem


def test_distinct_rules_are_first_appearance_order():
    plan = build_plan(parse_rule_spec("S1,A1:2,S1"))
    assert distinct_rule_ids(plan) == ["S1", "A1"]


def test_describe_plan_reads_naturally():
    assert describe_plan(build_plan(["A1"])) == "1 fault: A1"
    assert "3 faults" in describe_plan(build_plan(parse_rule_spec("A1:2,S1")))


# ---------------------------------------------------------------------------
# namespacing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule_id", ALL_RULES)
def test_namespacing_is_exactly_reversible(rule_id):
    """Undoing the suffix must give the library file back character for
    character. Anything else means the rewrite touched something it was not
    asked to  - a string, a comment, an attribute."""
    import pathlib

    original = pathlib.Path(get_rule(rule_id).__file__).read_text(encoding="utf-8")
    suffix = f"_NS_{rule_id}"
    renamed, _ = codegen.namespace_rule_source(original, suffix)
    assert renamed.replace(suffix, "") == original


@pytest.mark.parametrize("rule_id", ALL_RULES)
def test_namespacing_moves_only_names_the_module_owns(rule_id):
    """Library helpers (`to_mm`, `length_unit_scale`) are shared by every
    rule and must NOT be suffixed - they are inlined once."""
    import pathlib
    import re

    original = pathlib.Path(get_rule(rule_id).__file__).read_text(encoding="utf-8")
    suffix = f"_NS_{rule_id}"
    renamed, owned = codegen.namespace_rule_source(original, suffix)
    moved = set(re.findall(r"(\w+)" + re.escape(suffix), renamed))
    assert moved <= owned


def test_namespacing_preserves_comments():
    """Rule source is lifted verbatim so a human can read it in the emitted
    file; a rename that ate the comments would defeat that."""
    import pathlib

    original = pathlib.Path(get_rule("A1").__file__).read_text(encoding="utf-8")
    renamed, _ = codegen.namespace_rule_source(original, "_A1")
    comments = [ln for ln in original.splitlines() if ln.strip().startswith("#")]
    assert [ln for ln in renamed.splitlines() if ln.strip().startswith("#")] == comments
    assert comments, "this test is vacuous if the rule has no comments"


def test_the_names_that_actually_collide_are_all_renamed():
    """The three contract functions plus the metadata constants are defined
    by every single rule, so they are the collision that matters."""
    rule = codegen.extract_rule(get_rule("A1"), suffix="_A1")
    tree = ast.parse(rule.text)
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"applicable_A1", "candidates_A1", "apply_violation_A1"} <= defined
    assert not ({"applicable", "candidates", "apply_violation"} & defined)
    assert rule.fn("candidates") == "candidates_A1"


def test_an_unsuffixed_rule_keeps_its_plain_contract_names():
    rule = codegen.extract_rule(get_rule("A1"))
    tree = ast.parse(rule.text)
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"applicable", "candidates", "apply_violation"} <= defined
    assert rule.suffix == ""
    assert rule.fn("candidates") == "candidates"


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("spec", [
    "A1", "A1,S1", "A1:3", "A1:2,A3,S5", ",".join(ALL_RULES),
])
def test_multi_scripts_parse_and_pass_the_static_gate(spec):
    plan, rules, script = build(spec)
    ast.parse(script)

    required = ["main"]
    for rule_id in rules:
        required += [f"applicable_{rule_id}", f"candidates_{rule_id}",
                     f"apply_violation_{rule_id}"]
    report = check_source(script, required_defs=tuple(required))
    assert report.ok, report.summary()


@pytest.mark.parametrize("spec", ["A1,S1", "A1:3", "A1:2,A3,S5", ",".join(ALL_RULES)])
def test_multi_scripts_import_cleanly_and_bind_the_right_functions(spec):
    """The real prize: ten rules that each define `candidates` coexisting in
    one namespace, with every FAULTS row pointing at its OWN rule."""
    plan, rules, script = build(spec)
    namespace: dict = {}
    exec(compile(script, "<multi>", "exec"), namespace)

    assert len(namespace["FAULTS"]) == len(plan)
    assert namespace["FAULT_COUNT"] == len(plan)

    for row, fault in zip(namespace["FAULTS"], plan):
        assert row["rule_id"] == fault.rule_id
        assert row["slot"] == fault.slot
        assert row["label"] == fault.label
        for contract in ("applicable", "candidates", "apply_violation"):
            assert row[contract] is namespace[f"{contract}_{fault.rule_id}"]


def test_a_repeated_rule_is_inlined_once_not_twice():
    """`A1:3` is three faults from ONE copy of A1's code. Inlining it three
    times would be three colliding definitions, and the last would win."""
    plan, rules, script = build("A1:3")
    assert len(rules) == 1
    assert script.count("def candidates_A1(") == 1
    assert len(plan) == 3

    namespace: dict = {}
    exec(compile(script, "<multi>", "exec"), namespace)
    rows = namespace["FAULTS"]
    assert len({id(r["candidates"]) for r in rows}) == 1, "all three share one function"
    assert [r["label"] for r in rows] == ["A1#1", "A1#2", "A1#3"]


def test_every_rule_pair_can_share_a_file():
    """Pairwise, because the constants that collide are not the obvious ones:
    THRESHOLD_MM is defined by four rules and _has_swept_profile by two."""
    import itertools
    import pathlib

    for left, right in itertools.combinations(ALL_RULES, 2):
        plan = build_plan([left, right])
        rules = {r: codegen.extract_rule(get_rule(r), suffix=f"_{r}")
                 for r in (left, right)}
        script = codegen.assemble_multi(
            plan=plan, rules=rules, harness_source=STUB_HARNESS,
            source_ifc=pathlib.Path("m.ifc"), output_stem="t",
            model_name="stub", harness_origin="test",
        )
        namespace: dict = {}
        exec(compile(script, f"<{left}+{right}>", "exec"), namespace)
        assert namespace[f"candidates_{left}"] is not namespace[f"candidates_{right}"]


def test_the_plan_is_visible_in_the_generated_header():
    """A reader opening the file has to be able to see what it injects
    without running it."""
    _, _, script = build("A1:2,S5")
    head = script[:4000]
    assert "A1#1" in head and "A1#2" in head and "S5" in head
    assert "ALL-OR-NOTHING" in head


# ---------------------------------------------------------------------------
# the harness contract the multi shape is asked for
# ---------------------------------------------------------------------------
def test_multi_harness_prompts_ask_for_the_loop():
    from ifcfault import harness as H

    parts = {s.name: s.task for s in H.MULTI_PARTS}
    assert "exclude=frozenset(used)" in parts["main"], "the exclusion set is the whole point"
    assert "ALL-OR-NOTHING" in parts["main"]
    assert '"mutations"' in parts["main"], "the record must be an array"
    # Per-fault values must come from the FAULTS row, never a module global,
    # because a multi-fault script has no single RULE_ID.
    assert 'fault["colour_rgb"]' in parts["_mark_violation"]
    assert 'fault["slot"]' in parts["_mark_violation"], "marker GUIDs must not collide"
    assert 'ctx["faults"]' in parts["_report_top"]
    assert 'ctx["faults"]' in parts["_report_bottom"]


def test_single_fault_prompts_are_untouched_by_the_multi_shape():
    """The single-rule prompts are left byte-identical so that every harness
    generated before multi-fault existed still hits the LLM cache."""
    from ifcfault import harness as H

    single = {s.name: s.task for s in H.PARTS}
    assert 'ctx["faults"]' not in single["_report_top"]
    assert "FAULTS" not in single["main"]
    assert "COLOUR_RGB" in single["_mark_violation"], "single mode still reads globals"


def test_both_shapes_have_the_same_four_parts():
    """`choose_part_to_repair` blames by function name and is shared, so the
    two shapes must agree on what the four functions are called."""
    from ifcfault import harness as H

    assert [s.name for s in H.PARTS] == [s.name for s in H.MULTI_PARTS]


# ---------------------------------------------------------------------------
# validating a multi-fault run
# ---------------------------------------------------------------------------
def test_read_faults_normalises_both_record_shapes():
    """A single-fault script writes the flat shape it always has; a
    multi-fault one writes an array. Every check downstream is written once."""
    from ifcfault import validate

    single = validate.read_faults({
        "rule_id": "A1",
        "mutation": {"target_global_id": "G1", "rule_id": "A1"},
        "marking": {"painted_items": 2},
        "colour": {"hex": "#E6194B"},
    })
    assert len(single) == 1
    assert single[0]["rule_id"] == "A1" and single[0]["label"] == "A1"
    assert single[0]["mutation"]["target_global_id"] == "G1"

    multi = validate.read_faults({
        "fault_count": 2,
        "mutations": [
            {"slot": 1, "label": "A1#1", "rule_id": "A1",
             "mutation": {"target_global_id": "G1"}, "marking": {}},
            {"slot": 2, "label": "S1", "rule_id": "S1",
             "mutation": {"target_global_id": "G2"}, "marking": {}},
        ],
    })
    assert [f["label"] for f in multi] == ["A1#1", "S1"]
    assert [f["mutation"]["target_global_id"] for f in multi] == ["G1", "G2"]


def test_allowed_sets_are_the_union_over_every_fault():
    """Each fault is accounted for exactly as it would be alone; the check
    that consumes the union stays as strict as it was for one."""
    from ifcfault import validate

    mutations = [
        {"rule_id": "A1", "target_global_id": "DOOR1", "attribute": "IfcDoor.OverallWidth",
         "extra": {}},
        {"rule_id": "S4", "target_global_id": "COL1", "attribute": "(entity deleted)",
         "extra": {"deleted_global_ids": ["COL1", "COL2"]}},
    ]
    changed, removed, added = validate.derive_allowed_sets_multi(None, None, mutations)
    assert changed == {"DOOR1"}
    assert removed == {"COL1", "COL2"}


def test_a_short_delivery_is_a_harness_failure_not_a_pass():
    """A file named for three faults that contains two is a WRONG test case,
    not a partial one."""
    from ifcfault import validate

    record = {"fault_count": 2, "mutations": [
        {"slot": 1, "label": "A1", "rule_id": "A1",
         "mutation": {"target_global_id": "G1"}, "marking": {}},
        {"slot": 2, "label": "S1", "rule_id": "S1",
         "mutation": {"target_global_id": "G2"}, "marking": {}},
    ]}
    got = [f["rule_id"] for f in validate.read_faults(record)]
    assert got != ["A1", "S1", "A1"], "sanity"
    assert got == ["A1", "S1"]


def test_report_must_mention_every_fault(tmp_path):
    """The multi-fault form of the wrong-key bug: the report describes two of
    three injected faults, nothing crashes, and the reader is quietly told
    about less than the file contains."""
    from ifcfault import validate

    record = {
        "fault_count": 2,
        "colored": False,
        "mutations": [
            {"slot": 1, "label": "A1", "rule_id": "A1",
             "mutation": {"target_global_id": "AAA"},
             "colour": {"hex": "#E6194B"}, "marking": {}},
            {"slot": 2, "label": "S1", "rule_id": "S1",
             "mutation": {"target_global_id": "BBB"},
             "colour": {"hex": "#911EB4"}, "marking": {}},
        ],
    }
    report = tmp_path / "r.txt"
    report.write_text(
        "2 faults injected\n  AAA  #E6194B\n"
        "  No visual marking. Re-run with --colored.\n", encoding="utf-8")
    result = validate.check_report_matches_record(report, record, colored=False)
    assert not result.passed
    assert "BBB" in result.message

    report.write_text(
        "2 faults injected\n  AAA  #E6194B\n  BBB  #911EB4\n"
        "  No visual marking. Re-run with --colored.\n", encoding="utf-8")
    assert validate.check_report_matches_record(report, record, colored=False).passed


def test_fault_count_must_match_the_mutations_array(tmp_path):
    from ifcfault import validate

    record = {
        "fault_count": 3,                      # lies
        "mutations": [
            {"slot": 1, "label": "A1", "rule_id": "A1",
             "mutation": {"target_global_id": "AAA"},
             "colour": {"hex": "#E6194B"}, "marking": {}},
            {"slot": 2, "label": "S1", "rule_id": "S1",
             "mutation": {"target_global_id": "BBB"},
             "colour": {"hex": "#911EB4"}, "marking": {}},
        ],
    }
    report = tmp_path / "r.txt"
    report.write_text("2 faults\nAAA #E6194B\nBBB #911EB4\n--colored\n", encoding="utf-8")
    result = validate.check_report_matches_record(report, record, colored=False)
    assert not result.passed
    assert "fault_count" in result.message
