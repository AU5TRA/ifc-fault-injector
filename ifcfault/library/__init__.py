"""
The rule library  - the "saved apply()" side of this tool.

A rule id resolves to a module implementing the three-function contract in
`contract.py`. When you ask for one of these by id, its source is lifted out
verbatim and inlined into the generated script; no LLM is asked to reinvent
it.

Adding a rule permanently = one new module here plus one line in REGISTRY.
Rules synthesized by the model land in `generated/` and are picked up
automatically on the next run, so a clause only has to be synthesized once.
"""
from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from . import (
    a1_door_width, a2_riser_tread, a3_fire_rating, a4_door_clearance, a5_dead_end,
    s1_span_depth, s2_column_dimension, s3_slab_thickness, s4_floating_column, s5_soft_storey,
)

#: Hand-written, reviewed rules.
BUILTIN: dict[str, ModuleType] = {
    m.RULE_ID: m
    for m in (
        a1_door_width, a2_riser_tread, a3_fire_rating, a4_door_clearance, a5_dead_end,
        s1_span_depth, s2_column_dimension, s3_slab_thickness, s4_floating_column, s5_soft_storey,
    )
}


def _load_generated() -> dict[str, ModuleType]:
    """Rules previously synthesized by the model and kept on disk.

    A generated rule is only reachable here because a human left the file in
    place after it passed validation  - it is not trusted merely because it
    was generated.
    """
    from . import generated

    found: dict[str, ModuleType] = {}
    for info in pkgutil.iter_modules(generated.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{generated.__name__}.{info.name}")
        rule_id = getattr(module, "RULE_ID", None)
        if rule_id:
            found[rule_id] = module
    return found


def registry() -> dict[str, ModuleType]:
    """Every rule available right now: built-in plus previously generated.
    Built-ins win a name collision."""
    combined = dict(_load_generated())
    combined.update(BUILTIN)
    return combined


def get(rule_id: str) -> ModuleType | None:
    return registry().get(rule_id.upper())


def describe_all() -> list[dict]:
    """Listing for `ifcfault rules`, sorted by id."""
    out = []
    for rule_id, module in sorted(registry().items()):
        out.append({
            "rule_id": rule_id,
            "clause": getattr(module, "CLAUSE", ""),
            "domain": getattr(module, "DOMAIN", ""),
            "element": getattr(module, "ELEMENT", ""),
            "source": "built-in" if rule_id in BUILTIN else "generated",
            "module": module.__name__,
        })
    return out
