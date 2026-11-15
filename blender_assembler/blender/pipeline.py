# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare a truck for Blender import: extract definitions, scan, plan, and convert models.

This module avoids `bpy` and can be tested with the fake converter.

    prepare(conv, dealer="/def/vehicle/truck_dealer/scania/scania_s2016_4x2_460.sii")
    prepare(conv, selection=Selection(...))
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..core.report import Report, SkippedItem
from ..core.sii import SiiError
from ..core.truck import TruckDefinition, scan_truck
from ..core.truck.dealer import DealerSelection, DealerSkip, parse_dealer_file, selection_from_dealer
from ..core.truck.plan import AssemblyPlan, PlanOptions, build_plan
from ..core.truck.selection import Selection
from .converter import ConvertReport, Converter, ConverterError, ExtractResult


@dataclass
class Prepared:
    defn: TruckDefinition
    selection: Selection
    plan: AssemblyPlan
    dealer_skipped: list[DealerSkip]
    extract: ExtractResult
    convert: ConvertReport
    stale: list[str]
    notes: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def failures(self) -> dict[str, str]:
        """Game model path -> why its conversion failed (now or in an earlier run)."""
        out = dict(self.convert.failed)
        out.update(dict(self.convert.failed_before))
        out.update({p: "path rejected as unsafe" for p in self.convert.rejected})
        return out


def stale_warning(reasons: list[str]) -> str:
    shown = "; ".join(reasons[:3]) + (f"; and {len(reasons) - 3} more" if len(reasons) > 3 else "")
    return ("the game archives changed since this cache was built. Converted models and defs may be out of "
            f"date. Clear the cache to rebuild them. ({shown})")


def prepare(conv: Converter, dealer: str | None = None, selection: Selection | None = None,
            options: PlanOptions | None = None, retry_failed: bool = False,
            log: Callable[[str], None] = print,
            progress: Callable[[int, int, str], None] | None = None) -> Prepared:
    if (dealer is None) == (selection is None):
        raise ConverterError("Choose either a dealer configuration or a selection.")
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    stale = conv.stale_reasons()
    if stale:
        log("WARNING: " + stale_warning(stale))

    dealer_cfg = None
    if dealer is not None:
        t = time.perf_counter()
        path = conv.extract_file(dealer)
        try:
            dealer_cfg = parse_dealer_file(path, conv.defs)
        except SiiError as exc:
            raise ConverterError(f"Dealer configuration file cannot be read: {dealer}; {exc}") from exc
        if not dealer_cfg.brand_model:
            raise ConverterError(f"Dealer configuration file has no truck model data: {dealer}.")
        truck = dealer_cfg.brand_model
        timings["dealer_config"] = time.perf_counter() - t
    else:
        truck = selection.truck

    t = time.perf_counter()
    ext = conv.extract_truck(truck)
    if ext.error:
        raise ConverterError(f"Extracting game data for {truck} failed: {ext.error}")
    timings["extract_defs"] = time.perf_counter() - t
    log(f"defs {truck}: " + (f"extracted {ext.files} files in {ext.seconds:.1f}s" if ext.extracted else "cached"))

    t = time.perf_counter()
    defn = scan_truck(conv.defs, truck)
    timings["scan"] = time.perf_counter() - t
    log(f"scan {timings['scan']:.1f}s: {len(defn.all_accessories())} accessories, {len(defn.slots)} slots, "
        f"{len(defn.warnings)} warnings")

    notes: list[str] = []
    dealer_skipped: list[DealerSkip] = []
    if dealer_cfg is not None:
        ds: DealerSelection = selection_from_dealer(dealer_cfg, defn)
        selection, dealer_skipped, notes = ds.selection, ds.skipped, list(ds.notes)

    plan = build_plan(defn, selection, options)
    log(f"plan: {len(plan.base)} base model(s), {sum(len(v) for v in plan.pending.values())} addon model(s), "
        f"{len(plan.skipped)} skipped, {len(plan.model_paths())} unique model(s) to convert")

    t = time.perf_counter()
    def conversion_progress(done: int, total: int, key: str) -> None:
        log(f"  convert [{done}/{total}] {key}")
        if progress:
            progress(done, total, key)
    report = conv.convert_models(plan.model_paths(), retry_failed=retry_failed,
                                 progress=conversion_progress)
    timings["convert"] = time.perf_counter() - t
    log(report.summary())
    timings["total"] = time.perf_counter() - t0
    return Prepared(defn, selection, plan, dealer_skipped, ext, report, stale, notes, timings)


def seed_report(title: str, prep: Prepared) -> Report:
    """A Report holding everything known before the Blender phase."""
    rep = Report(title)
    if prep.stale:
        rep.warnings.append("STALE CACHE: " + stale_warning(prep.stale))
    for s in prep.plan.skipped:
        rep.skipped.append(SkippedItem(s.slot, s.unit_name, s.model_path, s.reason))
    for d in prep.dealer_skipped:
        rep.skipped.append(SkippedItem(d.local.lstrip("."), d.data_path or "", None,
                                       f"dealer entry ({d.class_name}): {d.reason}"))
    for path, reason in prep.failures().items():
        rep.skipped.append(SkippedItem("(conversion)", "", path, reason.splitlines()[0], kind="failed"))
    rep.notes += prep.notes
    rep.stats.update({f"time_{k}": round(v, 1) for k, v in prep.timings.items()})
    rep.stats["models_converted"] = len(prep.convert.converted)
    rep.stats["models_cached"] = len(prep.convert.cached)
    return rep
