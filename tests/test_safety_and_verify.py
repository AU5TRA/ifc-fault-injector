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
    result = validate.check_report_matches_record(report, record)
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
    assert validate.check_report_matches_record(report, record).passed


def test_report_record_crosscheck_notices_a_missing_target_id(tmp_path):
    report = tmp_path / "r.txt"
    report.write_text("HOW TO SPOT IT IN A VIEWER\n  Colour: RED (#E6194B)\n", encoding="utf-8")
    record = {
        "mutation": {"target_global_id": "0abcDEF"},
        "colour": {"hex": "#E6194B"},
        "marking": {},
    }
    result = validate.check_report_matches_record(report, record)
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
