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
     library  - the file it wrote must parse, must differ from the source in
     exactly the ways the mutation record declares, and the clause must
     actually be violated.

A pass checks ONE run in ONE mode, because the script writes one IFC per run.
The plain (`--no-color`) pass is the one that carries the guarantees and is
always made; a `--colored` pass is a separate execution, checking the marked
file that run produced. Each pass verifies what its own run created and
assumes nothing about a file it did not see.

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

from .config import NAME_TAG_TEMPLATE
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


def merge_passes(plain: ValidationResult, marked: ValidationResult) -> ValidationResult:
    """Fold a --colored pass into the plain one, as a single verdict.

    Both passes ran the same script, so their check names overlap. The marked
    pass's contribution is the marking checks; where a name collides the
    plain result is kept, because that is the run whose file is the actual
    deliverable. The verdict is the AND of the two: a script that cannot
    produce a findable marked file has not passed, even though the file you
    would hand a checker is fine.
    """
    seen = {c.name for c in plain.checks}
    combined = list(plain.checks) + [c for c in marked.checks if c.name not in seen]

    merged = ValidationResult(
        ok=plain.ok and marked.ok,
        fault=plain.fault if not plain.ok else marked.fault,
        retryable=plain.retryable if not plain.ok else marked.retryable,
        checks=combined,
        returncode=plain.returncode if plain.returncode else marked.returncode,
        stdout=plain.stdout,
        # The repair loop reads stderr to blame a function by traceback, so
        # the failing run's is the one that has to survive the merge.
        stderr=plain.stderr if not plain.ok else marked.stderr,
        outputs={**plain.outputs,
                 **{f"colored_{k}": v for k, v in marked.outputs.items()}},
        record=plain.record or marked.record,
    )
    merged.feedback = "\n".join(filter(None, [
        plain.feedback,
        f"[--colored pass] {marked.feedback}" if marked.feedback else "",
    ]))[:3000]
    return merged


# ---------------------------------------------------------------------------
# what the mutation record says is allowed to differ
# ---------------------------------------------------------------------------
def read_faults(record: dict) -> list[dict]:
    """The record's faults, normalised to one shape whatever wrote it.

    A multi-fault script writes a `mutations` array; a single-fault one
    writes the flat, singular shape it always has. Normalising here rather
    than at every use site means every check below is written once and works
    for a plan of one fault or twelve.

    Each entry: {"slot", "label", "rule_id", "mutation", "marking"}.
    """
    entries = record.get("mutations")
    if isinstance(entries, list):
        out = []
        for i, entry in enumerate(entries, start=1):
            mutation = entry.get("mutation") or {}
            rule_id = entry.get("rule_id") or mutation.get("rule_id") or ""
            out.append({
                "slot": entry.get("slot", i),
                "label": entry.get("label") or rule_id,
                "rule_id": rule_id,
                "mutation": mutation,
                "marking": entry.get("marking") or {},
                "colour": entry.get("colour") or {},
            })
        return out

    mutation = record.get("mutation") or {}
    rule_id = record.get("rule_id") or mutation.get("rule_id") or ""
    return [{
        "slot": 1,
        "label": rule_id,
        "rule_id": rule_id,
        "mutation": mutation,
        "marking": record.get("marking") or {},
        "colour": record.get("colour") or {},
    }]


def derive_allowed_sets_multi(source_model, output_model,
                              mutations: list[dict]) -> tuple[set, set, set]:
    """The union of what every fault in the plan declares.

    Each fault is accounted for exactly as it would be on its own, then the
    sets are unioned. The check that uses them stays as strict as it was for
    one fault: a GlobalId no fault declared is still an unexplained diff.

    One deliberate consequence of the union: if fault 2 modifies an element
    fault 1 already modified, the diff check cannot tell them apart. That is
    caught elsewhere - `main()` threads an exclusion set through
    `candidates()` precisely so two faults never choose the same target.
    """
    changed: set = set()
    removed: set = set()
    added: set = set()
    for mutation in mutations:
        c, r, a = derive_allowed_sets(source_model, output_model, mutation)
        changed |= c
        removed |= r
        added |= a
    return changed, removed, added


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
def check_marking(model, rule_id: str, name_tag: str,
                  pset_name: str) -> list[C.CheckResult]:
    """The colored file has to actually be findable in a viewer. Colour alone
    is not enough (Revit drops it), so at least one of the three handles must
    be verifiably present, and the marker box must always be.

    Takes an already-open model: the caller has parsed this file once
    already, and re-reading a 340MB IFC to count styled items is minutes
    spent for nothing.
    """
    results = []
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


def check_report_matches_record(report_path: Path, record: dict,
                                colored: bool = False) -> C.CheckResult:
    """The prose report must not contradict the machine-readable record.

    This exists because of a specific, recurring class of generated-code bug:
    the report function reads a key the marking function never wrote
    (`coloured_count` instead of `painted_items`), so the report cheerfully
    announces that nothing was coloured while the file is fully coloured.
    Nothing crashes and every section is present, so only a cross-check
    against the record catches it.

    `colored` catches the mirror-image bug the flag introduced: a
    `_report_bottom` that ignores ctx["colored"] and prints its marked-up
    boilerplate regardless, telling the reader to look for a colour and a
    marker box in a file that has neither.
    """
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return C.CheckResult("report_matches_record", False, message=f"unreadable: {e!r}")

    problems = []
    faults = read_faults(record)
    multi = len(faults) > 1

    stated = re.search(r"(?:colou?red|painted|styled)\s+items?\s*[:=]\s*(\d+)",
                       text, re.IGNORECASE)

    # EVERY fault must be findable in the prose. A report that describes two
    # of three injected faults is the multi-fault version of the wrong-key
    # bug: nothing crashes, every section is present, and the reader is
    # quietly told about less than the file contains.
    for entry in faults:
        where = f" (fault {entry['slot']}, {entry['label']})" if multi else ""
        target = entry["mutation"].get("target_global_id")
        if target and target not in text:
            problems.append(
                f"the target GlobalId {target}{where} does not appear in the report")

        # The colour legend is printed in both modes, so the rule's own colour
        # is expected in the text either way - it is only the CLAIM of having
        # applied it that is mode-dependent.
        colour_hex = (entry["colour"] or {}).get("hex")
        if colour_hex and colour_hex not in text:
            problems.append(f"the colour {colour_hex}{where} does not appear in the report")

    if multi:
        declared = record.get("fault_count")
        if isinstance(declared, int) and declared != len(faults):
            problems.append(
                f"the record says fault_count={declared} but carries {len(faults)} "
                f"mutation entries"
            )
        # The reader has to be able to tell there is more than one.
        if not re.search(r"\b%d\b" % len(faults), text):
            problems.append(
                f"the report never states that {len(faults)} faults were injected, so a "
                f"reader cannot tell it describes more than one"
            )

    if colored:
        # If the report states a coloured-item count, it has to be the real
        # one. With several faults the printed count belongs to whichever
        # fault the regex hit first, so it must match SOME fault's count
        # rather than one particular fault's.
        painted_counts = [e["marking"].get("painted_items") for e in faults]
        painted_counts = [p for p in painted_counts if isinstance(p, int)]
        if painted_counts and stated and int(stated.group(1)) not in painted_counts:
            problems.append(
                f"the report says {stated.group(1)} coloured item(s) but no fault in the "
                f"record reports that many ({painted_counts}) - the report is reading a "
                f"key the marking step never wrote"
            )

        if any(e["marking"].get("marker_global_ids") for e in faults) and "arker" not in text:
            problems.append("marker boxes were added but the report never mentions a marker")
    else:
        # Nothing was marked. A report that claims otherwise sends the reader
        # hunting for a colour that is not in the file.
        marked = [e["label"] for e in faults if e["marking"]]
        if marked:
            problems.append(
                f"the run was unmarked but the record carries a non-empty marking block "
                f"for {', '.join(marked)} - _mark_violation was called when it should "
                f"not have been"
            )
        if stated and int(stated.group(1)) > 0:
            problems.append(
                f"the report claims {stated.group(1)} coloured item(s) on a --no-color "
                f"run - _report_bottom is ignoring ctx['colored']"
            )
        if "--colored" not in text:
            problems.append(
                "an unmarked report never mentions --colored, so the reader is not told "
                "how to get a file they can actually find the fault in"
            )

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
               timeout_s: int = DEFAULT_TIMEOUT_S,
               colored: bool = False) -> subprocess.CompletedProcess:
    """Run the emitted script in the mode we are about to check.

    The flag is passed explicitly in BOTH directions rather than relying on
    the script's default, so a generated `main()` that gets the default the
    wrong way round is caught here instead of silently producing the other
    file than the one this validation pass is written to check.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(script_path),
         "--source", str(source_ifc), "--outdir", str(workdir),
         "--colored" if colored else "--no-color"],
        capture_output=True, text=True, timeout=timeout_s,
    )


def validate(script_path: Path, source_ifc: Path, workdir: Path, *,
             rule_id: str, output_stem: str, name_tag: str, pset_name: str,
             rule_is_synthesized: bool, colored: bool = False,
             expected_plan: tuple = (),
             timeout_s: int = DEFAULT_TIMEOUT_S) -> ValidationResult:
    """Execute the emitted script in one mode and check what that mode wrote.

    The script writes ONE IFC per run, so a pass checks exactly the file that
    run produced - there is no second file to reason about and none is
    assumed.

      colored=False  the default, and the mode that carries the guarantees
                     worth having: the file must parse, must violate the
                     clause, and must differ from the source in EXACTLY the
                     ways the mutation record declares.

      colored=True   the marked-up file. Same correctness checks minus the
                     strict diff - marking deliberately adds styled items,
                     a marker proxy and a property set, so a diff run here
                     would have to be loosened to pass, and a loosened diff
                     proves nothing. The strict version runs in the plain
                     pass instead. What this mode adds is proof that the
                     marking is actually findable in a viewer.
    """
    result = ValidationResult(ok=False)

    try:
        proc = run_script(script_path, source_ifc, workdir, timeout_s, colored=colored)
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

    # -- the three expected outputs ---------------------------------------
    ifc_name = f"{output_stem}_colored.ifc" if colored else f"{output_stem}.ifc"
    outputs = {
        "ifc": workdir / ifc_name,
        "report_txt": workdir / f"{output_stem}_report.txt",
        "record_json": workdir / f"{output_stem}_record.json",
    }
    result.outputs = {k: str(v) for k, v in outputs.items()}
    missing = [name for name, path in outputs.items() if not path.exists()]

    # The mode's OTHER file must not appear. A `main()` that ignores the flag
    # and writes both looks like a pass on every check below while quietly
    # handing a compliance checker a coloured file.
    other_name = f"{output_stem}.ifc" if colored else f"{output_stem}_colored.ifc"
    strays = [other_name] if (workdir / other_name).exists() else []

    result.checks.append(C.CheckResult(
        "all_outputs_written", not missing and not strays,
        {"expected": list(outputs), "missing": missing,
         "unexpected": strays, "mode": "--colored" if colored else "--no-color"},
        message=("" if not (missing or strays) else
                 "; ".join(filter(None, [
                     f"did not write: {', '.join(missing)}" if missing else "",
                     f"wrote {', '.join(strays)} despite "
                     f"{'--colored' if colored else '--no-color'}" if strays else "",
                 ]))),
    ))
    if missing or strays:
        result.fault = FAULT_HARNESS
        result.retryable = True
        flag = "--colored" if colored else "--no-color"
        parts = []
        if missing:
            parts.append(f"the script exited 0 but did not write {', '.join(missing)}. "
                         f"All three outputs are required.")
        if strays:
            parts.append(f"the script was run with {flag} and still wrote "
                         f"{', '.join(strays)}. Exactly ONE IFC is written per run, "
                         f"named {ifc_name} in this mode  - honour the flag.")
        result.feedback = " ".join(parts)
        return result

    # -- report + record ---------------------------------------------------
    result.checks.append(check_report_sections(outputs["report_txt"]))
    try:
        record = json.loads(outputs["record_json"].read_text(encoding="utf-8"))
        result.record = record
        faults = read_faults(record)
        if not faults or not faults[0]["mutation"]:
            raise ValueError("no mutation recorded")
        result.checks.append(C.CheckResult(
            "record_parsed", True,
            {"fault_count": len(faults),
             "labels": [f["label"] for f in faults],
             "targets": [f["mutation"].get("target_global_id") for f in faults]},
        ))
        result.checks.append(
            check_report_matches_record(outputs["report_txt"], record, colored=colored))
    except Exception as e:
        result.checks.append(C.CheckResult("record_parsed", False, message=repr(e)))
        result.fault = FAULT_HARNESS
        result.retryable = True
        result.feedback = (f"{outputs['record_json'].name} is missing or malformed ({e!r}). It "
                           f"must be valid JSON containing a 'mutations' array (or, for a "
                           f"single-fault script, a 'mutation' object).")
        return result

    # -- did the script deliver the plan it was named for? -----------------
    # A file called ..._A1x2_S1.ifc that contains two faults is a wrong test
    # case, not a partial one, so this is checked before anything else about
    # the content.
    if expected_plan:
        got = [f["rule_id"] for f in faults]
        result.checks.append(C.CheckResult(
            "plan_delivered", got == list(expected_plan),
            {"expected": list(expected_plan), "injected": got},
            message="" if got == list(expected_plan) else
                    f"the plan asked for {len(expected_plan)} fault(s) "
                    f"({', '.join(expected_plan)}) but the record declares {len(got)} "
                    f"({', '.join(got) or 'none'})",
        ))
        if got != list(expected_plan):
            result.fault = FAULT_HARNESS
            result.retryable = True
            result.feedback = (
                f"main() must inject EVERY fault in FAULTS, in order, or write nothing "
                f"and return non-zero. Expected {list(expected_plan)}, recorded {got}."
            )
            return result

    # -- the faulty file this mode wrote ----------------------------------
    parse = C.check_parses_cleanly(outputs["ifc"])
    if colored:
        # Owned by _mark_violation here: an unparseable file in this mode
        # means the marking broke it, not that the rule did.
        parse.name = "colored_parses_cleanly"
    result.checks.append(parse)
    if not parse.passed:
        result.fault = FAULT_HARNESS if colored else FAULT_RULE
        result.retryable = True if colored else rule_is_synthesized
        result.feedback = f"the faulty IFC does not reparse: {parse.message}"
        return result

    output_model = ifcopenshell.open(str(outputs["ifc"]))
    source_model = ifcopenshell.open(str(source_ifc))

    result.checks.append(C.check_no_dangling_references(output_model))
    result.checks.append(C.check_no_degenerate_relationships(output_model))

    # -- did every clause actually get violated? ---------------------------
    # Every fault is re-derived independently. One fault passing says nothing
    # about the next, and a plan is only as good as its weakest entry.
    multi = len(faults) > 1
    for entry in faults:
        entry_rule = (entry["rule_id"] or rule_id).upper()
        suffix = f" [{entry['label']}]" if multi else ""
        clause_check = C.CLAUSE_CHECKS.get(entry_rule)
        if clause_check is not None:
            try:
                check = clause_check(output_model, entry["mutation"], source_model)
            except Exception as e:
                check = C.CheckResult(
                    f"{entry_rule.lower()}_clause_violated", False,
                    message=f"the re-derivation itself raised: {e!r}",
                )
            if multi:
                check.name = f"{check.name}{suffix}"
            result.checks.append(check)
        else:
            result.checks.append(C.CheckResult(
                f"clause_rederivation_available{suffix}", True,
                {"rule_id": entry_rule},
                message="no independent re-derivation exists for this clause (it is newly "
                        "synthesized)  - structural checks only",
            ))

    # -- did anything else change? ----------------------------------------
    if not colored:
        changed, removed, added = derive_allowed_sets_multi(
            source_model, output_model, [f["mutation"] for f in faults])
        result.checks.append(C.check_no_unintended_diff(
            source_model, output_model, changed, removed, added
        ))

    # -- is the marked file actually findable? -----------------------------
    if colored:
        # Marking is checked once per rule, not once per fault: two A1 faults
        # share one IfcSurfaceStyle and one name-tag prefix by design, so
        # checking the rule twice would assert the same thing twice.
        seen_rules: set[str] = set()
        for entry in faults:
            entry_rule = (entry["rule_id"] or rule_id).upper()
            if entry_rule in seen_rules:
                continue
            seen_rules.add(entry_rule)
            tag = (NAME_TAG_TEMPLATE.format(rule_id=entry_rule)
                   if multi else name_tag)
            suffix = f" [{entry_rule}]" if multi else ""
            for check in check_marking(output_model, entry_rule, tag, pset_name):
                if multi:
                    check.name = f"{check.name}{suffix}"
                result.checks.append(check)

    # -- verdict ----------------------------------------------------------
    failures = result.failed()
    if not failures:
        result.ok = True
        result.fault = FAULT_NONE
        return result

    # Multi-fault check names carry a " [A1]" / " [A1#2]" suffix so a report
    # says WHICH fault failed; ownership is decided on the stem before it.
    harness_owned = {
        "report_complete", "report_matches_record", "record_parsed",
        "all_outputs_written", "script_completed", "plan_delivered",
        "colour_applied", "marker_box_present", "findable_by_name_or_property",
        "colored_parses_cleanly",
    }
    if any(f.name.split(" [")[0] in harness_owned for f in failures):
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
