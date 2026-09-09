You are writing ONE function of the harness for a standalone Python script that
injects a single building-code compliance violation into a real IFC building
model.

The rule logic, the helper library and the configuration constants are already
written and will sit ABOVE your function in the same file. The other harness
functions are being written separately. Write only the one function you are
asked for.

Respond with ONE fenced python code block containing exactly that one function
and nothing else. No prose, no other definitions, no `if __name__` block.

Put any imports you need INSIDE the function body (`import json`, `import os`,
`import argparse`, `import time`, `import dataclasses`). Nothing else may be
imported.

## Already defined above your function - use these, never redefine them

### Configuration constants

    SOURCE_IFC            str    absolute path to the source .ifc
    OUTPUT_STEM           str    base filename for the outputs, no extension
    RULE_ID               str    e.g. "A1"
    RULE_CLAUSE           str    the code clause text
    RULE_DOMAIN           str    "architectural" or "structural"
    RULE_ELEMENT          str    the IFC type targeted, e.g. "IfcDoor"
    RULE_ORIGIN           str    provenance of the rule logic
    RULE_DELETES_ELEMENTS bool   True when the rule removes elements, so the
                                 violating element no longer exists to colour
    TARGET_IS_A_STOREY    bool   True when mutation.target_global_id is a
                                 storey rather than a single element
    COLOUR_NAME           str    e.g. "RED"
    COLOUR_HEX            str    e.g. "#E6194B"
    COLOUR_RGB            tuple  (r, g, b) floats 0..1
    NAME_TAG_PREFIX       str    e.g. "[!A1 VIOLATION!] "
    VIOLATION_PSET        str    "Pset_ViolationMarker"
    MARKER_SIZE_MM        float  marker box edge length
    GENERATOR_VERSION     str
    HARNESS_GENERATED_BY  str    the model that wrote this harness
    SCRIPT_GENERATED_AT   str
    COLOUR_LEGEND         list   of (rule_id, colour_name, hex) tuples

### The rule

    applicable(model) -> Applicability            .ok: bool, .reason: str
    candidates(model, exclude=frozenset()) -> list[ScoredTarget]
    apply_violation(model, target, params) -> Mutation
    best_target(targets) -> ScoredTarget          the only correct way to choose

    ScoredTarget: .global_id .score .justification .element_ids .extra
    Mutation:     .rule_id .element_type .target_global_id .attribute
                  .before .after .clause .description .extra

### Library helpers

    sha256_of(path) -> str
    length_unit_name(model) -> str
    length_unit_scale(model) -> float
    storey_of(model, element)
    global_xyz_mm(model, element, scale=None) -> (x, y, z) mm or None
    describe_element(model, element) -> dict with keys ifc_type, name,
        global_id, step_id, storey_name, storey_elevation_mm, world_xyz_mm
    find_dangling_references(model) -> list[str]        empty means clean

    violation_style(model, rule_id, rgb, cache) -> style
    paint_element(model, element, style) -> int          count of styled items
    tag_element_name(element, rule_id, template) -> (before, after)
    attach_violation_pset(model, element, fields, pset_name) -> pset
    add_marker_box(model, xyz_mm, storey, rule_id, name, description, style,
                   size_mm, guid_seed) -> proxy element

### The shared context dict

Several harness functions pass one `ctx` dict between them. Its keys are fixed:

{{context_keys}}

## Hard rules

- Imports allowed: argparse, dataclasses, json, os, sys, time, ifcopenshell.
  Nothing else, and put them inside the function.
- NEVER import or call subprocess, shutil, requests, httpx, urllib, socket,
  tempfile, importlib, eval, exec, compile, os.remove, os.system, rmtree.
  You write to disk only via `model.write(path)` and `open(path, "w")`.
- Never modify or delete the --source file, and never write outside --outdir.
- No f-string may contain a backslash escape; build such strings by
  concatenation or with a named variable.
- Read optional dictionary keys with `.get(...)`, never `[...]`.
- Use `os.path.join(...)` for every output path.
- No network access.

## Your task

The JSON below carries `function_to_write` (the exact signature) and `task`
(what it must do), plus context about this specific script. Write that one
function.

{{input_json}}
