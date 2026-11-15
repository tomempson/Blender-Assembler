# SPDX-License-Identifier: GPL-3.0-or-later
"""Evaluate accessory compatibility without Blender dependencies."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Accessory, TruckDefinition
from .selection import Selection


@dataclass(frozen=True)
class Verdict:
    valid: bool
    reasons: tuple[str, ...] = ()


def _matches(pattern: str, name: str) -> bool:
    expr = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(expr, name) is not None


def _resolved_refs(refs):
    # Scanner marks paint jobs and unresolved names separately; both are deliberately
    # ignored for validity while remaining available in the report/tooltips.
    return [ref.name for ref in refs if ref.status == "resolved"]


def _references_any(patterns: list[str], names: set[str]) -> bool:
    return any(_matches(pattern, name) for pattern in patterns for name in names)


def validity(acc: Accessory, selected: set[str], units: dict[str, Accessory]) -> Verdict:
    reasons: list[str] = []
    suitable = _resolved_refs(acc.suitable_for)
    if suitable and not _references_any(suitable, selected):
        reasons.append("needs one of: " + ", ".join(suitable))

    conflicts = _resolved_refs(acc.conflict_with)
    conflict_hits = {name for name in selected if name != acc.unit_name
                     and _references_any(conflicts, {name})}
    for selected_name in selected:
        if selected_name == acc.unit_name:
            continue
        selected_acc = units.get(selected_name)
        if selected_acc and _references_any(_resolved_refs(selected_acc.conflict_with), {acc.unit_name}):
            conflict_hits.add(selected_name)
    if conflict_hits:
        reasons.append("conflicts with: " + ", ".join(sorted(conflict_hits)))
    return Verdict(not reasons, tuple(reasons))


def evaluate_options(defn: TruckDefinition, selection: Selection) -> dict[str, Verdict]:
    units = {acc.unit_name: acc for acc in defn.all_accessories()}
    selected = {name for name in (selection.chassis, selection.cabin, selection.interior) if name}
    selected.update(selection.accessories.values())
    return {name: validity(acc, selected, units) for name, acc in units.items()}


def reset_invalid_accessories(defn: TruckDefinition, selection: Selection) -> tuple[Selection, list[str]]:
    """Return a copied selection with invalid slot choices cleared, plus reset descriptions."""
    from copy import deepcopy
    checked = deepcopy(selection)
    verdicts = evaluate_options(defn, checked)
    resets: list[str] = []
    for slot, unit in list(checked.accessories.items()):
        verdict = verdicts.get(unit, Verdict(False, ("unit is not scanned",)))
        if not verdict.valid:
            resets.append(f"{slot}: {unit} ({'; '.join(verdict.reasons)})")
            checked.accessories.pop(slot, None)
    return checked, resets
