"""
Fixed, project-wide constants: the violation colour table and the LLM
defaults.

The colour table is the contract between an emitted script, the .txt report
it writes, and whatever you are looking at the file in. A rule ALWAYS gets
the same colour, so once you have seen one report you can read any of them.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "qwen/qwen-2.5-coder-32b-instruct"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def model_name() -> str:
    return os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL


# ---------------------------------------------------------------------------
# violation colours
# ---------------------------------------------------------------------------
class Colour:
    """One entry in the violation colour table."""

    __slots__ = ("name", "hex", "rgb")

    def __init__(self, name: str, hex_code: str):
        self.name = name
        self.hex = hex_code.upper()
        h = hex_code.lstrip("#")
        self.rgb = tuple(round(int(h[i:i + 2], 16) / 255.0, 4) for i in (0, 2, 4))

    def __repr__(self) -> str:
        return f"Colour({self.name!r}, {self.hex!r})"

    def describe(self) -> str:
        r, g, b = self.rgb
        return f"{self.name} {self.hex} (rgb {r}, {g}, {b})"


# A distinguishable-by-eye palette; one fixed colour per rule id.
COLOURS: dict[str, Colour] = {
    "A1": Colour("RED", "#E6194B"),
    "A2": Colour("ORANGE", "#F58231"),
    "A3": Colour("YELLOW", "#FFE119"),
    "A4": Colour("GREEN", "#3CB44B"),
    "A5": Colour("BLUE", "#0082C8"),
    "S1": Colour("PURPLE", "#911EB4"),
    "S2": Colour("CYAN", "#46F0F0"),
    "S3": Colour("MAGENTA", "#F032E6"),
    "S4": Colour("BROWN", "#AA6E28"),
    "S5": Colour("MAROON", "#800000"),
}

# Any rule id outside the table above (i.e. a newly synthesized one) cycles
# through these, so a new rule still gets a stable, documented colour.
EXTRA_COLOURS: list[Colour] = [
    Colour("LIME", "#D2F53C"),
    Colour("TEAL", "#008080"),
    Colour("PINK", "#FABEBE"),
    Colour("OLIVE", "#808000"),
    Colour("NAVY", "#000080"),
    Colour("CORAL", "#FF7F50"),
]


def colour_for(rule_id: str) -> Colour:
    """Stable colour for any rule id, known or newly synthesized."""
    known = COLOURS.get(rule_id.upper())
    if known is not None:
        return known
    # Deterministic pick from the overflow palette  - same rule id always
    # lands on the same colour, on every machine.
    import hashlib

    digest = hashlib.sha256(rule_id.upper().encode("utf-8")).digest()
    return EXTRA_COLOURS[digest[0] % len(EXTRA_COLOURS)]


def colour_legend() -> str:
    lines = ["Fixed violation colour table:"]
    for rule_id, colour in COLOURS.items():
        lines.append(f"  {rule_id:<4} {colour.name:<8} {colour.hex}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# how the marker/tagging is surfaced in a viewer
# ---------------------------------------------------------------------------
# Prefixed onto the violating element's Name. IFC viewers and Revit both show
# element names, so this is the most portable "find it" handle there is.
NAME_TAG_TEMPLATE = "[!{rule_id} VIOLATION!] "

# Property set attached to every violating element. Revit surfaces IFC
# property sets as element parameters, so these are filterable/schedulable
# even when Revit ignores the IfcSurfaceStyle colour.
VIOLATION_PSET = "Pset_ViolationMarker"

# Edge length of the marker box dropped at the original location of a
# DELETED element (rules that remove geometry leave nothing to colour).
MARKER_SIZE_MM = 500.0
