# SPDX-License-Identifier: GPL-3.0-or-later
"""Map game paths into the conversion cache while enforcing path containment."""
from __future__ import annotations

from pathlib import Path

from ..path_safety import join_game_path, normalize_game_path


def model_key(game_path: str) -> str | None:
    """Canonical manifest key for a model: normalised game path with its .pmd extension."""
    return normalize_game_path(game_path)


def resolve_model(game_path: str, base: Path) -> Path | None:
    """"/vehicle/truck/x/cabin.pmd" -> <base>/vehicle/truck/x/cabin.pim, or None if unsafe."""
    target = join_game_path(base, game_path)
    if target is None:
        return None
    return target.with_suffix(".pim") if target.suffix.lower() == ".pmd" else target.with_name(target.name + ".pim")
