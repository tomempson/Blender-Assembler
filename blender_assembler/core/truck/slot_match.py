# SPDX-License-Identifier: GPL-3.0-or-later
"""Map locator names to accessory slots.

Exact matches take priority. Otherwise, the longest matching slot-name prefix wins, which makes
overlapping names such as `badge` and `badge_a` deterministic.
"""
from __future__ import annotations

from collections.abc import Iterable


class SlotMatcher:
    def __init__(self, slot_names: Iterable[str]):
        self.slots: frozenset[str] = frozenset(slot_names)
        self._by_length = sorted(self.slots, key=lambda s: (-len(s), s))
        self._cache: dict[str, str | None] = {}

    def slot_for(self, locator_name: str) -> str | None:
        """The slot a locator belongs to, or None."""
        if locator_name in self._cache:
            return self._cache[locator_name]
        result: str | None = None
        if locator_name in self.slots:
            result = locator_name
        else:
            for slot in self._by_length:
                if locator_name.startswith(slot):
                    result = slot
                    break
        self._cache[locator_name] = result
        return result

    def matches(self, locator_name: str, slot: str) -> bool:
        return self.slot_for(locator_name) == slot
