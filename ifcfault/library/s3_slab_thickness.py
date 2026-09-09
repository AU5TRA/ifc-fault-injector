"""
S3  - slab thinner than the code minimum for its span.

Mechanism: shorten the slab's extrusion Depth on a private copy of its Body,
keeping the footprint profile exactly as it was. Brep slabs fall back to a
bounding-box replacement of the thin axis.

As with S2, the new thickness is a fraction of the existing one rather than
a constant, so the result is always a genuine reduction AND clears the
threshold  - a fixed 120mm would be an increase on a 100mm topping slab.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import replace_brep_cross_section_private, set_extrusion_depth_private
from .helpers import length_unit_scale, slabs_with_thickness

RULE_ID = "S3"
CLAUSE = ("ACI 318 Table 7.3.1.1  - minimum thickness of a one-way solid slab "
          "(150 mm for the spans in these models)")
DOMAIN = "structural"
ELEMENT = "IfcSlab"
THRESHOLD_MM = 150.0

CEILING_MM = THRESHOLD_MM - 30.0   # comfortably under the minimum
REDUCTION_FACTOR = 0.7             # always a real reduction
MIN_SENSIBLE_THICKNESS_MM = 40.0

# A structural floor slab lives in this band. Thinner is usually a finish
# layer or a topping; thicker is usually a raft or a mat foundation.
REALISTIC_MIN_MM = 150.0
REALISTIC_MAX_MM = 400.0


def _thickness_for(before_mm: float) -> float:
    return max(MIN_SENSIBLE_THICKNESS_MM, min(CEILING_MM, before_mm * REDUCTION_FACTOR))


def applicable(model: ifcopenshell.file) -> Applicability:
    slabs = slabs_with_thickness(model)
    if not slabs:
        return Applicability(
            False,
            f"no IfcSlab with measurable thickness geometry "
            f"({len(model.by_type('IfcSlab'))} IfcSlab total)",
        )
    return Applicability(True, f"{len(slabs)} slab(s) with measurable thickness")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    slabs = [s for s in slabs_with_thickness(model) if s.global_id not in exclude]
    if not slabs:
        return []

    # Prefer a slab that currently MEETS the minimum, and sits in the
    # realistic structural band  - cutting a 60mm screed proves nothing.
    good = [s for s in slabs
            if s.thickness_mm >= THRESHOLD_MM and s.thickness_mm <= REALISTIC_MAX_MM]
    pool = good if good else slabs
    good_ids = {s.global_id for s in good}

    out = []
    for s in pool:
        already_bad = s.thickness_mm < THRESHOLD_MM
        new_t = _thickness_for(s.thickness_mm)
        out.append(ScoredTarget(
            global_id=s.global_id,
            score=s.thickness_mm,  # thickest qualifying slab = clearest deliberate cut
            justification=f"thickness={s.thickness_mm:.0f}mm -> {new_t:.0f}mm"
                          + (f" (within the realistic {REALISTIC_MIN_MM:.0f}-"
                             f"{REALISTIC_MAX_MM:.0f}mm structural band)"
                             if s.global_id in good_ids else " (fallback: outside the "
                             f"{REALISTIC_MIN_MM:.0f}-{REALISTIC_MAX_MM:.0f}mm band)")
                          + (f" (ALREADY under the {THRESHOLD_MM:.0f}mm minimum before "
                             f"injection)" if already_bad else ""),
            element_ids=(s.slab_id,),
            extra={"before_thickness_mm": s.thickness_mm, "already_noncompliant": already_bad},
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    slab = model.by_guid(target.global_id)
    expected_before = target.extra["before_thickness_mm"]

    new_thickness_mm = (float(params["new_thickness_mm"]) if "new_thickness_mm" in params
                        else _thickness_for(expected_before))

    try:
        before_mm = set_extrusion_depth_private(model, slab, new_thickness_mm, scale)
        mechanism = "private extrusion Depth change (swept solid)"
    except RuntimeError:
        before_dims = replace_brep_cross_section_private(
            model, slab, scale, new_width_mm=new_thickness_mm
        )
        before_mm = before_dims["width_mm"]
        mechanism = "private bounding-box replacement of the thin axis (IfcFacetedBrep fallback)"

    already_bad = target.extra.get("already_noncompliant", False)
    note = ("" if not already_bad else
            f" (this slab was already under the {THRESHOLD_MM:.0f}mm minimum, so the injected "
            f"defect is the further reduction)")
    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcSlab",
        target_global_id=target.global_id,
        attribute="slab thickness",
        before=before_mm,
        after=new_thickness_mm,
        clause=CLAUSE,
        description=f"Slab thickness cut from {before_mm:.0f}mm to {new_thickness_mm:.0f}mm, "
                    f"below the {THRESHOLD_MM:.0f}mm minimum{note}",
        extra={
            "element_id": slab.id(),
            "threshold_mm": THRESHOLD_MM,
            "thickness_derived_from_section": "new_thickness_mm" not in params,
            "already_noncompliant_before": already_bad,
            "mechanism": mechanism,
        },
    )
