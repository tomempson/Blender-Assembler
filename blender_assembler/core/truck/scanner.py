# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan truck definition files into a `TruckDefinition`.

Input is the defs root (the folder that contains `def/`) plus a truck name, because
absolute includes such as "/def/vehicle/truck/common_trlr_cables.sui" point outside
the truck folder. Pure Python: reads files only, never touches Blender or model files.

Per-file failures become warnings so one bad file does not stop the scan.
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from ..path_safety import is_safe_segment, join_game_path, normalize_game_path
from ..sii.errors import SiiError
from ..sii.includes import IncludeResolver
from ..sii.parser import Unit, parse_file
from . import classes as C
from .model import (Accessory, DefPathRef, Fallback, ModelRefs, Slot, SlotRef, TruckDefinition,
                    UnitRef)

TRUCK_DIR = Path("def") / "vehicle" / "truck"
_EXAMPLES = 3   # examples listed in aggregated warnings


def wildcard_regex(pattern: str) -> re.Pattern[str]:
    """`*` is the only wildcard (matches any run of characters); everything else is literal."""
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")))


def truck_root(defs_root: Path, brand_model: str) -> Path:
    return defs_root / TRUCK_DIR / brand_model


def split_truck_folder(folder: Path) -> tuple[Path, str]:
    """`<defs_root>/def/vehicle/truck/<brand_model>` -> (defs_root, brand_model)."""
    folder = Path(os.path.abspath(folder))
    parts = folder.parts
    if len(parts) >= 5 and [p.lower() for p in parts[-4:-1]] == ["def", "vehicle", "truck"]:
        return Path(*parts[:-4]), parts[-1]
    raise ValueError(f"{folder} is not a <defs_root>/def/vehicle/truck/<brand.model> folder")


def list_trucks(defs_root: Path) -> list[str]:
    base = defs_root / TRUCK_DIR
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "data.sii").is_file())


class _Scan:
    def __init__(self, defs_root: Path, brand_model: str):
        self.defs_root = Path(defs_root)
        self.brand_model = brand_model
        self.root = truck_root(self.defs_root, brand_model)
        self.resolver = IncludeResolver(self.defs_root)
        self.warnings: list[str] = []
        self.stats: Counter[str] = Counter()
        self.file_units: dict[str, list[Unit]] = {}          # def path -> units in that file
        self.unknown_classes: dict[str, list[str]] = defaultdict(list)
        self.seen_names: dict[str, str] = {}                  # accessory unit name -> def path

    # Paths
    def game_path(self, path: Path) -> str:
        try:
            return "/" + Path(os.path.abspath(path)).relative_to(os.path.abspath(self.defs_root)).as_posix()
        except ValueError:
            return str(path)

    def where(self, err: SiiError) -> str:
        loc = f"{self.game_path(err.path) if err.path else '?'}:{err.line}"
        for p, n in reversed(err.chain):
            loc += f" (included from {self.game_path(p)}:{n})"
        return loc

    # Parsing
    def parse(self, path: Path) -> list[Unit] | None:
        self.stats["files_parsed"] += 1
        gp = self.game_path(path)
        try:
            sii = parse_file(path, resolver=self.resolver)
        except SiiError as err:
            self.stats["files_failed"] += 1
            self.warnings.append(f"[parse] {gp}: {type(err).__name__} at {self.where(err)}: {err.message}")
            return None
        except Exception as err:  # noqa: BLE001 - one bad file must never stop a scan
            self.stats["files_failed"] += 1
            self.warnings.append(f"[parse] {gp}: unexpected {type(err).__name__}: {err}")
            return None
        for w in sii.warnings:
            self.warnings.append(f"[sii] {w}")
        self.file_units[gp] = sii.units
        self.stats["units"] += len(sii.units)
        return sii.units

    # Building
    def text(self, unit: Unit, key: str) -> str | None:
        value = unit.attrs.get(key)
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            self.warnings.append(f"[attr] {self.game_path(unit.source)}:{unit.line}: '{key}' in "
                                 f"{unit.name!r} is a list/tuple, expected a single value; ignored")
            return None
        return str(value)

    def strings(self, unit: Unit, key: str) -> list[str]:
        return [str(v) for v in unit.get_list(key) if v is not None]

    def make_accessory(self, unit: Unit, slot: str, kind: str, path: Path) -> Accessory:
        t = lambda k: self.text(unit, k)  # noqa: E731
        if kind in ("chassis", "cabin"):
            models = ModelRefs(exterior=t("model") or t("detail_model"),
                               collision=t("collision") or t("coll"))
        elif kind == "interior":
            models = ModelRefs(exterior=t("ext_model"), interior=t("model"),
                               collision=t("collision") or t("coll"))
        else:
            models = ModelRefs(exterior=t("exterior_model"), interior=t("interior_model"),
                               exterior_uk=t("exterior_model_uk"), interior_uk=t("interior_model_uk"),
                               collision=t("coll") or t("collision"))
        if models.is_empty():
            self.stats["accessories_without_model"] += 1
        acc = Accessory(
            unit_name=unit.name,
            class_name=unit.class_name,
            slot=slot,
            display_name=t("name") or unit.name,
            models=models,
            icon=t("icon"),
            look=t("look"),
            variant=t("variant"),
            variant_uk=t("variant_uk"),
            ext_look=t("ext_look") if kind == "interior" else None,
            ext_variant=t("ext_variant") if kind == "interior" else None,
            suitable_for=[UnitRef(s) for s in self.strings(unit, "suitable_for")],
            conflict_with=[UnitRef(s) for s in self.strings(unit, "conflict_with")],
            defaults=[DefPathRef(s) for s in self.strings(unit, "defaults")],
            overrides=[DefPathRef(s) for s in self.strings(unit, "overrides")],
            part_type=t("part_type"),
            def_path=self.game_path(path),
            source=path,
            raw=unit,
        )
        expected_suffix = slot if kind == "addon" else kind
        suffix = unit.name.rsplit(".", 1)[-1]
        if suffix != expected_suffix:
            self.warnings.append(f"[name] {acc.def_path}: unit {unit.name!r} ends in '.{suffix}' but sits in "
                                 f"'{slot}'; folder wins")
        prev = self.seen_names.get(unit.name)
        if prev is not None:
            self.warnings.append(f"[name] {acc.def_path}: unit name {unit.name!r} already defined in {prev}")
        else:
            self.seen_names[unit.name] = acc.def_path
        return acc

    def note_other_class(self, unit: Unit, path: Path) -> None:
        if unit.class_name in C.EXCLUDED_CLASSES:
            self.stats["excluded_units"] += 1
        else:
            self.stats["unknown_class_units"] += 1
            self.unknown_classes[unit.class_name].append(self.game_path(path))

    def scan_base(self, folder: str, wanted_class: str) -> list[Accessory]:
        out: list[Accessory] = []
        d = self.root / folder
        if not d.is_dir():
            self.warnings.append(f"[folder] {self.game_path(d)}: missing")
            return out
        for path in sorted(d.glob("*.sii")):
            units = self.parse(path)
            for u in units or ():
                if u.class_name == wanted_class:
                    out.append(self.make_accessory(u, folder, folder, path))
                else:
                    self.note_other_class(u, path)
        return out

    def scan_slot_folder(self, d: Path, slot: str, legacy: bool) -> Slot:
        accs: list[Accessory] = []
        for path in sorted(d.glob("*.sii")):
            units = self.parse(path)
            for u in units or ():
                if C.is_addon_class(u.class_name):
                    accs.append(self.make_accessory(u, slot, "addon", path))
                elif C.is_unknown_accessory_class(u.class_name):
                    self.unknown_classes[u.class_name].append(self.game_path(path))
                    self.stats["unknown_accessory_units"] += 1
                    accs.append(self.make_accessory(u, slot, "addon", path))
                else:
                    self.note_other_class(u, path)
        nested = [p for p in d.iterdir() if p.is_dir()]
        self.stats["nested_dirs_skipped"] += len(nested)
        accs.sort(key=lambda a: (a.display_name, a.unit_name))
        return Slot(slot, accs, legacy)

    # Main
    def run(self) -> TruckDefinition:
        if not self.root.is_dir():
            raise FileNotFoundError(f"truck folder not found: {self.root}")

        truck_data = None
        data_path = self.root / "data.sii"
        if data_path.is_file():
            for u in self.parse(data_path) or ():
                if u.class_name == C.TRUCK_DATA_CLASS and truck_data is None:
                    truck_data = u
                elif u.class_name != C.TRUCK_DATA_CLASS:
                    self.note_other_class(u, data_path)
        else:
            self.warnings.append(f"[folder] {self.game_path(data_path)}: missing")

        for p in self.root.iterdir():
            if p.is_file() and p.name != "data.sii":
                self.stats["toplevel_files_skipped"] += 1

        chassis = self.scan_base("chassis", C.CHASSIS_CLASS)
        cabins = self.scan_base("cabin", C.CABIN_CLASS)
        interiors = self.scan_base("interior", C.INTERIOR_CLASS)

        slots: dict[str, Slot] = {}
        acc_dir = self.root / "accessory"
        if acc_dir.is_dir():
            for p in sorted(acc_dir.iterdir()):
                if p.is_dir():
                    slots[p.name] = self.scan_slot_folder(p, p.name, legacy=False)
                elif p.suffix == ".sii":
                    self.warnings.append(f"[folder] {self.game_path(p)}: .sii directly in accessory/, "
                                         f"not in a slot folder; skipped")
        for p in sorted(self.root.iterdir()):
            if p.is_dir() and p.name not in C.KNOWN_FOLDERS:
                if p.name in slots:
                    self.warnings.append(f"[folder] legacy folder '{p.name}' has the same name as an "
                                         f"accessory/ slot; merged into it")
                    legacy = self.scan_slot_folder(p, p.name, legacy=True)
                    slots[p.name].accessories.extend(legacy.accessories)
                else:
                    slots[p.name] = self.scan_slot_folder(p, p.name, legacy=True)
                    if not slots[p.name].accessories:
                        self.warnings.append(f"[folder] {self.game_path(p)}: legacy folder without "
                                             f"accessories")

        for p in self.root.iterdir():
            if p.is_dir() and p.name in C.SKIPPED_FOLDERS:
                self.stats["folders_skipped"] += 1

        indexed: dict[str, str] = {}
        for folder in sorted(C.NAME_INDEX_FOLDERS):
            d = self.root / folder
            if not d.is_dir():
                continue
            for path in sorted(d.glob("*.sii")):
                for u in self.parse(path) or ():
                    if u.class_name in C.NAME_INDEX_CLASSES and not u.is_local:
                        indexed[u.name] = folder
        self.stats["indexed_names"] = len(indexed)

        requires = [SlotRef(s, s in slots) for s in self.strings(truck_data, "require")] if truck_data else []
        for r in requires:
            if not r.known:
                self.warnings.append(f"[ref] data.sii require[] names slot '{r.name}', which has no folder")
        fallbacks = self.read_fallbacks(truck_data) if truck_data else []

        defn = TruckDefinition(
            brand_model=self.brand_model, root=self.root, defs_root=self.defs_root,
            truck_data=truck_data, requires=requires, fallbacks=fallbacks,
            chassis=sorted(chassis, key=lambda a: (a.display_name, a.unit_name)),
            cabins=sorted(cabins, key=lambda a: (a.display_name, a.unit_name)),
            interiors=sorted(interiors, key=lambda a: (a.display_name, a.unit_name)),
            slots=slots, warnings=self.warnings, indexed_names=indexed,
        )
        self.resolve_refs(defn)
        for cls, paths in sorted(self.unknown_classes.items()):
            kind = "included as accessory" if cls.startswith("accessory_") else "ignored"
            self.warnings.append(f"[class] unknown class '{cls}' {kind}: {len(paths)} unit(s), "
                                 f"e.g. {', '.join(sorted(set(paths))[:_EXAMPLES])}")
        self.stats["accessories"] = len(defn.all_accessories())
        self.stats["slots"] = len(slots)
        defn.stats = dict(self.stats)
        return defn

    def read_fallbacks(self, truck_data: Unit) -> list[Fallback]:
        out: list[Fallback] = []
        for entry in self.strings(truck_data, "fallback"):
            folder, sep, file = entry.partition("|")
            if not sep:
                self.warnings.append(f"[ref] data.sii fallback[] entry {entry!r} is not '<folder>|<file>'")
                continue
            def_path = f"/def/vehicle/truck/{self.brand_model}/{folder}/{file}"
            out.append(Fallback(folder, file, def_path))
        return out

    def classify_def_path(self, ref: DefPathRef) -> None:
        units = self.file_units.get(ref.path)
        if units is not None:
            ref.status = "resolved"
            ref.units = [u.name for u in units if not u.is_local]
            return
        on_disk = join_game_path(self.defs_root, ref.path)
        if on_disk is None:                  # never probe the disk for an unsafe path
            ref.status = "outside_root"
            return
        if not on_disk.is_file():
            ref.status = "missing"
            return
        prefix = f"/def/vehicle/truck/{self.brand_model}/"
        rest = ref.path[len(prefix):].split("/") if ref.path.startswith(prefix) else []
        skipped = bool(rest) and (rest[0] in C.SKIPPED_FOLDERS or "paint_job" in rest[:-1])
        ref.status = "skipped_folder" if skipped else "not_scanned"

    def resolve_refs(self, defn: TruckDefinition) -> None:
        names = {a.unit_name for a in defn.all_accessories()} | set(defn.indexed_names)
        pattern_hits: dict[str, bool] = {}
        unresolved: Counter[str] = Counter()
        missing_paths: list[tuple[str, str]] = []
        for acc in defn.all_accessories():
            for ref in acc.suitable_for + acc.conflict_with:
                if ref.is_pattern:
                    self.stats["wildcard_unit_refs"] += 1
                    hit = pattern_hits.get(ref.name)
                    if hit is None:
                        rx = wildcard_regex(ref.name)
                        hit = pattern_hits[ref.name] = any(rx.fullmatch(n) for n in names)
                else:
                    hit = ref.name in names
                suffix = ref.name.rsplit(".", 1)[-1]
                if hit:
                    ref.status = "resolved"
                elif suffix in C.NOT_INDEXED_SUFFIXES:
                    ref.status = "not_indexed"
                    self.stats["not_indexed_unit_refs"] += 1
                else:
                    ref.status = "unresolved"
                    unresolved[suffix] += 1
                    self.stats["unresolved_unit_refs"] += 1
            for ref in acc.defaults + acc.overrides:
                self.classify_def_path(ref)
                self.stats[f"def_refs_{ref.status}"] += 1
                if ref.status == "missing":
                    missing_paths.append((acc.def_path, ref.path))
        outside: list[str] = []
        for fb in defn.fallbacks:
            if not is_safe_segment(fb.folder) or normalize_game_path(fb.def_path) is None:
                self.warnings.append(f"[ref] data.sii fallback[] entry '{fb.folder}|{fb.file}' is not a safe "
                                     f"path; ignored")
                continue
            if not (self.root / fb.folder).is_dir():
                outside.append(fb.folder)   # wheel parts (f_tire, r_hub, ...) live outside the truck folder
                continue
            probe = DefPathRef(fb.def_path)
            self.classify_def_path(probe)
            if probe.status == "missing":
                missing_paths.append(("data.sii fallback[]", fb.def_path))
        if outside:
            self.stats["fallbacks_outside_truck"] = len(outside)
            self.warnings.append(f"[wheel] {len(outside)} data.sii fallback(s) for folders outside the truck "
                                 f"folder are not assembled: {', '.join(outside)}")
        if unresolved:
            top = ", ".join(f"'.{k}' x{n}" for k, n in unresolved.most_common(8))
            self.warnings.append(f"[ref] {sum(unresolved.values())} suitable_for/conflict_with name(s) "
                                 f"match no scanned unit, by unit-name suffix: {top}")
        for src, path in missing_paths[:20]:
            self.warnings.append(f"[ref] {src}: def file {path} does not exist")
        if len(missing_paths) > 20:
            self.warnings.append(f"[ref] ... and {len(missing_paths) - 20} more missing def file reference(s)")


def scan_truck(defs_root: Path | str, brand_model: str) -> TruckDefinition:
    """Scan one extracted truck. Raises FileNotFoundError only if the truck folder itself is missing."""
    return _Scan(Path(defs_root), brand_model).run()
