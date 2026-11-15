# SPDX-License-Identifier: GPL-3.0-or-later
"""Name helpers.

Variant and look names differ in case between defs and models (cabin def says
`variant: Default`, the model's variant inventory says `default`), so they are stored
exactly as written and compared case-insensitively.
"""
from __future__ import annotations

from collections.abc import Iterable


def norm_name(name: str | None) -> str:
    return (name or "").casefold()


def same_name(a: str | None, b: str | None) -> bool:
    return norm_name(a) == norm_name(b)


def find_name(wanted: str | None, candidates: Iterable[str]) -> str | None:
    """Return the candidate matching `wanted` case-insensitively, or None."""
    key = norm_name(wanted)
    for c in candidates:
        if norm_name(c) == key:
            return c
    return None
