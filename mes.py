"""
mes.py
======
MES data layer — tracks production targets, live counters, and genealogy
/ traceability records for each configured work order.

Used by mes_hmi.py (production threads write via record_*; UI reads for display).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from prod import BallPen
from work_orders import WorkOrder


class LineStatus(Enum):
    IDLE    = "■ Idle"
    RUNNING = "● Running"
    DONE    = "✓ Done"
    STOPPED = "■ Stopped"


@dataclass
class LineData:
    order_number: int        = 0
    name:         str        = ""
    target:       int        = 0
    produced:     int        = 0
    rejected:     int        = 0
    status:       LineStatus = LineStatus.IDLE
    parts:        Dict[str, str] = field(default_factory=dict)

    def yield_pct(self) -> float:
        total = self.produced + self.rejected
        return round(self.produced / total * 100, 1) if total else 0.0

    def target_met(self) -> bool:
        return self.target > 0 and self.produced >= self.target


class MES:
    """Central MES state — written from production threads, read from UI thread."""

    def __init__(self) -> None:
        self.lines: Dict[int, LineData] = {}
        self.genealogy: List[BallPen] = []

    # ── Configuration ─────────────────────────────────────────────────────────

    def configure(self, work_orders: List[WorkOrder]) -> None:
        """(Re)configure the set of work orders tracked by the MES."""
        self.lines = {
            wo.order_number: LineData(
                order_number=wo.order_number, name=wo.name,
                target=wo.quantity, parts=dict(wo.parts),
            )
            for wo in work_orders
        }

    def set_target(self, order_number: int, target: int) -> None:
        self.lines[order_number].target = target

    def reset_line(self, order_number: int) -> None:
        d = self.lines[order_number]
        d.produced = 0
        d.rejected = 0
        d.status   = LineStatus.IDLE

    # ── Called from production threads ────────────────────────────────────────

    def record_produced(self, order_number: int) -> bool:
        """Increment produced count.  Returns True if target is now met."""
        d = self.lines[order_number]
        d.produced += 1
        if d.target_met():
            d.status = LineStatus.DONE
            return True
        return False

    def record_rejected(self, order_number: int) -> None:
        self.lines[order_number].rejected += 1

    def set_status(self, order_number: int, status: LineStatus) -> None:
        self.lines[order_number].status = status

    def record_genealogy(self, pen: BallPen) -> None:
        """Record a shipped pen and its four traced components."""
        self.genealogy.append(pen)

    # ── Convenience reads ─────────────────────────────────────────────────────

    def data(self, order_number: int) -> LineData:
        return self.lines[order_number]
