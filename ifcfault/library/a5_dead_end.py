"""
A5  - corridor severed so a group of rooms is left behind a dead end.

Mechanism: delete exactly ONE IfcRelSpaceBoundary  - the one linking the
target door to the smaller side of the bridge it forms in the space
adjacency graph. The door keeps its boundary to the other side, so from the
isolated branch it now dead-ends into a wall instead of connecting through.

This is the only rule here that attacks connectivity rather than a
dimension, which makes it the interesting one: nothing about the geometry
changes, so a checker that only measures things will not see it.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .helpers import dead_end_bridges

RULE_ID = "A5"
CLAUSE = ("IBC 1020.4  - dead-end corridors shall not exceed 6100 mm "
          "(with sprinkler allowance); 1006.2 requires a second egress path")
DOMAIN = "architectural"
ELEMENT = "IfcDoor"


def applicable(model: ifcopenshell.file) -> Applicability:
    bridges = dead_end_bridges(model)
    if not bridges:
        n_spaces = len(model.by_type("IfcSpace"))
        n_boundaries = len(model.by_type("IfcRelSpaceBoundary"))
        return Applicability(
            False,
            f"no door forms a bridge edge touching a corridor space "
            f"({n_spaces} IfcSpace, {n_boundaries} IfcRelSpaceBoundary in this model  - either "
            f"the file carries no door-level space-boundary data, or no space borders a room "
            f"named like a corridor)",
        )
    return Applicability(True, f"{len(bridges)} candidate corridor bridge-door(s) found")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    bridges = [b for b in dead_end_bridges(model) if b.global_id not in exclude]
    if not bridges:
        return []

    # A branch of 2+ rooms is a real dead-end corridor story. A single
    # isolated room is a weaker one, and a branch that swallows half the
    # building is nonsense  - so prefer small-but-not-trivial branches.
    multi_room = [b for b in bridges if b.isolated_branch_size >= 2]
    pool = multi_room if multi_room else bridges

    out = [ScoredTarget(
        global_id=b.global_id,
        score=-float(b.isolated_branch_size),  # smallest plausible branch wins
        justification=f"severing this door isolates {b.isolated_branch_size} space(s) from the "
                      f"rest of the building via a corridor",
        element_ids=(b.door_id,),
        extra={
            "space_a_id": b.space_a_id,
            "space_b_id": b.space_b_id,
            "branch_space_id": b.branch_space_id,
            "remaining_space_id": b.remaining_space_id,
            "isolated_branch_size": b.isolated_branch_size,
        },
    ) for b in pool]
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    door = model.by_guid(target.global_id)
    branch_space = model.by_id(target.extra["branch_space_id"])
    remaining_space = model.by_id(target.extra["remaining_space_id"])

    # Find the ONE boundary linking this door to the branch side and remove
    # only that. Removing both would delete the door's connectivity entirely,
    # which is a different (and more obvious) defect.
    to_remove = None
    for rel in model.get_inverse(door):
        if rel.is_a("IfcRelSpaceBoundary") and rel.RelatingSpace.id() == branch_space.id():
            to_remove = rel
            break
    if to_remove is None:
        raise RuntimeError(
            f"A5: no IfcRelSpaceBoundary links door {target.global_id} to branch space "
            f"#{branch_space.id()}  - the candidate list is stale"
        )

    severed_global_id = to_remove.GlobalId
    branch_label = branch_space.Name or branch_space.LongName or f"#{branch_space.id()}"
    model.remove(to_remove)

    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcDoor",
        target_global_id=target.global_id,
        attribute="IfcRelSpaceBoundary (one severed)",
        before=f"connected to spaces {branch_space.GlobalId} and {remaining_space.GlobalId}",
        after=f"connected only to space {remaining_space.GlobalId}",
        clause=CLAUSE,
        description=f"Corridor connectivity severed at this door, leaving "
                    f"{target.extra['isolated_branch_size']} space(s) behind a dead end via "
                    f"'{branch_label}'  - exceeds the IBC 1020.4 dead-end allowance",
        extra={
            "element_id": door.id(),
            "severed_space_global_id": branch_space.GlobalId,
            "severed_space_name": branch_label,
            "remaining_space_global_id": remaining_space.GlobalId,
            "remaining_space_name": remaining_space.Name or remaining_space.LongName,
            "isolated_branch_size": target.extra["isolated_branch_size"],
            "severed_relationship_global_id": severed_global_id,
            "mechanism": "one IfcRelSpaceBoundary deleted (connectivity, not geometry)",
        },
    )
