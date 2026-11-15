# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the locator and part records needed from a converted text PIM file.

Supported records:
    Locator { Name: "x"  [Hookup: "h"]  Index: 0  Position: ( &hex &hex &hex )
              Rotation: ( w x y z, hex floats )  Scale: ( &hex &hex &hex ) }
    Part    { Name: "defaultpart"  Pieces: 0 1 2  Locators: 0 1 2 }
This line scanner is not a full PIM parser. It reads one text file and uses only Python.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .sii.values import decode_hex_float

_HEX = re.compile(r"&[0-9A-Fa-f]{8}")
_KV = re.compile(r'^(\w+)\s*:\s*(.*)$')


@dataclass(frozen=True)
class PimLocator:
    index: int
    name: str
    hookup: str = ""
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_wxyz: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    part: str = ""            # part that owns this locator ("" if none lists it)

    @property
    def is_hookup(self) -> bool:
        return bool(self.hookup)


@dataclass
class PimInfo:
    locators: list[PimLocator] = field(default_factory=list)
    parts: dict[str, list[int]] = field(default_factory=dict)     # part name -> locator indices

    def names(self) -> set[str]:
        return {loc.name for loc in self.locators}


def _unquote(text: str) -> str:
    text = text.strip()
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text


def _floats(text: str) -> tuple[float, ...]:
    return tuple(decode_hex_float(h) for h in _HEX.findall(text))


def read_pim(path: Path | str) -> PimInfo:
    """Read locators and parts from a .pim text file."""
    info = PimInfo()
    section: str | None = None
    cur: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if section is None:
            if line in ("Locator {", "Part {"):
                section, cur = line.split()[0], {}
            continue
        if line == "}":
            if section == "Locator":
                pos = _floats(cur.get("Position", ""))
                rot = _floats(cur.get("Rotation", ""))
                sca = _floats(cur.get("Scale", ""))
                info.locators.append(PimLocator(
                    index=int(cur.get("Index", len(info.locators))),
                    name=_unquote(cur.get("Name", "")),
                    hookup=_unquote(cur.get("Hookup", "")),
                    position=pos if len(pos) == 3 else (0.0, 0.0, 0.0),
                    rotation_wxyz=rot if len(rot) == 4 else (1.0, 0.0, 0.0, 0.0),
                    scale=sca if len(sca) == 3 else (1.0, 1.0, 1.0)))
            else:
                info.parts[_unquote(cur.get("Name", ""))] = [int(i) for i in cur.get("Locators", "").split()]
            section = None
            continue
        m = _KV.match(line)
        if m:
            cur[m.group(1)] = m.group(2)
    owner = {i: part for part, idxs in info.parts.items() for i in idxs}
    info.locators = [PimLocator(l.index, l.name, l.hookup, l.position, l.rotation_wxyz, l.scale,
                                owner.get(l.index, "")) for l in info.locators]
    return info
