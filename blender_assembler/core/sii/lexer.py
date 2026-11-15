# SPDX-License-Identifier: GPL-3.0-or-later
"""SII lexer: text -> list of Lexeme.

Also holds the encoded-file detection that runs on raw bytes before lexing.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import IncludeSite, SiiEncodedError, SiiSyntaxError

# Lexeme kinds
STRING = "string"
NUMBER = "number"
HEXFLOAT = "hexfloat"
WORD = "word"
SYM = "sym"
INCLUDE = "include"   # the `@include` keyword; the path string follows as a STRING lexeme
EOF = "eof"

_MASTER = re.compile(
    r"""
    (?P<ws>[ \t\r\n\f\v]+)
  | (?P<lcomment>(?:\#|//)[^\n]*)
  | (?P<bcomment>/\*.*?\*/)
  | (?P<bopen>/\*)
  | (?P<string>"(?:[^"\\\n]|\\.)*")
  | (?P<sopen>")
  | (?P<hexfloat>&[0-9A-Fa-f]{8})(?![\w.])
  | (?P<number>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?f?)(?![\w.])
  | (?P<word>[A-Za-z0-9_.]+)
  | (?P<include>@include)(?![\w])
  | (?P<sym>[{}()\[\]:,;])
  | (?P<bad>.)
    """,
    re.VERBOSE | re.DOTALL,
)

_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}
_ESCAPE_RE = re.compile(r"\\(.)")

# Signatures used to detect binary or encrypted SII variants.
_ENCODED_SIGNATURES = (b"ScsC", b"BSII", b"3nK")


@dataclass(frozen=True, slots=True)
class Lexeme:
    kind: str
    value: str          # decoded text (strings unquoted and unescaped)
    path: Path
    line: int
    chain: tuple[IncludeSite, ...] = ()


def _unescape(body: str) -> str:
    # Unknown escapes are kept verbatim (backslash included) rather than guessed at.
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(0)), body)


def check_encoded(data: bytes, path: Path, chain: tuple[IncludeSite, ...] = ()) -> None:
    """Raise SiiEncodedError if `data` is not plain SII text."""
    head = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    stripped = head.lstrip()
    for sig in _ENCODED_SIGNATURES:
        if stripped.startswith(sig):
            raise SiiEncodedError(path, sig.decode("ascii"), chain)
    if b"\x00" in data:
        raise SiiEncodedError(path, "contains NUL bytes", chain)


def decode_text(data: bytes, path: Path, chain: tuple[IncludeSite, ...] = ()) -> str:
    check_encoded(data, path, chain)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        line = data[: exc.start].count(b"\n") + 1
        raise SiiSyntaxError(f"not valid UTF-8 ({exc.reason})", path, line, chain) from None


def tokenize(text: str, path: Path, chain: tuple[IncludeSite, ...] = ()) -> list[Lexeme]:
    """Split SII text into lexemes. Comments and whitespace are dropped."""
    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line_of(pos: int) -> int:
        return bisect.bisect_right(line_starts, pos)

    out: list[Lexeme] = []
    append = out.append
    for m in _MASTER.finditer(text):
        kind = m.lastgroup
        if kind in ("ws", "lcomment", "bcomment"):
            continue
        pos = m.start()
        if kind == "string":
            append(Lexeme(STRING, _unescape(m.group(kind)[1:-1]), path, line_of(pos), chain))
        elif kind in ("word", "number", "hexfloat", "sym"):
            append(Lexeme(kind, m.group(kind), path, line_of(pos), chain))
        elif kind == "include":
            append(Lexeme(INCLUDE, "@include", path, line_of(pos), chain))
        elif kind == "bopen":
            raise SiiSyntaxError("unterminated /* block comment", path, line_of(pos), chain)
        elif kind == "sopen":
            raise SiiSyntaxError("unterminated string", path, line_of(pos), chain)
        else:  # bad
            raise SiiSyntaxError(f"unexpected character {m.group(kind)!r}", path, line_of(pos), chain)
    append(Lexeme(EOF, "", path, line_of(len(text)), chain))
    return out
