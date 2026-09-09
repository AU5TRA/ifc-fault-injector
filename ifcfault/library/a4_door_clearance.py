"""
A4  - door pushed into a corner, destroying its accessible manoeuvring
clearance.

Mechanism: move the IfcOpeningElement's placement along the host wall's
local X axis until the door sits a token distance from the nearest wall end.
The door entity itself is untouched  - it is hosted in the opening, so moving
the opening moves the door with it, and only one placement changes.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import set_opening_x_along_wall
from .helpers import (
    doors_with_clearance, get_host_wall, get_opening, length_unit_scale, mm, to_mm,
    wall_axis_length,
)

RULE_ID = "A4"
CLAUSE = ("ANSI A117.1 404.2.4 / ADA  - latch-side manoeuvring clearance at a door "
          "(min. 300 mm beyond the latch side for a front approach)")
DOMAIN = "architectural"
ELEMENT = "IfcDoor"
THRESHOLD_MM = 300.0

DEFAULT_TARGET_CLEARANCE_MM = 150.0


def applicable(model: ifcopenshell.file) -> Applicability:
    cands = doors_with_clearance(model)
    if not cands:
        return Applicability(
            False,
            "no door found that is hosted in a wall of measurable length with a realistic "
            "existing clearance (500-3000mm) to the nearest wall end",
        )
    return Applicability(True, f"{len(cands)} door(s) with a movable, measurable wall-end clearance")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    cands = [c for c in doors_with_clearance(model) if c.global_id not in exclude]
    out = [ScoredTarget(
        global_id=c.global_id,
        # A door that is ALREADY close to a corner is the most marginal, and
        # therefore the most realistic one to tip over the line.
        score=-c.nearest_end_clearance_mm,
        justification=f"currently {c.nearest_end_clearance_mm:.0f}mm clear of the nearest "
                      f"wall end; will be moved to {DEFAULT_TARGET_CLEARANCE_MM:.0f}mm",
        element_ids=(c.door_id,),
        extra={"before_clearance_mm": c.nearest_end_clearance_mm},
    ) for c in cands]
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    door = model.by_guid(target.global_id)
    opening = get_opening(model, door)
    wall = get_host_wall(model, door)
    wall_len = wall_axis_length(model, wall)

    axp = opening.ObjectPlacement.RelativePlacement
    coords = axp.Location.Coordinates
    x = coords[0]

    target_clear_mm = float(params.get("target_clearance_mm", DEFAULT_TARGET_CLEARANCE_MM))
    target_clear_native = mm(model, target_clear_mm, scale)

    # Push toward whichever wall end is already nearer, so the door travels
    # the shortest distance and stays inside its own wall.
    distance_to_start, distance_to_end = x, wall_len - x
    new_x = target_clear_native if distance_to_start <= distance_to_end else wall_len - target_clear_native
    moved_toward = "wall start" if distance_to_start <= distance_to_end else "wall end"

    set_opening_x_along_wall(model, opening, new_x, coords[1:])

    before_mm = target.extra["before_clearance_mm"]
    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcDoor",
        target_global_id=target.global_id,
        attribute="IfcOpeningElement.ObjectPlacement (offset along host wall axis)",
        before=before_mm,
        after=target_clear_mm,
        clause=CLAUSE,
        description=f"Door opening moved to {target_clear_mm:.0f}mm from the nearest wall corner "
                    f"(was {before_mm:.0f}mm), inside the {THRESHOLD_MM:.0f}mm manoeuvring "
                    f"clearance the clause requires",
        extra={
            "element_id": door.id(),
            "host_wall_global_id": wall.GlobalId,
            "host_wall_name": wall.Name,
            "host_wall_length_mm": to_mm(model, wall_len, scale),
            "opening_global_id": opening.GlobalId,
            "moved_toward": moved_toward,
            "threshold_mm": THRESHOLD_MM,
            "mechanism": "opening placement moved along the host wall's local X axis",
        },
    )
