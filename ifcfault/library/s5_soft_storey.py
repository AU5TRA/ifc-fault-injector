"""
S5  - soft storey: an abrupt loss of lateral stiffness at one level.

Mechanism: delete every IfcWall contained in one storey. The storeys above
and below keep their walls, so the remaining structure has a stiffness
discontinuity at exactly one level  - the classic soft-storey failure mode
(open ground floor, parking level, double-height lobby).

This is the bluntest rule in the library by design: it is the only one that
removes many elements at once, and the mutation record therefore carries the
complete list of what went, so the result stays checkable.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import delete_element
from .helpers import elements_of_storey, global_xyz_mm, length_unit_scale, storeys_sorted, to_mm

RULE_ID = "S5"
CLAUSE = ("ASCE 7 Table 12.3-2 / EC8 4.2.3.3  - soft-storey vertical irregularity: "
          "a storey with markedly lower lateral stiffness than the one above")
DOMAIN = "structural"
ELEMENT = "IfcWall"

MIN_WALLS = 2  # removing one wall is not a storey-level stiffness change


def _walls_of(model, storey, exclude):
    return [w for w in elements_of_storey(model, storey, "IfcWall") if w.GlobalId not in exclude]


def applicable(model: ifcopenshell.file) -> Applicability:
    storeys = storeys_sorted(model)
    usable = [s for s in storeys if len(_walls_of(model, s, frozenset())) >= MIN_WALLS]
    if not usable:
        return Applicability(
            False,
            f"no storey contains at least {MIN_WALLS} walls "
            f"({len(storeys)} storey(s), {len(model.by_type('IfcWall'))} IfcWall total)",
        )
    # A soft storey is a DISCONTINUITY, which means some other storey has to
    # keep its walls. Emptying the only wall-bearing storey in the model just
    # produces a building with no walls at all - a different defect, and not
    # the one this clause is about.
    if len(usable) < 2:
        return Applicability(
            False,
            f"only one storey ('{usable[0].Name}') carries walls, so emptying it would "
            f"remove every wall in the model rather than create a stiffness "
            f"discontinuity between storeys",
        )
    return Applicability(True, f"{len(usable)} storey(s) carry at least {MIN_WALLS} walls")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    storeys = storeys_sorted(model)
    scale = length_unit_scale(model)
    wall_counts = {s.GlobalId: len(_walls_of(model, s, exclude)) for s in storeys}

    out = []
    for i, storey in enumerate(storeys):
        walls = _walls_of(model, storey, exclude)
        if len(walls) < MIN_WALLS:
            continue
        # Some other storey must be left holding walls, or there is no
        # discontinuity to point at afterwards.
        others = sum(count for gid, count in wall_counts.items()
                     if gid != storey.GlobalId)
        if others < MIN_WALLS:
            continue

        # A storey already taller than the one below it is a naturally "soft"
        # candidate (a lobby or parking level), so prefer it.
        gap_mm = 0.0
        if i > 0 and storeys[i - 1].Elevation is not None and storey.Elevation is not None:
            gap_mm = to_mm(model, storey.Elevation - storeys[i - 1].Elevation, scale)

        out.append(ScoredTarget(
            global_id=storey.GlobalId,  # the STOREY is the target, not any one wall
            score=gap_mm * 1000.0 + len(walls),
            justification=f"storey '{storey.Name}' contains {len(walls)} wall(s)"
                          + (f"; storey height from the one below is {gap_mm:.0f}mm"
                             if gap_mm else ""),
            element_ids=tuple(w.id() for w in walls),
            extra={
                "storey_name": storey.Name,
                "storey_elevation_mm": (to_mm(model, storey.Elevation, scale)
                                        if storey.Elevation is not None else None),
                "wall_global_ids": [w.GlobalId for w in walls],
                "wall_count": len(walls),
                "height_gap_mm": gap_mm,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    storey_name = target.extra["storey_name"]
    wall_global_ids = list(target.extra["wall_global_ids"])

    removed_global_ids: list[str] = []
    modified_global_ids: list[str] = []
    relationship_types: set[str] = set()
    deleted = []

    for gid in wall_global_ids:
        wall = model.by_guid(gid)
        deleted.append({
            "global_id": gid,
            "name": wall.Name,
            "step_id": wall.id(),
            "location_mm": global_xyz_mm(model, wall),
        })
        cascade = delete_element(model, wall)
        removed_global_ids.extend(cascade["removed_global_ids"])
        modified_global_ids.extend(cascade["modified_global_ids"])
        relationship_types.update(cascade["relationship_types"])

    removed_set = set(removed_global_ids)
    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcWall",
        target_global_id=target.global_id,  # the storey's own GlobalId
        attribute="(all walls on the storey deleted)",
        before=len(deleted),
        after=0,
        clause=CLAUSE,
        description=f"All {len(deleted)} wall(s) on storey '{storey_name}' deleted, creating a "
                    f"soft-storey lateral-stiffness discontinuity at that level while the "
                    f"storeys above and below keep theirs",
        extra={
            "storey_name": storey_name,
            "storey_elevation_mm": target.extra["storey_elevation_mm"],
            "height_gap_mm": target.extra["height_gap_mm"],
            "deleted_wall_global_ids": [d["global_id"] for d in deleted],
            "deleted_wall_count": len(deleted),
            "deleted_walls": deleted,
            "cascade_removed_global_ids": sorted(removed_set),
            "cascade_modified_global_ids": sorted(set(modified_global_ids) - removed_set),
            "relationship_types_cleaned": sorted(relationship_types),
            "mechanism": "bulk entity deletion with full relationship cleanup",
        },
    )
