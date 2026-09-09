"""
The gate an emitted script has to pass before it is handed over.

Nothing generated is trusted on the strength of looking right. A candidate
script is:

  1. checked statically (`safety.py`)  - never executed until it passes
  2. EXECUTED IN A SUBPROCESS against the real source model. Not imported,
     not exec'd in this process: an infinite loop, a segfault in
     ifcopenshell, or a stray `os.remove` in generated code stays inside a
     process boundary with a timeout on it.
  3. checked on its RESULTS by `verify/`, which shares no code with the rule
     library  - the files it wrote must parse, must differ from the source in
     exactly the ways the mutation record declares, and the clause must
     actually be violated.

Failures are then CLASSIFIED, because the right response differs:

  harness   - the generated harness is broken (crashed, wrote nothing, left a
             report section out). Feed the error back to the model and retry.
  rule      - the rule and this model do not fit (clause not actually
             violated, unexplained diff, structural damage). For a SAVED
             rule, retrying the model cannot help: the harness did its job
             and the rule logic is the reviewed kind. Escalate with evidence.
             For a SYNTHESIZED rule the model wrote the logic too, so this is
             feedback-worthy after all.
  environment  - the source file is missing, unreadable, or the rule reports
             itself inapplicable to this model. Nothing to fix in code.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import ifcopenshell

from .verify import checks as C

DEFAULT_TIMEOUT_S = 1800  # a 340MB IFC takes minutes just to parse

REQUIRED_REPORT_SECTIONS = (
    "INPUT MODEL",
    "INJECTED VIOLATION",
    "WHERE TO FIND IT",
    "HOW TO SPOT IT IN A VIEWER",
    "OUTPUT FILES",
    "SELF-CHECKS",
    "COLOUR LEGEND",
)

FAULT_NONE = "none"
FAULT_HARNESS = "harness"
FAULT_RULE = "rule"
FAULT_ENVIRONMENT = "environment"

#: The rule's three contract functions. A traceback dying inside one of these
#: is the RULE's fault, not the harness's - and getting that wrong is
#: expensive: the repair loop spends every round rewriting a `main()` that was
#: never broken while the real bug sits untouched in `candidates()`.
RULE_FUNCTIONS = ("applicable", "candidates", "apply_violation")


def innermost_frame(stderr: str) -> Optional[str]:
    """The function name of the deepest frame in a traceback."""
    if not stderr:
        return None
    frame = None
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("File ") and ", in " in stripped:
            frame = stripped.rsplit(", in ", 1)[1].strip()
    return frame


@dataclass
class ValidationResult:
    ok: bool
    fault: str = FAULT_NONE
    #: Whether feeding `feedback` back to the model could plausibly fix this.
    #: A saved rule that does not fit a model is not something the model can
    #: repair, so retrying would just burn calls and land in the same place.
    retryable: bool = False
    checks: list[C.CheckResult] = field(default_factory=list)
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    outputs: dict = field(default_factory=dict)
    record: Optional[dict] = None
    feedback: str = ""

    def failed(self) -> list[C.CheckResult]:
        return [c for c in self.checks if not c.passed]

    def text_report(self) -> str:
        lines = [
            "EMISSION-TIME VALIDATION",
            "",
            f"  result        : {'PASSED' if self.ok else 'FAILED'}",
            f"  fault class   : {self.fault}"
            + ("" if self.ok else f" (retryable: {self.retryable})"),
            f"  script exit   : {self.returncode}",
            "",
            "  Checks run by ifcfault/verify/, which shares no code with the rule",
            "  library  - an agreement here is an agreement reached twice, by two",
            "  independent routes.",
            "",
        ]
        for check in self.checks:
            lines.append(check.line())
        if not self.ok and self.feedback:
            lines += ["", "  WHY IT FAILED", ""]
            lines += [f"    {line}" for line in self.feedback.splitlines()]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# what the mutation record says is allowed to differ
# ---------------------------------------------------------------------------
def derive_allowed_sets(source_model, output_model, mutation: dict) -> tuple[set, set, set]:
    """(changed, removed, added) GlobalId sets the record accounts for.

    Anything outside these is an unexplained diff. Kept deliberately literal:
    each entry is here because some rule's record explicitly declares it, not
    because it seemed likely to be fine.
    """
    extra = mutation.get("extra", {}) or {}
    target = mutation.get("target_global_id")

    changed: set[str] = set()
    removed: set[str] = set()
    added: set[str] = set()

    is_deletion = mutation.get("attribute") == "(entity deleted)"
    if is_deletion:
        removed.update(extra.get("deleted_global_ids") or [target])
    elif mutation.get("rule_id") == "S5":
        # The target is the storey, which survives; its walls are what went.
        removed.update(extra.get("deleted_wall_global_ids") or [])
        changed.add(target)
    else:
        changed.add(target)

    # Cascades from delete_element, declared in full by the rule.
    removed.update(extra.get("cascade_removed_global_ids") or [])
    changed.update(extra.get("cascade_modified_global_ids") or [])

    # A5 removes one space boundary outright.
    severed = extra.get("severed_relationship_global_id")
    if severed:
        removed.add(severed)

    # A4's real edit is the opening's placement, not the door's attributes.
    opening = extra.get("opening_global_id")
    if opening:
        changed.add(opening)

    # A2/A3 fallback paths attach a NEW IfcPropertySet (plus its
    # IfcRelDefinesByProperties) to the target. Both are IfcRoot subtypes
    # with fresh GlobalIds, so discover them rather than guessing: any
    # property definition on the target that the source did not have is a
    # directly attributable side effect of this mutation.
    if target and not is_deletion:
        try:
            out_el = output_model.by_guid(target)
            src_el = source_model.by_guid(target)
            src_psets = {
                rel.RelatingPropertyDefinition.GlobalId
                for rel in source_model.get_inverse(src_el)
                if rel.is_a("IfcRelDefinesByProperties")
                and hasattr(rel.RelatingPropertyDefinition, "GlobalId")
            }
            for rel in output_model.get_inverse(out_el):
                if not rel.is_a("IfcRelDefinesByProperties"):
                    continue
                pdef = rel.RelatingPropertyDefinition
                if getattr(pdef, "GlobalId", None) and pdef.GlobalId not in src_psets:
                    added.add(pdef.GlobalId)
                    added.add(rel.GlobalId)
        except Exception:
            pass

    changed.discard(None)
    removed.discard(None)
    return changed, removed, added


# ---------------------------------------------------------------------------
# marked-file checks
# ---------------------------------------------------------------------------
def check_marking(colored_path: Path, rule_id: str, name_tag: str,
                  pset_name: str) -> list[C.CheckResult]:
    """The colored file has to actually be findable in a viewer. Colour alone
    is not enough (Revit drops it), so at least one of the three handles must
    be verifiably present, and the marker box must always be."""
    results = []
    parse = C.check_parses_cleanly(colored_path)
    parse.name = "colored_parses_cleanly"
    results.append(parse)
    if not parse.passed:
        return results

    model = ifcopenshell.open(str(colored_path))

    styled = len(model.by_type("IfcStyledItem"))
    styles = [s for s in model.by_type("IfcSurfaceStyle")
              if (s.Name or "").upper() == f"VIOLATION_{rule_id.upper()}"]
    results.append(C.CheckResult(
        "colour_applied", bool(styles) and styled > 0,
        {"violation_surface_styles": len(styles), "styled_items_total": styled},
        message="" if styles else f"no IfcSurfaceStyle named VIOLATION_{rule_id} was created",
    ))

    markers = [p for p in model.by_type("IfcBuildingElementProxy")
               if (p.ObjectType or "") == "ViolationMarker"]
    results.append(C.CheckResult(
        "marker_box_present", bool(markers),
        {"marker_count": len(markers),
         "marker_names": [m.Name for m in markers[:3]]},
        message="" if markers else "no ViolationMarker proxy was added, so a viewer that "
                                   "ignores IFC colours has nothing to show",
    ))

    tagged = [e for e in model.by_type("IfcRoot")
              if getattr(e, "Name", None) and name_tag in e.Name]
    pset_hits = [p for p in model.by_type("IfcPropertySet") if p.Name == pset_name]
    results.append(C.CheckResult(
        "findable_by_name_or_property", bool(tagged) or bool(pset_hits),
        {"name_tagged_elements": len(tagged), "violation_psets": len(pset_hits)},
        message="" if (tagged or pset_hits) else
                "neither the name tag nor the violation property set is present",
    ))
    return results


def check_report_matches_record(report_path: Path, record: dict) -> C.CheckResult:
    """The prose report must not contradict the machine-readable record.

    This exists because of a specific, recurring class of generated-code bug:
    the report function reads a key the marking function never wrote
    (`coloured_count` instead of `painted_items`), so the report cheerfully
    announces that nothing was coloured while the file is fully coloured.
    Nothing crashes and every section is present, so only a cross-check
    against the record catches it.
    """
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return C.CheckResult("report_matches_record", False, message=f"unreadable: {e!r}")

    problems = []
    mutation = record.get("mutation", {}) or {}
    marking = record.get("marking", {}) or {}
    colour = record.get("colour", {}) or {}

    target = mutation.get("target_global_id")
    if target and target not in text:
        problems.append(f"the target GlobalId {target} does not appear in the report")
    if colour.get("hex") and colour["hex"] not in text:
        problems.append(f"the colour {colour['hex']} does not appear in the report")

    # If the report states a coloured-item count, it has to be the real one.
    painted = marking.get("painted_items")
    if isinstance(painted, int):
        stated = re.search(r"(?:colou?red|painted|styled)\s+items?\s*[:=]\s*(\d+)",
                           text, re.IGNORECASE)
        if stated and int(stated.group(1)) != painted:
            problems.append(
                f"the report says {stated.group(1)} coloured item(s) but the record says "
                f"{painted} - the report is reading a key the marking step never wrote"
            )

    markers = marking.get("marker_global_ids") or []
    if markers and "arker" not in text:
        problems.append("marker boxes were added but the report never mentions a marker")

    return C.CheckResult(
        "report_matches_record", not problems, {"problems": problems},
        message="; ".join(problems)[:400],
    )


def check_report_sections(report_path: Path) -> C.CheckResult:
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return C.CheckResult("report_complete", False, message=f"unreadable: {e!r}")
    missing = [s for s in REQUIRED_REPORT_SECTIONS if s not in text]
    return C.CheckResult(
        "report_complete", not missing,
        {"length_chars": len(text), "missing_sections": missing},
        message="" if not missing else f"report is missing section(s): {', '.join(missing)}",
    )


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def run_script(script_path: Path, source_ifc: Path, workdir: Path,
               timeout_s: int = DEFAULT_TIMEOUT_S) -> subprocess.CompletedProcess:
    workdir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(script_path),
         "--source", str(source_ifc), "--outdir", str(workdir)],
        capture_output=True, text=True, timeout=timeout_s,
    )


def validate(script_path: Path, source_ifc: Path, workdir: Path, *,
             rule_id: str, output_stem: str, name_tag: str, pset_name: str,
             rule_is_synthesized: bool,
             timeout_s: int = DEFAULT_TIMEOUT_S) -> ValidationResult:
    """Execute the emitted script and check everything it produced."""
    result = ValidationResult(ok=False)

    try:
        proc = run_script(script_path, source_ifc, workdir, timeout_s)
    except subprocess.TimeoutExpired:
        result.fault = FAULT_HARNESS
        result.retryable = True
        result.feedback = (f"the script did not finish within {timeout_s}s  - most likely an "
                           f"unbounded loop in the harness")
        result.checks.append(C.CheckResult("script_completed", False, message="timed out"))
        return result

    result.returncode = proc.returncode
    result.stdout = proc.stdout or ""
    result.stderr = proc.stderr or ""

    if proc.returncode != 0:
        combined = (result.stdout + "\n" + result.stderr).lower()
        inapplicable = "not applicable" in combined or "no candidates" in combined
        frame = innermost_frame(result.stderr)
        crashed_in_rule = frame in RULE_FUNCTIONS

        if inapplicable:
            result.fault = FAULT_ENVIRONMENT
            result.retryable = False
            message = "the rule reports itself inapplicable to this model"
        elif crashed_in_rule:
            # The rule's own code raised. Only the model can fix that, and
            # only if the model wrote the rule in the first place.
            result.fault = FAULT_RULE
            result.retryable = rule_is_synthesized
            message = f"the script crashed inside the rule's {frame}()"
        else:
            result.fault = FAULT_HARNESS
            result.retryable = True
            message = (f"the script crashed inside {frame}()" if frame
                       else "the script exited non-zero")

        result.checks.append(C.CheckResult(
            "script_completed", False,
            {"returncode": proc.returncode, "innermost_frame": frame},
            message=message,
        ))
        tail = (result.stderr or result.stdout or "").strip()
        result.feedback = tail[-3000:] if tail else f"exit code {proc.returncode}, no output"
        return result

    result.checks.append(C.CheckResult("script_completed", True, {"returncode": 0}))

    # -- the four expected outputs ----------------------------------------
    outputs = {
        "unmarked_ifc": workdir / f"{output_stem}.ifc",
        "colored_ifc": workdir / f"{output_stem}_colored.ifc",
        "report_txt": workdir / f"{output_stem}_report.txt",
        "record_json": workdir / f"{output_stem}_record.json",
    }
    result.outputs = {k: str(v) for k, v in outputs.items()}
    missing = [name for name, path in outputs.items() if not path.exists()]
    result.checks.append(C.CheckResult(
        "all_outputs_written", not missing,
        {"expected": list(outputs), "missing": missing},
        message="" if not missing else f"did not write: {', '.join(missing)}",
    ))
    if missing:
        result.fault = FAULT_HARNESS
        result.retryable = True
        result.feedback = (f"the script exited 0 but did not write {', '.join(missing)}. "
                           f"Every one of the four outputs is required.")
        return result

    # -- report + record ---------------------------------------------------
    result.checks.append(check_report_sections(outputs["report_txt"]))
    try:
        record = json.loads(outputs["record_json"].read_text(encoding="utf-8"))
        result.record = record
        mutation = record["mutation"]
        result.checks.append(C.CheckResult(
            "record_parsed", True,
            {"rule_id": record.get("rule_id"), "target": mutation.get("target_global_id")},
        ))
        result.checks.append(check_report_matches_record(outputs["report_txt"], record))
    except Exception as e:
        result.checks.append(C.CheckResult("record_parsed", False, message=repr(e)))
        result.fault = FAULT_HARNESS
        result.retryable = True
        result.feedback = (f"{outputs['record_json'].name} is missing or malformed ({e!r}). It "
                           f"must be valid JSON containing a 'mutation' object.")
        return result

    # -- the unmarked faulty file -----------------------------------------
    parse = C.check_parses_cleanly(outputs["unmarked_ifc"])
    result.checks.append(parse)
    if not parse.passed:
        result.fault = FAULT_RULE
        result.retryable = rule_is_synthesized
        result.feedback = f"the faulty IFC does not reparse: {parse.message}"
        return result

    output_model = ifcopenshell.open(str(outputs["unmarked_ifc"]))
    source_model = ifcopenshell.open(str(source_ifc))

    result.checks.append(C.check_no_dangling_references(output_model))
    result.checks.append(C.check_no_degenerate_relationships(output_model))

    # -- did the clause actually get violated? -----------------------------
    clause_check = C.CLAUSE_CHECKS.get(rule_id.upper())
    if clause_check is not None:
        try:
            result.checks.append(clause_check(output_model, mutation, source_model))
        except Exception as e:
            result.checks.append(C.CheckResult(
                f"{rule_id.lower()}_clause_violated", False,
                message=f"the re-derivation itself raised: {e!r}",
            ))
    else:
        result.checks.append(C.CheckResult(
            "clause_rederivation_available", True,
            {"rule_id": rule_id},
            message="no independent re-derivation exists for this clause (it is newly "
                    "synthesized)  - structural checks only",
        ))

    # -- did anything else change? ----------------------------------------
    changed, removed, added = derive_allowed_sets(source_model, output_model, mutation)
    result.checks.append(C.check_no_unintended_diff(
        source_model, output_model, changed, removed, added
    ))

    # -- is the marked file actually findable? -----------------------------
    result.checks.extend(check_marking(outputs["colored_ifc"], rule_id, name_tag, pset_name))

    # -- verdict ----------------------------------------------------------
    failures = result.failed()
    if not failures:
        result.ok = True
        result.fault = FAULT_NONE
        return result

    harness_owned = {
        "report_complete", "report_matches_record", "record_parsed",
        "all_outputs_written", "script_completed",
        "colour_applied", "marker_box_present", "findable_by_name_or_property",
        "colored_parses_cleanly",
    }
    if any(f.name in harness_owned for f in failures):
        result.fault = FAULT_HARNESS
        result.retryable = True
    else:
        # Structural or clause failure: the harness did its job, but the rule
        # and this model did not agree. Retrying only helps if the model wrote
        # the rule in the first place.
        result.fault = FAULT_RULE
        result.retryable = rule_is_synthesized

    result.feedback = "\n".join(
        f"{f.name}: {f.message or f.evidence}" for f in failures
    )[:3000]
    return result
