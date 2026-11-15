# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse truck dealer configurations into selections.

Dealer configurations under `/def/vehicle/truck_dealer/<brand>/` describe complete in-game trucks:

    vehicle : .truck { accessories[]: .data  accessories[]: .chassis ... }
    vehicle_accessory       : .chassis { data_path: "/def/vehicle/truck/<bm>/chassis/4x2.sii" }
    vehicle_addon_accessory : .mirror  { data_path: "/def/vehicle/truck/<bm>/accessory/mirror/painted.sii" }
    vehicle_wheel_accessory : .f_tire  { offset: 0  data_path: "/def/vehicle/f_tire/1.sii" }
    vehicle_paint_job_accessory : .paint { ...colours...  data_path: ".../paint_job/color5m.sii" }

Each data_path names a def file; the scanner already knows which accessory each file defines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..sii.parser import parse_file
from .model import Accessory, TruckDefinition
from .selection import Selection

_TRUCK_PATH = re.compile(r"^/def/vehicle/truck/([^/]+)/")

# Dealer unit classes that are not addons, with the reason they are not assembled.
_SKIP_CLASSES = {
    "vehicle_wheel_accessory": "wheel accessories are not assembled",
    "vehicle_paint_job_accessory": "paint jobs are not modelled in v1 (no paint_job indexing)",
}
_NOT_MODELLED_FOLDERS = {
    "engine": "engines have no model in v1",
    "transmission": "transmissions have no model in v1",
    "head_light": "head lights have no model in v1",
}


@dataclass
class DealerEntry:
    local: str                  # ".chassis"
    class_name: str
    data_path: str | None
    attrs: dict


@dataclass
class DealerConfig:
    path: Path
    entries: list[DealerEntry]
    brand_model: str | None
    problems: list[str] = field(default_factory=list)


@dataclass
class DealerSkip:
    local: str
    class_name: str
    data_path: str | None
    reason: str


@dataclass
class DealerSelection:
    selection: Selection
    skipped: list[DealerSkip]
    notes: list[str]


def parse_dealer_file(path: Path | str, defs_root: Path | str | None = None) -> DealerConfig:
    """Parse one dealer config. Raises SiiError for unreadable files."""
    path = Path(path)
    sii = parse_file(path, defs_root)
    problems = [f"[sii] {w}" for w in sii.warnings]
    vehicles = [u for u in sii.units if u.class_name == "vehicle"]
    if not vehicles:
        return DealerConfig(path, [], None, problems + [f"{path.name}: no 'vehicle' unit"])
    by_name = {u.name: u for u in sii.units}
    entries: list[DealerEntry] = []
    for ref in vehicles[0].get_list("accessories"):
        unit = by_name.get(str(ref))
        if unit is None:
            problems.append(f"accessories[] names {ref!s}, which has no unit in {path.name}")
            continue
        dp = unit.attrs.get("data_path")
        entries.append(DealerEntry(unit.name, unit.class_name, str(dp) if dp is not None else None,
                                   dict(unit.attrs)))
    brand = None
    for e in entries:
        m = _TRUCK_PATH.match(e.data_path or "")
        if m:
            brand = m.group(1)
            break
    return DealerConfig(path, entries, brand, problems)


def dealer_config_matches_truck(path: Path | str, brand_model: str) -> bool:
    """Match a dealer filename to its truck model, not just its brand directory.

    Dealer filenames vary in whether the model's internal underscores are retained
    (for example `scania_s2016_...` versus `volvo_fh16_2012_...`).
    """
    brand, dot, model = brand_model.partition(".")
    if not dot or not brand or not model:
        return False
    stem = Path(path).stem.casefold()
    prefixes = {f"{brand}_{model}", f"{brand}_{model.replace('_', '')}"}
    return any(stem == prefix.casefold() or stem.startswith(prefix.casefold() + "_")
               for prefix in prefixes)


def _kind(entry: DealerEntry) -> str:
    """chassis / cabin / interior / addon / truck_data / other, from the def file's folder."""
    m = re.match(r"^/def/vehicle/truck/[^/]+/(.*)$", entry.data_path or "")
    if not m:
        return "other"
    rest = m.group(1)
    if rest == "data.sii":
        return "truck_data"
    folder = rest.split("/", 1)[0]
    if folder in ("chassis", "cabin", "interior"):
        return folder
    if folder in _NOT_MODELLED_FOLDERS or folder == "paint_job":
        return folder
    return "addon"      # accessory/<slot>/x.sii or a legacy folder such as badge/x.sii


def selection_from_dealer(cfg: DealerConfig, defn: TruckDefinition) -> DealerSelection:
    """Turn a dealer config into a Selection for `defn`. Entries that are not assembled are returned
    in `skipped` with the reason; nothing is silently dropped."""
    by_def_path: dict[str, list[Accessory]] = {}
    for acc in defn.all_accessories():
        by_def_path.setdefault(acc.def_path, []).append(acc)

    sel = Selection(defn.brand_model)
    skipped: list[DealerSkip] = []
    notes: list[str] = list(cfg.problems)

    def skip(e: DealerEntry, reason: str) -> None:
        skipped.append(DealerSkip(e.local, e.class_name, e.data_path, reason))

    for e in cfg.entries:
        if e.class_name in _SKIP_CLASSES:
            skip(e, _SKIP_CLASSES[e.class_name])
            continue
        kind = _kind(e)
        if kind == "truck_data":
            continue            # .data: the truck's own data.sii, not an accessory
        if kind in _NOT_MODELLED_FOLDERS:
            skip(e, _NOT_MODELLED_FOLDERS[kind])
            continue
        if kind == "paint_job":
            skip(e, "paint jobs are not modelled in v1 (no paint_job indexing)")
            continue
        if kind == "other" or e.data_path is None:
            skip(e, f"data_path {e.data_path!r} is not a def file of {defn.brand_model}")
            continue
        if not e.data_path.startswith(f"/def/vehicle/truck/{defn.brand_model}/"):
            skip(e, f"belongs to another truck: {e.data_path}")
            continue
        accs = by_def_path.get(e.data_path)
        if not accs:
            skip(e, f"def file not scanned or not found: {e.data_path}")
            continue
        acc = accs[0]
        if len(accs) > 1:
            notes.append(f"{e.data_path} defines {len(accs)} accessory units; using {acc.unit_name}")
        if kind in ("chassis", "cabin", "interior"):
            current = getattr(sel, kind)
            if current is not None:
                skip(e, f"second {kind} in the config; kept {current}")
                continue
            setattr(sel, kind, acc.unit_name)
        else:
            if acc.slot in sel.accessories:
                skip(e, f"slot '{acc.slot}' already holds {sel.accessories[acc.slot]}")
                continue
            sel.accessories[acc.slot] = acc.unit_name
    if sel.chassis is None:
        notes.append("the dealer config has no chassis")
    return DealerSelection(sel, skipped, notes)
