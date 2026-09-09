"""
S1  - beam too shallow for its span (deflection-control violation).

Mechanism: give the beam a private, resized cross-section, keeping its span
and position. Swept-solid beams get an exact rectangular profile swap; Brep
beams fall back to a bounding-box replacement.

The new depth is DERIVED FROM THE SPAN, not a fixed number. A constant depth
only produces a violation on beams that happen to be long enough: 400mm is
non-compliant over an 11m span (L/d 27.7) but perfectly fine over an 8m one
(L/d 20.0). Solving for the depth that lands at a chosen L/d makes the rule
correct on every beam it accepts.

Caveat worth knowing: the Brep fallback identifies "length", "depth" and
"width" by sorting the bounding-box axes longest-to-shortest. For a beam
that is strongly rotated in plan, or curved, that ordering can pick the
wrong axis, and the depth this rule sets will not be the depth a checker
measures. `candidates()` therefore prefers beams with a real swept profile,
and the independent verifier re-derives L/d itself rather than trusting the
mutation record.
"""
from __future__ import annotations

import ifcopenshell

from .contract import Applicability, Mutation, ScoredTarget
from .edits import replace_brep_cross_section_private, replace_profile_private
from .helpers import beams_with_size, length_unit_scale, resolve_body_items

RULE_ID = "S1"
CLAUSE = ("EC2 7.4.2 / ACI 318 Table 9.3.1.1  - span-to-depth ratio limit for "
          "deflection control (24 for a simply supported member)")
DOMAIN = "structural"
ELEMENT = "IfcBeam"
MAX_L_OVER_D = 24.0

# The L/d the injected section aims for: a clear 15% past the limit, so the
# violation survives rounding, unit conversion and a checker's own tolerance.
TARGET_L_OVER_D = 27.6

# Never produce a physically absurd section, however long the span.
MIN_SENSIBLE_DEPTH_MM = 60.0


def _has_swept_profile(model, element) -> bool:
    return any(i.is_a("IfcExtrudedAreaSolid") for i in resolve_body_items(element))


def _depth_for(span_mm: float) -> float:
    """The depth that puts this span at TARGET_L_OVER_D."""
    return max(MIN_SENSIBLE_DEPTH_MM, span_mm / TARGET_L_OVER_D)


def applicable(model: ifcopenshell.file) -> Applicability:
    beams = beams_with_size(model)
    if not beams:
        return Applicability(
            False,
            f"no IfcBeam with measurable span and depth geometry and a span of at least 3m "
            f"({len(model.by_type('IfcBeam'))} IfcBeam total)",
        )
    return Applicability(True, f"{len(beams)} beam(s) with measurable span/depth geometry")


def candidates(model: ifcopenshell.file, exclude=frozenset()) -> list[ScoredTarget]:
    beams = [b for b in beams_with_size(model) if b.global_id not in exclude]
    if not beams:
        return []

    # Only a beam that is CURRENTLY within the limit gives a genuinely
    # injected defect. Cutting an already-overspanned beam deeper just makes
    # a pre-existing problem worse, and a checker would have flagged the
    # untouched file too.
    compliant = [b for b in beams if (b.length_mm / b.depth_mm) <= MAX_L_OVER_D]
    pool = compliant if compliant else beams
    compliant_ids = {b.global_id for b in compliant}

    out = []
    for b in pool:
        before_ratio = b.length_mm / b.depth_mm
        element = model.by_guid(b.global_id)
        swept = _has_swept_profile(model, element)
        already_bad = b.global_id not in compliant_ids

        # Prefer a real swept profile (exact resize) over a Brep (bounding-box
        # approximation), then the longest span.
        score = (1_000_000.0 if swept else 0.0) + b.length_mm
        out.append(ScoredTarget(
            global_id=b.global_id,
            score=score,
            justification=f"span={b.length_mm:.0f}mm, depth={b.depth_mm:.0f}mm, current "
                          f"L/d={before_ratio:.1f}"
                          + ("" if swept else " (Brep geometry  - bounding-box resize)")
                          + (" (ALREADY over the limit before injection)" if already_bad else ""),
            element_ids=(b.element_id,),
            extra={
                "before_span_mm": b.length_mm,
                "before_width_mm": b.width_mm,
                "before_depth_mm": b.depth_mm,
                "before_L_over_d": before_ratio,
                "has_swept_profile": swept,
                "already_noncompliant": already_bad,
            },
        ))
    out.sort(key=lambda t: (-t.score, t.global_id))
    return out


def apply_violation(model: ifcopenshell.file, target: ScoredTarget, params: dict) -> Mutation:
    scale = length_unit_scale(model)
    beam = model.by_guid(target.global_id)
    span_mm = target.extra["before_span_mm"]
    width_mm = target.extra["before_width_mm"]
    before_depth_mm = target.extra["before_depth_mm"]

    new_depth_mm = float(params["new_depth_mm"]) if "new_depth_mm" in params else _depth_for(span_mm)

    try:
        replace_profile_private(model, beam, width_mm, new_depth_mm, scale)
        mechanism = "private IfcRectangleProfileDef swap (swept solid)"
    except RuntimeError:
        replace_brep_cross_section_private(model, beam, scale, new_depth_mm=new_depth_mm)
        mechanism = "private bounding-box replacement (IfcFacetedBrep fallback)"

    before_ratio = span_mm / before_depth_mm
    after_ratio = span_mm / new_depth_mm
    return Mutation(
        rule_id=RULE_ID,
        element_type="IfcBeam",
        target_global_id=target.global_id,
        attribute="cross-section depth",
        before=before_depth_mm,
        after=new_depth_mm,
        clause=CLAUSE,
        description=f"Beam depth cut from {before_depth_mm:.0f}mm to {new_depth_mm:.0f}mm over a "
                    f"{span_mm:.0f}mm span, driving span-to-depth from {before_ratio:.1f} to "
                    f"{after_ratio:.1f}  - past the limit of {MAX_L_OVER_D:.0f}",
        extra={
            "element_id": beam.id(),
            "span_mm": span_mm,
            "before_L_over_d": before_ratio,
            "after_L_over_d": after_ratio,
            "max_L_over_d": MAX_L_OVER_D,
            "target_L_over_d": TARGET_L_OVER_D,
            "depth_derived_from_span": "new_depth_mm" not in params,
            "already_noncompliant_before": target.extra.get("already_noncompliant", False),
            "has_swept_profile": target.extra.get("has_swept_profile"),
            "mechanism": mechanism,
        },
    )
