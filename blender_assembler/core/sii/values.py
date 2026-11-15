# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed SII value helpers."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any


class Token(str):
    """A bare (unquoted) SII word: an identifier, enum value or unit pointer.

    Subclasses str so it can be compared and printed like one, but stays
    distinguishable from quoted strings via isinstance(value, Token).
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return f"Token({str.__repr__(self)})"


@dataclass(frozen=True)
class Placement:
    """Position plus rotation quaternion, written `(x, y, z) (w; x, y, z)` in SII."""

    position: tuple[float, ...]
    rotation_wxyz: tuple[float, float, float, float]


def decode_hex_float(text: str) -> float:
    """`&3f800000` -> 1.0 (IEEE-754 float32 bits, big-endian hex)."""
    digits = text[1:] if text.startswith("&") else text
    return struct.unpack(">f", bytes.fromhex(digits))[0]


def parse_number(text: str) -> int | float:
    """Int or float. A C-style `f` suffix (`-0.008f`, seen in real toy defs) marks a float."""
    if text.endswith("f"):
        return float(text[:-1])
    if any(c in text for c in ".eE"):
        return float(text)
    return int(text)


def word_value(text: str) -> Any:
    """Bare words: nil/true/false become None/True/False, anything else a Token."""
    if text == "nil":
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    return Token(text)
