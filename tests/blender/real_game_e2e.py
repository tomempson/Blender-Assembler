# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the registered add-on against a real ETS2 install in background Blender.

This is opt-in because it needs the game, ConverterPIX, and the SCS Blender Tools fork.
All converted files and the saved .blend are kept under the caller's disposable cache dir.
"""
from __future__ import annotations

import argparse
import addon_utils
import importlib.util
from pathlib import Path
import sys

import bpy


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def arguments() -> argparse.Namespace:
    try:
        separator = sys.argv.index("--")
    except ValueError as exc:
        raise RuntimeError("Expected arguments after Blender's -- separator.") from exc
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-dir", required=True, type=Path)
    parser.add_argument("--converter-executable", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--output-blend", required=True, type=Path)
    parser.add_argument("--dealer", required=True)
    return parser.parse_args(sys.argv[separator + 1:])


def require_finished(result, operation: str) -> None:
    if "FINISHED" not in result:
        raise AssertionError(f"{operation} returned {result}")


def main() -> None:
    args = arguments()
    if bpy.app.version < (5, 3, 0):
        raise AssertionError(f"Blender 5.3 or later is required; found {bpy.app.version_string}.")
    if not (args.game_dir / "base.scs").is_file():
        raise AssertionError(f"No base.scs found in game folder {args.game_dir}")
    if not args.converter_executable.is_file():
        raise AssertionError(f"ConverterPIX was not found: {args.converter_executable}")
    if not (args.game_dir / "locale.scs").is_file():
        raise AssertionError(f"No locale.scs found in game folder {args.game_dir}")

    addon_utils.disable("blender_assembler", default_set=False)
    for name in list(sys.modules):
        if name == "blender_assembler" or name.startswith("blender_assembler."):
            del sys.modules[name]
    package_path = ROOT / "blender_assembler"
    spec = importlib.util.spec_from_file_location(
        "blender_assembler", package_path / "__init__.py",
        submodule_search_locations=[str(package_path)])
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load Blender Assembler from the workspace.")
    addon = importlib.util.module_from_spec(spec)
    sys.modules["blender_assembler"] = addon
    spec.loader.exec_module(addon)
    addon_utils.enable("blender_assembler", default_set=True, persistent=False)
    if Path(sys.modules["blender_assembler"].__file__).resolve().parent != package_path.resolve():
        raise AssertionError("The integration test loaded Blender Assembler from outside the workspace.")
    if "blender_assembler" not in bpy.context.preferences.addons:
        raise AssertionError("Blender Assembler did not register its add-on preferences.")

    from blender_assembler.blender import operators

    # Snapshot the user's SCS settings file before the test changes any SCS globals.
    operators.ensure_scs_enabled()
    config_path = operators.scs_config_path()
    config_existed = config_path.is_file()
    config_bytes = config_path.read_bytes() if config_existed else None

    captured_reports = []
    original_assemble = operators.assemble

    def capture_assembly(*call_args, **call_kwargs):
        result = original_assemble(*call_args, **call_kwargs)
        captured_reports.append(result[1])
        return result

    operators.assemble = capture_assembly
    try:
        prefs = bpy.context.preferences.addons["blender_assembler"].preferences
        prefs.game_dir = str(args.game_dir)
        prefs.cache_dir = str(args.cache_dir)
        prefs.converter_path = str(args.converter_executable)
        prefs.jobs = 1

        require_finished(bpy.ops.eta.refresh_archives("EXEC_DEFAULT"), "Mount Archives")
        if not prefs.archives or not all(item.enabled for item in prefs.archives):
            raise AssertionError("The default archive list is empty or contains disabled archives.")
        require_finished(bpy.ops.eta.refresh_locales("EXEC_DEFAULT"), "Refresh Languages")
        if not prefs.locale or not any(item.label.casefold() == "en_gb" for item in prefs.locales):
            raise AssertionError("The game language list did not select or expose English (United Kingdom).")
        require_finished(bpy.ops.eta.refresh_trucks("EXEC_DEFAULT"), "Refresh Trucks")

        state = bpy.context.scene.eta_state
        truck = "scania.s_2016"
        if truck not in {item.name for item in state.truck_options}:
            raise AssertionError(f"{truck} was not found in the real game truck list.")
        state.truck = truck
        require_finished(bpy.ops.eta.load_truck("EXEC_DEFAULT"), "Load Truck")
        if args.dealer not in {item.name for item in state.dealer_options}:
            raise AssertionError(f"Dealer config was not offered for {truck}: {args.dealer}")
        state.dealer_config = args.dealer
        require_finished(bpy.ops.eta.load_dealer("EXEC_DEFAULT"), "Load Dealer Configuration")
        if state.truck != truck or not (state.cabin or state.chassis):
            raise AssertionError("Loading the dealer configuration did not populate the truck selection.")

        require_finished(bpy.ops.eta.assemble("EXEC_DEFAULT", replace_existing=False), "Assemble")
        if not captured_reports:
            raise AssertionError("The Assemble operator completed without producing an assembly report.")
        report = captured_reports[-1]
        if report.failed:
            details = "; ".join(f"{item.unit_name}: {item.reason}" for item in report.failed[:5])
            raise AssertionError(f"Assembly reported failed model imports: {details}")
        if not report.placed:
            raise AssertionError("The real dealer configuration produced no accessory placements.")
        if any(item.side != "exterior" for item in report.placed):
            raise AssertionError("The assembly unexpectedly placed an interior model.")
        if "Assembly complete!" not in state.status or ", 0 failed" not in state.status:
            raise AssertionError(f"Unexpected assembly status: {state.status!r}")

        collection_name = f"Blender Assembler - {truck}"
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            raise AssertionError(f"Assembly collection was not created: {collection_name}")
        roots = [obj for obj in collection.all_objects if obj.get("eta_placement_root")]
        if len(roots) != report.stats.get("imports"):
            raise AssertionError(
                f"Saved placement roots ({len(roots)}) do not match import count ({report.stats.get('imports')}).")
        if len(roots) < len(report.placed) + 1:
            raise AssertionError("The assembly is missing one or more base model roots.")

        args.output_blend.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(args.output_blend))
        print("REAL_GAME_E2E_PASS")
        print(f"BLENDER_VERSION {bpy.app.version_string}")
        print(f"ARCHIVES {len(prefs.archives)}; TRUCKS {len(state.truck_options)}; LOCALES {len(prefs.locales)}")
        print(f"DEALER {args.dealer}")
        print(f"ASSEMBLY {report.summary()}; roots {len(roots)}; imports {report.stats.get('imports')}")
        print(f"BLEND {args.output_blend}")
    finally:
        operators.assemble = original_assemble
        if config_existed:
            if not config_path.is_file() or config_path.read_bytes() != config_bytes:
                config_path.write_bytes(config_bytes)
        elif config_path.exists():
            config_path.unlink()


main()
