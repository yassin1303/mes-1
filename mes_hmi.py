"""
mes_hmi.py
==========
MES HMI — the single Ball Pen HMI window, with one physical production
line fed by a queue of configurable work orders. Each work order has its
own choice of parts (e.g. a "Golden Pen" order built from a Golden Barrel
/ Golden Tip / Golden Cartridge / Golden Cap, next to a "Regular Pen"
order built from standard parts) and its own quantity target. Orders run
one at a time, in queue order — the line always shows what it's building
now and what's queued up next.

Features:
  - Work Order Setup dialog: choose how many work orders to queue, name
    each one, set its quantity, and pick the parts (barrel / tip /
    cartridge / cap) it is built from. Part combinations can be saved as
    a named recipe (e.g. "Golden Pen") and reloaded into any future order.
  - One animated pipeline + live counters for the single production line,
    plus a "Next Order" preview of what the queue will run next.
  - START / STOP the queue; a finished order auto-advances to the next
    queued one.
  - Shared rejection (NG) log across all work orders.
  - Genealogy & Traceability log: every shipped pen's 4 components
    (barrel, tip, cartridge, cap), each with its part variant, the
    machine that made it, a timestamp, and the work-order number it was
    built under.

Run:
    python mes_hmi.py
"""

from __future__ import annotations

import os, queue, sys, threading, time, tkinter as tk, textwrap
from tkinter import ttk, messagebox, simpledialog, filedialog

sys.path.insert(0, os.path.dirname(__file__))
from prod import ProductionLine, PenType, BallPen
from mes  import MES, LineStatus
from work_orders import (
    WorkOrder, COMPONENT_NAMES, PART_CATALOG, DEFAULT_PARTS,
    load_presets, save_preset, default_work_orders,
)

# ── Colour palette ─────────────────────────────────────────────────────────────
BG     = "#1a2332"
PANEL  = "#243044"
ACCENT = "#00b4d8"
GREEN  = "#2dc653"
RED    = "#e63946"
AMBER  = "#f4a261"
PURPLE = "#9b59b6"
FG     = "#e0e8f0"
FG_DIM = "#7a8fa8"
HEADER = "#0d1b2a"

COMP_COLORS = {
    "barrel":    "#3498db",
    "tip":       "#9b59b6",
    "cartridge": "#2ecc71",
    "cap":       "#e74c3c",
    "pen":       "#f39c12",
}

MAX_ORDERS = 6


def _pdf_literal(value: str) -> bytes:
    encoded = value.encode("cp1252", errors="replace")
    return b"(" + encoded.replace(b"\\", b"\\\\").replace(
        b"(", b"\\("
    ).replace(b")", b"\\)") + b")"


def _build_pdf(lines: list[str]) -> bytes:
    """Create a small paginated text PDF without requiring third-party packages."""
    wrapped = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, width=108, break_long_words=True) or [""])

    page_lines = [wrapped[i:i + 44] for i in range(0, len(wrapped), 44)] or [[]]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # Pages tree, filled after page objects are created.
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
    ]
    page_ids = []
    for page_number, page in enumerate(page_lines, start=1):
        page_id = len(objects) + 1
        content_id = page_id + 1
        page_ids.append(page_id)
        commands = [b"BT /F1 8 Tf 44 750 Td 14 TL"]
        for line in page:
            commands.extend((_pdf_literal(line), b" Tj T*"))
        commands.extend((
            b"ET BT /F1 8 Tf 44 28 Td ",
            _pdf_literal(f"Page {page_number} of {len(page_lines)}"),
            b" Tj ET",
        ))
        stream = b"\n".join(commands)
        objects.extend((
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>").encode("ascii"),
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream + b"\nendstream",
        ))

    kids = b" ".join(f"{page_id} 0 R".encode("ascii") for page_id in page_ids)
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_ids)).encode("ascii") + b" >>"

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(pdf)


# ── Pipeline geometry (mirrors hmi.py) ─────────────────────────────────────────
_CW,  _CH    = 650, 168
_BHW, _BHH   = 30,  13
_LANE_Y = {"barrel": 26, "tip": 68, "cartridge": 110, "cap": 152}
_CTR_Y  = 89
_X_MAKE = 50;  _X_QC = 145;  _X_BIN = 240
_X_ASSM = 345; _X_FQC = 435; _X_PACK = 520; _X_SHIP = 610

STEP_DELAY = 0.40   # seconds per stage (same as hmi.py)

# ── Event types ────────────────────────────────────────────────────────────────
EVT_ASSEMBLED = "assembled"
EVT_REJECTED  = "rejected"
EVT_STAGE     = "stage"
EVT_STATUS    = "status"
EVT_STOPPED   = "stopped"


# ── Instrumented production line ───────────────────────────────────────────────

class InstrumentedLine(ProductionLine):
    """ProductionLine that posts UI events, stops when the target is met."""

    def __init__(self, work_order: WorkOrder, target: int,
                 event_q: queue.Queue, stop_event: threading.Event, mes: MES):
        super().__init__(
            pen_type=PenType.STANDARD,
            order_number=work_order.order_number,
            order_name=work_order.name,
            part_variants=work_order.parts,
            defect_multipliers=work_order.multipliers(),
        )
        self._target = target
        self._q      = event_q
        self._stop   = stop_event
        self._mes    = mes

    # helpers ──────────────────────────────────────────────────────────────────

    def _stage(self, key: str, comp: str) -> None:
        if not self._stop.is_set():
            self._q.put({"type": EVT_STAGE, "order_number": self.order_number,
                         "stage": key, "component": comp})
            time.sleep(STEP_DELAY)

    def _refill_bins(self) -> None:
        for name in self.COMPONENT_NAMES:
            while not self.bins[name]:
                if self._stop.is_set():
                    return
                self._stage(f"make_{name}", name)
                raw = self.makers[name].process()

                self._stage(f"qc_{name}", name)
                inspected = self.qcs[name].process(raw)

                if inspected is None:
                    self._mes.record_rejected(self.order_number)
                    self._q.put({
                        "type":         EVT_REJECTED,
                        "order_number": self.order_number,
                        "order_name":   self.order_name,
                        "item":         f"{name.capitalize()} #{raw.serial}",
                        "station":      f"QC – {name}",
                        "reason":       "Defective component",
                    })
                else:
                    self._stage(f"bin_{name}", name)
                    self.bins[name].append(inspected)

    def _assemble_one(self):
        if self._stop.is_set():
            return None

        self._stage("assembly", "pen")
        parts  = {n: self.bins[n].popleft() for n in self.COMPONENT_NAMES}
        pen    = self.assembly.process(parts, self.pen_type, self.order_number, self.order_name)

        self._stage("final_qc", "pen")
        pen_ok = self.final_qc.process(pen)
        if pen_ok is None:
            self._mes.record_rejected(self.order_number)
            self._q.put({
                "type":         EVT_REJECTED,
                "order_number": self.order_number,
                "order_name":   self.order_name,
                "item":         f"{self.order_name} Pen #{pen.serial}",
                "station":      "Final QC",
                "reason":       "Assembly defect",
            })
            return None

        self._stage("packaging", "pen")
        result = self.packaging.process(pen_ok)
        self._mes.record_genealogy(result)
        target_met = self._mes.record_produced(self.order_number)
        self._q.put({"type": EVT_ASSEMBLED, "order_number": self.order_number, "pen": result})

        self._stage("shipped", "pen")
        return result, target_met

    # main loop ────────────────────────────────────────────────────────────────

    def run_until_done(self) -> None:
        self._mes.set_status(self.order_number, LineStatus.RUNNING)
        self._q.put({"type": EVT_STATUS, "order_number": self.order_number,
                     "msg": LineStatus.RUNNING.value})

        while not self._stop.is_set() and len(self.shipped) < self._target:
            self._refill_bins()
            if self._stop.is_set():
                break
            result = self._assemble_one()
            if result is not None:
                pen, target_met = result
                self.shipped.append(pen)
                if target_met:
                    break   # target reached — exit loop cleanly

        if len(self.shipped) >= self._target:
            final_status = LineStatus.DONE
        else:
            final_status = LineStatus.STOPPED
            self._mes.set_status(self.order_number, final_status)

        self._q.put({"type": EVT_STATUS, "order_number": self.order_number,
                     "msg": f"{final_status.value}  —  {len(self.shipped)} shipped"})
        self._q.put({"type": EVT_STOPPED, "order_number": self.order_number})


# ── The single production line panel ────────────────────────────────────────────

class ProductionLinePanel(tk.LabelFrame):
    """The one physical line: current order, its pipeline/stats, and a
    preview of the next order waiting in the queue."""

    def __init__(self, parent: tk.Widget, on_start, on_stop):
        super().__init__(
            parent,
            text="  PRODUCTION LINE  ",
            bg=BG, fg=ACCENT,
            font=("Courier New", 11, "bold"),
            bd=2, relief="ridge", labelanchor="n",
            padx=8, pady=6,
        )
        self._on_start = on_start
        self._on_stop  = on_stop

        self._active_stage: str | None       = None
        self._stage_items:  dict[str, tuple] = {}

        self._build()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build(self) -> None:
        # Current order header
        cur = tk.Frame(self, bg=BG)
        cur.pack(fill="x")
        tk.Label(cur, text="Current Order:", bg=BG, fg=FG,
                 font=("Courier New", 10, "bold")).pack(side="left")
        self._current_name_var = tk.StringVar(value="— idle —")
        tk.Label(cur, textvariable=self._current_name_var, bg=BG, fg=ACCENT,
                 font=("Courier New", 10, "bold")).pack(side="left", padx=6)
        tk.Label(cur, text="Target:", bg=BG, fg=FG,
                 font=("Courier New", 10)).pack(side="left", padx=(16, 0))
        self._target_val_var = tk.StringVar(value="—")
        tk.Label(cur, textvariable=self._target_val_var, bg=BG, fg=ACCENT,
                 font=("Courier New", 10, "bold")).pack(side="left", padx=6)

        self._current_parts_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._current_parts_var, bg=BG, fg=FG_DIM,
                 font=("Courier New", 8)).pack(fill="x", pady=(0, 6))

        # Pipeline canvas
        self._build_pipeline()

        # Stats bar
        stats = tk.Frame(self, bg=PANEL, padx=10, pady=6)
        stats.pack(fill="x", pady=(4, 0))

        self._status_var   = tk.StringVar(value=LineStatus.IDLE.value)
        self._progress_var = tk.StringVar(value="Produced:  0 / 0")
        self._rejected_var = tk.StringVar(value="Rejected: 0")
        self._yield_var    = tk.StringVar(value="Yield: 0.0 %")

        tk.Label(stats, textvariable=self._status_var,
                 bg=PANEL, fg=AMBER, font=("Courier New", 9, "bold"),
                 width=22, anchor="w").grid(row=0, column=0, padx=(0, 10))

        tk.Label(stats, textvariable=self._progress_var,
                 bg=PANEL, fg=GREEN, font=("Courier New", 9)).grid(row=0, column=1, padx=6)

        tk.Label(stats, textvariable=self._rejected_var,
                 bg=PANEL, fg=RED, font=("Courier New", 9)).grid(row=0, column=2, padx=6)

        tk.Label(stats, textvariable=self._yield_var,
                 bg=PANEL, fg=ACCENT, font=("Courier New", 9)).grid(row=0, column=3, padx=6)

        # Queue start / stop buttons
        btn_row = tk.Frame(self, bg=BG, pady=6)
        btn_row.pack()

        self._btn_start = tk.Button(
            btn_row, text="▶  START",
            bg=GREEN, fg="#000", activebackground="#22a844",
            font=("Courier New", 10, "bold"),
            width=10, relief="flat", cursor="hand2",
            command=self._on_start,
        )
        self._btn_start.pack(side="left", padx=6)

        self._btn_stop = tk.Button(
            btn_row, text="■  STOP",
            bg=RED, fg="#fff", activebackground="#c0303a",
            font=("Courier New", 10, "bold"),
            width=10, relief="flat", cursor="hand2",
            state="disabled",
            command=self._on_stop,
        )
        self._btn_stop.pack(side="left", padx=6)

        # Next order preview
        nxt = tk.Frame(self, bg=PANEL, padx=10, pady=6)
        nxt.pack(fill="x", pady=(8, 0))

        tk.Label(nxt, text="NEXT ORDER:", bg=PANEL, fg=FG_DIM,
                 font=("Courier New", 9, "bold")).grid(row=0, column=0, sticky="w")
        self._next_name_var = tk.StringVar(value="— none queued —")
        tk.Label(nxt, textvariable=self._next_name_var, bg=PANEL, fg=AMBER,
                 font=("Courier New", 9, "bold")).grid(row=0, column=1, padx=8, sticky="w")
        self._next_parts_var = tk.StringVar(value="")
        tk.Label(nxt, textvariable=self._next_parts_var, bg=PANEL, fg=FG_DIM,
                 font=("Courier New", 8)).grid(row=1, column=0, columnspan=2,
                                                sticky="w", pady=(2, 0))

    def _build_pipeline(self) -> None:
        c = tk.Canvas(self, bg=PANEL, width=_CW, height=_CH, highlightthickness=0)
        c.pack()
        self._canvas = c

        abbr = {"barrel": "BAR", "tip": "TIP", "cartridge": "CART", "cap": "CAP"}

        def box(cx, cy, label, key):
            x0, y0 = cx - _BHW, cy - _BHH
            x1, y1 = cx + _BHW, cy + _BHH
            r = c.create_rectangle(x0, y0, x1, y1, fill=PANEL, outline=FG_DIM, width=1)
            t = c.create_text(cx, cy, text=label, fill=FG_DIM,
                              font=("Courier New", 7, "bold"), justify="center")
            self._stage_items[key] = (r, t)

        def arrow(x1, y1, x2, y2):
            c.create_line(x1, y1, x2, y2, fill=FG_DIM, width=1,
                          arrow=tk.LAST, arrowshape=(5, 7, 3))

        for name, y in _LANE_Y.items():
            s = abbr[name]
            box(_X_MAKE, y, f"MAKE\n{s}",  f"make_{name}")
            box(_X_QC,   y, f"QC\n{s}",    f"qc_{name}")
            box(_X_BIN,  y, f"BIN\n{s}",   f"bin_{name}")
            arrow(_X_MAKE + _BHW, y,   _X_QC  - _BHW, y)
            arrow(_X_QC  + _BHW, y,   _X_BIN - _BHW, y)
            arrow(_X_BIN + _BHW, y,   _X_ASSM - _BHW, _CTR_Y)

        box(_X_ASSM, _CTR_Y, "ASSM",       "assembly")
        box(_X_FQC,  _CTR_Y, "FINAL\nQC",  "final_qc")
        box(_X_PACK, _CTR_Y, "PACK",       "packaging")
        box(_X_SHIP, _CTR_Y, "SHIP",       "shipped")

        arrow(_X_ASSM + _BHW, _CTR_Y, _X_FQC  - _BHW, _CTR_Y)
        arrow(_X_FQC  + _BHW, _CTR_Y, _X_PACK - _BHW, _CTR_Y)
        arrow(_X_PACK + _BHW, _CTR_Y, _X_SHIP - _BHW, _CTR_Y)

    # ── Order transitions ──────────────────────────────────────────────────────

    def begin_order(self, work_order: WorkOrder) -> None:
        self._current_name_var.set(f"{work_order.name}   (Order #{work_order.order_number})")
        self._current_parts_var.set(work_order.parts_summary())
        self._target_val_var.set(str(work_order.quantity))
        self.reset_pipeline()
        self._status_var.set(LineStatus.IDLE.value)
        self._progress_var.set(f"Produced:  0 / {work_order.quantity}")
        self._rejected_var.set("Rejected: 0")
        self._yield_var.set("Yield: 0.0 %")
        self.enable_stop(True)

    def show_idle(self) -> None:
        self._current_name_var.set("— idle —")
        self._current_parts_var.set("")
        self._target_val_var.set("—")
        self.reset_pipeline()
        self._status_var.set(LineStatus.IDLE.value)
        self._progress_var.set("Produced:  0 / 0")
        self._rejected_var.set("Rejected: 0")
        self._yield_var.set("Yield: 0.0 %")
        self.enable_stop(False)

    def show_queue_complete(self) -> None:
        self._current_name_var.set("✓ Queue complete")
        self.reset_pipeline()
        self.enable_stop(False)

    def set_next(self, work_order: WorkOrder | None) -> None:
        if work_order is None:
            self._next_name_var.set("— none queued —")
            self._next_parts_var.set("")
        else:
            self._next_name_var.set(f"{work_order.name}   (Order #{work_order.order_number})")
            self._next_parts_var.set(work_order.parts_summary())

    def enable_start(self, on: bool) -> None:
        self._btn_start.config(state="normal" if on else "disabled")

    def enable_stop(self, on: bool) -> None:
        self._btn_stop.config(state="normal" if on else "disabled")

    # ── Stage highlighting ─────────────────────────────────────────────────────

    def highlight_stage(self, stage: str, component: str) -> None:
        if self._active_stage and self._active_stage in self._stage_items:
            r, t = self._stage_items[self._active_stage]
            self._canvas.itemconfig(r, fill=PANEL, outline=FG_DIM)
            self._canvas.itemconfig(t, fill=FG_DIM)
        self._active_stage = stage
        if stage not in self._stage_items:
            return
        color = COMP_COLORS.get(component, ACCENT)
        r, t = self._stage_items[stage]
        self._canvas.itemconfig(r, fill=color, outline=color)
        self._canvas.itemconfig(t, fill="#111111")

    def reset_pipeline(self) -> None:
        for r, t in self._stage_items.values():
            self._canvas.itemconfig(r, fill=PANEL, outline=FG_DIM)
            self._canvas.itemconfig(t, fill=FG_DIM)
        self._active_stage = None

    # ── Live display update ────────────────────────────────────────────────────

    def refresh(self, line_data) -> None:
        self._status_var.set(line_data.status.value)
        self._progress_var.set(f"Produced:  {line_data.produced} / {line_data.target}")
        self._rejected_var.set(f"Rejected: {line_data.rejected}")
        self._yield_var.set(f"Yield: {line_data.yield_pct()} %")

    def set_status_text(self, msg: str) -> None:
        self._status_var.set(msg)


# ── Work-order configuration row / dialog ───────────────────────────────────────

class OrderRow:
    """One editable row inside the Work Order Setup dialog."""

    def __init__(self, parent: tk.Widget, index: int, work_order: WorkOrder,
                 preset_names: list[str], on_preset_saved):
        self._on_preset_saved = on_preset_saved

        self.frame = tk.Frame(parent, bg=PANEL, pady=4)
        self.frame.pack(fill="x", pady=2)

        tk.Label(self.frame, text=f"#{index}", bg=PANEL, fg=FG_DIM, width=3,
                 font=("Courier New", 9, "bold")).grid(row=0, column=0, padx=3)

        self.name_var = tk.StringVar(value=work_order.name)
        tk.Entry(self.frame, textvariable=self.name_var, width=14,
                 bg=BG, fg=FG, insertbackground=FG, relief="flat",
                 font=("Courier New", 9)).grid(row=0, column=1, padx=3)

        self.qty_var = tk.StringVar(value=str(work_order.quantity))
        tk.Entry(self.frame, textvariable=self.qty_var, width=5, justify="center",
                 bg=BG, fg=FG, insertbackground=FG, relief="flat",
                 font=("Courier New", 9)).grid(row=0, column=2, padx=3)

        self.part_vars: dict[str, tk.StringVar] = {}
        col = 3
        for comp in COMPONENT_NAMES:
            var = tk.StringVar(value=work_order.part(comp))
            ttk.Combobox(self.frame, textvariable=var, width=17, state="readonly",
                         values=list(PART_CATALOG[comp].keys()),
                         font=("Courier New", 9)).grid(row=0, column=col, padx=2)
            self.part_vars[comp] = var
            col += 1

        self.preset_var = tk.StringVar()
        self.preset_box = ttk.Combobox(self.frame, textvariable=self.preset_var, width=13,
                                        state="readonly", values=preset_names,
                                        font=("Courier New", 9))
        self.preset_box.grid(row=0, column=col, padx=(8, 2)); col += 1

        tk.Button(self.frame, text="Load", command=self._load_preset,
                   bg=ACCENT, fg="#000", font=("Courier New", 8, "bold"),
                   relief="flat", width=5).grid(row=0, column=col, padx=2); col += 1

        tk.Button(self.frame, text="Save As…", command=self._save_preset,
                   bg=AMBER, fg="#000", font=("Courier New", 8, "bold"),
                   relief="flat", width=8).grid(row=0, column=col, padx=2)

    def _load_preset(self) -> None:
        name = self.preset_var.get()
        if not name:
            return
        parts = load_presets().get(name)
        if not parts:
            return
        for comp, var in self.part_vars.items():
            var.set(parts.get(comp, DEFAULT_PARTS[comp]))

    def _save_preset(self) -> None:
        name = simpledialog.askstring(
            "Save combination",
            "Name for this parts combination\n(e.g. \"Premium Pen\"):",
            parent=self.frame,
        )
        if not name:
            return
        parts = {c: v.get() for c, v in self.part_vars.items()}
        save_preset(name, parts)
        self._on_preset_saved()

    def set_preset_names(self, names: list[str]) -> None:
        self.preset_box.configure(values=names)

    def to_work_order(self, order_number: int) -> WorkOrder:
        name = self.name_var.get().strip() or f"Order {order_number}"
        try:
            qty = int(self.qty_var.get())
        except ValueError:
            qty = 0
        parts = {c: v.get() for c, v in self.part_vars.items()}
        return WorkOrder(order_number, name, qty, parts)

    def destroy(self) -> None:
        self.frame.destroy()


class WorkOrderDialog(tk.Toplevel):
    """Lets the user choose how many work orders to run and configure each."""

    def __init__(self, parent: tk.Widget, work_orders: list[WorkOrder], on_apply):
        super().__init__(parent)
        self.title("Configure Work Orders")
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()
        self._on_apply = on_apply
        self._rows: list[OrderRow] = []

        tk.Label(self, text="WORK ORDER SETUP", bg=BG, fg=ACCENT,
                 font=("Courier New", 12, "bold")).pack(pady=(10, 2))
        tk.Label(self, text="Choose how many work orders to produce, name each one,\n"
                             "set its quantity, and pick the parts it is built from.",
                 bg=BG, fg=FG_DIM, font=("Courier New", 8)).pack(pady=(0, 8))

        count_row = tk.Frame(self, bg=BG)
        count_row.pack(pady=(0, 8))
        tk.Label(count_row, text="Number of work orders:", bg=BG, fg=FG,
                 font=("Courier New", 10)).pack(side="left", padx=4)
        self._count_var = tk.StringVar(value=str(max(1, len(work_orders))))
        tk.Spinbox(count_row, from_=1, to=MAX_ORDERS, width=4,
                   textvariable=self._count_var, bg=PANEL, fg=FG,
                   font=("Courier New", 10), justify="center",
                   command=self._resize_rows).pack(side="left", padx=4)
        tk.Button(count_row, text="Apply Count", command=self._resize_rows,
                  bg=ACCENT, fg="#000", font=("Courier New", 9, "bold"),
                  relief="flat").pack(side="left", padx=8)

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=12)
        for i, (text, w) in enumerate([
            ("#", 3), ("Name", 14), ("Qty", 5), ("Barrel", 17), ("Tip", 17),
            ("Cartridge", 17), ("Cap", 17), ("Preset", 13), ("", 5), ("", 8),
        ]):
            tk.Label(header, text=text, bg=BG, fg=FG_DIM, width=w, anchor="w",
                     font=("Courier New", 8, "bold")).grid(row=0, column=i, padx=2)

        self._rows_frame = tk.Frame(self, bg=BG, padx=12)
        self._rows_frame.pack(fill="both", expand=True)

        for wo in (work_orders or [WorkOrder(1, "Order 1", 10, dict(DEFAULT_PARTS))]):
            self._add_row(wo)

        btn_row = tk.Frame(self, bg=BG, pady=10)
        btn_row.pack()
        tk.Button(btn_row, text="Apply", command=self._apply,
                  bg=GREEN, fg="#000", font=("Courier New", 10, "bold"),
                  width=10, relief="flat").pack(side="left", padx=6)
        tk.Button(btn_row, text="Cancel", command=self.destroy,
                  bg=RED, fg="#fff", font=("Courier New", 10, "bold"),
                  width=10, relief="flat").pack(side="left", padx=6)

    def _preset_names(self) -> list[str]:
        return sorted(load_presets().keys())

    def _add_row(self, work_order: WorkOrder) -> None:
        row = OrderRow(self._rows_frame, len(self._rows) + 1, work_order,
                        self._preset_names(), self._refresh_preset_lists)
        self._rows.append(row)

    def _refresh_preset_lists(self) -> None:
        names = self._preset_names()
        for row in self._rows:
            row.set_preset_names(names)

    def _resize_rows(self) -> None:
        try:
            n = max(1, min(MAX_ORDERS, int(self._count_var.get())))
        except ValueError:
            n = len(self._rows)
        self._count_var.set(str(n))

        while len(self._rows) < n:
            idx = len(self._rows) + 1
            self._add_row(WorkOrder(idx, f"Order {idx}", 10, dict(DEFAULT_PARTS)))
        while len(self._rows) > n:
            self._rows.pop().destroy()

    def _apply(self) -> None:
        work_orders = []
        for i, row in enumerate(self._rows, start=1):
            wo = row.to_work_order(i)
            if wo.quantity <= 0:
                messagebox.showerror("Invalid input",
                                      f"Order #{i} ({wo.name}) needs a positive quantity.",
                                      parent=self)
                return
            work_orders.append(wo)
        self._on_apply(work_orders)
        self.destroy()


# ── Main MES HMI window ────────────────────────────────────────────────────────

class MesHMI(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Ball Pen MES — Manufacturing Execution System")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._mes      = MES()
        self._shared_q: queue.Queue = queue.Queue()
        self._work_orders: list[WorkOrder] = default_work_orders()
        self._mes.configure(self._work_orders)

        self._pending:    list[WorkOrder]       = []
        self._current_wo: WorkOrder | None      = None
        self._stop_event: threading.Event       = threading.Event()
        self._thread:     threading.Thread | None = None

        self._build_ui()
        self._reset_queue()
        self._poll()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self, bg=HEADER, pady=10)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="BALL PEN MES  —  Manufacturing Execution System",
            bg=HEADER, fg=ACCENT,
            font=("Courier New", 14, "bold"),
        ).pack()

        # Work order toolbar
        toolbar = tk.Frame(self, bg=PANEL, pady=8, padx=14)
        toolbar.pack(fill="x")

        tk.Button(toolbar, text="⚙  Configure Work Orders", command=self._open_work_order_dialog,
                  bg=ACCENT, fg="#000", font=("Courier New", 10, "bold"),
                  relief="flat", cursor="hand2").pack(side="left")

        tk.Button(toolbar, text="Export PDF Report", command=self._export_report,
                  bg=GREEN, fg="#000", font=("Courier New", 10, "bold"),
                  relief="flat", cursor="hand2").pack(side="right")

        self._queue_summary_var = tk.StringVar(value="")
        tk.Label(toolbar, textvariable=self._queue_summary_var, bg=PANEL, fg=FG_DIM,
                 font=("Courier New", 9)).pack(side="left", padx=16)

        # The single production line
        body = tk.Frame(self, bg=BG, padx=14, pady=10)
        body.pack(fill="x")
        self._line_panel = ProductionLinePanel(body, self._start_queue, self._stop_queue)
        self._line_panel.pack(fill="x")

        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x")

        # Rejection (NG) log
        self._build_log()

        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x")

        # Genealogy & traceability
        self._build_genealogy()

    def _build_log(self) -> None:
        log_frame = tk.Frame(self, bg=BG, padx=14, pady=8)
        log_frame.pack(fill="both", expand=True)

        tk.Label(
            log_frame, text="NG LOG  —  REJECTIONS",
            bg=BG, fg=FG_DIM, font=("Courier New", 9, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("MES.Treeview",
                        background=PANEL, foreground=FG,
                        fieldbackground=PANEL, rowheight=22,
                        font=("Courier New", 9))
        style.configure("MES.Treeview.Heading",
                        background=HEADER, foreground=ACCENT,
                        font=("Courier New", 9, "bold"), relief="flat")
        style.map("MES.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#000")])

        cols = ("Order #", "Order", "Item", "Station", "Reason")
        wrap = tk.Frame(log_frame, bg=BG)
        wrap.pack(fill="both", expand=True)

        self._tree = ttk.Treeview(
            wrap, columns=cols, show="headings",
            style="MES.Treeview", height=7,
        )
        for col, w in zip(cols, (60, 130, 160, 170, 220)):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=w, anchor="w")

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _build_genealogy(self) -> None:
        gen_frame = tk.Frame(self, bg=BG, padx=14, pady=8)
        gen_frame.pack(fill="both", expand=True)

        tk.Label(
            gen_frame, text="GENEALOGY & TRACEABILITY  —  each pen's 4 traced components",
            bg=BG, fg=FG_DIM, font=("Courier New", 9, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        cols = ("Variant", "Machine", "Timestamp", "Order #")
        wrap = tk.Frame(gen_frame, bg=BG)
        wrap.pack(fill="both", expand=True)

        self._gen_tree = ttk.Treeview(
            wrap, columns=cols, show="tree headings",
            style="MES.Treeview", height=9,
        )
        self._gen_tree.heading("#0", text="Pen / Component")
        self._gen_tree.column("#0", width=230, anchor="w")
        for col, w in zip(cols, (190, 150, 150, 70)):
            self._gen_tree.heading(col, text=col)
            self._gen_tree.column(col, width=w, anchor="w")

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._gen_tree.yview)
        self._gen_tree.configure(yscrollcommand=sb.set)
        self._gen_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        tk.Frame(self, bg=BG, height=8).pack()

    # ── Work order queue ───────────────────────────────────────────────────────

    def _reset_queue(self) -> None:
        self._pending    = list(self._work_orders)
        self._current_wo = None
        self._line_panel.show_idle()
        self._advance_preview()
        self._sync_start_button()
        self._update_queue_summary()

    def _advance_preview(self) -> None:
        self._line_panel.set_next(self._pending[0] if self._pending else None)

    def _update_queue_summary(self) -> None:
        self._queue_summary_var.set(f"Queue: {len(self._pending)} order(s) pending")

    def _sync_start_button(self) -> None:
        self._line_panel.enable_start(self._current_wo is None and bool(self._pending))

    def _open_work_order_dialog(self) -> None:
        if self._current_wo is not None:
            messagebox.showwarning(
                "Order running",
                "Stop the production line before reconfiguring work orders.", parent=self)
            return
        WorkOrderDialog(self, list(self._work_orders), self._apply_work_orders)

    def _apply_work_orders(self, work_orders: list[WorkOrder]) -> None:
        self._work_orders = work_orders
        self._mes.configure(work_orders)
        self._reset_queue()

    def _export_report(self) -> None:
        filename = f"mes_production_report_{time.strftime('%Y%m%d_%H%M%S')}.pdf"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save MES PDF Report",
            defaultextension=".pdf",
            initialfile=filename,
            filetypes=[("PDF files", "*.pdf")],
        )
        if not path:
            return

        report_lines = [
            "BALL PEN MES — PRODUCTION REPORT",
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "PRODUCTION ORDERS",
        ]
        if not self._mes.lines:
            report_lines.append("No production orders configured.")
        for order_number, data in sorted(self._mes.lines.items()):
            report_lines.extend((
                f"Order #{order_number}: {data.name}",
                f"  Status: {data.status.value} | Target: {data.target} | "
                f"Produced: {data.produced} | Rejected: {data.rejected} | "
                f"Yield: {data.yield_pct()}%",
                "  Parts: " + ", ".join(
                    f"{component.capitalize()}: {variant}"
                    for component, variant in data.parts.items()
                ),
            ))

        report_lines.extend(("", "REJECTIONS"))
        rejection_rows = self._tree.get_children("")
        if not rejection_rows:
            report_lines.append("No rejected items.")
        for row_id in rejection_rows:
            values = self._tree.item(row_id, "values")
            report_lines.append(
                f"Order #{values[0]} | {values[1]} | {values[2]} | "
                f"{values[3]} | {values[4]}"
            )

        report_lines.extend(("", "GENEALOGY & TRACEABILITY"))
        if not self._mes.genealogy:
            report_lines.append("No shipped pens recorded.")
        for pen in self._mes.genealogy:
            pen_timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pen.timestamp))
            report_lines.append(
                f"Pen #{pen.serial} | Order #{pen.order_number} ({pen.order_name}) | {pen_timestamp}"
            )
            for component_name in COMPONENT_NAMES:
                component = pen.components.get(component_name)
                if component is None:
                    continue
                component_timestamp = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(component.timestamp)
                )
                report_lines.append(
                    f"  {component_name.capitalize()}: {component.variant or '—'} | "
                    f"Machine: {component.machine} | {component_timestamp}"
                )

        try:
            with open(path, "wb") as report_file:
                report_file.write(_build_pdf(report_lines))
        except OSError as exc:
            messagebox.showerror("Export failed", f"Could not save the PDF report:\n{exc}", parent=self)
            return
        messagebox.showinfo("Report saved", f"PDF report saved to:\n{path}", parent=self)

    def _start_queue(self) -> None:
        if self._current_wo is not None:
            return
        if not self._pending:
            messagebox.showinfo("Queue empty",
                                 "Configure at least one work order first.", parent=self)
            return
        self._launch_next()

    def _launch_next(self) -> None:
        if not self._pending:
            self._current_wo = None
            self._line_panel.show_queue_complete()
            self._sync_start_button()
            self._update_queue_summary()
            return

        wo = self._pending.pop(0)
        self._current_wo = wo
        self._mes.reset_line(wo.order_number)
        self._mes.set_target(wo.order_number, wo.quantity)
        self._stop_event = threading.Event()

        self._line_panel.begin_order(wo)
        self._advance_preview()
        self._line_panel.enable_start(False)
        self._update_queue_summary()

        line = InstrumentedLine(wo, wo.quantity, self._shared_q, self._stop_event, self._mes)
        self._thread = threading.Thread(target=line.run_until_done, daemon=True)
        self._thread.start()

    def _stop_queue(self) -> None:
        self._stop_event.set()
        self._line_panel.enable_stop(False)

    def _on_order_stopped(self, order_number: int) -> None:
        if self._current_wo is None or self._current_wo.order_number != order_number:
            return
        status = self._mes.data(order_number).status
        self._current_wo = None
        self._line_panel.refresh(self._mes.data(order_number))

        if status == LineStatus.DONE:
            self._launch_next()
        else:
            self._sync_start_button()
            self._update_queue_summary()

    # ── Genealogy display ─────────────────────────────────────────────────────

    def _insert_genealogy(self, pen: BallPen) -> None:
        ts = time.strftime("%H:%M:%S", time.localtime(pen.timestamp))
        parent = self._gen_tree.insert(
            "", 0, text=f"Pen #{pen.serial}  —  {pen.order_name}",
            values=("", "ASSEMBLY", ts, pen.order_number), open=True,
        )
        for name in COMPONENT_NAMES:
            comp = pen.components.get(name)
            if comp is None:
                continue
            cts = time.strftime("%H:%M:%S", time.localtime(comp.timestamp))
            self._gen_tree.insert(
                parent, "end", text=f"    {name.capitalize()}",
                values=(comp.variant or "—", comp.machine, cts, pen.order_number),
            )
        # keep the log from growing unbounded during a long run
        top_level = self._gen_tree.get_children("")
        for old in top_level[200:]:
            self._gen_tree.delete(old)

    # ── Event pump (UI thread, every 50 ms) ───────────────────────────────────

    def _poll(self) -> None:
        try:
            while True:
                evt          = self._shared_q.get_nowait()
                t            = evt["type"]
                order_number = evt.get("order_number")
                is_current   = (self._current_wo is not None
                                 and self._current_wo.order_number == order_number)

                if t == EVT_STAGE and is_current:
                    self._line_panel.highlight_stage(evt["stage"], evt["component"])

                elif t == EVT_STATUS and is_current:
                    self._line_panel.set_status_text(evt["msg"])

                elif t == EVT_ASSEMBLED:
                    if is_current:
                        self._line_panel.refresh(self._mes.data(order_number))
                    self._insert_genealogy(evt["pen"])

                elif t == EVT_REJECTED:
                    if is_current:
                        self._line_panel.refresh(self._mes.data(order_number))
                    self._tree.insert("", "end", values=(
                        order_number if order_number is not None else "?",
                        evt.get("order_name", "?"), evt["item"], evt["station"], evt["reason"],
                    ))
                    self._tree.yview_moveto(1.0)

                elif t == EVT_STOPPED:
                    self._on_order_stopped(order_number)

        except queue.Empty:
            pass
        self.after(50, self._poll)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    MesHMI().mainloop()
