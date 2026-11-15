# SPDX-License-Identifier: GPL-3.0-or-later
"""@include expansion at lexeme level.

`@include "file"` is textual inclusion. Relative paths resolve against the including
file's directory. Paths starting with `/` resolve against the defs root: the folder
that contains `def/` (so "/def/vehicle/truck/x.sui" -> <defs_root>/def/vehicle/truck/x.sui).
Each spliced lexeme keeps its own file/line plus the chain of include sites.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from ..path_safety import is_within, join_game_path
from .errors import IncludeSite, SiiIncludeError
from .lexer import EOF, INCLUDE, STRING, Lexeme, decode_text, tokenize


def _key(path: Path) -> str:
    """Normalised absolute path for caching and cycle detection, without filesystem calls."""
    return os.path.normcase(os.path.abspath(path))


class IncludeResolver:
    """Expands includes for one or many files. Caches lexed fragments by path."""

    def __init__(self, defs_root: Path | None):
        self.defs_root = defs_root
        self._cache: dict[str, list[Lexeme]] = {}

    def resolve_path(self, ref: str, including_file: Path, site: Lexeme) -> Path:
        """Resolve an include reference. With a defs root, the result must stay inside it
        (def files may come from untrusted mod archives)."""
        if ref.startswith("/"):
            if self.defs_root is None:
                raise SiiIncludeError(f"absolute include {ref!r} but no defs root was given",
                                      site.path, site.line, site.chain)
            target = join_game_path(self.defs_root, ref)
            if target is None:
                raise SiiIncludeError(f"include path {ref!r} is not a safe game path "
                                      f"('..', '.', drive letters and backslashes are not allowed)",
                                      site.path, site.line, site.chain)
            return target
        target = including_file.parent / ref
        if self.defs_root is not None and not is_within(self.defs_root, target):
            raise SiiIncludeError(f"include path {ref!r} points outside the defs root {self.defs_root}",
                                  site.path, site.line, site.chain)
        return target

    def _lex_file(self, path: Path, chain: tuple[IncludeSite, ...], site: Lexeme | None) -> list[Lexeme]:
        key = _key(path)
        cached = self._cache.get(key)
        if cached is None:
            try:
                data = path.read_bytes()
            except OSError as exc:
                if site is None:
                    raise SiiIncludeError(f"cannot read file: {exc.strerror}", path, 0, chain) from None
                raise SiiIncludeError(f"included file not found or unreadable: {path} ({exc.strerror})",
                                      site.path, site.line, site.chain) from None
            cached = tokenize(decode_text(data, path, chain), path)
            if site is not None:  # cache shared fragments only, not every top-level file
                self._cache[key] = cached
        if not chain:
            return cached
        return [replace(lx, chain=chain) for lx in cached]

    def expand_file(self, path: Path) -> list[Lexeme]:
        """Lex `path` and splice all includes, recursively. Ends with one EOF lexeme."""
        return self._expand(self._lex_file(path, (), None), path, (), (_key(path),))

    def expand_lexemes(self, lexemes: list[Lexeme], path: Path) -> list[Lexeme]:
        return self._expand(lexemes, path, (), (_key(path),))

    def _expand(self, lexemes: list[Lexeme], path: Path, chain: tuple[IncludeSite, ...],
                stack: tuple[str, ...]) -> list[Lexeme]:
        if not any(lx.kind == INCLUDE for lx in lexemes):
            return lexemes  # most files have no includes
        out: list[Lexeme] = []
        i, n = 0, len(lexemes)
        while i < n:
            lx = lexemes[i]
            if lx.kind != INCLUDE:
                out.append(lx)
                i += 1
                continue
            if i + 1 >= n or lexemes[i + 1].kind != STRING:
                raise SiiIncludeError("@include must be followed by a quoted path", lx.path, lx.line, lx.chain)
            target = self.resolve_path(lexemes[i + 1].value, path, lx)
            resolved = _key(target)
            site_chain = chain + ((lx.path, lx.line),)
            if resolved in stack:
                cycle = " -> ".join(stack + (resolved,))
                raise SiiIncludeError(f"include cycle: {cycle}", lx.path, lx.line, lx.chain)
            inner = self._lex_file(target, site_chain, lx)
            inner = [x for x in inner if x.kind != EOF]
            out.extend(self._expand(inner, target, site_chain, stack + (resolved,)))
            i += 2
        return out
