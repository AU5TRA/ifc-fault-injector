"""
Read-only IFC queries. Nothing in this module ever mutates a model.

Every function here is a candidate for being source-extracted and inlined
verbatim into a generated standalone script, so this module obeys two rules:

  1. flat namespace  - no classes to inherit from, no relative imports
     between helpers beyond plain top-level function calls
  2. only `ifcopenshell`, `math`, `dataclasses` and `typing` are imported

Iteration order is always sorted by GlobalId (or a documented secondary
key), so candidate rankings built on top of these results are reproducible
run to run and machine to machine.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import ifcopenshell


# ---------------------------------------------------------------------------
# units
#
# `length_unit_scale` returns the mm -> native multiplier, so:
#     native_value = mm_value * scale
#     mm_value     = native_value / scale
# For a millimetre model scale is 1.0; for a metre model it is 0.001.
# ---------------------------------------------------------------------------
_SI_PREFIX_FACTOR = {
    None: 1.0, "MILLI": 1e-3, "CENTI": 1e-2, "DECI": 1e-1,
    "DECA": 1e1, "HECTO": 1e2, "KILO": 1e3, "MICRO": 1e-6,
}


def length_unit_scale(model: ifcopenshell.file) -> float:
    """Multiplier converting millimetres -> this model's native length unit.

    Handles IfcSIUnit (with prefix) and IfcConversionBasedUnit (feet/inches,
    which real-world US models do use). Falls back to metres, the most
    common unprefixed case, if the project declares nothing usable.
    """
    projects = model.by_type("IfcProject")
    if not projects:
        return 1e-3
    units = getattr(projects[0].UnitsInContext, "Units", None) or ()
    for u in units:
        if u.is_a("IfcSIUnit") and u.UnitType == "LENGTHUNIT":
            metres_per_native = _SI_PREFIX_FACTOR.get(u.Prefix, 1.0)
            return 1e-3 / metres_per_native
        if u.is_a("IfcConversionBasedUnit") and u.UnitType == "LENGTHUNIT":
            factor = u.ConversionFactor
            # ConversionFactor is an IfcMeasureWithUnit whose ValueComponent
            # is how many SI units make one of these (e.g. 0.3048 m = 1 ft).
            try:
                metres_per_native = float(factor.ValueComponent.wrappedValue)
            except Exception:
                continue
            if metres_per_native > 0:
                return 1e-3 / metres_per_native
    return 1e-3


def length_unit_name(model: ifcopenshell.file) -> str:
    """Human-readable unit name, for the report header."""
    projects = model.by_type("IfcProject")
    if not projects:
        return "unknown"
    units = getattr(projects[0].UnitsInContext, "Units", None) or ()
    for u in units:
        if u.is_a("IfcSIUnit") and u.UnitType == "LENGTHUNIT":
            return f"{u.Prefix or ''}{u.Name}".strip()
        if u.is_a("IfcConversionBasedUnit") and u.UnitType == "LENGTHUNIT":
            return str(u.Name)
    return "unknown"


def mm(model: ifcopenshell.file, value_mm: float, scale: Optional[float] = None) -> float:
    """Millimetres -> native units."""
    return value_mm * (scale if scale is not None else length_unit_scale(model))


def to_mm(model: ifcopenshell.file, native_value: float, scale: Optional[float] = None) -> float:
    """Native units -> millimetres."""
    return native_value / (scale if scale is not None else length_unit_scale(model))


def sorted_by_guid(elements):
    return sorted(elements, key=lambda e: e.GlobalId)


# ---------------------------------------------------------------------------
# property sets
# ---------------------------------------------------------------------------
def psets_of(element) -> list:
    out = []
    for rel in getattr(element, "IsDefinedBy", []) or []:
        if rel.is_a("IfcRelDefinesByProperties"):
            pdef = rel.RelatingPropertyDefinition
            if pdef.is_a("IfcPropertySet"):
                out.append(pdef)
    return out


def find_prop(pset, name: str):
    for p in pset.HasProperties:
        if p.Name == name:
            return p
    return None


def prop_value(pset, name: str):
    p = find_prop(pset, name)
    if p is None or p.NominalValue is None:
        return None
    return p.NominalValue.wrappedValue


# ---------------------------------------------------------------------------
# spatial structure
# ---------------------------------------------------------------------------
def storeys_sorted(model: ifcopenshell.file) -> list:
    """Storeys bottom-to-top. Falls back to GlobalId order when elevations
    are missing, so the order is always total and deterministic."""
    storeys = model.by_type("IfcBuildingStorey")
    with_elev = [s for s in storeys if s.Elevation is not None]
    if len(with_elev) >= 2:
        return sorted(with_elev, key=lambda s: (s.Elevation, s.GlobalId))
    return sorted_by_guid(storeys)


def elements_of_storey(model: ifcopenshell.file, storey, ifc_type: str) -> list:
    out = []
    for rel in model.get_inverse(storey):
        if rel.is_a("IfcRelContainedInSpatialStructure"):
            out.extend(e for e in rel.RelatedElements if e.is_a(ifc_type))
    return sorted_by_guid(out)


def storey_of(model: ifcopenshell.file, element):
    """The IfcBuildingStorey containing this element, or None."""
    for rel in model.get_inverse(element):
        if rel.is_a("IfcRelContainedInSpatialStructure"):
            structure = rel.RelatingStructure
            if structure.is_a("IfcBuildingStorey"):
                return structure
    return None


def global_xyz_mm(model: ifcopenshell.file, element,
                  scale: Optional[float] = None) -> Optional[tuple[float, float, float]]:
    """World-coordinate origin of an element's placement, in millimetres.
    Returns None for an element with no resolvable placement.

    Pass `scale` when calling this in a loop  - resolving the unit scale means
    a by_type("IfcProject") lookup, which is wasteful thousands of times over.
    """
    import ifcopenshell.util.placement as ifc_placement

    if getattr(element, "ObjectPlacement", None) is None:
        return None
    try:
        m = ifc_placement.get_local_placement(element.ObjectPlacement)
    except Exception:
        return None
    if scale is None:
        scale = length_unit_scale(model)
    return (float(m[0, 3]) / scale, float(m[1, 3]) / scale, float(m[2, 3]) / scale)


# ---------------------------------------------------------------------------
# geometry: dimension reader for beams / columns / slabs
#
# Handles simple IfcExtrudedAreaSolid profiles and IfcFacetedBrep meshes,
# and follows IfcMappedItem to read the mapped definition WITHOUT modifying
# it (several elements usually share one mapped representation).
# ---------------------------------------------------------------------------
def resolve_body_items(element):
    """Every geometric item making up this element's 'Body' representation,
    with IfcMappedItem indirection followed one level."""
    rep = getattr(element, "Representation", None)
    if rep is None:
        return []
    out = []
    for r in rep.Representations:
        if r.RepresentationIdentifier != "Body":
            continue
        for item in r.Items:
            if item.is_a("IfcMappedItem"):
                mrep = item.MappingSource.MappedRepresentation
                out.extend(mrep.Items)
            else:
                out.append(item)
    return out


def profile_bbox_mm(profile, scale):
    """(smaller, larger) in-plane extent of a profile, in millimetres."""
    if profile.is_a("IfcRectangleProfileDef"):
        return profile.XDim / scale, profile.YDim / scale
    pts = []
    outer = getattr(profile, "OuterCurve", None)
    if outer is not None and outer.is_a("IfcCompositeCurve"):
        for seg in outer.Segments:
            pc = seg.ParentCurve
            if pc.is_a("IfcPolyline"):
                pts.extend(p.Coordinates for p in pc.Points)
    elif outer is not None and outer.is_a("IfcPolyline"):
        pts.extend(p.Coordinates for p in outer.Points)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(xs) - min(xs)) / scale, (max(ys) - min(ys)) / scale


def brep_points(item):
    pts = []
    shell = getattr(item, "Outer", None)
    if shell is None:
        return pts
    for face in getattr(shell, "CfsFaces", ()) or ():
        for bound in getattr(face, "Bounds", ()) or ():
            loop = bound.Bound
            if loop.is_a("IfcPolyLoop"):
                pts.extend(p.Coordinates for p in loop.Polygon)
    return pts


def brep_axis_extents(item):
    pts = brep_points(item)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    zs = [p[2] for p in pts]
    return {"x": (min(xs), max(xs)), "y": (min(ys), max(ys)), "z": (min(zs), max(zs))}


@dataclass(frozen=True)
class ElementSize:
    element_id: int
    global_id: str
    length_mm: float          # span (beam) / height (column) / extrusion depth
    width_mm: Optional[float]  # smaller cross-section dimension
    depth_mm: Optional[float]  # larger cross-section dimension


def element_size_mm(model: ifcopenshell.file, element) -> Optional[ElementSize]:
    """Length + cross-section of a linear element, in millimetres.

    For a swept solid this is exact (extrusion Depth + profile bbox). For a
    Brep it is the axis-aligned bounding box with the axes ordered
    longest -> shortest, which is only meaningful for a member that is
    roughly axis-aligned; a strongly rotated or curved Brep member will
    report misleading numbers. Rules that care should say so.
    """
    scale = length_unit_scale(model)
    for item in resolve_body_items(element):
        if item.is_a("IfcExtrudedAreaSolid"):
            bbox = profile_bbox_mm(item.SweptArea, scale)
            length = item.Depth / scale
            if bbox:
                return ElementSize(element.id(), element.GlobalId, length, min(bbox), max(bbox))
            return ElementSize(element.id(), element.GlobalId, length, None, None)
        if item.is_a("IfcFacetedBrep"):
            extents = brep_axis_extents(item)
            if extents is None:
                continue
            spans = {ax: (hi - lo) for ax, (lo, hi) in extents.items()}
            length_ax, depth_ax, width_ax = sorted(spans, key=lambda ax: spans[ax], reverse=True)
            return ElementSize(
                element.id(), element.GlobalId,
                spans[length_ax] / scale, spans[width_ax] / scale, spans[depth_ax] / scale,
            )
    return None


def slab_thickness_mm(model: ifcopenshell.file, slab) -> Optional[float]:
    """Thickness = smallest overall dimension of the slab's Body."""
    scale = length_unit_scale(model)
    for item in resolve_body_items(slab):
        if item.is_a("IfcExtrudedAreaSolid"):
            bbox = profile_bbox_mm(item.SweptArea, scale)
            dims = [item.Depth / scale] + (list(bbox) if bbox else [])
            return min(dims) if dims else None
        if item.is_a("IfcFacetedBrep"):
            extents = brep_axis_extents(item)
            if extents is None:
                continue
            spans = [(hi - lo) / scale for lo, hi in extents.values()]
            return min(spans) if spans else None
    return None


def beams_with_size(model: ifcopenshell.file, min_span_mm: float = 3000.0) -> list[ElementSize]:
    out = []
    for b in sorted_by_guid(model.by_type("IfcBeam")):
        sz = element_size_mm(model, b)
        if sz is not None and sz.depth_mm and sz.length_mm >= min_span_mm:
            out.append(sz)
    return out


def columns_with_size(model: ifcopenshell.file) -> list[ElementSize]:
    out = []
    for c in sorted_by_guid(model.by_type("IfcColumn")):
        sz = element_size_mm(model, c)
        if sz is not None:
            out.append(sz)
    return out


@dataclass(frozen=True)
class SlabThickness:
    slab_id: int
    global_id: str
    thickness_mm: float


def slabs_with_thickness(model: ifcopenshell.file) -> list[SlabThickness]:
    out = []
    for s in sorted_by_guid(model.by_type("IfcSlab")):
        t = slab_thickness_mm(model, s)
        if t and t > 0:
            out.append(SlabThickness(s.id(), s.GlobalId, t))
    return out


# ---------------------------------------------------------------------------
# doors
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DoorWidth:
    door_id: int
    global_id: str
    width_mm: float


def door_widths(model: ifcopenshell.file) -> list[DoorWidth]:
    scale = length_unit_scale(model)
    out = []
    for d in sorted_by_guid(model.by_type("IfcDoor")):
        if d.OverallWidth is not None:
            out.append(DoorWidth(d.id(), d.GlobalId, to_mm(model, d.OverallWidth, scale)))
    return out


def get_opening(model, door):
    for rel in model.get_inverse(door):
        if rel.is_a("IfcRelFillsElement"):
            return rel.RelatingOpeningElement
    return None


def get_host_wall(model, door):
    for rel in model.get_inverse(door):
        if rel.is_a("IfcRelFillsElement"):
            opening = rel.RelatingOpeningElement
            for rel2 in model.get_inverse(opening):
                if rel2.is_a("IfcRelVoidsElement"):
                    return rel2.RelatingBuildingElement
    return None


def wall_axis_length(model, wall) -> Optional[float]:
    """Length of a wall's 'Axis' polyline, in NATIVE units (not mm)."""
    rep = getattr(wall, "Representation", None)
    if rep is None:
        return None
    for r in rep.Representations:
        if r.RepresentationIdentifier == "Axis":
            for item in r.Items:
                if item.is_a("IfcPolyline"):
                    pts = [p.Coordinates for p in item.Points]
                    if len(pts) >= 2:
                        return math.dist(pts[0][:2], pts[-1][:2])
    return None


@dataclass(frozen=True)
class DoorClearance:
    door_id: int
    global_id: str
    nearest_end_clearance_mm: float


def doors_with_clearance(model: ifcopenshell.file) -> list[DoorClearance]:
    """Doors sitting in a wall of plausible length, far enough from a wall
    end that pushing them closer is a genuine change (A4's target set)."""
    scale = length_unit_scale(model)
    out = []
    for d in sorted_by_guid(model.by_type("IfcDoor")):
        opening = get_opening(model, d)
        wall = get_host_wall(model, d)
        if opening is None or wall is None:
            continue
        wlen = wall_axis_length(model, wall)
        if wlen is None or not (1.0 <= wlen <= mm(model, 12000, scale)):
            continue
        placement = opening.ObjectPlacement
        if placement is None or not placement.is_a("IfcLocalPlacement"):
            continue
        axp = placement.RelativePlacement
        if not axp.is_a("IfcAxis2Placement3D"):
            continue
        x = axp.Location.Coordinates[0]
        nearest_mm = to_mm(model, min(x, wlen - x), scale)
        if 500 < nearest_mm <= 3000:
            out.append(DoorClearance(d.id(), d.GlobalId, nearest_mm))
    return out


# ---------------------------------------------------------------------------
# stairs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StairRiserTread:
    element_id: int
    global_id: str
    element_type: str
    pset_name: Optional[str]
    riser_mm: Optional[float]
    tread_mm: Optional[float]


def stair_riser_treads(model: ifcopenshell.file) -> list[StairRiserTread]:
    scale = length_unit_scale(model)
    out = []
    for etype in ("IfcStairFlight", "IfcStair"):
        for el in sorted_by_guid(model.by_type(etype)):
            for pdef in psets_of(el):
                riser = prop_value(pdef, "RiserHeight")
                tread = prop_value(pdef, "TreadLength")
                if riser is None and tread is None:
                    continue
                out.append(
                    StairRiserTread(
                        el.id(), el.GlobalId, etype, pdef.Name,
                        to_mm(model, riser, scale) if riser is not None else None,
                        to_mm(model, tread, scale) if tread is not None else None,
                    )
                )
    return out


def stairs_without_riser_data(model: ifcopenshell.file) -> list:
    """Stair entities carrying no RiserHeight anywhere  - the fallback target
    set for formalizing a missing-riser violation."""
    have_data = {s.element_id for s in stair_riser_treads(model)}
    out = []
    for etype in ("IfcStairFlight", "IfcStair"):
        for el in sorted_by_guid(model.by_type(etype)):
            if el.id() not in have_data:
                out.append(el)
    return out


# ---------------------------------------------------------------------------
# fire ratings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FireRating:
    element_id: int
    global_id: str
    element_type: str
    pset_name: str
    value: str


def wall_fire_ratings(model: ifcopenshell.file) -> list[FireRating]:
    out = []
    for w in sorted_by_guid(model.by_type("IfcWall")):
        for pdef in psets_of(w):
            v = prop_value(pdef, "FireRating")
            if v is not None and str(v).strip():
                out.append(FireRating(w.id(), w.GlobalId, w.is_a(), pdef.Name, str(v)))
    return out


def door_fire_ratings(model: ifcopenshell.file) -> list[FireRating]:
    out = []
    for d in sorted_by_guid(model.by_type("IfcDoor")):
        for pdef in psets_of(d):
            v = prop_value(pdef, "FireRating")
            if v is not None and str(v).strip():
                out.append(FireRating(d.id(), d.GlobalId, "IfcDoor", pdef.Name, str(v)))
    return out


# ---------------------------------------------------------------------------
# space adjacency graph (dead-end / egress connectivity)
# ---------------------------------------------------------------------------
CORRIDOR_KEYWORDS = ("corridor", "hall", "gang", "overloop", "circulat", "vestibule", "lobby")


def is_corridor_space(space) -> bool:
    ln = (space.LongName or "").lower()
    nm = (space.Name or "").lower()
    return any(k in ln or k in nm for k in CORRIDOR_KEYWORDS)


@dataclass(frozen=True)
class DeadEndBridge:
    door_id: int
    global_id: str
    space_a_id: int
    space_b_id: int
    branch_space_id: int      # smaller side  - the one that gets isolated
    remaining_space_id: int
    isolated_branch_size: int


def dead_end_bridges(model: ifcopenshell.file) -> list[DeadEndBridge]:
    """Doors that are bridge edges in the space-adjacency graph and touch a
    corridor  - severing one isolates a branch behind a dead end."""
    door_spaces: dict[int, dict[int, object]] = {}
    for r in model.by_type("IfcRelSpaceBoundary"):
        el = r.RelatedBuildingElement
        if el is not None and el.is_a("IfcDoor"):
            door_spaces.setdefault(el.id(), {})[r.RelatingSpace.id()] = r.RelatingSpace

    edges = []
    for door_id in sorted(door_spaces):
        spaces = door_spaces[door_id]
        if len(spaces) == 2:
            sa, sb = sorted(spaces.values(), key=lambda s: s.GlobalId)
            edges.append((door_id, sa, sb))
    if not edges:
        return []

    adj: dict[int, set[int]] = {}
    for _, sa, sb in edges:
        adj.setdefault(sa.id(), set()).add(sb.id())
        adj.setdefault(sb.id(), set()).add(sa.id())

    def reachable(start, skip_pair):
        seen = {start}
        stack = [start]
        while stack:
            cur = stack.pop()
            for nxt in adj.get(cur, ()):
                if frozenset((cur, nxt)) == skip_pair:
                    continue
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    out = []
    for door_id, sa, sb in edges:
        if not (is_corridor_space(sa) or is_corridor_space(sb)):
            continue
        pair = frozenset((sa.id(), sb.id()))
        side_a = reachable(sa.id(), pair)
        if sb.id() in side_a:
            continue  # not a bridge  - another route still connects the two
        side_b = reachable(sb.id(), pair)
        if len(side_a) <= len(side_b):
            branch_id, remaining_id = sa.id(), sb.id()
        else:
            branch_id, remaining_id = sb.id(), sa.id()
        out.append(DeadEndBridge(
            door_id, model.by_id(door_id).GlobalId, sa.id(), sb.id(),
            branch_id, remaining_id, min(len(side_a), len(side_b)),
        ))
    out.sort(key=lambda e: (e.isolated_branch_size, e.global_id))
    return out
