"""
A1  - egress door narrower than the code minimum.

Mechanism: write IfcDoor.OverallWidth directly. The cleanest possible
injection  - one attribute, no geometry rebuild, no relationships touched.
"""
from __future__ import annotations

from collections import Counter

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .helpers import door_widths, length_unit_scale, mm, to_mm

RULE_ID = "A1"
CLAUSE = "IBC 1010.1.1  - egress door clear width shall be not less than 815 mm"
DOMAIN = "architectural"
ELEMENT = "IfcDoor"
THRESHOLD_MM = 815.0

DEFAULT_NEW_WIDTH_MM = 750.0

# Single-leaf doors live in this band; anything outside it is a double door,
# a shaft hatch or bad data, none of which make a clean egress-width story.
SINGLE_LEAF_MIN_MM = 700.0
SINGLE_LEAF_MAX_MM = 1100.0


def applicable(model: ifcopenshell.file) -> Applicability:
    bucket = [w for w in door_widths(model)
              if SINGLE_LEAF_MIN_MM <= w.width_mm <= SINGLE_LEAF_MAX_MM]
    if not bucket:
        return Applicability(
            False,
            f"no door with a single-leaf width in the "
            f"{SINGLE_LEAF_MIN_MM:.0f}-{SINGLE_LEAF_MAX_MM:.0f}mm band "
            f"({len(model.by_type('IfcDoor'))} IfcDoor total)",
        )
    return Applicability(True, f"{len(bucket)} candidate door(s) in the single-leaf width band")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    widths = [w for w in door_widths(model) if w.global_id not in exclude]
    bucket = [w for w in widths if SINGLE_LEAF_MIN_MM <= w.width_mm <= SINGLE_LEAF_MAX_MM]
    if not bucket:
        return []

    # Prefer the model's MODAL door width: that is the building's standard
    # egress door, so narrowing it is the most representative defect rather
    # than an edge case nobody would walk through.
    counts = Counter(round(w.width_mm) for w in bucket)
    out = []
    for w in bucket:
        rounded = round(w.width_mm)
        out.append(ScoredTarget(
            global_id=w.global_id,
            score=float(counts[rounded]),
            justification=f"width={rounded}mm; {counts[rounded]} door(s) in this model share "
                          f"that width (the modal single-leaf size)",
            element_ids=(w.door_id,),
            extra={"before_width_mm": w.width_mm},
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    door = model.by_guid(target.global_id)
    scale = length_unit_scale(model)
    new_width_mm = float(params.get("new_width_mm", DEFAULT_NEW_WIDTH_MM))

    before_native = door.OverallWidth
    before_mm = to_mm(model, before_native, scale) if before_native is not None else None
    door.OverallWidth = mm(model, new_width_mm, scale)

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcDoor",
        target_global_id=target.global_id,
        attribute="IfcDoor.OverallWidth",
        before=before_mm,
        after=new_width_mm,
        clause=CLAUSE,
        description=f"Egress door clear width reduced from {before_mm:.1f}mm to "
                    f"{new_width_mm:.1f}mm, below the {THRESHOLD_MM:.0f}mm minimum",
        extra={
            "element_id": door.id(),
            "threshold_mm": THRESHOLD_MM,
            "mechanism": "direct attribute write",
        },
    )
