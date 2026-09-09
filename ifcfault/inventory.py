"""
A small, typed summary of a model  - the only thing about an IFC file the
model is ever shown.

The model never sees raw STEP text. Partly for cost (these files run to
hundreds of megabytes) but mostly because it is the wrong input: a code
model asked to write a rule against pasted STEP lines writes something that
pattern-matches those exact lines. Given counts and a schema instead, it has
to write a rule that queries the model properly.
"""
from __future__ import annotations

from pathlib import Path

import ifcopenshell

INVENTORY_TYPES = (
    "IfcDoor", "IfcWindow", "IfcWall", "IfcWallStandardCase", "IfcBeam", "IfcColumn",
    "IfcSlab", "IfcFooting", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRailing",
    "IfcRoof", "IfcCovering", "IfcSpace", "IfcBuildingStorey", "IfcOpeningElement",
    "IfcRelSpaceBoundary", "IfcBuildingElementProxy", "IfcCurtainWall", "IfcPlate",
    "IfcMember", "IfcPile",
)


def model_inventory(model: ifcopenshell.file, source_path: Path | None = None) -> dict:
    """Schema, units, storeys and entity counts. Nothing geometric, nothing
    that identifies an individual element."""
    counts = {}
    for typename in INVENTORY_TYPES:
        try:
            n = len(model.by_type(typename))
        except Exception:
            continue
        if n:
            counts[typename] = n

    storeys = []
    for storey in model.by_type("IfcBuildingStorey"):
        storeys.append({"name": storey.Name, "elevation": storey.Elevation})
    storeys.sort(key=lambda s: (s["elevation"] is None, s["elevation"]))

    unit_name = "unknown"
    projects = model.by_type("IfcProject")
    if projects:
        for unit in getattr(projects[0].UnitsInContext, "Units", None) or ():
            if unit.is_a("IfcSIUnit") and unit.UnitType == "LENGTHUNIT":
                unit_name = f"{unit.Prefix or ''}{unit.Name}"
                break
            if unit.is_a("IfcConversionBasedUnit") and unit.UnitType == "LENGTHUNIT":
                unit_name = str(unit.Name)
                break

    inventory = {
        "schema": model.schema,
        "length_unit": unit_name,
        "entity_counts": counts,
        "storey_count": len(storeys),
        "storeys": storeys[:40],
        "has_space_boundaries": bool(counts.get("IfcRelSpaceBoundary")),
    }
    if source_path is not None:
        inventory["file_name"] = Path(source_path).name
        inventory["file_size_mb"] = round(Path(source_path).stat().st_size / (1 << 20), 1)
    return inventory
