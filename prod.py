"""
prod.py
=======
Ball-pen production line.  Supports two pen variants that share the same
four components (barrel, tip, cartridge, cap) but differ in tip quality:

  PenType.STANDARD  — regular tip   (higher defect rate: 10 %)
  PenType.PREMIUM   — premium tip   (lower  defect rate:  2 %)

Backward-compatible with hmi.py:
  • ProductionLine.COMPONENT_NAMES is still a class attribute
  • AssemblyStation.process(parts) still works (pen_type defaults to STANDARD)
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Deque, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 1. Domain model
# ---------------------------------------------------------------------------

class Quality(Enum):
    OK        = auto()
    DEFECTIVE = auto()


class PenType(Enum):
    STANDARD = "Standard"   # regular tip
    PREMIUM  = "Premium"    # premium tip


# Both variants use the same four components.
COMPONENT_NAMES: Tuple[str, ...] = ("barrel", "tip", "cartridge", "cap")

# Only the tip defect rate differs between variants.
TIP_DEFECT_RATE: Dict[PenType, float] = {
    PenType.STANDARD: 0.10,
    PenType.PREMIUM:  0.02,
}


@dataclass
class Component:
    name:      str
    serial:    int
    quality:   Quality = Quality.OK
    # Traceability — which part variant this is, which machine made it, and when.
    variant:   str     = ""
    machine:   str     = ""
    timestamp: float   = field(default_factory=time.time)


@dataclass
class BallPen:
    serial:       int
    pen_type:     PenType                = PenType.STANDARD
    components:   Dict[str, Component]   = field(default_factory=dict)
    quality:      Quality                = Quality.OK
    packaged:     bool                   = False
    # Traceability — which work order this pen was built under, and when.
    order_number: int                    = 0
    order_name:   str                    = ""
    timestamp:    float                  = field(default_factory=time.time)

    def is_complete(self) -> bool:
        return all(self.components.get(n) for n in COMPONENT_NAMES)


# ---------------------------------------------------------------------------
# 2. Stations
# ---------------------------------------------------------------------------

class Station(ABC):
    def __init__(self, name: str, defect_rate: float = 0.0):
        self.name        = name
        self.defect_rate = defect_rate
        self.processed   = 0
        self.rejected    = 0

    @abstractmethod
    def process(self, item):
        ...

    def _maybe_defect(self) -> Quality:
        return Quality.DEFECTIVE if random.random() < self.defect_rate else Quality.OK

    def log(self, message: str) -> None:
        print(f"[{self.name:<20}] {message}")


class ComponentMaker(Station):
    def __init__(self, component_name: str, defect_rate: float = 0.05,
                 variant: str = "", machine_id: str = ""):
        super().__init__(name=f"Make {component_name}", defect_rate=defect_rate)
        self.component_name = component_name
        self.variant        = variant
        self.machine_id      = machine_id or f"MKR-{component_name.upper()}-1"
        self._counter = 0

    def process(self, _ignored=None) -> Component:
        self._counter += 1
        self.processed += 1
        part = Component(
            name=self.component_name,
            serial=self._counter,
            quality=self._maybe_defect(),
            variant=self.variant,
            machine=self.machine_id,
            timestamp=time.time(),
        )
        self.log(f"produced {part.name}#{part.serial} -> {part.quality.name}")
        return part


class QualityControl(Station):
    def process(self, item: Component) -> Optional[Component]:
        self.processed += 1
        if item.quality is Quality.DEFECTIVE:
            self.rejected += 1
            self.log(f"REJECT {item.name}#{item.serial}")
            return None
        self.log(f"pass   {item.name}#{item.serial}")
        return item


class AssemblyStation(Station):
    def __init__(self, defect_rate: float = 0.02):
        super().__init__(name="Assembly", defect_rate=defect_rate)
        self._counter = 0

    # pen_type defaults to STANDARD so legacy callers (hmi.py) need no change.
    def process(self, parts: dict, pen_type: PenType = PenType.STANDARD,
                order_number: int = 0, order_name: str = "") -> BallPen:
        self._counter += 1
        self.processed += 1
        pen = BallPen(
            serial=self._counter,
            pen_type=pen_type,
            components=parts,
            quality=self._maybe_defect(),
            order_number=order_number,
            order_name=order_name or pen_type.value,
            timestamp=time.time(),
        )
        self.log(f"assembled {pen.pen_type.value} Pen#{pen.serial} -> {pen.quality.name}")
        return pen


class FinalInspection(Station):
    def process(self, pen: BallPen) -> Optional[BallPen]:
        self.processed += 1
        if not pen.is_complete() or pen.quality is Quality.DEFECTIVE:
            self.rejected += 1
            self.log(f"REJECT {pen.pen_type.value} Pen#{pen.serial}")
            return None
        self.log(f"pass   {pen.pen_type.value} Pen#{pen.serial}")
        return pen


class Packaging(Station):
    def __init__(self):
        super().__init__(name="Packaging")

    def process(self, pen: BallPen) -> BallPen:
        self.processed += 1
        pen.packaged = True
        self.log(f"packaged {pen.pen_type.value} Pen#{pen.serial}")
        return pen


# ---------------------------------------------------------------------------
# 3. Production line
# ---------------------------------------------------------------------------

class ProductionLine:
    """Orchestrates one variant's production stations."""

    # Class-level tuple kept for hmi.py backward compat.
    COMPONENT_NAMES = COMPONENT_NAMES

    def __init__(self, pen_type: PenType = PenType.STANDARD,
                 order_number: int = 0, order_name: str = "",
                 part_variants: Optional[Dict[str, str]] = None,
                 defect_multipliers: Optional[Dict[str, float]] = None):
        self.pen_type     = pen_type
        self.order_number = order_number
        self.order_name   = order_name or pen_type.value

        part_variants      = part_variants or {}
        defect_multipliers = defect_multipliers or {}

        tip_rate = TIP_DEFECT_RATE[pen_type]
        self.makers: Dict[str, ComponentMaker] = {
            n: ComponentMaker(
                n,
                defect_rate=(tip_rate if n == "tip" else 0.05) * defect_multipliers.get(n, 1.0),
                variant=part_variants.get(n, ""),
                machine_id=f"MKR-{n.upper()}-{order_number or 1}",
            )
            for n in self.COMPONENT_NAMES
        }
        self.qcs: Dict[str, QualityControl] = {
            n: QualityControl(name=f"QC {n}") for n in self.COMPONENT_NAMES
        }
        self.bins: Dict[str, Deque[Component]] = {
            n: deque() for n in self.COMPONENT_NAMES
        }
        self.assembly  = AssemblyStation()
        self.final_qc  = FinalInspection(name="Final QC")
        self.packaging = Packaging()
        self.shipped:  List[BallPen] = []

    def _refill_bins(self) -> None:
        for name in self.COMPONENT_NAMES:
            while not self.bins[name]:
                raw = self.makers[name].process()
                inspected = self.qcs[name].process(raw)
                if inspected is not None:
                    self.bins[name].append(inspected)

    def _assemble_one(self) -> Optional[BallPen]:
        parts = {n: self.bins[n].popleft() for n in self.COMPONENT_NAMES}
        pen   = self.assembly.process(parts, self.pen_type, self.order_number, self.order_name)
        pen   = self.final_qc.process(pen)
        if pen is None:
            return None
        return self.packaging.process(pen)

    def run(self, target: int) -> None:
        print(f"\n=== {self.pen_type.value} Pen: target = {target} ===\n")
        while len(self.shipped) < target:
            self._refill_bins()
            pen = self._assemble_one()
            if pen is not None:
                self.shipped.append(pen)
        self._report()

    def _report(self) -> None:
        print(f"\n=== {self.pen_type.value} Pen — Production Report ===")
        stations: List[Station] = [
            *self.makers.values(), *self.qcs.values(),
            self.assembly, self.final_qc, self.packaging,
        ]
        for s in stations:
            print(f"  {s.name:<20} processed={s.processed:<4} rejected={s.rejected}")
        print(f"\n  Shipped: {len(self.shipped)}")


# ---------------------------------------------------------------------------
# 4. Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    random.seed(42)
    ProductionLine(PenType.STANDARD).run(target=5)
    random.seed(42)
    ProductionLine(PenType.PREMIUM).run(target=5)
