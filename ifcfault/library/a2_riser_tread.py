"""
A2  - stair riser taller than the code maximum.

Mechanism: overwrite the RiserHeight property on the stair's existing
property set.

Fallback: a lot of real models carry no riser/tread data at all. Rather than
fabricate plausible-looking stair geometry, this rule then CREATES a
Pset_StairFlightCommon stating the non-compliant riser explicitly, and says
so in the mutation record. The violation is real and documented either way,
but the report distinguishes the two modes.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import any_owner_history, get_or_create_pset, set_prop_single_value
from .helpers import length_unit_scale, mm, psets_of, stair_riser_treads, stairs_without_riser_data

RULE_ID = "A2"
CLAUSE = "IBC 1011.5.2  - stair riser height shall be not more than 178 mm"
DOMAIN = "architectural"
ELEMENT = "IfcStairFlight"
THRESHOLD_MM = 178.0

DEFAULT_NEW_RISER_MM = 190.0


def applicable(model: ifcopenshell.file) -> Applicability:
    real = stair_riser_treads(model)
    if real:
        return Applicability(True, f"{len(real)} stair record(s) carry real RiserHeight data")
    stairs = model.by_type("IfcStairFlight") or model.by_type("IfcStair")
    if stairs:
        return Applicability(
            True,
            f"no riser/tread data anywhere in this model, but {len(stairs)} stair record(s) "
            f"exist to document a non-compliant riser on (fallback mode)",
        )
    return Applicability(False, "model contains no IfcStairFlight or IfcStair")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    out = []
    for s in stair_riser_treads(model):
        if s.global_id in exclude or s.riser_mm is None:
            continue
        out.append(ScoredTarget(
            global_id=s.global_id,
            score=100.0 + s.riser_mm,  # real data always outranks the fallback
            justification=f"real RiserHeight={s.riser_mm:.1f}mm in {s.pset_name}",
            element_ids=(s.element_id,),
            extra={"mode": "real", "pset_name": s.pset_name, "before_riser_mm": s.riser_mm},
        ))

    if not out:
        for s in stairs_without_riser_data(model):
            if s.GlobalId in exclude:
                continue
            out.append(ScoredTarget(
                global_id=s.GlobalId,
                score=1.0,
                justification=f"{s.is_a()} carries no RiserHeight anywhere; a new "
                              f"Pset_StairFlightCommon will document the violation",
                element_ids=(s.id(),),
                extra={"mode": "fallback"},
            ))

    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    owner_history = any_owner_history(model)
    element = model.by_guid(target.global_id)
    new_riser_mm = float(params.get("new_riser_mm", DEFAULT_NEW_RISER_MM))
    mode = target.extra["mode"]

    if mode == "real":
        pset_name = target.extra["pset_name"]
        before_mm = target.extra["before_riser_mm"]
        pset = next(p for p in psets_of(element) if p.Name == pset_name)
        mechanism = f"overwrote RiserHeight in the existing {pset_name}"
        description = (f"Stair riser height increased from {before_mm:.1f}mm to "
                       f"{new_riser_mm:.1f}mm, above the {THRESHOLD_MM:.0f}mm maximum")
    else:
        before_mm = None
        pset_name = "Pset_StairFlightCommon"
        pset = get_or_create_pset(model, element, pset_name, owner_history, guid_seed=RULE_ID)
        mechanism = f"created {pset_name} (no riser data existed in the baseline model)"
        description = (f"Stair riser height documented as {new_riser_mm:.1f}mm, above the "
                       f"{THRESHOLD_MM:.0f}mm maximum  - no riser data existed in the baseline model")

    set_prop_single_value(
        model, pset, "RiserHeight", "IfcPositiveLengthMeasure",
        mm(model, new_riser_mm, scale), owner_history,
    )

    return Mutation(
        rule_id=RULE_ID,
        element_type=element.is_a(),
        target_global_id=target.global_id,
        attribute=f"{pset_name}.RiserHeight",
        before=before_mm,
        after=new_riser_mm,
        clause=CLAUSE,
        description=description,
        extra={
            "element_id": element.id(),
            "mode": mode,
            "pset_name": pset_name,
            "threshold_mm": THRESHOLD_MM,
            "mechanism": mechanism,
        },
    )
