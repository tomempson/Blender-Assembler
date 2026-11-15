# SPDX-License-Identifier: GPL-3.0-or-later
"""User selections and pure-Python helpers for accessory pickers."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .model import Accessory, TruckDefinition

SELECTION_VERSION = 1


def filtered_slot_names(slot_names, query: str = "") -> list[str]:
    """Filter displayed slot names using the UI's case-insensitive substring rule."""
    needle = query.casefold().strip()
    return [name for name in slot_names if not needle or needle in name.casefold()]


def options_for_slot(defn: TruckDefinition, slot_name: str, query: str = "") -> list[Accessory]:
    """Return choices from one slot, optionally matching unit or display names."""
    slot = defn.slots.get(slot_name)
    if slot is None:
        return []
    needle = query.casefold().strip()
    if not needle:
        return list(slot.accessories)
    return [acc for acc in slot.accessories
            if needle in acc.unit_name.casefold() or needle in acc.display_name.casefold()]


@dataclass
class Selection:
    truck: str
    chassis: str | None = None
    cabin: str | None = None
    interior: str | None = None
    accessories: dict[str, str] = field(default_factory=dict)   # slot -> unit name
    rhd: bool = False

    def to_json(self) -> str:
        return json.dumps({"version": SELECTION_VERSION, **asdict(self)}, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> Selection:
        if not isinstance(data, dict) or not isinstance(data.get("truck"), str):
            raise ValueError("selection must be a JSON object with a 'truck' string")
        acc = data.get("accessories") or {}
        if not isinstance(acc, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in acc.items()):
            raise ValueError("selection 'accessories' must map slot names to unit names")
        for key in ("chassis", "cabin", "interior"):
            if data.get(key) is not None and not isinstance(data[key], str):
                raise ValueError(f"selection '{key}' must be a unit name or null")
        return cls(truck=data["truck"], chassis=data.get("chassis"), cabin=data.get("cabin"),
                   interior=data.get("interior"), accessories=dict(acc), rhd=bool(data.get("rhd", False)))

    @classmethod
    def load(cls, path: Path) -> Selection:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
