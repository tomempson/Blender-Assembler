# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse SII lexemes, with included files already expanded, into units.

Grammar:
    file      := 'SiiNunit' '{' unit* '}'
    unit      := class_name ':' unit_name '{' attribute* '}'
    attribute := key ':' value | key '[' ']' ':' value | key '[' INT ']' ':' value
    value     := STRING | NUMBER | HEXFLOAT | WORD | tuple | placement
    tuple     := '(' value (',' value)* ')'
    placement := tuple '(' value ';' value ',' value ',' value ')'

Array values may use a count line (`key: N`), append entries (`key[]:`), or indexed entries
(`key[i]:`). A count mismatch produces a warning, not an error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SiiSyntaxError
from .includes import IncludeResolver
from .lexer import EOF, HEXFLOAT, NUMBER, STRING, SYM, WORD, Lexeme, decode_text, tokenize
from .values import Placement, decode_hex_float, parse_number, word_value


@dataclass
class Unit:
    class_name: str                     # e.g. "accessory_addon_data"
    name: str                           # e.g. "chrome.scania.s_2016.mirror" or ".local"
    attrs: dict[str, Any]               # arrays are lists
    source: Path                        # top-level file the unit belongs to
    line: int
    origin: tuple[Path, int] | None = None   # where the header physically is (may be an included .sui)

    @property
    def is_local(self) -> bool:
        return self.name.startswith(".")

    def get(self, key: str, default: Any = None) -> Any:
        return self.attrs.get(key, default)

    def get_list(self, key: str) -> list:
        value = self.attrs.get(key)
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]


@dataclass
class SiiFile:
    path: Path
    units: list[Unit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _ArrayBuilder:
    __slots__ = ("items", "count", "count_line", "first_line")

    def __init__(self, first_line: int):
        self.items: list[Any] = []
        self.count: int | None = None
        self.count_line = 0
        self.first_line = first_line

    def append(self, value: Any) -> None:
        # Fill the first hole left by a count/indexed form, else append.
        self.items.append(value)

    def set(self, index: int, value: Any) -> None:
        while len(self.items) <= index:
            self.items.append(_MISSING)
        self.items[index] = value


_MISSING = object()


class _Parser:
    def __init__(self, lexemes: list[Lexeme], source: Path):
        self.lx = lexemes
        self.i = 0
        self.source = source
        self.warnings: list[str] = []

    # Helpers
    def peek(self, offset: int = 0) -> Lexeme:
        return self.lx[min(self.i + offset, len(self.lx) - 1)]

    def next(self) -> Lexeme:
        lx = self.lx[self.i]
        if lx.kind != EOF:
            self.i += 1
        return lx

    def error(self, message: str, lx: Lexeme | None = None) -> SiiSyntaxError:
        lx = lx or self.peek()
        return SiiSyntaxError(message, lx.path, lx.line, lx.chain)

    def expect_sym(self, sym: str) -> Lexeme:
        lx = self.next()
        if lx.kind != SYM or lx.value != sym:
            raise self.error(f"expected {sym!r}, found {_describe(lx)}", lx)
        return lx

    def is_sym(self, sym: str, offset: int = 0) -> bool:
        lx = self.peek(offset)
        return lx.kind == SYM and lx.value == sym

    def where(self, lx: Lexeme) -> str:
        loc = f"{lx.path}:{lx.line}"
        if lx.chain:
            loc += " (via " + " <- ".join(f"{p.name}:{n}" for p, n in reversed(lx.chain)) + ")"
        return loc

    # Grammar
    def parse_file(self) -> list[Unit]:
        head = self.next()
        if head.kind != WORD or head.value != "SiiNunit":
            raise self.error(f"file must start with 'SiiNunit', found {_describe(head)}", head)
        self.expect_sym("{")
        units: list[Unit] = []
        while not self.is_sym("}"):
            if self.peek().kind == EOF:
                raise self.error("unexpected end of file: missing '}' closing SiiNunit")
            units.append(self.parse_unit())
        self.expect_sym("}")
        trailing = self.peek()
        if trailing.kind != EOF:
            raise self.error(f"unexpected {_describe(trailing)} after the closing '}}' of SiiNunit", trailing)
        return units

    def parse_unit(self) -> Unit:
        cls = self.next()
        if cls.kind != WORD:
            raise self.error(f"expected a unit class name, found {_describe(cls)}", cls)
        self.expect_sym(":")
        name = self.next()
        if name.kind != WORD:
            raise self.error(f"expected a unit name after '{cls.value} :', found {_describe(name)}", name)
        self.expect_sym("{")

        attrs: dict[str, Any] = {}
        scalar_lines: dict[str, int] = {}
        arrays: dict[str, _ArrayBuilder] = {}
        while not self.is_sym("}"):
            if self.peek().kind == EOF:
                raise self.error(f"unexpected end of file inside unit {name.value!r} (missing '}}')")
            self.parse_attribute(attrs, scalar_lines, arrays, name.value)
        self.expect_sym("}")

        for key, arr in arrays.items():
            attrs[key] = self.finish_array(key, arr, scalar_lines, attrs, name.value)
        return Unit(cls.value, name.value, attrs, self.source, cls.line if not cls.chain else cls.chain[0][1],
                    (cls.path, cls.line))

    def parse_attribute(self, attrs: dict[str, Any], scalar_lines: dict[str, int],
                        arrays: dict[str, _ArrayBuilder], unit_name: str) -> None:
        key_lx = self.next()
        if key_lx.kind != WORD:
            raise self.error(f"expected an attribute name or '}}' in unit {unit_name!r}, "
                             f"found {_describe(key_lx)}", key_lx)
        key = key_lx.value
        index: int | None = None
        is_array = False
        if self.is_sym("["):
            self.next()
            is_array = True
            if not self.is_sym("]"):
                idx = self.next()
                if idx.kind != NUMBER or not idx.value.isdigit():
                    raise self.error(f"array index must be a non-negative integer, found {_describe(idx)}", idx)
                index = int(idx.value)
            self.expect_sym("]")
        self.expect_sym(":")
        value = self.parse_value()

        if is_array:
            arr = arrays.get(key)
            if arr is None:
                arr = arrays[key] = _ArrayBuilder(key_lx.line)
            if index is None:
                arr.append(value)
            else:
                arr.set(index, value)
            return

        if key in arrays:
            self.warnings.append(f"{self.where(key_lx)}: scalar '{key}' assigned after array entries "
                                 f"in unit {unit_name!r}; array kept, scalar ignored")
            return
        if key in attrs:
            self.warnings.append(f"{self.where(key_lx)}: '{key}' redefined in unit {unit_name!r} "
                                 f"(first at line {scalar_lines[key]}); last value wins")
        attrs[key] = value
        scalar_lines[key] = key_lx.line

    def finish_array(self, key: str, arr: _ArrayBuilder, scalar_lines: dict[str, int],
                     attrs: dict[str, Any], unit_name: str) -> list:
        count: int | None = None
        if key in attrs:
            declared = attrs.pop(key)
            if isinstance(declared, int) and not isinstance(declared, bool):
                count = declared
            else:
                self.warnings.append(f"{self.source}:{scalar_lines[key]}: '{key}' has scalar value "
                                     f"{declared!r} and array entries in unit {unit_name!r}; scalar ignored")
        items = arr.items
        holes = sum(1 for v in items if v is _MISSING)
        if count is not None and count != len(items):
            self.warnings.append(f"{self.source}:{scalar_lines[key]}: '{key}' declares {count} item(s) "
                                 f"but {len(items)} were given in unit {unit_name!r}")
            if count > len(items):
                items = items + [_MISSING] * (count - len(items))
                holes += count - len(arr.items)
        if holes:
            self.warnings.append(f"{self.source}:{arr.first_line}: '{key}' has {holes} missing "
                                 f"entr{'y' if holes == 1 else 'ies'} in unit {unit_name!r}; filled with None")
        return [None if v is _MISSING else v for v in items]

    def parse_value(self) -> Any:
        lx = self.next()
        if lx.kind == STRING:
            return lx.value
        if lx.kind == NUMBER:
            return parse_number(lx.value)
        if lx.kind == HEXFLOAT:
            return decode_hex_float(lx.value)
        if lx.kind == WORD:
            return word_value(lx.value)
        if lx.kind == SYM and lx.value == "(":
            first = self.parse_tuple_rest()
            if self.is_sym("("):
                return self.parse_placement_rotation(first)
            return first
        raise self.error(f"expected a value, found {_describe(lx)}", lx)

    def parse_tuple_rest(self) -> tuple:
        items = [self.parse_value()]
        while self.is_sym(","):
            self.next()
            items.append(self.parse_value())
        self.expect_sym(")")
        return tuple(items)

    def parse_placement_rotation(self, position: tuple) -> Placement:
        self.expect_sym("(")
        w = self.parse_value()
        self.expect_sym(";")
        x = self.parse_value()
        self.expect_sym(",")
        y = self.parse_value()
        self.expect_sym(",")
        z = self.parse_value()
        self.expect_sym(")")
        return Placement(position, (w, x, y, z))


def _describe(lx: Lexeme) -> str:
    if lx.kind == EOF:
        return "end of file"
    if lx.kind == STRING:
        return f"string {lx.value!r}"
    return repr(lx.value)


def parse_file(path: Path | str, defs_root: Path | str | None = None,
               resolver: IncludeResolver | None = None) -> SiiFile:
    """Parse one SII file, expanding @include. Raises SiiError subclasses on failure."""
    path = Path(path)
    if resolver is None:
        resolver = IncludeResolver(Path(defs_root) if defs_root is not None else None)
    lexemes = resolver.expand_file(path)
    parser = _Parser(lexemes, path)
    units = parser.parse_file()
    return SiiFile(path, units, parser.warnings)


def parse_text(text: str, path: Path | str = "<text>", defs_root: Path | str | None = None) -> SiiFile:
    """Parse SII text held in memory (tests). Relative includes resolve against `path`'s folder."""
    path = Path(path)
    resolver = IncludeResolver(Path(defs_root) if defs_root is not None else None)
    lexemes = resolver.expand_lexemes(tokenize(decode_text(text.encode("utf-8"), path), path), path)
    parser = _Parser(lexemes, path)
    units = parser.parse_file()
    return SiiFile(path, units, parser.warnings)
