"""
S2  - column cross-section reduced below the minimum dimension / slenderness
limit.

Mechanism: private rectangular profile swap to a small square section,
keeping the column's height and position. Brep columns fall back to a
bounding-box replacement.

The new dimension is DERIVED from the column's existing section rather than
fixed, for two reasons. A constant of 250mm is the EC8 minimum itself, so
setting a section TO it is compliant, not a violation. And plenty of real
models already have columns below 250mm  - on those, a fixed 250mm would
make the column *larger* and the file *more* compliant than before. Taking
a fraction of whatever is there guarantees the section always ends up both
under the minimum and strictly more slender than it started.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import replace_brep_cross_section_private, replace_profile_private
from .helpers import columns_with_size, length_unit_scale, resolve_body_items

RULE_ID = "S2"
CLAUSE = ("EC8 5.4.1.2.1 / EC2 5.8.3.1  - minimum cross-sectional dimension for a "
          "primary seismic column (250 mm) and second-order slenderness limit")
DOMAIN = "structural"
ELEMENT = "IfcColumn"
MIN_DIMENSION_MM = 250.0

# Comfortably under the minimum, so the violation survives rounding.
CEILING_MM = MIN_DIMENSION_MM - 50.0
# Fraction of the existing section, so the result is always a reduction.
REDUCTION_FACTOR = 0.7
MIN_SENSIBLE_DIM_MM = 60.0
# Below this height an "IfcColumn" is a pedestal, a stub or a base plate, not
# a primary seismic column the clause is about. Preferred, not required.
MIN_PLAUSIBLE_HEIGHT_MM = 2000.0


def _has_swept_profile(model, element) -> bool:
    return any(i.is_a("IfcExtrudedAreaSolid") for i in resolve_body_items(element))


def _dimension_for(before_min_dim: float | None) -> float:
    """A section that is both under the code minimum and smaller than what
    is already there."""
    if not before_min_dim:
        return CEILING_MM
    return max(MIN_SENSIBLE_DIM_MM, min(CEILING_MM, before_min_dim * REDUCTION_FACTOR))


def applicable(model: ifcopenshell.file) -> Applicability:
    cols = [c for c in columns_with_size(model) if c.width_mm and c.depth_mm]
    if not cols:
        return Applicability(
            False,
            f"no IfcColumn with a measurable cross-section "
            f"({len(model.by_type('IfcColumn'))} IfcColumn total)",
        )
    return Applicability(True, f"{len(cols)} column(s) with a measurable cross-section")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    cols = [c for c in columns_with_size(model)
            if c.global_id not in exclude and c.width_mm and c.depth_mm]
    if not cols:
        return []

    # A column already under the minimum is a pre-existing defect, not an
    # injected one  - prefer one that currently complies, and that is tall
    # enough to actually be a column. Each filter falls back rather than
    # emptying the candidate list.
    compliant = [c for c in cols if min(c.width_mm, c.depth_mm) >= MIN_DIMENSION_MM]
    pool = compliant if compliant else cols
    compliant_ids = {c.global_id for c in compliant}

    plausible = [c for c in pool if c.length_mm >= MIN_PLAUSIBLE_HEIGHT_MM]
    pool = plausible if plausible else pool
    plausible_ids = {c.global_id for c in plausible}

    out = []
    for c in pool:
        min_dim = min(c.width_mm, c.depth_mm)
        element = model.by_guid(c.global_id)
        swept = _has_swept_profile(model, element)
        already_bad = c.global_id not in compliant_ids
        is_stub = c.global_id not in plausible_ids
        new_dim = _dimension_for(min_dim)

        # Prefer an exact swept-profile resize, then a plausible column
        # height, then the tallest  - which gives the worst slenderness.
        score = (1_000_000.0 if swept else 0.0) + c.length_mm
        out.append(ScoredTarget(
            global_id=c.global_id,
            score=score,
            justification=f"height={c.length_mm:.0f}mm, current min dimension={min_dim:.0f}mm "
                          f"-> {new_dim:.0f}mm"
                          + ("" if swept else " (Brep geometry  - bounding-box resize)")
                          + (f" (fallback: shorter than {MIN_PLAUSIBLE_HEIGHT_MM:.0f}mm, so "
                             f"likely a pedestal rather than a storey column)" if is_stub else "")
                          + (f" (ALREADY under the {MIN_DIMENSION_MM:.0f}mm minimum before "
                             f"injection)" if already_bad else ""),
            element_ids=(c.element_id,),
            extra={
                "before_height_mm": c.length_mm,
                "before_width_mm": c.width_mm,
                "before_depth_mm": c.depth_mm,
                "before_min_dim_mm": min_dim,
                "has_swept_profile": swept,
                "already_noncompliant": already_bad,
                "is_stub": is_stub,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    column = model.by_guid(target.global_id)
    height_mm = target.extra["before_height_mm"]
    before_min_dim = target.extra["before_min_dim_mm"]

    new_dim_mm = (float(params["new_dim_mm"]) if "new_dim_mm" in params
                  else _dimension_for(before_min_dim))

    try:
        replace_profile_private(model, column, new_dim_mm, new_dim_mm, scale)
        mechanism = "private IfcRectangleProfileDef swap (swept solid)"
    except RuntimeError:
        replace_brep_cross_section_private(
            model, column, scale, new_depth_mm=new_dim_mm, new_width_mm=new_dim_mm
        )
        mechanism = "private bounding-box replacement (IfcFacetedBrep fallback)"

    before_slenderness = height_mm / before_min_dim if before_min_dim else None
    after_slenderness = height_mm / new_dim_mm
    already_bad = target.extra.get("already_noncompliant", False)

    note = ("" if not already_bad else
            f" (this column was already under the {MIN_DIMENSION_MM:.0f}mm minimum at "
            f"{before_min_dim:.0f}mm, so the injected defect is the increased slenderness)")
    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcColumn",
        target_global_id=target.global_id,
        attribute="cross-section dimension",
        before=before_min_dim,
        after=new_dim_mm,
        clause=CLAUSE,
        description=f"Column cross-section reduced from {before_min_dim:.0f}mm to a "
                    f"{new_dim_mm:.0f}mm square over a {height_mm:.0f}mm height, below the "
                    f"{MIN_DIMENSION_MM:.0f}mm minimum; slenderness "
                    f"{before_slenderness:.1f} -> {after_slenderness:.1f}{note}",
        extra={
            "element_id": column.id(),
            "height_mm": height_mm,
            "before_slenderness": before_slenderness,
            "after_slenderness": after_slenderness,
            "min_dimension_mm": MIN_DIMENSION_MM,
            "dimension_derived_from_section": "new_dim_mm" not in params,
            "already_noncompliant_before": already_bad,
            "has_swept_profile": target.extra.get("has_swept_profile"),
            "mechanism": mechanism,
        },
    )
