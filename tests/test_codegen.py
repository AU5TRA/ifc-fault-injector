"""
The code generator: does an emitted script actually stand alone?

These tests need no API key and no LLM call - they use a stub harness that
touches every library name a real harness would, so the closure has to
resolve all of them.
"""
from __future__ import annotations

import ast

import pytest

from ifcfault import codegen
from ifcfault.library import BUILTIN, get

# Touches every name the generated harness is told it can rely on. If the
# closure misses one, the compile step below fails with NameError material.
STUB_HARNESS = '''
def main(argv=None):
    import argparse, dataclasses, json, os, sys, time
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=SOURCE_IFC)
    parser.add_argument("--outdir", default=".")
    parser.add_argument("--params", default="{}")
    args = parser.parse_args(argv)
    model = ifcopenshell.open(args.source)
    check = applicable(model)
    if not check.ok:
        print("not applicable:", check.reason)
        return 2
    targets = candidates(model)
    if not targets:
        print("no candidates")
        return 2
    target = best_target(targets)
    mutation = apply_violation(model, target, json.loads(args.params))
    cache = {}
    style = violation_style(model, RULE_ID, COLOUR_RGB, cache)
    element = None
    if mutation.attribute != "(entity deleted)" and not TARGET_IS_A_STOREY:
        element = model.by_guid(mutation.target_global_id)
        paint_element(model, element, style)
        tag_element_name(element, RULE_ID, NAME_TAG_PREFIX.replace(RULE_ID, "{rule_id}"))
        attach_violation_pset(model, element, {"ViolationRule": RULE_ID}, VIOLATION_PSET)
        describe_element(model, element)
        storey_of(model, element)
        global_xyz_mm(model, element, length_unit_scale(model))
    add_marker_box(model, (0.0, 0.0, 0.0), None, RULE_ID, "m", "d", style,
                   MARKER_SIZE_MM, "0")
    find_dangling_references(model)
    length_unit_name(model)
    sha256_of(args.source)
    _ = (RULE_CLAUSE, RULE_DOMAIN, RULE_ELEMENT, RULE_ORIGIN, RULE_DELETES_ELEMENTS,
         COLOUR_NAME, COLOUR_HEX, COLOUR_LEGEND, OUTPUT_STEM, GENERATOR_VERSION,
         HARNESS_GENERATED_BY, SCRIPT_GENERATED_AT, dataclasses.asdict(mutation))
    return 0
'''


def _assemble(rule_id: str) -> str:
    from pathlib import Path

    rule = codegen.extract_rule(get(rule_id))
    return codegen.assemble(
        rule=rule, harness_source=STUB_HARNESS,
        source_ifc=Path("model.ifc"), output_stem=f"t_{rule_id}",
        model_name="test", harness_origin="test",
    )


@pytest.mark.parametrize("rule_id", sorted(BUILTIN))
def test_emitted_script_compiles_and_defines_the_contract(rule_id):
    script = _assemble(rule_id)
    compile(script, f"<{rule_id}>", "exec")  # raises SyntaxError if malformed

    defined = {
        node.name
        for node in ast.parse(script).body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    for required in ("main", "applicable", "candidates", "apply_violation"):
        assert required in defined, f"{rule_id}: {required} missing from the emitted script"


@pytest.mark.parametrize("rule_id", sorted(BUILTIN))
def test_emitted_script_has_no_unresolved_library_names(rule_id):
    """Every bare name the script reads must be defined in it, imported by
    it, or a builtin. This is what makes it standalone."""
    script = _assemble(rule_id)
    tree = ast.parse(script)

    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif isinstance(node, ast.Import):
            defined.update((a.asname or a.name.split(".")[0]) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            defined.update((a.asname or a.name) for a in node.names)

    import builtins

    known = defined | set(dir(builtins))
    # Names bound inside functions (params, locals, comprehension targets) are
    # not module-level, so only check what the LIBRARY layer references.
    library_names = {
        symbol.name for symbol in codegen.build_index()[0].values()
    }
    referenced = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    missing = (referenced & library_names) - known
    assert not missing, f"{rule_id}: emitted script references undefined {sorted(missing)}"


def test_closure_is_tight_not_a_whole_library_dump():
    """An A1 script must not drag in the deletion machinery or the
    space-adjacency graph. If it does, the closure is not doing its job."""
    a1 = _assemble("A1")
    assert "def delete_element" not in a1
    assert "def dead_end_bridges" not in a1

    a5 = _assemble("A5")
    assert "def dead_end_bridges" in a5, "A5 must carry the graph builder it uses"

    s4 = _assemble("S4")
    assert "def delete_element" in s4, "S4 must carry the deletion primitive it uses"


def test_rule_module_absolute_imports_are_carried_over():
    """A1 uses collections.Counter. Dropping the rule module's own imports
    produces a script that compiles and then dies with NameError at runtime -
    which is exactly what happened before this was handled."""
    a1 = _assemble("A1")
    assert "from collections import Counter" in a1


def test_emitted_script_is_pure_ascii():
    """A generated script prints to a Windows console, which is frequently
    cp1252. A stray em dash in a clause string turns a working script into a
    UnicodeEncodeError at the worst possible moment."""
    for rule_id in sorted(BUILTIN):
        script = _assemble(rule_id)
        bad = {ch for ch in script if ord(ch) > 127}
        assert not bad, f"{rule_id}: non-ASCII characters in the emitted script: {bad}"
