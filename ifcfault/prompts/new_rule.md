You are writing a NEW violation-injection rule for a building-code clause that
has no existing implementation.

Your code will be inlined into a standalone script and executed against a real
IFC building model. It must be correct, not merely plausible: a rule that runs
without error but does not actually violate the clause is a failure.

Respond with ONE fenced python code block and nothing else.

## The contract

Define exactly these module-level names:

    RULE_ID   = "<the rule id you were given>"
    CLAUSE    = "<the clause, quoted well enough to cite in a report>"
    DOMAIN    = "architectural" | "structural"
    ELEMENT   = "<the IFC type you target, e.g. 'IfcWindow'>"

    def applicable(model) -> Applicability
    def candidates(model, exclude=frozenset()) -> list[ScoredTarget]
    def apply_violation(model, target, params) -> Mutation

Private helpers are fine; prefix them with `_`.

## Types available (already defined  - do not redefine)

    Applicability(ok: bool, reason: str)
        `reason` is ALWAYS populated, in both directions, and ends up in a
        report a human reads. When ok is False, say what you looked for and
        what you found instead, with counts.

    ScoredTarget(global_id, score, justification, element_ids=(), extra={})
        score: higher is better. Ties break on global_id.
        justification: why this element, with the numbers that decided it.
        extra: anything apply_violation needs that candidates already computed.

    Mutation(rule_id, element_type, target_global_id, attribute, before, after,
             clause, description, extra={})
        This IS the report. `description` is one sentence a human reads.
        `before`/`after` are the actual values, in millimetres for lengths.
        Put a "mechanism" key in `extra` describing HOW you changed the file.

    best_target(targets) -> ScoredTarget

## Library helpers available (already defined  - do not redefine)

Units. `length_unit_scale` returns the mm -> native multiplier, so
`native = mm_value * scale` and `mm_value = native / scale`. NEVER assume the
model is in millimetres; many are in metres.

    length_unit_scale(model) -> float
    mm(model, value_mm, scale=None) -> float        mm -> native
    to_mm(model, native_value, scale=None) -> float native -> mm
    sorted_by_guid(elements) -> list

Property sets:

    psets_of(element) -> list
    find_prop(pset, name) -> property or None
    prop_value(pset, name) -> the wrapped value or None
    get_or_create_pset(model, element, pset_name, owner_history, guid_seed="")
    set_prop_single_value(model, pset, name, value_type, value, owner_history)
    any_owner_history(model)

Spatial:

    storeys_sorted(model) -> list          bottom to top
    elements_of_storey(model, storey, ifc_type) -> list
    storey_of(model, element)
    global_xyz_mm(model, element, scale=None) -> (x, y, z) mm or None

Geometry reading:

    resolve_body_items(element) -> list     Body items, IfcMappedItem followed
    element_size_mm(model, element) -> ElementSize | None
        .length_mm, .width_mm (smaller section dim), .depth_mm (larger)
    slab_thickness_mm(model, slab) -> float | None
    profile_bbox_mm(profile, scale), brep_axis_extents(item)

Geometry writing  - ALWAYS use these, never edit a representation in place. A
real model shares one mapped representation across hundreds of elements, so
editing it silently resizes all of them:

    replace_profile_private(model, element, new_width_mm, new_depth_mm, scale)
    set_extrusion_depth_private(model, element, new_depth_mm, scale) -> old_mm
    replace_brep_cross_section_private(model, element, scale,
                                       new_depth_mm=None, new_width_mm=None)
    set_opening_x_along_wall(model, opening, new_x_native, other_coords)

The first three raise RuntimeError when the element's Body is not the shape
they expect. The normal pattern is to try the swept-solid version and fall
back to the Brep one:

    try:
        replace_profile_private(model, el, w, d, scale)
    except RuntimeError:
        replace_brep_cross_section_private(model, el, scale, new_depth_mm=d)

Deletion  - returns a COMPLETE account of the cascade, which you must record in
`Mutation.extra` or the result cannot be verified:

    delete_element(model, element) -> dict
        {"removed_global_ids": [...], "modified_global_ids": [...],
         "relationship_types": [...]}

Doors and walls:

    door_widths(model), get_opening(model, door), get_host_wall(model, door),
    wall_axis_length(model, wall)   # NATIVE units, not mm

## Requirements your rule must meet

1. **Target something that currently COMPLIES.** The whole point is to inject
   a defect that was not there before. If the element already violates the
   clause, a checker would have flagged the untouched file too and the test
   case is worthless. Prefer compliant elements in `candidates()`; if none
   exist, either say so in `applicable()` or record
   `"already_noncompliant": True` in the mutation's extra and say it in the
   description.

2. **Derive the injected value from the element, not from a constant.** A
   fixed number only violates the clause for some elements. Solve for a value
   that clears the threshold by a clear margin (10-20%) whatever the element's
   original dimensions.

3. **Be deterministic.** No `random`, no `uuid4`, no `set` iteration order, no
   dict ordering assumptions. `candidates()` must return the same order on
   every run and every machine, so end it with exactly this:

       out.sort(key=lambda t: (-t.score, t.global_id))
       return out

   `sorted_by_guid` takes IFC ELEMENTS, which have `.GlobalId`. A
   `ScoredTarget` is not an IFC element - its field is `.global_id` - so
   passing your candidate list to `sorted_by_guid` raises AttributeError.

4. **Honour `exclude`.** Skip any element whose GlobalId is in it.

5. **Read-only in `applicable` and `candidates`.** Mutate only in
   `apply_violation`.

6. **Leave the file valid.** No dangling references, no relationship left with
   an empty member list. Using `delete_element` gives you this for free; doing
   your own `model.remove()` does not.

7. **Record enough to write a report.** Someone reading only the `Mutation`
   must be able to find the element in a viewer and understand what changed.

## Hard rules

- Only these imports: `math`, `collections`, `itertools`, `statistics`,
  `dataclasses`, `typing`, `ifcopenshell`. Nothing else.
- No `eval`, `exec`, `compile`, `open`, `__import__`, `input`, no file I/O, no
  network, no subprocess. Your code only reads and edits the in-memory model.
- No relative imports. Everything you need is already defined above your code.
- Do not write a `main()`, do not parse arguments, do not write any file  - a
  separate harness does that.

## Input

{{input_json}}
