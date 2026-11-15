# SPDX-License-Identifier: GPL-3.0-or-later
"""Print a scanned TruckDefinition, or run real-data checks over every extracted truck.

Usage:
  py -3 tools/dump_truck.py <defs_root>/def/vehicle/truck/<brand.model> [--summary] [--raw] [--out FILE]
  py -3 tools/dump_truck.py <defs_root> <brand.model>                 [--summary] [--raw] [--out FILE]
  py -3 tools/dump_truck.py --all <defs_root>          # scan every truck, one summary line each + timing
  py -3 tools/dump_truck.py --parse-all <defs_root>    # parser only: every .sii of every truck (skips paint_job/)

<defs_root> is the folder that contains def/ (e.g. C:/ETS2Cache/defs).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blender_assembler.core.sii import Placement, SiiError, Unit, parse_file  # noqa: E402
from blender_assembler.core.sii.includes import IncludeResolver  # noqa: E402
from blender_assembler.core.truck import TruckDefinition, scan_truck  # noqa: E402
from blender_assembler.core.truck.scanner import TRUCK_DIR, list_trucks, split_truck_folder  # noqa: E402


def to_json(obj: Any, raw: bool) -> Any:
    if isinstance(obj, Unit):
        if not raw:
            return None
        return {"class_name": obj.class_name, "name": obj.name, "source": str(obj.source), "line": obj.line,
                "attrs": {k: to_json(v, raw) for k, v in obj.attrs.items()}}
    if isinstance(obj, Placement):
        return {"position": list(obj.position), "rotation_wxyz": list(obj.rotation_wxyz)}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out = {}
        for f in dataclasses.fields(obj):
            if f.name == "raw" and not raw:
                continue
            out[f.name] = to_json(getattr(obj, f.name), raw)
        return out
    if isinstance(obj, dict):
        return {str(k): to_json(v, raw) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json(v, raw) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, str):
        return str(obj)       # Token -> plain str
    return obj


def summary_line(d: TruckDefinition) -> str:
    s = d.stats
    return (f"{d.brand_model}: {len(d.chassis)} chassis, {len(d.cabins)} cabins, {len(d.interiors)} interiors, "
            f"{len(d.slots)} slots ({sum(1 for x in d.slots.values() if x.legacy)} legacy), "
            f"{s.get('accessories', 0)} accessories, {s.get('files_parsed', 0)} files "
            f"({s.get('files_failed', 0)} failed), {len(d.warnings)} warnings")


def print_summary(d: TruckDefinition) -> None:
    print(summary_line(d))
    print("stats:", json.dumps(d.stats, sort_keys=True))
    print("requires:", ", ".join(f"{r.name}{'' if r.known else '(?)'}" for r in d.requires))
    for name in sorted(d.slots):
        slot = d.slots[name]
        print(f"  {'L ' if slot.legacy else '  '}{name:<22} {len(slot.accessories)}")
    kinds = Counter(w.split("]", 1)[0] + "]" for w in d.warnings)
    print("warnings by kind:", dict(kinds))
    for w in d.warnings:
        print("  " + w)


def run_all(defs_root: Path) -> int:
    trucks = list_trucks(defs_root)
    t0 = time.perf_counter()
    totals: Counter[str] = Counter()
    for bm in trucks:
        t = time.perf_counter()
        d = scan_truck(defs_root, bm)
        totals.update({k: v for k, v in d.stats.items() if isinstance(v, int)})
        totals["warnings"] += len(d.warnings)
        print(f"{time.perf_counter() - t:6.2f}s  {summary_line(d)}")
    total = time.perf_counter() - t0
    print(f"\n{len(trucks)} trucks in {total:.1f}s ({total / max(len(trucks), 1):.2f}s per truck)")
    print("totals:", json.dumps(dict(sorted(totals.items()))))
    return 0


def run_parse_all(defs_root: Path) -> int:
    resolver = IncludeResolver(defs_root)
    base = defs_root / TRUCK_DIR
    ok = failed = units = 0
    errors: Counter[str] = Counter()
    examples: dict[str, str] = {}
    warnings = 0
    t0 = time.perf_counter()
    for path in sorted(base.rglob("*.sii")):
        if "paint_job" in path.relative_to(base).parts:
            continue
        try:
            sii = parse_file(path, resolver=resolver)
        except SiiError as err:
            failed += 1
            key = f"{type(err).__name__}: {err.message}"
            errors[key] += 1
            examples.setdefault(key, str(err))
            continue
        ok += 1
        units += len(sii.units)
        warnings += len(sii.warnings)
    total = time.perf_counter() - t0
    print(f"parsed {ok + failed} files in {total:.1f}s: {ok} ok, {failed} failed, {units} units, "
          f"{warnings} parser warnings")
    for key, n in errors.most_common():
        print(f"  {n} x {key}\n     e.g. {examples[key]}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="truck folder, or defs root when brand_model is given")
    ap.add_argument("brand_model", nargs="?")
    ap.add_argument("--all", metavar="DEFS_ROOT", help="scan every truck under DEFS_ROOT")
    ap.add_argument("--parse-all", metavar="DEFS_ROOT", help="parse every .sii under DEFS_ROOT/def/vehicle/truck")
    ap.add_argument("--summary", action="store_true", help="human-readable summary instead of JSON")
    ap.add_argument("--raw", action="store_true", help="include raw parsed units in the JSON")
    ap.add_argument("--out", help="write JSON to this file instead of stdout")
    args = ap.parse_args(argv)

    if args.all:
        return run_all(Path(args.all))
    if args.parse_all:
        return run_parse_all(Path(args.parse_all))
    if not args.path:
        ap.error("give a truck folder, or a defs root and a brand.model")
    if args.brand_model:
        defs_root, bm = Path(args.path), args.brand_model
    else:
        defs_root, bm = split_truck_folder(Path(args.path))

    defn = scan_truck(defs_root, bm)
    if args.summary:
        print_summary(defn)
        return 0
    text = json.dumps(to_json(defn, args.raw), indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({summary_line(defn)})")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
