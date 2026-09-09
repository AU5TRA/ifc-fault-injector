"""
Making an injected fault findable in a 3D viewer, and describing it well
enough to write a report about.

Three independent handles are applied to every violation, deliberately
redundant because no single one survives every viewer:

  1. COLOUR   IfcStyledItem -> IfcSurfaceStyle on the element's own Body.
              Honoured by BIMvision, Solibri, FZKViewer, usBIM, BlenderBIM.
              Revit's IFC importer frequently drops presentation styles, so
              colour alone is not enough.

  2. NAME TAG the element's Name is prefixed with e.g. "[!A1 VIOLATION!] ".
              Every viewer shows element names, and Revit lets you search
              and schedule on them. This is the most portable handle.

  3. MARKER   a brightly coloured IfcBuildingElementProxy box dropped at the
              violation's location. Revit imports proxies as Generic Models,
              so the box shows up even when the colour and the styling do
              not. For a rule that DELETES geometry, this is the only thing
              left to look at.

Plus an IfcPropertySet (Pset_ViolationMarker) carrying the rule id, clause
and colour  - Revit surfaces IFC property sets as element parameters, so the
fault is filterable and schedulable there.

Styling is applied per-OCCURRENCE. A styled item is attached to the
element's own top-level Body items, which for a type-mapped element is the
IfcMappedItem itself  - never the shared mapped representation underneath,
which hundreds of sibling elements point at.
"""
from __future__ import annotations

import hashlib

import ifcopenshell

from .edits import any_owner_history, deterministic_guid
from .helpers import global_xyz_mm, length_unit_scale, mm, storey_of


# ---------------------------------------------------------------------------
# generic
# ---------------------------------------------------------------------------
def sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def body_context(model: ifcopenshell.file):
    """The model's top-level 'Model'/'Design' geometric context, needed to
    hang any new geometry off. Prefers a real parent context over a
    sub-context."""
    contexts = model.by_type("IfcGeometricRepresentationContext")
    for ctx in contexts:
        if ctx.is_a("IfcGeometricRepresentationSubContext"):
            continue
        if getattr(ctx, "ContextType", None) in ("Model", "Design"):
            return ctx
    for ctx in contexts:
        if not ctx.is_a("IfcGeometricRepresentationSubContext"):
            return ctx
    if contexts:
        return contexts[0]
    raise RuntimeError("model has no IfcGeometricRepresentationContext to attach geometry to")


# ---------------------------------------------------------------------------
# colour
# ---------------------------------------------------------------------------
def violation_style(model: ifcopenshell.file, rule_id: str, rgb: tuple, cache: dict):
    """One reusable style object per rule id, valid for this model's schema.

    IFC2X3 requires IfcStyledItem.Styles to hold IfcPresentationStyleAssignment.
    IFC4 deprecated that wrapper in favour of referencing IfcSurfaceStyle
    directly, and some strict IFC4 viewers ignore the deprecated form  - so
    the shape depends on the schema of the file being edited.
    """
    if rule_id in cache:
        return cache[rule_id]

    r, g, b = rgb
    colour = model.create_entity("IfcColourRgb", Name=None, Red=float(r), Green=float(g), Blue=float(b))
    try:
        shading = model.create_entity("IfcSurfaceStyleShading", SurfaceColour=colour, Transparency=0.0)
    except Exception:
        # IFC2X3's IfcSurfaceStyleShading has no Transparency attribute.
        shading = model.create_entity("IfcSurfaceStyleShading", SurfaceColour=colour)
    surface_style = model.create_entity(
        "IfcSurfaceStyle", Name=f"VIOLATION_{rule_id}", Side="BOTH", Styles=(shading,)
    )

    if model.schema.upper().startswith("IFC2X3"):
        style = model.create_entity("IfcPresentationStyleAssignment", Styles=(surface_style,))
    else:
        style = surface_style

    cache[rule_id] = style
    return style


def paint_element(model: ifcopenshell.file, element, style) -> int:
    """Colour every top-level Body item of this one element occurrence.
    Returns how many IfcStyledItems were created (0 = nothing to colour,
    which means the caller should rely on a marker box instead)."""
    rep = getattr(element, "Representation", None)
    if rep is None:
        return 0
    painted = 0
    for r in rep.Representations:
        if r.RepresentationIdentifier != "Body":
            continue
        for item in r.Items:
            model.create_entity("IfcStyledItem", Item=item, Styles=(style,), Name=None)
            painted += 1
    return painted


# ---------------------------------------------------------------------------
# name tag + property set
# ---------------------------------------------------------------------------
def tag_element_name(element, rule_id: str, template: str = "[!{rule_id} VIOLATION!] "):
    """Prefix the element's Name with a searchable marker.
    Returns (name_before, name_after)."""
    prefix = template.format(rule_id=rule_id)
    before = element.Name
    if before and before.startswith(prefix):
        return before, before
    after = f"{prefix}{before}" if before else prefix.strip()
    element.Name = after
    return before, after


def attach_violation_pset(model: ifcopenshell.file, element, fields: dict,
                          pset_name: str = "Pset_ViolationMarker"):
    """Attach a property set describing the violation to the element.

    Written with deterministic GlobalIds so re-running produces an identical
    file. Every value is stored as an IfcText so nothing depends on the
    viewer guessing a measure type.
    """
    owner_history = any_owner_history(model)
    properties = []
    for key in sorted(fields):
        properties.append(model.create_entity(
            "IfcPropertySingleValue", Name=key, Description=None,
            NominalValue=model.create_entity("IfcText", str(fields[key])), Unit=None,
        ))
    pset = model.create_entity(
        "IfcPropertySet",
        GlobalId=deterministic_guid("violation-pset", pset_name, element.GlobalId),
        OwnerHistory=owner_history, Name=pset_name, Description=None,
        HasProperties=tuple(properties),
    )
    model.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=deterministic_guid("violation-pset-rel", pset_name, element.GlobalId),
        OwnerHistory=owner_history, Name=None, Description=None,
        RelatedObjects=(element,), RelatingPropertyDefinition=pset,
    )
    return pset


# ---------------------------------------------------------------------------
# marker box
# ---------------------------------------------------------------------------
def add_marker_box(model: ifcopenshell.file, xyz_mm: tuple, storey, rule_id: str,
                   name: str, description: str, style, size_mm: float = 500.0,
                   guid_seed: str = ""):
    """Drop a coloured cube at a world location, as an IfcBuildingElementProxy.

    A proxy rather than an IfcAnnotation on purpose: Revit imports proxies as
    Generic Models (visible, selectable, schedulable) and routinely discards
    IfcAnnotation entirely.

    `xyz_mm` is in project world coordinates. The box is centred on it in
    plan and sits with its base at that Z.
    """
    scale = length_unit_scale(model)
    owner_history = any_owner_history(model)
    half = mm(model, size_mm / 2.0, scale)

    profile = model.create_entity(
        "IfcRectangleProfileDef", ProfileType="AREA", ProfileName=None,
        Position=model.create_entity(
            "IfcAxis2Placement2D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0)),
            RefDirection=None,
        ),
        XDim=half * 2, YDim=half * 2,
    )
    solid = model.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=profile,
        Position=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
            Axis=None, RefDirection=None,
        ),
        ExtrudedDirection=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=half * 2,
    )
    shape = model.create_entity(
        "IfcShapeRepresentation", ContextOfItems=body_context(model),
        RepresentationIdentifier="Body", RepresentationType="SweptSolid", Items=(solid,),
    )
    product_shape = model.create_entity(
        "IfcProductDefinitionShape", Name=None, Description=None, Representations=(shape,)
    )

    x_mm, y_mm, z_mm = xyz_mm
    placement = model.create_entity(
        "IfcLocalPlacement", PlacementRelTo=None,
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(
                float(mm(model, x_mm, scale)) - half,
                float(mm(model, y_mm, scale)) - half,
                float(mm(model, z_mm, scale)),
            )),
            Axis=None, RefDirection=None,
        ),
    )

    proxy = model.create_entity(
        "IfcBuildingElementProxy",
        GlobalId=deterministic_guid("violation-marker", rule_id, name, guid_seed,
                                    f"{x_mm:.3f},{y_mm:.3f},{z_mm:.3f}"),
        OwnerHistory=owner_history, Name=name, Description=description,
        ObjectType="ViolationMarker", ObjectPlacement=placement, Representation=product_shape,
    )
    # IFC4 dropped IfcBuildingElementProxy.CompositionType; IFC2X3 requires it.
    if model.schema.upper().startswith("IFC2X3"):
        try:
            proxy.CompositionType = "ELEMENT"
        except Exception:
            pass

    if storey is not None:
        model.create_entity(
            "IfcRelContainedInSpatialStructure",
            GlobalId=deterministic_guid("violation-marker-rel", rule_id, name, guid_seed),
            OwnerHistory=owner_history, Name=None, Description=None,
            RelatedElements=(proxy,), RelatingStructure=storey,
        )
    model.create_entity("IfcStyledItem", Item=solid, Styles=(style,), Name=None)
    return proxy


# ---------------------------------------------------------------------------
# description, for the report
# ---------------------------------------------------------------------------
def describe_element(model: ifcopenshell.file, element) -> dict:
    """Everything worth printing about where an element is."""
    storey = storey_of(model, element)
    xyz = global_xyz_mm(model, element)
    info = {
        "ifc_type": element.is_a(),
        "name": element.Name,
        "global_id": element.GlobalId,
        "step_id": element.id(),
        "storey_name": storey.Name if storey is not None else None,
        "storey_elevation_mm": None,
        "world_xyz_mm": xyz,
    }
    if storey is not None and storey.Elevation is not None:
        info["storey_elevation_mm"] = to_mm_value(model, storey.Elevation)
    return info


def to_mm_value(model: ifcopenshell.file, native_value: float) -> float:
    return native_value / length_unit_scale(model)


def describe_missing_element(source_model: ifcopenshell.file, global_id: str) -> dict:
    """Where a now-DELETED element used to be, read from the untouched
    source file. Used for the marker box location of a deletion rule."""
    element = source_model.by_guid(global_id)
    return describe_element(source_model, element)
