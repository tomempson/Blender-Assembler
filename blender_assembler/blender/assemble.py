# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute an assembly plan in Blender using independent imported model objects."""
from __future__ import annotations

import time
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import bpy
from mathutils import Matrix

from ..core.report import PlacedItem, Report, SkippedItem
from ..core.truck.constants import CABIN_AT_CHASSIS_ORIGIN, MAX_DEPTH
from ..core.truck.locator_index import LocatorEntry, LocatorIndex
from ..core.truck.slot_match import SlotMatcher
from ..core.truck.paths import resolve_model
from ..core.truck.names import find_name
from .locators import find_model_locators
from .pipeline import Prepared, seed_report
from .scs_bridge import ScsSession


class AssemblyError(RuntimeError):
    pass


@dataclass
class ImportedModel:
    root: bpy.types.Object
    locators: list[LocatorEntry]
    hookups: list[str]
    unmatched: list[str]
    placement_key: str
    signature: str


_MANIFEST_PROPERTY = "eta_assembly_manifest"
_MANIFEST_VERSION = 1


def _stable_hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def archive_signature(fingerprint) -> str:
    """Stable digest of mounted archives and settings that affect source lookup."""
    return _stable_hash(fingerprint)


def _placement_root(obj: bpy.types.Object) -> bpy.types.Object | None:
    current = obj
    while current is not None:
        if current.get("eta_placement_root"):
            return current
        current = current.parent
    return None


def _remove_named_objects(names) -> None:
    objects = [bpy.data.objects.get(name) for name in names]
    objects = [obj for obj in objects if obj is not None]

    def depth(obj):
        result = 0
        parent = obj.parent
        while parent is not None:
            result += 1
            parent = parent.parent
        return result

    for obj in sorted(objects, key=depth, reverse=True):
        if bpy.data.objects.get(obj.name) is obj:
            bpy.data.objects.remove(obj, do_unlink=True)


def _placement_objects(collection: bpy.types.Collection, key: str, signature: str | None = None):
    return [obj for obj in collection.all_objects
            if obj.get("eta_placement_key") == key
            and (signature is None or obj.get("eta_placement_signature") == signature)]


def _child_collection(parent: bpy.types.Collection, name: str) -> bpy.types.Collection:
    collection = parent.children.get(name)
    return collection if collection is not None else _collection(parent, name)


def _collection(parent: bpy.types.Collection, name: str) -> bpy.types.Collection:
    value = bpy.data.collections.new(name)
    parent.children.link(value)
    return value


def _copy_shared_materials(root: bpy.types.Object) -> float:
    """Copy shared materials once per imported model/look before applying the look."""
    started = time.perf_counter()
    copies: dict[int, bpy.types.Material] = {}
    for obj in root.children_recursive:
        if obj.type != "MESH" or not obj.data:
            continue
        for i, material in enumerate(obj.data.materials):
            if material is None or material.users <= 1:
                continue
            key = material.as_pointer()
            if key not in copies:
                copies[key] = material.copy()
                copies[key].name = material.name + " [Blender Assembler model copy]"
            obj.data.materials[i] = copies[key]
    return time.perf_counter() - started


def _apply_model_look(root: bpy.types.Object, wanted: str | None, report: Report, label: str) -> tuple[float, float]:
    inventory = root.scs_object_look_inventory
    if not inventory:
        if wanted:
            report.warnings.append(f"{label}: requested look {wanted!r}, but model has no look inventory")
        return 0.0, 0.0
    names = [item.name for item in inventory]
    name = find_name(wanted, names) if wanted else names[0]
    if name is None:
        report.warnings.append(f"{label}: look {wanted!r} not found; using {names[0]!r}")
        name = names[0]
    material_seconds = _copy_shared_materials(root)
    view_layer = bpy.context.view_layer
    old_active = view_layer.objects.active
    root.select_set(True)
    view_layer.objects.active = root
    look_started = time.perf_counter()
    try:
        root.scs_props.active_scs_look = names.index(name)
    finally:
        view_layer.objects.active = old_active
    return material_seconds, time.perf_counter() - look_started


def _apply_model_variant(root: bpy.types.Object, wanted: str | None, report: Report,
                         label: str) -> set[str] | None:
    inventory = root.scs_object_variant_inventory
    if not inventory:
        if wanted:
            report.warnings.append(f"{label}: requested variant {wanted!r}, but model has no variant inventory")
        return None
    names = [item.name for item in inventory]
    name = find_name(wanted, names) if wanted else names[0]
    if name is None:
        report.warnings.append(f"{label}: variant {wanted!r} not found; using {names[0]!r}")
        name = names[0]
    idx = names.index(name)
    included = {part.name for part in inventory[idx].parts if part.include}
    root.scs_props.active_scs_variant = idx
    old_active = bpy.context.view_layer.objects.active
    root.select_set(True)
    bpy.context.view_layer.objects.active = root
    try:
        result = bpy.ops.object.scs_tools_switch_objects_with_variant(variant_index=idx, view_type=1)
        if "FINISHED" not in result:
            raise RuntimeError(f"variant visibility operator returned {result}")
    except Exception as exc:
        report.warnings.append(f"{label}: SCS variant operator failed ({exc}); used direct part visibility")
        for obj in _variant_managed_descendants(root):
            obj.hide_set(obj.scs_props.scs_part not in included)
    finally:
        bpy.context.view_layer.objects.active = old_active
    removed = _delete_excluded_variant_objects(root, included)
    if removed:
        report.stats["variant_objects_removed"] = (
            report.stats.get("variant_objects_removed", 0) + removed)
    return included


def _resolved_inventory_index(wanted: str | None, names: list[str]) -> int:
    selected = find_name(wanted, names) if wanted else names[0]
    return names.index(selected) if selected is not None else 0


def _model_state_matches(root: bpy.types.Object, variant: str | None, look: str | None) -> bool:
    variants = root.scs_object_variant_inventory
    if variants:
        names = [item.name for item in variants]
        if root.scs_props.active_scs_variant != _resolved_inventory_index(variant, names):
            return False
    looks = root.scs_object_look_inventory
    if looks:
        names = [item.name for item in looks]
        if root.scs_props.active_scs_look != _resolved_inventory_index(look, names):
            return False
    return True


def _existing_model_variant_parts(root: bpy.types.Object, wanted: str | None,
                                  report: Report, label: str) -> set[str] | None:
    inventory = root.scs_object_variant_inventory
    if not inventory:
        if wanted:
            report.warnings.append(f"{label}: requested variant {wanted!r}, but model has no variant inventory")
        return None
    names = [item.name for item in inventory]
    selected = find_name(wanted, names) if wanted else names[0]
    if selected is None:
        report.warnings.append(f"{label}: variant {wanted!r} not found; using {names[0]!r}")
    idx = _resolved_inventory_index(wanted, names)
    return {part.name for part in inventory[idx].parts if part.include}


def _check_existing_model_look(root: bpy.types.Object, wanted: str | None,
                               report: Report, label: str) -> None:
    names = [item.name for item in root.scs_object_look_inventory]
    if not names and wanted:
        report.warnings.append(f"{label}: requested look {wanted!r}, but model has no look inventory")
    elif names and wanted and find_name(wanted, names) is None:
        report.warnings.append(f"{label}: look {wanted!r} not found; using {names[0]!r}")


def _variant_managed_descendants(root: bpy.types.Object) -> list[bpy.types.Object]:
    """Match the descendants SCS Tools considers when switching a variant."""
    managed = []
    for obj in root.children_recursive:
        if obj.type == "EMPTY" and obj.scs_props.empty_object_type in {"SCS Root", "SCS_Root"}:
            continue
        if (obj.type == "MESH" and obj.data and "scs_props" in obj.data
                and obj.data.scs_props.locator_preview_model_path):
            continue
        managed.append(obj)
    return managed


def _delete_excluded_variant_objects(root: bpy.types.Object, included_parts: set[str]) -> int:
    """Remove inactive-variant objects so unhide commands cannot reveal them later.

    If a surviving included object is parented below an excluded object, detach it
    to the nearest surviving ancestor while preserving its world transform.
    """
    descendants = _variant_managed_descendants(root)
    excluded = {obj for obj in descendants if obj.scs_props.scs_part not in included_parts}
    if not excluded:
        return 0

    for obj in descendants:
        if obj in excluded:
            continue
        if obj.parent not in excluded:
            continue
        world = obj.matrix_world.copy()
        parent = obj.parent
        while parent in excluded:
            parent = parent.parent
        obj.parent = parent
        obj.matrix_world = world

    def object_depth(obj):
        depth = 0
        parent = obj.parent
        while parent is not None:
            depth += 1
            parent = parent.parent
        return depth

    for obj in sorted(excluded, key=object_depth, reverse=True):
        bpy.data.objects.remove(obj, do_unlink=True)
    return len(excluded)


def assemble_steps(prep: Prepared, base_path: Path, scene=None, progress=None,
                  reuse_existing: bool = False, archive_signature: str = ""):
    """Import selected models, yielding after each one so Blender can redraw progress."""
    scene = scene or bpy.context.scene
    base_path = Path(base_path)
    title = f"Blender Assembler - {prep.defn.brand_model}"
    root_collection = bpy.data.collections.get(title)
    if root_collection is not None and not reuse_existing:
        raise AssemblyError(f"An assembly named {title} already exists. Remove it before assembling again.")
    if root_collection is None:
        root_collection = _collection(scene.collection, title)

    old_manifest = None
    if reuse_existing:
        try:
            stored = json.loads(root_collection.get(_MANIFEST_PROPERTY, ""))
            if (stored.get("version") == _MANIFEST_VERSION
                    and stored.get("archive_signature") == archive_signature
                    and isinstance(stored.get("placements"), dict)):
                old_manifest = stored
        except (TypeError, ValueError, AttributeError):
            pass
    if old_manifest is None:
        old_manifest = {"placements": {}}

    old_roots = {}
    for obj in root_collection.all_objects:
        if not obj.get("eta_placement_root"):
            continue
        key = obj.get("eta_placement_key")
        signature = obj.get("eta_placement_signature")
        if key and signature and old_manifest["placements"].get(key) == signature:
            old_roots[(key, signature)] = obj

    base_collections = {
        "cabin": _child_collection(root_collection, "Cabin"),
        "chassis": _child_collection(root_collection, "Chassis"),
    }
    accessory_collection = _child_collection(root_collection, "Accessories")
    run_id = uuid.uuid4().hex
    new_placements: dict[str, str] = {}
    new_object_names = set()
    forced_obsolete_names = set()
    report = seed_report(title, prep)
    report.notes.append("Models are imported independently for every placement.")
    matcher = SlotMatcher(prep.defn.slots)
    index = LocatorIndex(matcher)
    started = time.perf_counter()
    profile = {name: 0.0 for name in (
        "variant", "look", "material_copy", "locator_scan", "parenting",
        "index_report", "final_scene_update")}
    imports = 0
    reused = 0
    total_steps = max(1, len(prep.plan.base) + sum(len(v) for v in prep.plan.pending.values()))
    done = 0
    wm = bpy.context.window_manager
    wm.progress_begin(0, total_steps)

    def tick(label: str = "") -> None:
        nonlocal done
        done += 1
        wm.progress_update(min(done, total_steps))
        if progress:
            progress(done, total_steps, label)

    def grow_progress(extra: int) -> None:
        nonlocal total_steps
        if extra > 0:
            total_steps += extra
            # Locator multiplicity is learned only after the base models are imported.
            wm.progress_begin(0, total_steps)
            wm.progress_update(min(done, total_steps))

    def import_model(path: str, owner: str, side: str, variant: str | None, look: str | None,
                     collection: bpy.types.Collection, placement_key: str,
                     signature: str) -> tuple[ImportedModel | None, str | None]:
        nonlocal imports, reused
        pim = resolve_model(path, base_path)
        if pim is None or not pim.is_file():
            return None, f"Model file {path} is missing from cache: {pim}"
        # Include the converted model files as dependencies too. This catches
        # cache repairs or manual replacements even when the mounted archives
        # themselves have not changed.
        dependencies = []
        for dependency in (pim, pim.with_suffix(".pit"), pim.with_suffix(".pic")):
            try:
                stat = dependency.stat()
            except OSError:
                dependencies.append({"path": str(dependency.resolve()), "exists": False})
            else:
                dependencies.append({"path": str(dependency.resolve()), "size": stat.st_size,
                                     "mtime_ns": stat.st_mtime_ns})
        signature = _stable_hash({"placement": signature, "dependencies": dependencies})
        root = old_roots.get((placement_key, signature))
        if root is not None and not _model_state_matches(root, variant, look):
            forced_obsolete_names.update(
                obj.name for obj in _placement_objects(root_collection, placement_key, signature))
            root = None
        is_new = root is None
        if root is None:
            outcome = session.import_pim(pim, collection)
            if outcome.error or outcome.root is None:
                return None, outcome.error or "SCS Tools could not create a model root."
            root = outcome.root
            imports += 1
            for obj in outcome.objects:
                obj["eta_placement_key"] = placement_key
                obj["eta_placement_signature"] = signature
                obj["eta_assembly_run"] = run_id
                new_object_names.add(obj.name)
            root["eta_placement_root"] = True
        else:
            reused += 1
        if is_new:
            phase_started = time.perf_counter()
            included = _apply_model_variant(root, variant, report, owner)
            profile["variant"] += time.perf_counter() - phase_started
            material_seconds, look_seconds = _apply_model_look(root, look, report, owner)
            profile["material_copy"] += material_seconds
            profile["look"] += look_seconds
        else:
            included = _existing_model_variant_parts(root, variant, report, owner)
            _check_existing_model_look(root, look, report, owner)
        phase_started = time.perf_counter()
        found = find_model_locators(root, pim, owner, included, side)
        profile["locator_scan"] += time.perf_counter() - phase_started
        # Hide only the SCS_Root empty. Its child objects remain visible and retain
        # the hierarchy and locator data used by attachment and SCS Tools.
        root.hide_set(True)
        return ImportedModel(root, found.slots, found.hookups, found.unmatched,
                             placement_key, signature), None

    def place(model: ImportedModel, owner: str,
              target=None, at_origin: bool = False) -> tuple[bpy.types.Object, list[LocatorEntry]]:
        obj = model.root
        phase_started = time.perf_counter()
        if target is not None and not at_origin:
            obj.parent = target
            obj.matrix_parent_inverse.identity()
            obj.matrix_basis.identity()
        elif at_origin:
            obj.matrix_world = Matrix.Identity(4)
        obj["eta_placement_key"] = model.placement_key
        obj["eta_placement_signature"] = model.signature
        obj["eta_placement_root"] = True
        new_placements[model.placement_key] = model.signature
        profile["parenting"] += time.perf_counter() - phase_started
        entries = model.locators
        report.hookups.setdefault(owner, []).extend(model.hookups)
        if model.unmatched:
            report.notes.append(f"{owner}: {len(model.unmatched)} imported locator object(s) did not match PIM names")
        return obj, entries

    try:
        with ScsSession(base_path, import_collision=True) as session:
            for base in prep.plan.base:
                placement_key = f"base:{base.role}"
                signature = _stable_hash({
                    "archive": archive_signature, "role": base.role, "unit": base.unit_name,
                    "model": base.model_path, "look": base.look, "variant": base.variant,
                })
                if progress:
                    progress(done + 1, total_steps, base.role)
                yield base.role
                value, error = import_model(base.model_path, base.role, "exterior", base.variant, base.look,
                                            base_collections[base.role], placement_key, signature)
                if error:
                    raise AssemblyError(f"SCS Tools cannot import model {base.model_path}: {error}.")
                target = None
                at_origin = base.role in ("chassis", "cabin") and (base.role == "chassis" or CABIN_AT_CHASSIS_ORIGIN)
                _, entries = place(value, base.role, target, at_origin)
                index.add(entries)
                tick(base.role)
                yield base.role

            pending = {slot: list(models) for slot, models in prep.plan.pending.items()}
            for _depth in range(MAX_DEPTH):
                placed_any = False
                for slot in sorted(list(pending)):
                    models = pending[slot]
                    if not models:
                        pending.pop(slot)
                        continue
                    model = models.pop(0)
                    if not models:
                        pending.pop(slot)
                    locs = index.find(slot, side=model.side)
                    if not locs:
                        report.skipped.append(SkippedItem(slot, model.unit_name, model.model_path,
                                                          f"no matching {model.side} locator for slot '{slot}'"))
                        tick(model.unit_name)
                        yield model.unit_name
                        continue
                    grow_progress(len(locs) - 1)
                    locator_occurrences = {}
                    for loc in locs:
                        owner_root = _placement_root(loc.payload)
                        owner_key = owner_root.get("eta_placement_key") if owner_root else loc.owner
                        owner_signature = owner_root.get("eta_placement_signature", "") if owner_root else ""
                        locator_name = loc.name
                        occurrence_key = (owner_key, owner_signature, locator_name)
                        occurrence = locator_occurrences.get(occurrence_key, 0)
                        locator_occurrences[occurrence_key] = occurrence + 1
                        identity = {
                            "slot": model.slot, "unit": model.unit_name, "def": model.def_path,
                            "owner": owner_key, "locator": locator_name, "occurrence": occurrence,
                        }
                        placement_key = "accessory:" + _stable_hash(identity)
                        signature = _stable_hash({
                            "archive": archive_signature, **identity,
                            "owner_signature": owner_signature, "model": model.model_path,
                            "look": model.look, "variant": model.variant, "side": model.side,
                        })
                        if progress:
                            progress(done + 1, total_steps, model.unit_name)
                        yield model.unit_name
                        placement_model, error = import_model(
                            model.model_path, f"accessory:{model.unit_name}", model.side,
                            model.variant, model.look, accessory_collection, placement_key, signature)
                        if error:
                            report.skipped.append(SkippedItem(slot, model.unit_name, model.model_path,
                                                              error, kind="failed"))
                            tick(model.unit_name)
                            yield model.unit_name
                            continue
                        obj, entries = place(placement_model, f"accessory:{model.unit_name}", loc.payload)
                        phase_started = time.perf_counter()
                        index.add(entries)
                        report.placed.append(PlacedItem(slot, model.unit_name, model.model_path,
                                                        loc.name, loc.owner, loc.part, model.side))
                        profile["index_report"] += time.perf_counter() - phase_started
                        placed_any = True
                        tick(model.unit_name)
                        yield model.unit_name
                if not placed_any:
                    break

            for slot, models in pending.items():
                for model in models:
                    report.skipped.append(SkippedItem(slot, model.unit_name, model.model_path,
                                                      f"no matching {model.side} locator for slot '{slot}'"))

            phase_started = time.perf_counter()
            bpy.context.view_layer.update()
            profile["final_scene_update"] += time.perf_counter() - phase_started

        manifest_text = json.dumps({
            "version": _MANIFEST_VERSION,
            "archive_signature": archive_signature,
            "placements": new_placements,
        }, sort_keys=True, separators=(",", ":"))
        obsolete_names = []
        for obj in list(root_collection.all_objects):
            key = obj.get("eta_placement_key")
            signature = obj.get("eta_placement_signature")
            if obj.name in forced_obsolete_names or not key or new_placements.get(key) != signature:
                obsolete_names.append(obj.name)
            elif "eta_assembly_run" in obj:
                del obj["eta_assembly_run"]
        obsolete_count = len(obsolete_names)
        _remove_named_objects(obsolete_names)
        root_collection[_MANIFEST_PROPERTY] = manifest_text

        report.stats.update({
            "imports": imports,
            "reused_placements": reused,
            "removed_objects": obsolete_count,
            "import_seconds": round(session.import_seconds, 2),
        })
    except Exception:
        report.stats["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        _remove_named_objects(new_object_names)
        raise
    finally:
        wm.progress_end()

    report.stats.update({f"profile_{name}_seconds": round(value, 3) for name, value in profile.items()})
    accounted = session.import_seconds + sum(profile.values())
    profile["other"] = max(0.0, time.perf_counter() - started - accounted)
    report.stats["profile_other_seconds"] = round(profile["other"], 3)
    report.stats["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    removed_variant_objects = report.stats.get("variant_objects_removed", 0)
    if removed_variant_objects:
        report.notes.append(
            f"Permanently removed {removed_variant_objects} object(s) excluded by selected variants.")
    placement_count = max(1, len(report.placed))
    for name, value in profile.items():
        if name != "other":
            report.stats[f"profile_{name}_ms_per_placement"] = round(value * 1000 / placement_count, 3)
    return root_collection, report


def assemble(prep: Prepared, base_path: Path, scene=None,
             progress=None, reuse_existing: bool = False,
             archive_fingerprint=None) -> tuple[bpy.types.Collection, Report]:
    """Run assembly synchronously for scripts; the Blender UI uses ``assemble_steps``."""
    digest = archive_signature(archive_fingerprint or [])
    steps = assemble_steps(prep, base_path, scene, progress, reuse_existing, digest)
    while True:
        try:
            next(steps)
        except StopIteration as finished:
            return finished.value
