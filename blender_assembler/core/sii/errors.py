# SPDX-License-Identifier: GPL-3.0-or-later
"""SII error types. Every error carries a file path, a line number and the include chain."""
from __future__ import annotations

from pathlib import Path

# One include site: the file and line of an `@include` directive.
IncludeSite = tuple[Path, int]


class SiiError(Exception):
    """Base class for all SII reading errors."""

    def __init__(self, message: str, path: Path | None = None, line: int = 0,
                 chain: tuple[IncludeSite, ...] = ()):
        self.message = message
        self.path = path
        self.line = line
        self.chain = chain
        super().__init__(self.__str__())

    def location(self) -> str:
        where = f"{self.path}:{self.line}" if self.path is not None else "<unknown>"
        for inc_path, inc_line in reversed(self.chain):
            where += f"\n    included from {inc_path}:{inc_line}"
        return where

    def __str__(self) -> str:
        return f"{self.location()}: {self.message}"


class SiiSyntaxError(SiiError):
    """Text that does not follow the SII grammar."""


class SiiIncludeError(SiiError):
    """An @include that cannot be resolved, or an include cycle."""


class SiiEncodedError(SiiError):
    """A binary or encrypted SII file. It must be decoded with an external tool first."""

    def __init__(self, path: Path, signature: str, chain: tuple[IncludeSite, ...] = ()):
        super().__init__(
            f"file is encoded ({signature}), not SII text; decode it first "
            f"(e.g. with SII_Decrypt) and try again",
            path, 0, chain,
        )
