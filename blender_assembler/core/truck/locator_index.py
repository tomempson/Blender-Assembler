# SPDX-License-Identifier: GPL-3.0-or-later
"""Locator index: slot name -> model locators found in the models placed so far (pure Python).

`payload` is whatever the caller needs to attach to (a Blender object in blender/assemble.py).
Hookup locators are never slots; callers keep them out of the index.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import OWNER_ORDER
from .slot_match import SlotMatcher


@dataclass
class LocatorEntry:
    name: str                   # true locator name (no Blender ".001" suffix)
    owner: str                  # "chassis", "cabin" or "accessory:<unit name>"
    part: str = ""              # SCS part used for variant filtering.
    side: str = "exterior"
    payload: Any = None

    @property
    def owner_kind(self) -> str:
        return self.owner.split(":", 1)[0]


class LocatorIndex:
    def __init__(self, matcher: SlotMatcher):
        self.matcher = matcher
        self._entries: list[LocatorEntry] = []
        self._order: dict[str, int] = {}        # owner -> insertion order (accessories keep placement order)

    def add(self, entries: list[LocatorEntry]) -> None:
        for e in entries:
            self._order.setdefault(e.owner, len(self._order))
            self._entries.append(e)

    def __len__(self) -> int:
        return len(self._entries)

    def find(self, slot: str, side: str = "exterior") -> list[LocatorEntry]:
        """Locators for `slot` from the first owner (cabin, chassis, accessories in placement order)
        that has any. The same slot is never filled by multiple owners."""
        by_owner: dict[str, list[LocatorEntry]] = {}
        for e in self._entries:
            if e.side == side and self.matcher.matches(e.name, slot):
                by_owner.setdefault(e.owner, []).append(e)
        if not by_owner:
            return []
        first = min(by_owner, key=lambda o: (OWNER_ORDER.get(o.split(":", 1)[0], 99), self._order[o]))
        return by_owner[first]

    def slots_present(self) -> set[str]:
        return {s for e in self._entries if (s := self.matcher.slot_for(e.name)) is not None}
