"""
Generating the harness, one bounded function at a time.

The harness is NOT asked for in a single call. It is generated as four small
functions, each with a fixed signature, each requested and validated on its
own, then concatenated.

Two independent reasons, and they point the same way:

  * A 32B code model degrades badly on a long multi-step generation. Asked
    for "a main() that opens a model, mutates it, colours it, writes two IFCs
    and a seven-section report", it produces something that looks right and
    quietly drops a step. Asked for one function with one job and a named
    return shape, it is reliable.

  * The provider currently serving this model through OpenRouter truncates a
    long completion (HTTP 200, finish_reason "error", body cut mid-token),
    and asking it to continue does not splice cleanly - it re-opens the code
    fence and repeats half a line. Individually, each part here fits well
    inside what it will actually deliver.

The parts communicate through one `ctx` dict whose keys are fixed by
CONTEXT_KEYS below, so no part needs to know how any other is implemented.
A part that fails its static gate is regenerated on its own; the others are
untouched.
"""
from __future__ import annotations

from dataclasses import dataclass

from .llm import QwenClient, render_prompt
from .safety import check_source
from .synth import extract_code_block

MAX_PART_ATTEMPTS = 4

#: The keys every generated part can rely on finding in `ctx`.
CONTEXT_KEYS = """
    ctx["source"]            str    the --source path actually used
    ctx["outdir"]            str    the --outdir path actually used
    ctx["source_sha256"]     str
    ctx["schema"]            str    model.schema
    ctx["unit"]              str    length_unit_name(model)
    ctx["entity_count"]      int    len(model.by_type("IfcRoot"))
    ctx["target"]            ScoredTarget  the chosen candidate
    ctx["mutation"]          Mutation      what apply_violation returned
    ctx["where"]             dict   describe_element output for the target,
                                    or {} when the target was deleted
    ctx["marking"]           dict   what _mark_violation returned
    ctx["paths"]             dict   keys: unmarked, colored, report, record
    ctx["hashes"]            dict   keys: unmarked, colored (may be missing)
    ctx["self_checks"]       dict   keys: dangling_references (int),
                                    unmarked_reparsed (bool), warnings (list)
"""


@dataclass
class PartSpec:
    name: str
    signature: str
    task: str
    max_tokens: int = 1500


PARTS: tuple[PartSpec, ...] = (
    PartSpec(
        name="_mark_violation",
        signature="_mark_violation(model, mutation, target, source_path)",
        task="""
Mark the violation up so a human can find it in a 3D viewer. The faulty IFC
has ALREADY been written unmarked by the caller, so you are free to modify
`model` in place here.

Do all of this:

  1. style_cache = {} ; style = violation_style(model, RULE_ID, COLOUR_RGB, style_cache)

  2. If mutation.attribute is not "(entity deleted)" and RULE_ID != "S5":
     resolve element = model.by_guid(mutation.target_global_id), then
       painted = paint_element(model, element, style)
       name_before, name_after = tag_element_name(model.by_guid(mutation.target_global_id), RULE_ID, "[!{rule_id} VIOLATION!] ")
       attach_violation_pset(model, element, fields, VIOLATION_PSET)
     where `fields` is a dict of plain strings containing at least:
       "ViolationRule": RULE_ID, "ViolationClause": RULE_CLAUSE,
       "ViolationColour": COLOUR_NAME + " " + COLOUR_HEX,
       "ViolationAttribute": str(mutation.attribute),
       "ViolationBefore": str(mutation.before), "ViolationAfter": str(mutation.after),
       "ViolationDescription": str(mutation.description)
     Otherwise set painted = 0 and name_before = name_after = None.

  3. Work out where to put marker boxes, as a list of (x, y, z) mm tuples:
       - mutation.extra.get("deleted_original_locations_mm") is a dict of
         GlobalId -> [x, y, z]; use its values if present
       - else mutation.extra.get("deleted_walls") is a list of dicts each
         with a "location_mm" key; use those (cap at 12 boxes)
       - else if the element still exists, use global_xyz_mm(model, element)
     Skip any location that is None.

  4. For each location, call add_marker_box(model, location, storey, RULE_ID,
     name, description, style, MARKER_SIZE_MM, guid_seed) where `storey` is
     storey_of(model, element) when the element exists else None, `name` is
     NAME_TAG_PREFIX + a short label, and guid_seed is str(index).
     Collect the returned proxies' GlobalId values.

  5. Wrap steps 2 and 4 in try/except Exception and append a readable string
     to a `warnings` list on failure. Marking is a convenience; losing it
     must never raise.

Return exactly this dict:

    {"painted_items": int, "name_before": ..., "name_after": ...,
     "pset": VIOLATION_PSET, "marker_locations_mm": [...],
     "marker_global_ids": [...], "warnings": [...]}
""",
        max_tokens=1800,
    ),
    PartSpec(
        name="_report_top",
        signature="_report_top(ctx)",
        task="""
Return a STRING: the first half of the human-readable report. It must contain
these three headings, each alone on its own line, spelled exactly:

    INPUT MODEL
    INJECTED VIOLATION
    WHERE TO FIND IT

Under INPUT MODEL: the source path, its sha256, the IFC schema, the length
unit, and the entity count.

Under INJECTED VIOLATION: RULE_ID, RULE_DOMAIN, the full RULE_CLAUSE,
ctx["mutation"].description, the attribute changed, before -> after, the
mechanism from ctx["mutation"].extra.get("mechanism"), and why this element
was chosen (ctx["target"].justification).

Under WHERE TO FIND IT: ALWAYS print ctx["mutation"].target_global_id first,
on its own line, whatever ctx["where"] contains - for a rule that deletes
elements or targets a storey, ctx["where"] is empty and that id is the only
handle there is. Then everything in ctx["where"] when it is non-empty
(ifc_type, name, global_id, step_id, storey_name, storey_elevation_mm,
world_xyz_mm), and then EVERY key/value pair of ctx["mutation"].extra, one
per line, sorted by key, so nothing the rule recorded is lost. When
ctx["where"] is empty, say the target element was deleted or is a storey and
rely on the extra dict.

Format millimetre floats to one decimal place. Indent detail lines by two
spaces under their heading. Separate sections with a blank line and a line of
dashes.
""",
        max_tokens=1600,
    ),
    PartSpec(
        name="_report_bottom",
        signature="_report_bottom(ctx)",
        task="""
Return a STRING: the second half of the report. It must contain these four
headings, each alone on its own line, spelled exactly:

    HOW TO SPOT IT IN A VIEWER
    OUTPUT FILES
    SELF-CHECKS
    COLOUR LEGEND

Under HOW TO SPOT IT IN A VIEWER, read these EXACT keys - do not invent key
names, a wrong key silently reports zero:

    ctx["marking"]["painted_items"]        int, how many items were coloured
    ctx["marking"]["name_after"]           the searchable name tag
    ctx["marking"]["pset"]                 the property set name
    ctx["marking"]["marker_locations_mm"]  list of (x, y, z)

Print COLOUR_NAME and COLOUR_HEX, those four values, and MARKER_SIZE_MM. Use
`.get(key, default)` but with exactly those key spellings. Then include this
line verbatim, on a line of its own:

Revit's IFC import often discards IfcSurfaceStyle colours - if the element is not coloured, search for the name tag above or look for the marker box.

Under OUTPUT FILES: each path in ctx["paths"] with its hash from
ctx["hashes"] when present, and one line each saying what it is for. Use
these descriptions, which are accurate - do NOT describe the unmarked file
as unmodified, because it is the faulty model:

    unmarked : "The faulty model, with no visual marking - feed this to a
                compliance checker."
    colored  : "The same faults, marked up for a human - open this in Revit
                or an IFC viewer."
    report   : "This report."
    record   : "The same facts, machine-readable."

Under SELF-CHECKS: ctx["self_checks"]["dangling_references"],
ctx["self_checks"]["unmarked_reparsed"], and every string in
ctx["self_checks"]["warnings"]. State plainly that these are structural
self-checks only, and that independent clause verification was done when the
script was generated, not here.

Under COLOUR LEGEND: every (rule_id, name, hex) triple in COLOUR_LEGEND, one
per line.

Same formatting conventions as the first half.
""",
        max_tokens=1600,
    ),
    PartSpec(
        name="main",
        signature="main(argv=None)",
        task="""
Orchestrate everything and return an int exit code.

  1. argparse: --source (default SOURCE_IFC), --outdir (default "."),
     --params (default "{}"). Then os.makedirs(outdir, exist_ok=True).

  2. model = ifcopenshell.open(source). Call applicable(model); if not .ok,
     print the reason and return 2 without writing anything. Call
     candidates(model); if empty, print that and return 2. Choose with
     best_target(...).

  3. mutation = apply_violation(model, target, json.loads(args.params))

  4. Build paths: unmarked = os.path.join(outdir, OUTPUT_STEM + ".ifc"),
     colored = ... + "_colored.ifc", report = ... + "_report.txt",
     record = ... + "_record.json".

  5. model.write(unmarked)  -- BEFORE any marking, so it carries no hint.

  6. where = describe_element(model, model.by_guid(mutation.target_global_id))
     if mutation.attribute != "(entity deleted)" and RULE_ID != "S5", else {}.
     Wrap in try/except and fall back to {}.

  7. marking = _mark_violation(model, mutation, target, source)
     then model.write(colored)

  8. self_checks: dangling = len(find_dangling_references(model));
     reparsed = True/False from trying ifcopenshell.open(unmarked) in a
     try/except; warnings = marking.get("warnings", []).

  9. Build the ctx dict with EXACTLY the keys listed in the context section
     above, including hashes for unmarked and colored via sha256_of.

 10. Write the report: open(report, "w", encoding="utf-8") and write
     _report_top(ctx) + "\\n" + _report_bottom(ctx).

 11. Write the record with json.dumps(..., indent=2, default=str):
     {"rule_id": RULE_ID, "rule_clause": RULE_CLAUSE, "rule_origin": RULE_ORIGIN,
      "source_ifc": source, "source_sha256": ..., "schema": model.schema,
      "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
      "harness_generated_by": HARNESS_GENERATED_BY,
      "colour": {"name": COLOUR_NAME, "hex": COLOUR_HEX, "rgb": list(COLOUR_RGB)},
      "target": {"global_id": target.global_id, "score": target.score,
                 "justification": target.justification},
      "mutation": dataclasses.asdict(mutation),
      "marking": marking, "outputs": {...four paths and two hashes...},
      "self_checks": {...}}

 12. Print a few summary lines and return 0.
""",
        max_tokens=2000,
    ),
)


def _part_prompt(spec: PartSpec, shared: dict) -> str:
    import json

    from .llm import prompt_path

    template = prompt_path("harness.md").read_text(encoding="utf-8")
    template = template.replace("{{context_keys}}", CONTEXT_KEYS.strip())
    payload = dict(shared)
    payload["function_to_write"] = spec.signature
    payload["task"] = spec.task.strip()
    return template.replace("{{input_json}}", json.dumps(payload, indent=2, default=str))


def generate_part(client: QwenClient, spec: PartSpec, shared: dict, seed: int = 1) -> str:
    """One function, static-gated. Retries only this part."""
    messages = [{"role": "user", "content": _part_prompt(spec, shared)}]
    last_error = ""
    for attempt in range(1, MAX_PART_ATTEMPTS + 1):
        raw = client.chat(messages, purpose=f"harness-part:{spec.name}:attempt{attempt}",
                          seed=seed, max_tokens=spec.max_tokens)
        try:
            source = extract_code_block(raw)
        except ValueError as e:
            last_error = str(e)
        else:
            report = check_source(source, required_defs=(spec.name,))
            if report.ok:
                return source
            last_error = report.summary()

        messages += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content":
                f"Rejected: {last_error}\nResend ONLY the complete "
                f"{spec.signature} function as one fenced python code block. Define "
                f"nothing else."},
        ]
    raise RuntimeError(f"could not generate {spec.name} in {MAX_PART_ATTEMPTS} attempts. "
                       f"Last rejection: {last_error}")


def generate_harness(client: QwenClient, shared: dict, seed: int = 1,
                     log=print) -> tuple[str, dict[str, str]]:
    """Every part, concatenated. Returns (harness_source, {part: source})."""
    parts: dict[str, str] = {}
    for spec in PARTS:
        log(f"      generating {spec.name}()")
        parts[spec.name] = generate_part(client, spec, shared, seed)
    header = (
        "# The four functions below were generated one at a time, each validated\n"
        "# on its own before assembly. See ifcfault/harness.py for why.\n"
    )
    return header + "\n\n".join(parts[spec.name] for spec in PARTS), parts


def regenerate_part(client: QwenClient, part_name: str, shared: dict, previous: str,
                    failure: str, seed: int = 1) -> str:
    """Repair one part using the concrete failure the validator reported."""
    spec = next(s for s in PARTS if s.name == part_name)
    messages = [
        {"role": "user", "content": _part_prompt(spec, shared)},
        {"role": "assistant", "content": f"```python\n{previous}\n```"},
        {"role": "user", "content":
            "Run for real against a live model, the script failed:\n\n" + failure +
            f"\n\nFix {spec.name} and resend ONLY that complete function as one fenced "
            f"python code block."},
    ]
    raw = client.chat(messages, purpose=f"repair-harness-part:{part_name}",
                      seed=seed, max_tokens=spec.max_tokens)
    source = extract_code_block(raw)
    report = check_source(source, required_defs=(part_name,))
    if not report.ok:
        raise RuntimeError(f"repaired {part_name} failed the static gate: {report.summary()}")
    return source


#: Which generated function owns each report heading, so a missing section is
#: repaired in the function that was supposed to emit it.
_TOP_SECTIONS = ("INPUT MODEL", "INJECTED VIOLATION", "WHERE TO FIND IT")
_BOTTOM_SECTIONS = ("HOW TO SPOT IT IN A VIEWER", "OUTPUT FILES", "SELF-CHECKS",
                    "COLOUR LEGEND")

#: Which function owns each failing check.
_CHECK_OWNER = {
    "colour_applied": "_mark_violation",
    "marker_box_present": "_mark_violation",
    "findable_by_name_or_property": "_mark_violation",
    "colored_parses_cleanly": "_mark_violation",
    "all_outputs_written": "main",
    "record_parsed": "main",
    "script_completed": "main",
}


def blamed_by_traceback(stderr: str) -> str | None:
    """The generated function a traceback actually died inside.

    Without this, a crash anywhere in the harness is attributed to `main`
    (the only part that "runs"), and three repair rounds get spent rewriting
    a function that was never at fault while the real bug sits untouched in
    `_report_top`. Python names the frame; read it.
    """
    if not stderr:
        return None
    names = {spec.name for spec in PARTS}

    frames = []
    for line in stderr.splitlines():
        stripped = line.strip()
        # `  File "...", line 950, in _report_top`
        if stripped.startswith("File ") and ", in " in stripped:
            frames.append(stripped.rsplit(", in ", 1)[1].strip())
    if not frames:
        return None

    # The DEEPEST frame is where the error actually happened. If that is not
    # a harness function - a rule function, or a library helper - then no
    # harness part is at fault and blaming the outer `main` frame that merely
    # called into it would send the repair loop after the wrong code.
    if frames[-1] not in names:
        return None
    return frames[-1]


def choose_part_to_repair(validation) -> str:
    """The one function to regenerate, given what failed.

    Repairing the part that owns the failure keeps the other three - already
    validated - untouched, so a fix cannot regress something that worked.
    """
    # A traceback is the most reliable evidence there is, so it wins.
    blamed = blamed_by_traceback(getattr(validation, "stderr", "") or "")
    if blamed:
        return blamed

    failures = [c for c in validation.checks if not c.passed]
    for check in failures:
        if check.name == "report_complete":
            missing = (check.evidence or {}).get("missing_sections") or []
            if any(section in _BOTTOM_SECTIONS for section in missing):
                return "_report_bottom"
            if any(section in _TOP_SECTIONS for section in missing):
                return "_report_top"
        owner = _CHECK_OWNER.get(check.name)
        if owner:
            return owner
    # A crash with no specific check attached is main's problem: it is the
    # only part that sequences anything.
    return "main"


def assemble_parts(parts: dict[str, str]) -> str:
    header = (
        "# The four functions below were generated one at a time, each validated\n"
        "# on its own before assembly. See ifcfault/harness.py for why.\n"
    )
    return header + "\n\n".join(parts[spec.name] for spec in PARTS)
