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
    ctx["colored"]           bool   True when --colored was passed, so the one
                                    IFC written carries visual marking
    ctx["marking"]           dict   what _mark_violation returned, or {} when
                                    ctx["colored"] is False (it was not called)
    ctx["paths"]             dict   keys: ifc, report, record  - exactly one
                                    IFC path, marked or not per ctx["colored"]
    ctx["hashes"]            dict   key: ifc (may be missing)
    ctx["self_checks"]       dict   keys: dangling_references (int),
                                    ifc_reparsed (bool), warnings (list)
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
Mark the violation up so a human can find it in a 3D viewer. You are called
ONLY when the user passed --colored, and the caller writes the IFC AFTER you
return, so you are free to modify `model` in place here.

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

Under HOW TO SPOT IT IN A VIEWER, branch on ctx["colored"].

When ctx["colored"] is False, this run applied NO visual marking at all. Say
so plainly, say the GlobalId under WHERE TO FIND IT is the only handle, and
say that re-running the same command with --colored writes a marked-up copy.
Print nothing about painted items or marker boxes - there are none. Do not
read ctx["marking"]; it is empty.

When ctx["colored"] is True, read these EXACT keys - do not invent key names,
a wrong key silently reports zero:

    ctx["marking"]["painted_items"]        int, how many items were coloured
    ctx["marking"]["name_after"]           the searchable name tag
    ctx["marking"]["pset"]                 the property set name
    ctx["marking"]["marker_locations_mm"]  list of (x, y, z)

Print COLOUR_NAME and COLOUR_HEX, those four values, and MARKER_SIZE_MM. Use
`.get(key, default)` but with exactly those key spellings. Then include this
line verbatim, on a line of its own:

Revit's IFC import often discards IfcSurfaceStyle colours - if the element is not coloured, search for the name tag above or look for the marker box.

Under OUTPUT FILES: each path in ctx["paths"] with its hash from
ctx["hashes"] when present, and one line each saying what it is for. There is
exactly ONE IFC, under the key "ifc", and what it is depends on
ctx["colored"] - do NOT describe it as unmodified either way, because it is
the faulty model:

    ifc, when ctx["colored"] is False :
        "The faulty model, with no visual marking - feed this to a
         compliance checker."
    ifc, when ctx["colored"] is True :
        "The faulty model, marked up for a human - open this in Revit or an
         IFC viewer. Do NOT feed this one to a checker; the marking hands it
         the answer."
    report   : "This report."
    record   : "The same facts, machine-readable."

Under SELF-CHECKS: ctx["self_checks"]["dangling_references"],
ctx["self_checks"]["ifc_reparsed"], and every string in
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
     --params (default "{}"), and a visual-marking switch that defaults OFF:

         parser.add_argument("--colored", dest="colored", action="store_true",
                             help="write the faulty IFC marked up for a human "
                                  "viewer (coloured, name-tagged, marker box)")
         parser.add_argument("--no-color", dest="colored", action="store_false",
                             help="no visual marking at all (the default)")
         parser.set_defaults(colored=False)

     Use EXACTLY those flag spellings and that single shared dest, so
     --colored and --no-color are opposites of one another and the last one
     given wins. Then os.makedirs(outdir, exist_ok=True).

  2. model = ifcopenshell.open(source). Call applicable(model); if not .ok,
     print the reason and return 2 without writing anything. Call
     candidates(model); if empty, print that and return 2. Choose with
     best_target(...).

  3. mutation = apply_violation(model, target, json.loads(args.params))

  4. Build paths. This run writes exactly ONE IFC, and which one depends on
     args.colored:
         ifc_path = os.path.join(
             outdir, OUTPUT_STEM + ("_colored.ifc" if args.colored else ".ifc"))
         report = os.path.join(outdir, OUTPUT_STEM + "_report.txt")
         record = os.path.join(outdir, OUTPUT_STEM + "_record.json")
     Put them in ctx["paths"] under the keys "ifc", "report", "record".

  5. where = describe_element(model, model.by_guid(mutation.target_global_id))
     if mutation.attribute != "(entity deleted)" and RULE_ID != "S5", else {}.
     Wrap in try/except and fall back to {}. Do this BEFORE any marking, so
     the recorded name is the original one.

  6. If args.colored:
         marking = _mark_violation(model, mutation, target, source)
     else:
         marking = {}            # do NOT call _mark_violation at all
     Then model.write(ifc_path)  -- once, after that branch, so an unmarked
     run carries no hint of where the fault is and a marked run carries all
     of it.

  7. self_checks: dangling = len(find_dangling_references(model));
     reparsed = True/False from trying ifcopenshell.open(ifc_path) in a
     try/except, under the key "ifc_reparsed";
     warnings = marking.get("warnings", []).

  8. Build the ctx dict with EXACTLY the keys listed in the context section
     above, including ctx["colored"] = bool(args.colored) and
     ctx["hashes"] = {"ifc": sha256_of(ifc_path)}.

  9. Write the report: open(report, "w", encoding="utf-8") and write
     _report_top(ctx) + "\\n" + _report_bottom(ctx).

 10. Write the record with json.dumps(..., indent=2, default=str):
     {"rule_id": RULE_ID, "rule_clause": RULE_CLAUSE, "rule_origin": RULE_ORIGIN,
      "source_ifc": source, "source_sha256": ..., "schema": model.schema,
      "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
      "harness_generated_by": HARNESS_GENERATED_BY,
      "colored": bool(args.colored),
      "colour": {"name": COLOUR_NAME, "hex": COLOUR_HEX, "rgb": list(COLOUR_RGB)},
      "target": {"global_id": target.global_id, "score": target.score,
                 "justification": target.justification},
      "mutation": dataclasses.asdict(mutation),
      "marking": marking, "outputs": {...the three paths and the ifc hash...},
      "self_checks": {...}}

     "colored" is a TOP-LEVEL key of the record, not nested, because it is
     what tells a later reader which of the two files they are holding.

 11. Print a few summary lines - including whether marking was applied, and
     the --colored hint when it was not - and return 0.
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


def generate_part(client: QwenClient, spec: PartSpec, shared: dict, seed: int = 1,
                  prompt_builder=None) -> str:
    """One function, static-gated. Retries only this part.

    `prompt_builder` selects the shape being asked for: the single-fault
    prompts by default, or the multi-fault ones. The default is left exactly
    as it was so a single-rule emission produces the same request bytes it
    always has, and keeps hitting the on-disk LLM cache.
    """
    build = prompt_builder or _part_prompt
    messages = [{"role": "user", "content": build(spec, shared)}]
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


# ---------------------------------------------------------------------------
# multi-fault harness
# ---------------------------------------------------------------------------
#: What a multi-fault part can rely on finding in `ctx`. The single-fault
#: shape is a special case of this one with exactly one entry in "faults",
#: but it is NOT reused: the single-fault prompts are left byte-identical so
#: that every harness generated before multi-fault existed still hits the
#: LLM cache, and so a single-rule script keeps the exact file it always had.
MULTI_CONTEXT_KEYS = """
    ctx["source"]            str    the --source path actually used
    ctx["outdir"]            str    the --outdir path actually used
    ctx["source_sha256"]     str
    ctx["schema"]            str    model.schema
    ctx["unit"]              str    length_unit_name(model)
    ctx["entity_count"]      int    len(model.by_type("IfcRoot"))
    ctx["colored"]           bool   True when --colored was passed
    ctx["faults"]            list   ONE ENTRY PER INJECTED FAULT, in injection
                                    order. Each entry is a dict:
        entry["fault"]       dict   the FAULTS table row: slot, label, rule_id,
                                    occurrence, of, clause, domain, element,
                                    origin, colour_name, colour_hex, colour_rgb,
                                    name_tag, deletes_elements,
                                    target_is_a_storey
        entry["target"]      ScoredTarget  the candidate this fault chose
        entry["mutation"]    Mutation      what its apply_violation returned
        entry["where"]       dict   describe_element output, or {} when the
                                    target was deleted or is a storey
        entry["marking"]     dict   what _mark_violation returned for THIS
                                    fault, or {} when ctx["colored"] is False
    ctx["paths"]             dict   keys: ifc, report, record  - exactly one
                                    IFC path, marked or not per ctx["colored"]
    ctx["hashes"]            dict   key: ifc (may be missing)
    ctx["self_checks"]       dict   keys: dangling_references (int),
                                    ifc_reparsed (bool), warnings (list)
"""

MULTI_PARTS: tuple[PartSpec, ...] = (
    PartSpec(
        name="_mark_violation",
        signature="_mark_violation(model, mutation, target, source_path, fault)",
        task="""
Mark ONE violation up so a human can find it in a 3D viewer. You are called
once per injected fault, only when the user passed --colored, and the caller
writes the IFC after every fault has been marked.

`fault` is that fault's row from the FAULTS table. Take EVERY per-rule value
from it - never from a module-level global, because a multi-fault script has
no single RULE_ID or COLOUR_RGB:

    rule_id  = fault["rule_id"]
    rgb      = fault["colour_rgb"]
    tag      = fault["name_tag"]
    label    = fault["label"]          e.g. "A1" or "A1#2"

Do all of this:

  1. style_cache = {} ; style = violation_style(model, rule_id, rgb, style_cache)

  2. If mutation.attribute is not "(entity deleted)" and not
     fault["target_is_a_storey"]:
     resolve element = model.by_guid(mutation.target_global_id), then
       painted = paint_element(model, element, style)
       name_before, name_after = tag_element_name(model.by_guid(mutation.target_global_id), rule_id, tag)
       attach_violation_pset(model, element, fields, VIOLATION_PSET)
     where `fields` is a dict of plain strings containing at least:
       "ViolationRule": rule_id, "ViolationClause": fault["clause"],
       "ViolationColour": fault["colour_name"] + " " + fault["colour_hex"],
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

  4. For each location, call add_marker_box(model, location, storey, rule_id,
     name, description, style, MARKER_SIZE_MM, guid_seed) where `storey` is
     storey_of(model, element) when the element exists else None, `name` is
     tag + a short label, and guid_seed is str(fault["slot"]) + "_" + str(index).

     The slot MUST be part of guid_seed. Two faults of the same rule would
     otherwise derive the same marker GlobalId at the same coordinates and
     the second would collide with the first.

  5. Wrap steps 2 and 4 in try/except Exception and append a readable string
     to a `warnings` list on failure. Marking is a convenience; losing it
     must never raise, and one fault's marking failing must not stop the
     next fault from being marked.

Return exactly this dict:

    {"label": label, "painted_items": int, "name_before": ..., "name_after": ...,
     "pset": VIOLATION_PSET, "marker_locations_mm": [...],
     "marker_global_ids": [...], "warnings": [...]}
""",
        max_tokens=2000,
    ),
    PartSpec(
        name="_report_top",
        signature="_report_top(ctx)",
        task="""
Return a STRING: the first half of the human-readable report, covering the
input model and then EVERY fault in ctx["faults"].

It must contain these three headings, each alone on its own line, spelled
exactly:

    INPUT MODEL
    INJECTED VIOLATION
    WHERE TO FIND IT

Under INPUT MODEL: the source path, its sha256, the IFC schema, the length
unit, the entity count, and the number of faults (len(ctx["faults"])).

Then LOOP over ctx["faults"], in order, and for each entry emit a clearly
separated block introduced by a line naming the fault, like:

    --- FAULT 1 of 3: A1 (RED #E6194B) ---

using entry["fault"]["slot"], len(ctx["faults"]), entry["fault"]["label"],
entry["fault"]["colour_name"] and entry["fault"]["colour_hex"].

The two headings INJECTED VIOLATION and WHERE TO FIND IT go INSIDE each
per-fault block, so they appear once per fault. Under INJECTED VIOLATION:
the fault's label, rule_id, domain, the full clause, entry["mutation"].description,
the attribute changed, before -> after, the mechanism from
entry["mutation"].extra.get("mechanism"), and why this element was chosen
(entry["target"].justification).

Under WHERE TO FIND IT: ALWAYS print entry["mutation"].target_global_id
first, on its own line, whatever entry["where"] contains - for a rule that
deletes elements or targets a storey, entry["where"] is empty and that id is
the only handle there is. Then everything in entry["where"] when it is
non-empty (ifc_type, name, global_id, step_id, storey_name,
storey_elevation_mm, world_xyz_mm), and then EVERY key/value pair of
entry["mutation"].extra, one per line, sorted by key, so nothing the rule
recorded is lost. When entry["where"] is empty, say the target element was
deleted or is a storey and rely on the extra dict.

Format millimetre floats to one decimal place. Indent detail lines by two
spaces under their heading. Separate sections with a blank line and a line of
dashes.
""",
        max_tokens=2000,
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

Under HOW TO SPOT IT IN A VIEWER, branch on ctx["colored"].

When ctx["colored"] is False, no visual marking was applied to any fault. Say
so plainly, say the GlobalIds listed under WHERE TO FIND IT are the only
handles, and say that re-running the same command with --colored writes a
marked-up copy. Print nothing about painted items or marker boxes - there are
none. Do not read entry["marking"]; it is empty for every fault.

When ctx["colored"] is True, LOOP over ctx["faults"] and for each one print a
line naming the fault and its colour, then read these EXACT keys from
entry["marking"] - do not invent key names, a wrong key silently reports
zero:

    entry["marking"]["painted_items"]        int, how many items were coloured
    entry["marking"]["name_after"]           the searchable name tag
    entry["marking"]["pset"]                 the property set name
    entry["marking"]["marker_locations_mm"]  list of (x, y, z)

Use `.get(key, default)` but with exactly those key spellings. Print
MARKER_SIZE_MM once, after the loop. Then include this line verbatim, on a
line of its own:

Revit's IFC import often discards IfcSurfaceStyle colours - if the element is not coloured, search for the name tag above or look for the marker box.

Also state, in one sentence, that each fault carries its own rule's colour so
several faults in one file can be told apart.

Under OUTPUT FILES: each path in ctx["paths"] with its hash from
ctx["hashes"] when present, and one line each saying what it is for. There is
exactly ONE IFC, under the key "ifc", and what it is depends on
ctx["colored"] - do NOT describe it as unmodified either way, because it is
the faulty model:

    ifc, when ctx["colored"] is False :
        "The faulty model, carrying every injected fault, with no visual
         marking - feed this to a compliance checker."
    ifc, when ctx["colored"] is True :
        "The faulty model, every fault marked up in its own colour - open
         this in Revit or an IFC viewer. Do NOT feed this one to a checker;
         the marking hands it the answer."
    report   : "This report."
    record   : "The same facts, machine-readable."

Under SELF-CHECKS: ctx["self_checks"]["dangling_references"],
ctx["self_checks"]["ifc_reparsed"], and every string in
ctx["self_checks"]["warnings"]. State plainly that these are structural
self-checks only, and that independent clause verification was done when the
script was generated, not here.

Under COLOUR LEGEND: every (rule_id, name, hex) triple in COLOUR_LEGEND, one
per line. Mark the ones this script actually used, so a reader is not left
hunting the file for a colour that is not in it.

Same formatting conventions as the first half.
""",
        max_tokens=2000,
    ),
    PartSpec(
        name="main",
        signature="main(argv=None)",
        task="""
Orchestrate the whole plan and return an int exit code.

  1. argparse: --source (default SOURCE_IFC), --outdir (default "."),
     --params (default "{}"), and a visual-marking switch that defaults OFF:

         parser.add_argument("--colored", dest="colored", action="store_true",
                             help="write the faulty IFC marked up for a human "
                                  "viewer (coloured, name-tagged, marker box)")
         parser.add_argument("--no-color", dest="colored", action="store_false",
                             help="no visual marking at all (the default)")
         parser.set_defaults(colored=False)

     Use EXACTLY those flag spellings and that single shared dest, so
     --colored and --no-color are opposites of one another and the last one
     given wins. Then os.makedirs(outdir, exist_ok=True).

  2. model = ifcopenshell.open(source). params = json.loads(args.params).

  3. THE INJECTION LOOP. Keep `used = set()` of GlobalIds already spent, and
     `results = []`. For each `fault` in FAULTS, in order:

       a. app = fault["applicable"](model)
          If not app.ok: print which fault and why, and return 2 WITHOUT
          writing any file.

       b. targets = fault["candidates"](model, exclude=frozenset(used))
          Pass `used` every time. This is what makes two faults of the same
          rule land on two different elements.
          If not targets: print that fault N of M had no candidate left
          outside the exclusion set, and return 2 WITHOUT writing any file.

       c. target = best_target(targets)

       d. Add to `used`: target.global_id, and also every GlobalId the
          previous step already consumed. After applying, also add
          mutation.target_global_id and any ids in
          mutation.extra.get("deleted_global_ids") or
          mutation.extra.get("deleted_wall_global_ids") or
          mutation.extra.get("cascade_removed_global_ids"), so a later fault
          cannot pick an element that no longer exists.

       e. where = describe_element(model, model.by_guid(target.global_id))
          if mutation.attribute != "(entity deleted)" and not
          fault["target_is_a_storey"], else {}. Compute it BEFORE marking, and
          wrap in try/except falling back to {}.
          NOTE: compute `where` AFTER apply_violation but the element must
          still exist; if by_guid raises, fall back to {}.

       f. mutation = fault["apply_violation"](model, target, params)

       g. Append {"fault": fault, "target": target, "mutation": mutation,
                  "where": where, "marking": {}} to `results`.

     Do NOT wrap the loop in a try/except that swallows a rule's exception.
     If a rule raises, let it propagate: a traceback naming the rule is what
     tells the generator which code is at fault.

     It is ALL-OR-NOTHING. Returning 2 before writing anything is the whole
     point - a file named for three faults that contains two is worse than no
     file.

  4. Build paths. This run writes exactly ONE IFC, and which one depends on
     args.colored:
         ifc_path = os.path.join(
             outdir, OUTPUT_STEM + ("_colored.ifc" if args.colored else ".ifc"))
         report = os.path.join(outdir, OUTPUT_STEM + "_report.txt")
         record = os.path.join(outdir, OUTPUT_STEM + "_record.json")
     Put them in ctx["paths"] under the keys "ifc", "report", "record".

  5. If args.colored: for each entry in `results`, call
         entry["marking"] = _mark_violation(model, entry["mutation"],
                                            entry["target"], source,
                                            entry["fault"])
     Otherwise leave every entry["marking"] as {} and do NOT call
     _mark_violation at all.
     Then model.write(ifc_path) - once, after that branch.

  6. self_checks: dangling = len(find_dangling_references(model));
     reparsed = True/False from trying ifcopenshell.open(ifc_path) in a
     try/except, under the key "ifc_reparsed";
     warnings = every warning from every entry["marking"], concatenated.

  7. Build the ctx dict with EXACTLY the keys listed in the context section
     above. ctx["faults"] is `results`. ctx["colored"] = bool(args.colored).
     ctx["hashes"] = {"ifc": sha256_of(ifc_path)}.

  8. Write the report: open(report, "w", encoding="utf-8") and write
     _report_top(ctx) + "\\n" + _report_bottom(ctx).

  9. Write the record with json.dumps(..., indent=2, default=str):
     {"source_ifc": source, "source_sha256": ..., "schema": model.schema,
      "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
      "harness_generated_by": HARNESS_GENERATED_BY,
      "colored": bool(args.colored),
      "fault_count": len(results),
      "mutations": [ ... one entry per fault, in injection order ... ],
      "outputs": {...the three paths and the ifc hash...},
      "self_checks": {...}}

     Each entry of "mutations" is:
        {"slot": f["slot"], "label": f["label"], "rule_id": f["rule_id"],
         "rule_clause": f["clause"], "rule_origin": f["origin"],
         "colour": {"name": f["colour_name"], "hex": f["colour_hex"],
                    "rgb": list(f["colour_rgb"])},
         "target": {"global_id": t.global_id, "score": t.score,
                    "justification": t.justification},
         "mutation": dataclasses.asdict(m),
         "marking": entry["marking"]}
     where f = entry["fault"], t = entry["target"], m = entry["mutation"].

     "mutations" is a LIST and is named "mutations", not "mutation", even
     when the plan has one fault. A reader must never have to guess which
     shape it got.

 10. Print a line per injected fault, plus whether marking was applied and
     the --colored hint when it was not, and return 0.
""",
        max_tokens=2600,
    ),
)


def _multi_part_prompt(spec: PartSpec, shared: dict) -> str:
    import json

    from .llm import prompt_path

    template = prompt_path("harness.md").read_text(encoding="utf-8")
    template = template.replace("{{context_keys}}", MULTI_CONTEXT_KEYS.strip())
    payload = dict(shared)
    payload["function_to_write"] = spec.signature
    payload["task"] = spec.task.strip()
    return template.replace("{{input_json}}", json.dumps(payload, indent=2, default=str))


def generate_multi_harness(client: QwenClient, shared: dict, seed: int = 1,
                           log=print) -> tuple[str, dict[str, str]]:
    """The four multi-fault parts, concatenated."""
    parts: dict[str, str] = {}
    for spec in MULTI_PARTS:
        log(f"      generating {spec.name}()")
        parts[spec.name] = generate_part(client, spec, shared, seed,
                                         prompt_builder=_multi_part_prompt)
    return assemble_multi_parts(parts), parts


def regenerate_multi_part(client: QwenClient, part_name: str, shared: dict, previous: str,
                          failure: str, seed: int = 1) -> str:
    """Repair one multi-fault part using the concrete failure reported."""
    spec = next(s for s in MULTI_PARTS if s.name == part_name)
    messages = [
        {"role": "user", "content": _multi_part_prompt(spec, shared)},
        {"role": "assistant", "content": f"```python\n{previous}\n```"},
        {"role": "user", "content":
            "Run for real against a live model, the script failed:\n\n" + failure +
            f"\n\nFix {spec.name} and resend ONLY that complete function as one fenced "
            f"python code block."},
    ]
    raw = client.chat(messages, purpose=f"repair-multi-harness-part:{part_name}",
                      seed=seed, max_tokens=spec.max_tokens)
    source = extract_code_block(raw)
    report = check_source(source, required_defs=(part_name,))
    if not report.ok:
        raise RuntimeError(f"repaired {part_name} failed the static gate: {report.summary()}")
    return source


def assemble_multi_parts(parts: dict[str, str]) -> str:
    header = (
        "# The four functions below were generated one at a time, each validated\n"
        "# on its own before assembly. See ifcfault/harness.py for why.\n"
        "# This is the MULTI-FAULT shape: _mark_violation and the report loop\n"
        "# over FAULTS, and main() injects every fault or none.\n"
    )
    return header + "\n\n".join(parts[spec.name] for spec in MULTI_PARTS)
