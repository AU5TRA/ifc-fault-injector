"""
Schema-correct mutation primitives. Every rule's apply_violation() goes
through these rather than poking ifcopenshell directly, so the
"no dangling references, no degenerate relationships" guarantee lives in
exactly one reviewed place.

Same inlining constraints as helpers.py: flat namespace, stdlib +
ifcopenshell only.

Two invariants worth stating outright, because they are the reason this
module exists:

  * ifcopenshell's own `.remove()` nulls attributes that referenced the
    removed entity, but does NOT delete a relationship left holding zero
    members. An IfcRelDefinesByProperties with an empty RelatedObjects is
    an EXPRESS SET [1:?] cardinality violation even though nothing
    technically dangles. `delete_element` cleans those up.

  * Geometry edits NEVER touch a shared mapped representation. Real models
    have hundreds of elements pointing at one IfcMappedItem source; editing
    it in place would silently resize every one of them. Instead the target
    gets its own private replacement representation.
"""
from __future__ import annotations

import hashlib
import uuid

import ifcopenshell
import ifcopenshell.guid

from .helpers import (
    brep_axis_extents, find_prop, length_unit_scale, mm, psets_of, resolve_body_items, to_mm,
)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
def deterministic_guid(*parts: str) -> str:
    """A valid 22-character IFC GlobalId derived from a stable hash instead
    of uuid4(), so re-running a script produces a byte-identical file."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()[:16]
    return ifcopenshell.guid.compress(uuid.UUID(bytes=digest).hex)


def any_owner_history(model: ifcopenshell.file):
    """Reuse an existing IfcOwnerHistory rather than fabricating one  - new
    entities need it, and inventing an owner/application would be a bigger
    change to the file than the violation itself."""
    histories = model.by_type("IfcOwnerHistory")
    return histories[0] if histories else None


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------
# Relationship types whose LIST-valued attribute may reference our element:
# the element is filtered out of the list, and the relationship itself is
# dropped only if that would leave the list empty.
LIST_REF_ATTRS = {
    "IfcRelContainedInSpatialStructure": "RelatedElements",
    "IfcRelDefinesByProperties": "RelatedObjects",
    "IfcRelAssociatesMaterial": "RelatedObjects",
    "IfcRelDefinesByType": "RelatedObjects",
    "IfcRelAggregates": "RelatedObjects",
    "IfcRelNests": "RelatedObjects",
}

# Relationship types with SINGLE-valued attributes pointing at elements.
# With one side gone the relationship is meaningless, so it is dropped
# whenever it references our element.
SINGLE_REF_TYPES = (
    "IfcRelConnectsElements",
    "IfcRelConnectsPathElements",
    "IfcRelConnectsWithRealizingElements",
    "IfcRelVoidsElement",
    "IfcRelFillsElement",
    "IfcRelSpaceBoundary",
    "IfcRelInterferesElements",
)


def delete_element(model: ifcopenshell.file, element) -> dict:
    """Remove an element and every relationship instance referencing it.

    Returns a COMPLETE account of what changed:

        {"removed_global_ids":  [...],   # entities that no longer exist
         "modified_global_ids": [...],   # entities that merely lost a member
         "relationship_types":  [...]}   # IFC type names touched, deduped

    Completeness matters: the independent verifier compares the whole file
    against the source and allows only the changes the mutation record
    declares. An under-reported cascade shows up there as an unexplained
    diff, which is indistinguishable from a real bug.
    """
    eid = element.id()
    element_guid = element.GlobalId
    removed: list[str] = []
    modified: list[str] = []
    types: list[str] = []

    for rel in list(model.get_inverse(element)):
        # Capture identity BEFORE any possible model.remove(rel): calling a
        # method on a SWIG-wrapped entity after removal is a use-after-free
        # that can take down the interpreter.
        rel_type = rel.is_a()
        rel_guid = getattr(rel, "GlobalId", None)

        handled = False
        for typename, attr in LIST_REF_ATTRS.items():
            if rel.is_a(typename):
                remaining = tuple(e for e in getattr(rel, attr) if e.id() != eid)
                if remaining:
                    setattr(rel, attr, remaining)
                    if rel_guid:
                        modified.append(rel_guid)
                else:
                    # An emptied list would be an EXPRESS SET [1:?] violation,
                    # so the relationship itself has to go.
                    model.remove(rel)
                    if rel_guid:
                        removed.append(rel_guid)
                types.append(rel_type)
                handled = True
                break
        if handled:
            continue

        if any(rel.is_a(t) for t in SINGLE_REF_TYPES):
            model.remove(rel)
            if rel_guid:
                removed.append(rel_guid)
            types.append(rel_type)
            continue

        # Unknown relationship type: scan its direct attributes for a
        # reference to this element and drop it if found, since a dangling
        # reference would corrupt the file on write.
        info = rel.get_info(include_identifier=False, recursive=False)
        for value in info.values():
            if hasattr(value, "id") and not isinstance(value, (str, bytes)):
                try:
                    if value.id() == eid:
                        model.remove(rel)
                        if rel_guid:
                            removed.append(rel_guid)
                        types.append(rel_type)
                        break
                except Exception:
                    pass

    model.remove(element)
    removed.append(element_guid)

    # A relationship can be both modified and removed across two passes only
    # if it referenced the element twice; removal wins.
    removed_set = set(removed)
    return {
        "removed_global_ids": sorted(removed_set),
        "modified_global_ids": sorted(set(modified) - removed_set),
        "relationship_types": sorted(set(types)),
    }


def find_dangling_references(model: ifcopenshell.file) -> list[str]:
    """Sweep every entity for an attribute pointing at an id ifcopenshell can
    no longer resolve. Empty list means clean."""
    problems = []
    for inst in model:
        try:
            info = inst.get_info(include_identifier=True, recursive=False)
        except Exception as e:
            problems.append(f"{inst}: get_info failed: {e!r}")
            continue
        for attr_name, value in info.items():
            for v in (value if isinstance(value, (tuple, list)) else [value]):
                if hasattr(v, "id") and not isinstance(v, (str, bytes)):
                    try:
                        vid = v.id()
                        if vid:
                            model.by_id(vid)
                    except Exception:
                        problems.append(
                            f"{inst.is_a()}#{inst.id()}.{attr_name} references a missing entity"
                        )
    return problems


# ---------------------------------------------------------------------------
# property sets
# ---------------------------------------------------------------------------
def get_or_create_pset(model: ifcopenshell.file, element, pset_name: str, owner_history,
                       guid_seed: str = ""):
    for pdef in psets_of(element):
        if pdef.Name == pset_name:
            return pdef
    pset = model.create_entity(
        "IfcPropertySet",
        GlobalId=deterministic_guid("pset", pset_name, element.GlobalId, guid_seed),
        OwnerHistory=owner_history, Name=pset_name, Description=None, HasProperties=(),
    )
    model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=deterministic_guid("rel-pset", pset_name, element.GlobalId, guid_seed),
        OwnerHistory=owner_history, Name=None, Description=None,
        RelatedObjects=(element,), RelatingPropertyDefinition=pset,
    )
    return pset


def set_prop_single_value(model: ifcopenshell.file, pset, name: str, value_type: str, value,
                          owner_history=None):
    """Create or overwrite an IfcPropertySingleValue. Returns the previous
    NominalValue as a string, or None if the property did not exist."""
    existing = find_prop(pset, name)
    wrapped = model.create_entity(value_type, value)
    if existing is not None:
        old = existing.NominalValue
        old_repr = str(old) if old is not None else None
        existing.NominalValue = wrapped
        return old_repr
    new_prop = model.create_entity(
        "IfcPropertySingleValue", Name=name, Description=None, NominalValue=wrapped, Unit=None,
    )
    pset.HasProperties = tuple(pset.HasProperties) + (new_prop,)
    return None


# ---------------------------------------------------------------------------
# geometry  - always private to the target element
# ---------------------------------------------------------------------------
def _axis2placement2d(model):
    return model.create_entity(
        "IfcAxis2Placement2D",
        Location=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0)),
        RefDirection=None,
    )


def _find_body(element, ifc_class: str):
    """(representation, item) for the first Body item of `ifc_class`, following
    IfcMappedItem. Returns (None, None) if there is no such item."""
    rep = getattr(element, "Representation", None)
    if rep is None:
        return None, None
    for r in rep.Representations:
        if r.RepresentationIdentifier != "Body":
            continue
        for item in r.Items:
            if item.is_a("IfcMappedItem"):
                for sub in item.MappingSource.MappedRepresentation.Items:
                    if sub.is_a(ifc_class):
                        return r, sub
            elif item.is_a(ifc_class):
                return r, item
    return None, None


def _swap_body(model, element, old_rep, new_solid):
    """Give `element` a fresh IfcProductDefinitionShape in which `old_rep`
    is replaced by a private Body holding `new_solid`."""
    rep = element.Representation
    new_reps = []
    for r in rep.Representations:
        if r is old_rep:
            new_reps.append(model.create_entity(
                "IfcShapeRepresentation", ContextOfItems=r.ContextOfItems,
                RepresentationIdentifier="Body", RepresentationType="SweptSolid",
                Items=(new_solid,),
            ))
        else:
            new_reps.append(r)
    element.Representation = model.create_entity(
        "IfcProductDefinitionShape", Name=None, Description=None, Representations=tuple(new_reps),
    )


def replace_profile_private(model: ifcopenshell.file, element,
                            new_width_mm: float, new_depth_mm: float, scale: float):
    """Swap in a private rectangular profile of the requested cross-section,
    preserving the original extrusion length, direction and position.

    Raises RuntimeError if the Body is not a swept solid  - callers should
    fall back to replace_brep_cross_section_private.
    """
    rep, solid = _find_body(element, "IfcExtrudedAreaSolid")
    if solid is None:
        raise RuntimeError("no Body/IfcExtrudedAreaSolid found to replace")
    new_profile = model.create_entity(
        "IfcRectangleProfileDef", ProfileType="AREA", ProfileName=None,
        Position=_axis2placement2d(model),
        XDim=mm(model, new_width_mm, scale), YDim=mm(model, new_depth_mm, scale),
    )
    new_solid = model.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=new_profile, Position=solid.Position,
        ExtrudedDirection=solid.ExtrudedDirection, Depth=solid.Depth,
    )
    _swap_body(model, element, rep, new_solid)


def set_extrusion_depth_private(model: ifcopenshell.file, element,
                                new_depth_mm: float, scale: float) -> float:
    """Set a new extrusion Depth (a slab's thickness) keeping the same
    footprint profile, position and direction. Returns the ORIGINAL depth in mm."""
    rep, solid = _find_body(element, "IfcExtrudedAreaSolid")
    if solid is None:
        raise RuntimeError("no Body/IfcExtrudedAreaSolid found to resize")
    old_depth_mm = to_mm(model, solid.Depth, scale)
    new_solid = model.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=solid.SweptArea, Position=solid.Position,
        ExtrudedDirection=solid.ExtrudedDirection, Depth=mm(model, new_depth_mm, scale),
    )
    _swap_body(model, element, rep, new_solid)
    return old_depth_mm


def replace_brep_cross_section_private(
    model: ifcopenshell.file, element, scale: float,
    new_depth_mm: float | None = None, new_width_mm: float | None = None,
):
    """Fallback for a Body that is an IfcFacetedBrep (no profile to resize):
    replace it with a single axis-aligned box sized from the Brep's own
    bounding box, leaving the longest (length) axis untouched.

    Returns the ORIGINAL {'length_mm', 'depth_mm', 'width_mm'} for the record.
    """
    rep, brep = _find_body(element, "IfcFacetedBrep")
    if brep is None:
        raise RuntimeError("no Body/IfcFacetedBrep found to replace")
    extents = brep_axis_extents(brep)
    if extents is None:
        raise RuntimeError("could not extract any points from the IfcFacetedBrep")

    spans = {ax: (hi - lo) for ax, (lo, hi) in extents.items()}
    length_ax, depth_ax, width_ax = sorted(spans, key=lambda ax: spans[ax], reverse=True)
    before_mm = {
        "length_mm": to_mm(model, spans[length_ax], scale),
        "depth_mm": to_mm(model, spans[depth_ax], scale),
        "width_mm": to_mm(model, spans[width_ax], scale),
    }

    def centered(ax, new_span_mm):
        lo, hi = extents[ax]
        center = (lo + hi) / 2.0
        half = mm(model, new_span_mm, scale) / 2.0
        return center - half, center + half

    new_extents = dict(extents)
    if new_depth_mm is not None:
        new_extents[depth_ax] = centered(depth_ax, new_depth_mm)
    if new_width_mm is not None:
        new_extents[width_ax] = centered(width_ax, new_width_mm)

    (x0, x1), (y0, y1), (z0, z1) = new_extents["x"], new_extents["y"], new_extents["z"]
    new_profile = model.create_entity(
        "IfcRectangleProfileDef", ProfileType="AREA", ProfileName=None,
        Position=_axis2placement2d(model), XDim=(x1 - x0), YDim=(y1 - y0),
    )
    new_solid = model.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=new_profile,
        Position=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(x0, y0, z0)),
            Axis=None, RefDirection=None,
        ),
        ExtrudedDirection=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=(z1 - z0),
    )
    _swap_body(model, element, rep, new_solid)
    return before_mm


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------
def set_opening_x_along_wall(model: ifcopenshell.file, opening,
                             new_x_native: float, other_coords: tuple):
    """Move an IfcOpeningElement along its host wall's local X axis, keeping
    the other coordinates. Creates a fresh IfcCartesianPoint rather than
    editing the existing one, which could be shared."""
    axp = opening.ObjectPlacement.RelativePlacement
    axp.Location = model.create_entity(
        "IfcCartesianPoint", Coordinates=(new_x_native,) + tuple(other_coords)
    )
