# SPDX-License-Identifier: GPL-3.0-or-later
"""Open a real integration-test .blend and verify its assembled truck survived saving."""
from __future__ import annotations

from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import blender_assembler as addon  # noqa: E402


def main() -> None:
    try:
        separator = sys.argv.index("--")
    except ValueError as exc:
        raise RuntimeError("Expected the blend path and collection name after --.") from exc
    blend_path = Path(sys.argv[separator + 1])
    collection_name = sys.argv[separator + 2]
    addon.register()
    bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    collection = bpy.data.collections.get(collection_name)
    if collection is None:
        raise AssertionError(f"The saved file has no assembly collection named {collection_name!r}.")
    roots = [obj for obj in collection.all_objects if obj.get("eta_placement_root")]
    if len(roots) < 3:
        raise AssertionError(f"The saved assembly has only {len(roots)} placement roots.")
    if not any(obj.get("eta_placement_key") == "base:chassis" for obj in roots):
        raise AssertionError("The saved assembly is missing its chassis.")
    if not any(obj.get("eta_placement_key") == "base:cabin" for obj in roots):
        raise AssertionError("The saved assembly is missing its cabin.")
    if not any(obj.get("eta_placement_key", "").startswith("accessory:") for obj in roots):
        raise AssertionError("The saved assembly is missing its accessories.")
    print(f"SAVED_BLEND_PASS: {blend_path}; {len(roots)} placement roots")


main()
