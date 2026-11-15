# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless Blender smoke test for add-on registration and core UI properties."""
from __future__ import annotations

from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import blender_assembler as addon  # noqa: E402


def main() -> None:
    if bpy.app.version < (5, 3, 0):
        raise RuntimeError(f"Blender 5.3 or later is required; found {bpy.app.version_string}.")
    if addon.bl_info["name"] != "Blender Assembler":
        raise AssertionError("Unexpected add-on name in bl_info.")
    if addon.__name__ != "blender_assembler":
        raise AssertionError(f"Unexpected installed add-on module id: {addon.__name__!r}.")

    registered = False
    try:
        addon.register()
        registered = True
        if not hasattr(bpy.types.Scene, "eta_state"):
            raise AssertionError("The add-on did not register Scene.eta_state.")
        state = bpy.context.scene.eta_state
        if state.drive_side != "EU" or not state.strict or state.enforce_defaults:
            raise AssertionError("Unexpected default state values.")
        if not hasattr(bpy.ops.eta, "assemble") or not hasattr(bpy.ops.eta, "refresh_trucks"):
            raise AssertionError("Expected Blender Assembler operators were not registered.")
        print("BLENDER_SMOKE_PASS: register, scene properties, operator registration")
    finally:
        if registered:
            addon.unregister()
        if hasattr(bpy.types.Scene, "eta_state"):
            raise AssertionError("Scene.eta_state remained registered after unregister().")


main()
