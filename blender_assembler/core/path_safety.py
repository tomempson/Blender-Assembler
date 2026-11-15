# SPDX-License-Identifier: GPL-3.0-or-later
"""Containment checks for paths that come from game or mod data.

Def files name other files by game path ("/vehicle/truck/x/cabin.pmd", "/def/...sii").
Mod archives are untrusted, so a game path must never reach outside the folder it is
joined to: no `..` or `.` segments, no drive letters, no backslashes, no NUL bytes.
Pure Python, no filesystem calls.
"""
from __future__ import annotations

import os
from pathlib import Path

_BAD_CHARS = (":", "\\", "\x00")


def normalize_game_path(game_path: str) -> str | None:
    """Return "/a/b/c" for a safe absolute game path, or None if it is unsafe.

    Empty segments ("//") are collapsed; ".", ".." or any segment with ':' '\\' or NUL is rejected.
    """
    if not isinstance(game_path, str) or not game_path.startswith("/"):
        return None
    segments = [s for s in game_path.split("/") if s]
    if not segments:
        return None
    for seg in segments:
        if seg in (".", "..") or any(c in seg for c in _BAD_CHARS):
            return None
    return "/" + "/".join(segments)


def is_safe_segment(name: str) -> bool:
    """A single path component (folder or file name) with nothing that could navigate."""
    return bool(name) and name not in (".", "..") and "/" not in name and not any(c in name for c in _BAD_CHARS)


def join_game_path(root: Path, game_path: str) -> Path | None:
    """`root` joined with a game path, or None if the game path is unsafe."""
    norm = normalize_game_path(game_path)
    if norm is None:
        return None
    return Path(root).joinpath(*norm[1:].split("/"))


def is_within(root: Path, path: Path) -> bool:
    """True if `path` (after normalising `..`) lies inside `root`. No symlink resolution."""
    r = os.path.normcase(os.path.abspath(root))
    p = os.path.normcase(os.path.abspath(path))
    try:
        return os.path.commonpath([r, p]) == r
    except ValueError:      # different drives on Windows
        return False
