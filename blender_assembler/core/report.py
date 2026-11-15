# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python assembly report for placed and skipped items and their reasons."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class PlacedItem:
    slot: str
    unit_name: str
    model_path: str
    locator: str
    owner: str
    part: str = ""
    side: str = "exterior"


@dataclass
class SkippedItem:
    slot: str
    unit_name: str
    model_path: str | None
    reason: str
    kind: str = "skipped"       # "skipped": left out by design; "failed": something went wrong


@dataclass
class Report:
    title: str
    warnings: list[str] = field(default_factory=list)      # shown first, prominently (stale cache, ...)
    placed: list[PlacedItem] = field(default_factory=list)
    skipped: list[SkippedItem] = field(default_factory=list)
    hookups: dict[str, list[str]] = field(default_factory=dict)   # owner -> hookup names
    notes: list[str] = field(default_factory=list)
    stats: dict[str, float | int | str] = field(default_factory=dict)

    @property
    def failed(self) -> list[SkippedItem]:
        return [s for s in self.skipped if s.kind == "failed"]

    def summary(self) -> str:
        left_out = len(self.skipped) - len(self.failed)
        return f"{len(self.placed)} placed, {left_out} skipped, {len(self.failed)} failed"

    def to_dict(self) -> dict:
        return {
            "title": self.title, "summary": self.summary(), "warnings": self.warnings,
            "placed": [vars(p) for p in self.placed], "skipped": [vars(s) for s in self.skipped],
            "hookups": self.hookups, "notes": self.notes, "stats": self.stats,
        }

    def to_text(self) -> str:
        out: list[str] = [self.title, "=" * len(self.title), ""]
        if self.warnings:
            out.append("!" * 72)
            for w in self.warnings:
                out.append(f"WARNING: {w}")
            out += ["!" * 72, ""]
        out += [self.summary(), ""]
        out.append(f"PLACED ({len(self.placed)})")
        out.append("| Slot | Accessory unit | Side | Locator | Owner | Model |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for p in sorted(self.placed, key=lambda x: (x.owner, x.slot, x.locator)):
            locator = p.locator + (f" [{p.part}]" if p.part else "")
            out.append(f"| {p.slot} | {p.unit_name} | {p.side} | {locator} | {p.owner} | `{p.model_path}` |")
        out.append("")
        for kind, heading in (("failed", "FAILED"), ("skipped", "SKIPPED")):
            items = [s for s in self.skipped if s.kind == kind]
            out.append(f"{heading} ({len(items)})")
            for s in sorted(items, key=lambda x: (x.slot, x.unit_name)):
                out.append(f"  {s.slot:<16} {s.unit_name}")
                if s.model_path:
                    out.append(f"  {'':<16} model: {s.model_path}")
                out.append(f"  {'':<16} reason: {s.reason}")
            out.append("")
        if self.hookups:
            out.append("HOOKUP LOCATORS (not slots; listed for later)")
            for owner, names in self.hookups.items():
                counts: dict[str, int] = defaultdict(int)
                for n in names:
                    counts[n] += 1
                out.append(f"  {owner}: " + ", ".join(f"{n} x{c}" if c > 1 else n for n, c in sorted(counts.items())))
            out.append("")
        if self.notes:
            out.append("NOTES")
            out += [f"  {n}" for n in self.notes]
            out.append("")
        if self.stats:
            out.append("STATS")
            out += [f"  {k}: {v}" for k, v in self.stats.items()]
        return "\n".join(out).rstrip() + "\n"
