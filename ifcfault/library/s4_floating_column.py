"""
S4  - floating column: a discontinuous vertical load path.

Mechanism: delete the column(s) directly beneath a column on the storey
above, leaving that upper column with nothing carrying it down.

Two details make this rule correct rather than merely plausible:

  * Storeys are ordered by Elevation ONLY, and a storey without an
    Elevation is not considered at all. "The storey immediately below" has
    to mean the same thing here as it does to whatever checks the result;
    ordering by anything else (GlobalId, file order) silently picks a
    different storey and the injected defect does not exist.

  * EVERY supporting column within tolerance is deleted, not just the
    closest one. Real models frequently contain two coincident columns at a
    grid intersection (duplicated during modelling), and removing one of a
    coincident pair leaves the load path perfectly intact.
"""
from __future__ import annotations

import math

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import delete_element
from .helpers import elements_of_storey, global_xyz_mm, length_unit_scale

RULE_ID = "S4"
CLAUSE = ("ASCE 7 Table 12.3-2 / EC8 4.2.3.3  - vertical irregularity: "
          "in-plane discontinuity in the lateral force-resisting element")
DOMAIN = "structural"
ELEMENT = "IfcColumn"

# Any lower-storey column whose plan position is within this distance of the
# upper column is treated as carrying it, and is therefore removed. Kept
# deliberately wider than a checker's typical 500mm tolerance so nothing is
# left behind that a checker would still count as support.
SUPPORT_TOLERANCE_MM = 600.0


def _storeys_by_elevation(model: ifcopenshell.file) -> list:
    """Storeys that declare an Elevation, bottom to top. Storeys without one
    are excluded rather than guessed at."""
    storeys = [s for s in model.by_type("IfcBuildingStorey") if s.Elevation is not None]
    return sorted(storeys, key=lambda s: (s.Elevation, s.GlobalId))


def _opportunities(model: ifcopenshell.file, exclude=frozenset()) -> list[dict]:
    """Every (upper column, supporting columns below) pairing that could be
    turned into a floating column."""
    storeys = _storeys_by_elevation(model)
    scale = length_unit_scale(model)  # resolved once, not per placement lookup
    found = []
    for i in range(1, len(storeys)):
        upper_storey, lower_storey = storeys[i], storeys[i - 1]
        upper_cols = elements_of_storey(model, upper_storey, "IfcColumn")
        lower_cols = elements_of_storey(model, lower_storey, "IfcColumn")
        if not upper_cols or not lower_cols:
            continue

        lower_xy = []
        for c in lower_cols:
            xyz = global_xyz_mm(model, c, scale)
            if xyz is not None:
                lower_xy.append((c, xyz[0], xyz[1]))

        for uc in upper_cols:
            if uc.GlobalId in exclude:
                continue
            uxyz = global_xyz_mm(model, uc, scale)
            if uxyz is None:
                continue
            supports = []
            for lc, lx, ly in lower_xy:
                if lc.GlobalId in exclude:
                    continue
                dist = math.hypot(lx - uxyz[0], ly - uxyz[1])
                if dist <= SUPPORT_TOLERANCE_MM:
                    supports.append((lc, dist))
            if not supports:
                continue  # already unsupported in the source  - nothing to inject
            supports.sort(key=lambda pair: (pair[1], pair[0].GlobalId))
            found.append({
                "upper_storey": upper_storey,
                "lower_storey": lower_storey,
                "surviving": uc,
                "supports": supports,
            })
    return found


def applicable(model: ifcopenshell.file) -> Applicability:
    storeys = _storeys_by_elevation(model)
    if len(storeys) < 2:
        return Applicability(
            False,
            f"fewer than two storeys declare an Elevation ({len(storeys)} of "
            f"{len(model.by_type('IfcBuildingStorey'))} IfcBuildingStorey), so "
            f"'the storey below' is undefined",
        )
    opportunities = _opportunities(model)
    if not opportunities:
        return Applicability(
            False,
            f"no column on any storey sits within {SUPPORT_TOLERANCE_MM:.0f}mm in plan of a "
            f"column on the storey immediately below it, so no continuous load path exists "
            f"to break",
        )
    return Applicability(True, f"{len(opportunities)} continuous column stack(s) available to break")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    out = []
    for opp in _opportunities(model, exclude):
        supports = opp["supports"]
        primary, primary_dist = supports[0]
        # Prefer the smallest possible intervention: a stack carried by ONE
        # column below, aligned as tightly as possible.
        score = -(len(supports) * 1_000_000.0) - primary_dist
        support_desc = (f"{len(supports)} column(s) below within {SUPPORT_TOLERANCE_MM:.0f}mm"
                        if len(supports) > 1 else
                        f"1 column below, {primary_dist:.0f}mm plan offset")
        out.append(ScoredTarget(
            global_id=primary.GlobalId,
            score=score,
            justification=f"deleting {support_desc} on storey '{opp['lower_storey'].Name}' "
                          f"leaves '{opp['surviving'].Name}' floating on "
                          f"'{opp['upper_storey'].Name}'",
            element_ids=tuple(c.id() for c, _ in supports),
            extra={
                "delete_global_ids": [c.GlobalId for c, _ in supports],
                "delete_names": [c.Name for c, _ in supports],
                "support_count": len(supports),
                "primary_plan_offset_mm": primary_dist,
                "deleted_storey_name": opp["lower_storey"].Name,
                "deleted_storey_global_id": opp["lower_storey"].GlobalId,
                "surviving_column_global_id": opp["surviving"].GlobalId,
                "surviving_column_name": opp["surviving"].Name,
                "surviving_storey_name": opp["upper_storey"].Name,
                "support_tolerance_mm": SUPPORT_TOLERANCE_MM,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    delete_ids = list(target.extra["delete_global_ids"])

    # Capture locations BEFORE deleting  - the marker box for this violation
    # has to go where the column used to be, and afterwards there is nothing
    # left to ask.
    original_locations = {}
    for gid in delete_ids:
        column = model.by_guid(gid)
        original_locations[gid] = global_xyz_mm(model, column, scale)

    removed_global_ids: list[str] = []
    modified_global_ids: list[str] = []
    relationship_types: set[str] = set()
    deleted_names = []

    for gid in delete_ids:
        column = model.by_guid(gid)
        deleted_names.append(column.Name)
        cascade = delete_element(model, column)
        removed_global_ids.extend(cascade["removed_global_ids"])
        modified_global_ids.extend(cascade["modified_global_ids"])
        relationship_types.update(cascade["relationship_types"])

    removed_set = set(removed_global_ids)
    names = ", ".join(f"'{n}'" for n in deleted_names if n) or "(unnamed)"
    count_text = (f"{len(delete_ids)} coincident columns" if len(delete_ids) > 1
                  else "column")

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcColumn",
        target_global_id=target.global_id,
        attribute="(entity deleted)",
        before=names,
        after=None,
        clause=CLAUSE,
        description=f"Supporting {count_text} {names} deleted from storey "
                    f"'{target.extra['deleted_storey_name']}', leaving column "
                    f"'{target.extra['surviving_column_name']}' on storey "
                    f"'{target.extra['surviving_storey_name']}' with no vertical load path "
                    f"below it",
        extra={
            "deleted_global_ids": sorted(delete_ids),
            "deleted_names": deleted_names,
            "deleted_original_locations_mm": original_locations,
            "deleted_storey_name": target.extra["deleted_storey_name"],
            "surviving_column_global_id": target.extra["surviving_column_global_id"],
            "surviving_column_name": target.extra["surviving_column_name"],
            "surviving_storey_name": target.extra["surviving_storey_name"],
            "primary_plan_offset_mm": target.extra["primary_plan_offset_mm"],
            "support_tolerance_mm": SUPPORT_TOLERANCE_MM,
            "cascade_removed_global_ids": sorted(removed_set),
            "cascade_modified_global_ids": sorted(set(modified_global_ids) - removed_set),
            "relationship_types_cleaned": sorted(relationship_types),
            "mechanism": "entity deletion with full relationship cleanup",
        },
    )
