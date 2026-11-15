# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit-class lists used by the truck scanner.

Update the scanner tests when adding or changing a class.
"""
from __future__ import annotations

# Classes that become accessories when found directly in accessory/<slot>/ or a legacy folder.
ADDON_CLASSES: frozenset[str] = frozenset({
    "accessory_addon_data",
    "accessory_addon_int_data",
    "accessory_addon_patch_data",
    "accessory_addon_painted_data",
    "accessory_addon_int_ui_data",
    "accessory_addon_tank_data",
    "accessory_addon_trailer_cables_data",
    "accessory_horn_addon_data",
})

CHASSIS_CLASS = "accessory_chassis_data"
CABIN_CLASS = "accessory_cabin_data"
INTERIOR_CLASS = "accessory_interior_data"
TRUCK_DATA_CLASS = "accessory_truck_data"

# Classes that are known and deliberately not accessories. Kept as parsed units only.
EXCLUDED_CLASSES: frozenset[str] = frozenset({
    "physics_toy_data",
    "physics_patch_data",
    "trailer_cable_data",
    "glass_pane_data",
    "wiper_data",
    "sound_multiband_eq_node_data",
    "sound_multiband_eq_param_data",
    "sound_reverb_data",
    "sound_three_eq_data",
    "vehicle_lamp_setup",
    "vehicle_lamp_wire_composite",
    "transmission_names",
    "simple_paint_job_data",
    "accessory_paint_job_data",
    "accessory_engine_data",
    "accessory_transmission_data",
    "accessory_head_lights_data",
})

# Truck subfolders that never produce accessories (v1 non-goals or no models we need).
SKIPPED_FOLDERS: frozenset[str] = frozenset({"engine", "transmission", "head_light", "paint_job", "sound"})

# Of those, folders whose unit *names* are indexed so suitable_for/conflict_with can resolve
# (decision 2026-11-11). paint_job/ is deliberately not indexed: ~150k units.
NAME_INDEX_FOLDERS: frozenset[str] = frozenset({"engine", "transmission"})
NAME_INDEX_CLASSES: frozenset[str] = frozenset({"accessory_engine_data", "accessory_transmission_data"})

# Unit-name suffix of references that are known but not indexed.
NOT_INDEXED_SUFFIXES: frozenset[str] = frozenset({"paint_job"})

# Truck subfolders with a fixed meaning; any other subfolder is scanned as a legacy slot.
KNOWN_FOLDERS: frozenset[str] = SKIPPED_FOLDERS | {"chassis", "cabin", "interior", "accessory"}


def is_addon_class(class_name: str) -> bool:
    return class_name in ADDON_CLASSES


def is_unknown_accessory_class(class_name: str) -> bool:
    """An accessory_* class on neither list: included, with a warning, so new DLC classes get noticed."""
    return (class_name.startswith("accessory_") and class_name not in ADDON_CLASSES
            and class_name not in EXCLUDED_CLASSES
            and class_name not in (CHASSIS_CLASS, CABIN_CLASS, INTERIOR_CLASS, TRUCK_DATA_CLASS))
