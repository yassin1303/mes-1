"""
work_orders.py
==============
Work-order configuration for the Ball Pen MES: the part catalog (which
variants exist for each of the four components), the WorkOrder record
itself (name, quantity, order number, chosen parts), and persistence for
saved part combinations ("recipes") the user can re-apply to new orders.

Saved combinations are stored alongside this file in wo_presets.json so
they survive restarts of the HMI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List

COMPONENT_NAMES: tuple = ("barrel", "tip", "cartridge", "cap")

# Base defect rate for each component before the variant multiplier is applied.
BASE_DEFECT_RATE: Dict[str, float] = {
    "barrel":    0.05,
    "tip":       0.10,
    "cartridge": 0.05,
    "cap":       0.05,
}

# Variant catalog: component -> { variant label -> defect-rate multiplier }.
# A multiplier below 1.0 means that variant is made to a tighter tolerance
# (e.g. a premium tip is rejected less often than a regular one).
PART_CATALOG: Dict[str, Dict[str, float]] = {
    "barrel": {
        "Standard Barrel": 1.0,
        "Premium Barrel":  0.5,
        "Golden Barrel":   0.3,
    },
    "tip": {
        "Regular Tip": 1.0,
        "Premium Tip": 0.2,
        "Golden Tip":  0.1,
    },
    "cartridge": {
        "Standard Cartridge":    1.0,
        "Transparent Cartridge": 0.8,
        "Golden Cartridge":      0.5,
    },
    "cap": {
        "Standard Cap": 1.0,
        "Golden Cap":   0.3,
    },
}

DEFAULT_PARTS: Dict[str, str] = {
    "barrel":    "Standard Barrel",
    "tip":       "Regular Tip",
    "cartridge": "Standard Cartridge",
    "cap":       "Standard Cap",
}

PRESETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wo_presets.json")

# Shipped with the app so the dropdown isn't empty on a first run.
_BUILTIN_PRESETS: Dict[str, Dict[str, str]] = {
    "Regular Pen": dict(DEFAULT_PARTS),
    "Premium Pen": {
        "barrel":    "Premium Barrel",
        "tip":       "Premium Tip",
        "cartridge": "Transparent Cartridge",
        "cap":       "Golden Cap",
    },
    "Golden Pen": {
        "barrel":    "Golden Barrel",
        "tip":       "Golden Tip",
        "cartridge": "Golden Cartridge",
        "cap":       "Golden Cap",
    },
}


@dataclass
class WorkOrder:
    """One configured work order: what to build, how many, and under what name."""

    order_number: int
    name:         str
    quantity:     int
    parts:        Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PARTS))

    def part(self, component: str) -> str:
        return self.parts.get(component, DEFAULT_PARTS[component])

    def defect_multiplier(self, component: str) -> float:
        variant = self.part(component)
        return PART_CATALOG[component].get(variant, 1.0)

    def multipliers(self) -> Dict[str, float]:
        return {c: self.defect_multiplier(c) for c in COMPONENT_NAMES}

    def parts_summary(self) -> str:
        return "  |  ".join(f"{c.capitalize()}: {self.part(c)}" for c in COMPONENT_NAMES)


# ---------------------------------------------------------------------------
# Saved combinations ("recipes") — persisted to disk as JSON
# ---------------------------------------------------------------------------

def load_presets() -> Dict[str, Dict[str, str]]:
    """Built-in recipes, overlaid with whatever the user has saved to disk."""
    presets = dict(_BUILTIN_PRESETS)
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                presets.update(data)
        except (OSError, json.JSONDecodeError):
            pass
    return presets


def save_preset(name: str, parts: Dict[str, str]) -> None:
    presets = load_presets()
    presets[name] = {c: parts.get(c, DEFAULT_PARTS[c]) for c in COMPONENT_NAMES}
    with open(PRESETS_FILE, "w", encoding="utf-8") as fh:
        json.dump(presets, fh, indent=2)


def delete_preset(name: str) -> None:
    presets = load_presets()
    if name in presets:
        del presets[name]
        with open(PRESETS_FILE, "w", encoding="utf-8") as fh:
            json.dump(presets, fh, indent=2)


def default_work_orders() -> List[WorkOrder]:
    """Starting queue shown the first time the HMI is launched."""
    presets = load_presets()
    return [
        WorkOrder(1, "Regular Pen", 10, dict(presets.get("Regular Pen", DEFAULT_PARTS))),
        WorkOrder(2, "Premium Pen", 10, dict(presets.get("Premium Pen", DEFAULT_PARTS))),
        WorkOrder(3, "Golden Pen",  10, dict(presets.get("Golden Pen",  DEFAULT_PARTS))),
    ]
