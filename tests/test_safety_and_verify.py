"""
The three gates: the static safety check, the independence of the verifier,
and the cross-checks that catch generated code which "works" but lies.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ifcfault import harness as harness_mod
from ifcfault import synth, validate
from ifcfault.safety import check_source
from ifcfault.verify import checks as C

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# static safety gate
# ---------------------------------------------------------------------------
def test_accepts_a_reasonable_harness():
    report = check_source(
        "def main(argv=None):\n"
        "    import json, os\n"
        "    return 0\n",
        required_defs=("main",),
    )
    assert report.ok, report.violations


@pytest.mark.parametrize("snippet, expected", [
    ("import subprocess\ndef main():\n    return 0\n", "subprocess"),
    ("import shutil\ndef main():\n    return 0\n", "shutil"),
    ("def main():\n    eval('1')\n", "eval"),
    ("def main():\n    exec('x=1')\n", "exec"),
    ("import os\ndef main():\n    os.system('dir')\n", "os.system"),
    ("import os\ndef main():\n    os.remove('x')\n", "os.remove"),
    ("import httpx\ndef main():\n    return 0\n", "httpx"),
    ("from .helpers import mm\ndef main():\n    return 0\n", "relative import"),
])
def test_rejects_dangerous_code(snippet, expected):
    report = check_source(snippet, required_defs=("main",))
    assert not report.ok
    assert any(expected.split(".")[-1] in v for v in report.violations), report.violations


def test_reports_a_missing_required_definition():
    report = check_source("x = 1\n", required_defs=("main",))
    assert not report.ok
    assert any("main" in v for v in report.violations)


def test_rule_gate_bans_file_and_argument_handling():
    """A rule only reads and edits the in-memory model. A rule that opens
    files or parses arguments has misunderstood its job."""
    violations = synth.check_rule_source(
        "import os\n"
        "RULE_ID = 'X1'\nCLAUSE = 'c'\nDOMAIN = 'architectural'\n"
        "def applicable(model): pass\n"
        "def candidates(model, exclude=frozenset()): pass\n"
        "def apply_violation(model, target, params): pass\n"
    )
    assert any("must not import os" in v for v in violations), violations


def test_rule_gate_bans_nondeterminism():
    violations = synth.check_rule_source(
        "import random\n"
        "RULE_ID = 'X1'\nCLAUSE = 'c'\nDOMAIN = 'architectural'\n"
        "def applicable(model): pass\n"
        "def candidates(model, exclude=frozenset()): pass\n"
        "def apply_violation(model, target, params): return random.random()\n"
    )
    assert any("random" in v for v in violations), violations


# ---------------------------------------------------------------------------
# verifier independence
# ---------------------------------------------------------------------------
def test_verify_package_never_imports_the_rule_library():
    """The whole point of verify/ is that a bug shared with the injector
    cannot hide a real defect. Sharing code would quietly end that.

    Checked against the AST rather than the raw text, so the module is free
    to DISCUSS the library in its docstring without failing its own test.
    """
    import ast

    source = (REPO_ROOT / "ifcfault" / "verify" / "checks.py").read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(("." * (node.level or 0)) + (node.module or ""))

    for name in imported:
        assert "library" not in name, f"verify/checks.py imports {name!r}"


def test_verifier_derives_units_the_other_way_round():
    """helpers.length_unit_scale is native-per-mm; verify.mm_per_native is
    mm-per-native. Two conventions means an error in one does not cancel in
    the other."""
    from ifcfault.library.helpers import length_unit_scale

    assert "mm_per_native" in dir(C)
    assert length_unit_scale is not C.mm_per_native


# ---------------------------------------------------------------------------
# report / record cross-check
# ---------------------------------------------------------------------------
def test_report_record_crosscheck_catches_a_wrong_key(tmp_path):
    """The exact bug this check exists for: the report function reads a key
    the marking function never wrote, so it announces zero coloured items
    while the file is fully coloured. Every section is present and nothing
    crashes, so only this catches it."""
    report = tmp_path / "r.txt"
    report.write_text(
        "WHERE TO FIND IT\n  Global ID: 0abcDEF\n"
        "HOW TO SPOT IT IN A VIEWER\n  Colour: RED (#E6194B)\n"
        "  Coloured items: 0\n  Marker box size: 500\n",
        encoding="utf-8",
    )
    record = {
        "mutation": {"target_global_id": "0abcDEF"},
        "colour": {"hex": "#E6194B"},
        "marking": {"painted_items": 3, "marker_global_ids": ["m1"]},
    }
    result = validate.check_report_matches_record(report, record, colored=True)
    assert not result.passed
    assert "3" in result.message and "0" in result.message


def test_report_record_crosscheck_passes_when_consistent(tmp_path):
    report = tmp_path / "r.txt"
    report.write_text(
        "WHERE TO FIND IT\n  Global ID: 0abcDEF\n"
        "HOW TO SPOT IT IN A VIEWER\n  Colour: RED (#E6194B)\n"
        "  Coloured items: 3\n  Marker box at ...\n",
        encoding="utf-8",
    )
    record = {
        "mutation": {"target_global_id": "0abcDEF"},
        "colour": {"hex": "#E6194B"},
        "marking": {"painted_items": 3, "marker_global_ids": ["m1"]},
    }
    assert validate.check_report_matches_record(report, record, colored=True).passed


def test_report_record_crosscheck_notices_a_missing_target_id(tmp_path):
    report = tmp_path / "r.txt"
    report.write_text("HOW TO SPOT IT IN A VIEWER\n  Colour: RED (#E6194B)\n", encoding="utf-8")
    record = {
        "mutation": {"target_global_id": "0abcDEF"},
        "colour": {"hex": "#E6194B"},
        "marking": {},
    }
    result = validate.check_report_matches_record(report, record, colored=True)
    assert not result.passed
    assert "0abcDEF" in result.message


def test_missing_report_sections_are_reported(tmp_path):
    report = tmp_path / "r.txt"
    report.write_text("INPUT MODEL\nINJECTED VIOLATION\n", encoding="utf-8")
    result = validate.check_report_sections(report)
    assert not result.passed
    assert "COLOUR LEGEND" in result.message


# ---------------------------------------------------------------------------
# blame attribution
# ---------------------------------------------------------------------------
def test_traceback_blames_the_function_that_actually_raised():
    """Attributing every crash to main() burns the whole repair budget
    rewriting a function that was never broken."""
    stderr = (
        'Traceback (most recent call last):\n'
        '  File "s.py", line 1330, in <module>\n'
        '    raise SystemExit(main())\n'
        '  File "s.py", line 1223, in main\n'
        '    f.write(_report_top(ctx))\n'
        '  File "s.py", line 950, in _report_top\n'
        '    if RULE_DELETES_ELEMENTS:\n'
        'NameError: name \'RULE_DELETES_ELEMENTS\' is not defined\n'
    )
    assert harness_mod.blamed_by_traceback(stderr) == "_report_top"


def test_a_crash_inside_the_rule_is_the_rules_fault_not_the_harnesses():
    stderr = (
        'Traceback (most recent call last):\n'
        '  File "s.py", line 1223, in main\n'
        '    targets = candidates(model)\n'
        '  File "s.py", line 908, in candidates\n'
        '    return sorted_by_guid(targets)\n'
        "AttributeError: 'ScoredTarget' object has no attribute 'GlobalId'\n"
    )
    assert validate.innermost_frame(stderr) == "candidates"
    assert validate.innermost_frame(stderr) in validate.RULE_FUNCTIONS
    # ...and it is not one of the harness parts, so harness blaming declines it
    assert harness_mod.blamed_by_traceback(stderr) is None


# ---------------------------------------------------------------------------
# --colored / --no-color: one IFC per run
# ---------------------------------------------------------------------------
def test_unmarked_report_may_not_claim_colouring(tmp_path):
    """The mirror image of the wrong-key bug, introduced by the flag: a
    _report_bottom that ignores ctx["colored"] prints its marked-up
    boilerplate on an unmarked run, sending the reader hunting for a colour
    that is not in the file."""
    report = tmp_path / "r.txt"
    report.write_text(
        "WHERE TO FIND IT\n  Global ID: 0abcDEF\n"
        "HOW TO SPOT IT IN A VIEWER\n  Colour: RED (#E6194B)\n"
        "  Coloured items: 3\n  Re-run with --colored for a marked copy.\n",
        encoding="utf-8",
    )
    record = {
        "mutation": {"target_global_id": "0abcDEF"},
        "colour": {"hex": "#E6194B"},
        "marking": {},
    }
    result = validate.check_report_matches_record(report, record, colored=False)
    assert not result.passed
    assert "--no-color" in result.message


def test_unmarked_run_may_not_carry_a_marking_block(tmp_path):
    """_mark_violation must not be called at all without --colored. If the
    record shows it ran, the flag is being ignored."""
    report = tmp_path / "r.txt"
    report.write_text(
        "WHERE TO FIND IT\n  Global ID: 0abcDEF\n"
        "HOW TO SPOT IT IN A VIEWER\n  Colour: RED (#E6194B)\n"
        "  No visual marking; re-run with --colored.\n",
        encoding="utf-8",
    )
    record = {
        "mutation": {"target_global_id": "0abcDEF"},
        "colour": {"hex": "#E6194B"},
        "marking": {"painted_items": 3, "marker_global_ids": ["m1"]},
    }
    result = validate.check_report_matches_record(report, record, colored=False)
    assert not result.passed
    assert "marking" in result.message


def test_unmarked_report_tells_the_reader_how_to_get_a_marked_file(tmp_path):
    report = tmp_path / "r.txt"
    report.write_text(
        "WHERE TO FIND IT\n  Global ID: 0abcDEF\n"
        "HOW TO SPOT IT IN A VIEWER\n  Colour: RED (#E6194B)\n"
        "  No visual marking was applied.\n",
        encoding="utf-8",
    )
    record = {
        "mutation": {"target_global_id": "0abcDEF"},
        "colour": {"hex": "#E6194B"},
        "marking": {},
    }
    result = validate.check_report_matches_record(report, record, colored=False)
    assert not result.passed
    assert "--colored" in result.message

    report.write_text(
        "WHERE TO FIND IT\n  Global ID: 0abcDEF\n"
        "HOW TO SPOT IT IN A VIEWER\n  Colour: RED (#E6194B)\n"
        "  No visual marking. Re-run with --colored for a marked-up copy.\n",
        encoding="utf-8",
    )
    assert validate.check_report_matches_record(report, record, colored=False).passed


def test_run_script_always_passes_the_mode_explicitly(tmp_path, monkeypatch):
    """Neither mode may rely on the generated script's own default: a main()
    that gets the default backwards would otherwise write the other file and
    every check would be run against the wrong one."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()

    monkeypatch.setattr(validate.subprocess, "run", fake_run)

    validate.run_script(tmp_path / "s.py", tmp_path / "m.ifc", tmp_path / "w",
                        colored=False)
    assert "--no-color" in seen["cmd"] and "--colored" not in seen["cmd"]

    validate.run_script(tmp_path / "s.py", tmp_path / "m.ifc", tmp_path / "w",
                        colored=True)
    assert "--colored" in seen["cmd"] and "--no-color" not in seen["cmd"]


def test_merge_passes_fails_the_whole_emission_if_marking_fails():
    """A script whose unmarked file is perfect but whose marking is not
    findable has not passed. The plain pass's verdict alone would say it
    had."""
    plain = validate.ValidationResult(
        ok=True, fault=validate.FAULT_NONE,
        checks=[C.CheckResult("a1_clause_violated", True)],
        returncode=0, record={"rule_id": "A1"},
    )
    marked = validate.ValidationResult(
        ok=False, fault=validate.FAULT_HARNESS, retryable=True,
        checks=[C.CheckResult("a1_clause_violated", True),
                C.CheckResult("marker_box_present", False, message="no marker")],
        returncode=0, feedback="marker_box_present: no marker",
    )
    merged = validate.merge_passes(plain, marked)

    assert not merged.ok
    assert merged.fault == validate.FAULT_HARNESS and merged.retryable
    names = [c.name for c in merged.checks]
    assert names.count("a1_clause_violated") == 1, "a colliding check was duplicated"
    assert "marker_box_present" in names
    assert "--colored pass" in merged.feedback


def test_harness_prompt_pins_the_flag_spelling():
    """The generated main() gets these two flags by being told them
    literally. validate.run_script passes them literally too, so a drift in
    either spelling breaks every emission - pin both to the same strings."""
    from ifcfault import harness as H

    main_task = next(s for s in H.PARTS if s.name == "main").task
    assert '"--colored"' in main_task
    assert '"--no-color"' in main_task
    assert 'set_defaults(colored=False)' in main_task, "marking must default OFF"


def test_context_keys_describe_one_ifc_not_two():
    from ifcfault import harness as H

    assert 'ctx["colored"]' in H.CONTEXT_KEYS
    assert "unmarked, colored, report, record" not in H.CONTEXT_KEYS
