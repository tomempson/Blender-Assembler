# SPDX-License-Identifier: GPL-3.0-or-later
"""Find model locators under an imported SCS root."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import bpy

from ..core.pim import read_pim
from ..core.truck.locator_index import LocatorEntry
from .scs_bridge import hookup_of, is_model_locator, object_part

_DUP_SUFFIX = re.compile(r"\.\d{3}$")


@dataclass
class FoundLocators:
    slots: list[LocatorEntry]            # model locators that can be slots (payload = Blender object)
    hookups: list[str]                   # names of hookup locators (lights, beacons, ...): not slots
    unmatched: list[str]                 # locator objects whose name is not in the .pim (should be empty)


def true_name(blender_name: str, pim_names: set[str]) -> str | None:
    """The .pim locator name behind a Blender object name. The importer makes names unique with a
    '.001' suffix, so '.NNN' is stripped, but only if that gives a name the .pim really has."""
    if blender_name in pim_names:
        return blender_name
    stripped = _DUP_SUFFIX.sub("", blender_name)
    return stripped if stripped in pim_names else None


def find_model_locators(root: bpy.types.Object, pim_path: Path, owner: str,
                        included_parts: set[str] | None = None,
                        side: str = "exterior") -> FoundLocators:
    """Collect the model locators below `root`, tag each with `obj["eta_locator"]` (its true name),
    and return slot candidates and hookups separately."""
    pim_names = read_pim(pim_path).names()
    slots: list[LocatorEntry] = []
    hookups: list[str] = []
    unmatched: list[str] = []
    for obj in root.children_recursive:
        if not is_model_locator(obj):
            continue
        if included_parts is not None and object_part(obj) not in included_parts:
            continue
        name = true_name(obj.name, pim_names)
        if name is None:
            unmatched.append(obj.name)
            continue
        obj["eta_locator"] = name
        hook = hookup_of(obj)
        if hook:
            hookups.append(name)
            continue
        slots.append(LocatorEntry(name, owner, object_part(obj), side, obj))
    return FoundLocators(slots, hookups, unmatched)
