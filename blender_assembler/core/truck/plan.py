# SPDX-License-Identifier: GPL-3.0-or-later
"""Build an exterior-only assembly plan from a truck definition and selection."""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Accessory, TruckDefinition
from .selection import Selection

INTERIOR_DISABLED = "interior model importing is disabled; only exterior models are assembled"


class PlanError(ValueError):
    """The selection cannot be assembled at all (e.g. no chassis or cabin model)."""


@dataclass
class PlanOptions:
    rhd: bool = False
    strict: bool = True


@dataclass
class PlannedBase:
    role: str                   # "chassis" | "cabin"
    unit_name: str
    model_path: str             # game path, ".pmd"
    look: str | None = None
    variant: str | None = None


@dataclass
class PlannedModel:
    slot: str
    unit_name: str
    model_path: str
    side: str = "exterior"
    look: str | None = None
    variant: str | None = None
    def_path: str = ""


@dataclass
class PlannedSkip:
    slot: str
    unit_name: str
    model_path: str | None
    reason: str


@dataclass
class AssemblyPlan:
    brand_model: str
    base: list[PlannedBase]
    pending: dict[str, list[PlannedModel]]
    skipped: list[PlannedSkip] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    options: PlanOptions = field(default_factory=PlanOptions)

    def model_paths(self) -> list[str]:
        """Unique game model paths the plan needs, base models first."""
        out: dict[str, None] = {}
        for b in self.base:
            out[b.model_path] = None
        for slot in sorted(self.pending):
            for m in self.pending[slot]:
                out[m.model_path] = None
        return list(out)


def _units(defn: TruckDefinition) -> dict[str, Accessory]:
    return {a.unit_name: a for a in defn.all_accessories()}


def build_plan(defn: TruckDefinition, sel: Selection, options: PlanOptions | None = None) -> AssemblyPlan:
    opts = options or PlanOptions()
    units = _units(defn)
    plan = AssemblyPlan(defn.brand_model, [], {}, options=opts)

    def base(role: str, unit: str | None) -> None:
        if unit is None:
            return
        acc = units.get(unit)
        if acc is None or acc.slot != role:
            raise PlanError(f"{role.capitalize()} not found: {unit}.")
        if not acc.models.exterior:
            plan.skipped.append(PlannedSkip(role, unit, None, f"{role} has no model attribute"))
            return
        plan.base.append(PlannedBase(role, unit, acc.models.exterior, acc.look, acc.variant_uk if opts.rhd and
                                     acc.variant_uk else acc.variant))

    base("chassis", sel.chassis)
    base("cabin", sel.cabin)
    if not plan.base:
        raise PlanError("Select a cabin or chassis with a model before assembling.")

    if sel.interior:
        acc = units.get(sel.interior)
        if acc is None or acc.slot != "interior":
            plan.skipped.append(PlannedSkip("interior", sel.interior, None,
                                            f"interior {sel.interior!r} not found in {defn.brand_model}"))
        else:
            for path in (acc.models.interior, acc.models.exterior):
                if path:
                    plan.skipped.append(PlannedSkip("interior", acc.unit_name, path, INTERIOR_DISABLED))
            if not (acc.models.interior or acc.models.exterior):
                plan.skipped.append(PlannedSkip("interior", acc.unit_name, None, "interior has no model"))

    for slot in sorted(sel.accessories):
        unit = sel.accessories[slot]
        acc = units.get(unit)
        if acc is None:
            plan.skipped.append(PlannedSkip(slot, unit, None, f"unit not found in {defn.brand_model}"))
            continue
        if slot not in defn.slots:
            plan.skipped.append(PlannedSkip(slot, unit, None, f"'{slot}' is not a slot of {defn.brand_model}"))
            continue
        m = acc.models
        ext = (m.exterior_uk or m.exterior) if opts.rhd else m.exterior
        intr = (m.interior_uk or m.interior) if opts.rhd else m.interior
        if not ext and not intr:
            plan.skipped.append(PlannedSkip(slot, unit, None, "no model defined (logic-only accessory)"))
            continue
        if ext:
            plan.pending.setdefault(slot, []).append(
                PlannedModel(slot, unit, ext, "exterior", acc.look,
                             acc.variant_uk if opts.rhd and acc.variant_uk else acc.variant, acc.def_path))
        if intr:
            plan.skipped.append(PlannedSkip(slot, unit, intr, INTERIOR_DISABLED))
    return plan
