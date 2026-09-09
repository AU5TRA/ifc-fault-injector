"""
A3  - fire-rated assembly downgraded below what its occupancy requires.

Mechanism: rewrite the FireRating property. A wall is preferred (downgraded
to a lesser but still labelled value, e.g. "2 HR" -> "1 HR"); a door is
blanked outright.

Fallback: if the model carries no fire-rating data at all, the rule attaches
Pset_DoorCommon with a blank FireRating to a hosted door  - documenting the
missing rating rather than inventing one.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import any_owner_history, get_or_create_pset, set_prop_single_value
from .helpers import (
    door_fire_ratings, find_prop, get_host_wall, psets_of, sorted_by_guid, wall_fire_ratings,
)

RULE_ID = "A3"
CLAUSE = "IBC Chapter 7 / Table 716.1(2)  - opening protectives and fire-barrier ratings"
DOMAIN = "architectural"
ELEMENT = "IfcWall"


def _downgrade_label(value: str) -> str:
    """A lesser but still meaningful label for a real rating. Anything this
    does not recognise is blanked, since there is no smaller real value to
    step down to without guessing."""
    low = value.strip().lower()
    if low in ("2 hr", "2hr", "120", "120min", "120 min"):
        return "1 HR" if "hr" in low else "60"
    return ""


def _rating_rank(value: str) -> float:
    """Rough numeric severity of a rating label, so the highest-rated (most
    consequential) assembly is preferred. Tolerates the Dutch labels that
    show up in real European models."""
    v = value.lower()
    for token in ("minuten brandwerend en zelfsluitend", "minuten brandwerend",
                  "brandwerend", "min", "hr"):
        v = v.replace(token, "")
    try:
        return float(v.strip())
    except ValueError:
        return 0.0


def applicable(model: ifcopenshell.file) -> Applicability:
    walls = wall_fire_ratings(model)
    if walls:
        return Applicability(True, f"{len(walls)} wall(s) carry a real, non-blank FireRating")
    doors = door_fire_ratings(model)
    if doors:
        return Applicability(True, f"{len(doors)} door(s) carry a real, non-blank FireRating")
    hosted = [d for d in model.by_type("IfcDoor") if get_host_wall(model, d) is not None]
    if hosted:
        return Applicability(
            True,
            f"no FireRating data anywhere in this model; {len(hosted)} hosted door(s) exist "
            f"to document a missing rating on (fallback mode)",
        )
    return Applicability(False, "no walls or doors carry FireRating data, and no hosted door exists")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    walls = [w for w in wall_fire_ratings(model) if w.global_id not in exclude]
    if walls:
        out = [ScoredTarget(
            global_id=w.global_id,
            score=_rating_rank(w.value) + 1000.0,  # a wall always outranks a door
            justification=f"{w.element_type} rated '{w.value}' in {w.pset_name}",
            element_ids=(w.element_id,),
            extra={"mode": "wall", "pset_name": w.pset_name, "before_value": w.value},
        ) for w in walls]
        out.sort(key=lambda t: (-t.score, t.global_id))
        return out

    doors = [d for d in door_fire_ratings(model) if d.global_id not in exclude]
    if doors:
        out = [ScoredTarget(
            global_id=d.global_id,
            score=_rating_rank(d.value),
            justification=f"door rated '{d.value}' in {d.pset_name}",
            element_ids=(d.element_id,),
            extra={"mode": "door", "pset_name": d.pset_name, "before_value": d.value},
        ) for d in doors]
        out.sort(key=lambda t: (-t.score, t.global_id))
        return out

    out = []
    for d in sorted_by_guid(model.by_type("IfcDoor")):
        if d.GlobalId in exclude or get_host_wall(model, d) is None:
            continue
        out.append(ScoredTarget(
            global_id=d.GlobalId,
            score=0.0,
            justification="no fire-rating data exists in this model; a documented-missing "
                          "rating will be attached to this hosted door",
            element_ids=(d.id(),),
            extra={"mode": "fallback_missing"},
        ))
    out.sort(key=lambda t: t.global_id)
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    owner_history = any_owner_history(model)
    element = model.by_guid(target.global_id)
    mode = target.extra["mode"]

    if mode in ("wall", "door"):
        pset_name = target.extra["pset_name"]
        before_value = target.extra["before_value"]
        after_value = _downgrade_label(before_value) if mode == "wall" else ""
        pset = next(p for p in psets_of(element) if p.Name == pset_name)
        prop = find_prop(pset, "FireRating")
        # Preserve the original measure type rather than forcing IfcLabel  - 
        # the file may legitimately use IfcText or IfcIdentifier here.
        prop.NominalValue = model.create_entity(prop.NominalValue.is_a(), after_value)
        mechanism = f"overwrote FireRating in the existing {pset_name}"
        if mode == "wall":
            description = (f"Fire rating on this {element.is_a()} downgraded from "
                           f"'{before_value}' to '{after_value or '(blank)'}'")
        else:
            description = f"Fire rating blanked on this hosted door (was '{before_value}')"
    else:
        before_value = None
        after_value = ""
        pset_name = "Pset_DoorCommon"
        pset = get_or_create_pset(model, element, pset_name, owner_history, guid_seed=RULE_ID)
        set_prop_single_value(model, pset, "FireRating", "IfcLabel", "", owner_history)
        mechanism = f"created {pset_name} with a blank FireRating"
        description = ("Fire rating documented as missing on this hosted door  - no fire-rating "
                       "data exists in the baseline model")

    return Mutation(
        rule_id=RULE_ID,
        element_type=element.is_a(),
        target_global_id=target.global_id,
        attribute=f"{pset_name}.FireRating",
        before=before_value,
        after=after_value or "(blank)",
        clause=CLAUSE,
        description=description,
        extra={
            "element_id": element.id(),
            "mode": mode,
            "pset_name": pset_name,
            "mechanism": mechanism,
        },
    )
