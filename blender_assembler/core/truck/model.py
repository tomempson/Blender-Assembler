# SPDX-License-Identifier: GPL-3.0-or-later
"""Data models for parsed truck definitions."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..sii.parser import Unit


@dataclass
class ModelRefs:
    """Game paths of the models an accessory uses, e.g. "/vehicle/truck/x/acc/bar.pmd".

    Chassis and cabin: `model` -> exterior. Interior: `model` -> interior, `ext_model` -> exterior.
    Addons: exterior_model / interior_model and their _uk variants. `collision` and `coll` -> collision.
    """

    exterior: str | None = None
    interior: str | None = None
    exterior_uk: str | None = None      # RHD; fall back to exterior if None
    interior_uk: str | None = None
    collision: str | None = None

    def is_empty(self) -> bool:
        return not any((self.exterior, self.interior, self.exterior_uk, self.interior_uk))


@dataclass
class UnitRef:
    """A unit name from suitable_for[] / conflict_with[].

    References may contain `*` wildcards, such as `*.scania.s_2016.r_horn`.
    `*` is the only wildcard character; it matches any run of characters.
    """

    name: str
    # resolved:    exact name exists (accessories, or indexed engine/transmission names),
    #              or the pattern matches at least one of them
    # not_indexed: names a paint job ("*.paint_job"); paint_job/ is deliberately not indexed
    # unresolved: points at nothing and is ignored when evaluating validity.
    status: Literal["unchecked", "resolved", "not_indexed", "unresolved"] = "unchecked"

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"

    @property
    def is_pattern(self) -> bool:
        return "*" in self.name


@dataclass
class DefPathRef:
    """A def file path from defaults[] / overrides[], e.g. "/def/vehicle/truck/x/cabin/normal.sii"."""

    path: str
    status: Literal["unchecked", "resolved", "skipped_folder", "not_scanned", "missing",
                    "outside_root"] = "unchecked"   # outside_root: unsafe path, never touched on disk
    units: list[str] = field(default_factory=list)     # unit names defined in that file, when resolved


@dataclass
class SlotRef:
    """A slot name from require[] (data.sii)."""

    name: str
    known: bool = False                 # True if the truck has a slot folder of that name


@dataclass
class Fallback:
    """One data.sii fallback[] entry: "<folder>|<file>", e.g. "accessory|steering_w/standard.sii"."""

    folder: str
    file: str
    def_path: str                       # resolved game path of the file


@dataclass
class Accessory:
    unit_name: str
    class_name: str
    slot: str                           # folder name under accessory/ (exactly as found), or chassis/cabin/interior
    display_name: str                   # `name` attribute if present, else unit_name
    models: ModelRefs
    icon: str | None = None             # SCS UI accessory icon, relative to material/ui/accessory/
    look: str | None = None             # stored exactly as written; compare with names.same_name
    variant: str | None = None
    variant_uk: str | None = None
    ext_look: str | None = None         # interiors only
    ext_variant: str | None = None      # interiors only
    suitable_for: list[UnitRef] = field(default_factory=list)
    conflict_with: list[UnitRef] = field(default_factory=list)
    defaults: list[DefPathRef] = field(default_factory=list)
    overrides: list[DefPathRef] = field(default_factory=list)
    part_type: str | None = None
    def_path: str = ""                  # game path of the defining file
    source: Path | None = None
    raw: Unit | None = None


@dataclass
class Slot:
    name: str                           # Exact directory entry name.
    accessories: list[Accessory]
    legacy: bool = False                # True if found outside accessory/


@dataclass
class TruckDefinition:
    brand_model: str
    root: Path                          # <defs_root>/def/vehicle/truck/<brand_model>
    defs_root: Path                     # folder that contains def/
    truck_data: Unit | None
    requires: list[SlotRef]
    fallbacks: list[Fallback]
    chassis: list[Accessory]
    cabins: list[Accessory]
    interiors: list[Accessory]
    slots: dict[str, Slot]
    warnings: list[str]
    stats: dict[str, int] = field(default_factory=dict)
    # Unit names indexed for reference resolution only (engine/, transmission/): name -> folder
    indexed_names: dict[str, str] = field(default_factory=dict)

    def all_accessories(self) -> list[Accessory]:
        out = list(self.chassis) + list(self.cabins) + list(self.interiors)
        for slot in self.slots.values():
            out.extend(slot.accessories)
        return out
