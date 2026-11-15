# SPDX-License-Identifier: GPL-3.0-or-later
"""Build ConverterPIX commands and parse its output without subprocesses or Blender.

Command and output behavior:
- Every `-b` mounts one archive; later archives override earlier ones.
- Writing modes MUST get `-e`: without it output goes to `<archive>_exp` in the game folder.
- Extract and convert modes exit 0 even on failure. Errors print `<error> [...] ...`
  (not `<e>`), and a missing file for -extract_f prints only "Unable to open file for read:".
  Success is therefore also judged by the expected output file existing (caller's job).
- `-listdir` prints `[D] /path` / `[F] /path`, optionally `[F] (encrypted) /path`, then `-- done --`.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from os import PathLike

from .path_safety import normalize_game_path

StrPath = str | PathLike[str]


class ConverterArgError(ValueError):
    """A ConverterPIX command could not be built safely."""


def enabled_archive_paths(entries: Iterable[tuple[str, bool]]) -> list[str]:
    """Return enabled archives in configured priority order, highest priority first."""
    return [path for path, enabled in entries if enabled and path]


def converter_mount_order(priority_paths: Sequence[StrPath]) -> list[StrPath]:
    """Translate highest-priority-first UI order to ConverterPIX's mount order.

    ConverterPIX gives later ``-b`` archives precedence, so the lowest-priority archive
    must be mounted first.
    """
    return list(reversed(priority_paths))


# Archive order
def default_archive_order(file_names: Iterable[str]) -> list[str]:
    """Order .scs file names as decided on 2026-11-11: core.scs, base*.scs, def.scs, dlc_*.scs.

    Anything else in the game folder (effect.scs, locale.scs, version.scs, ...) is left out.
    Within base*/dlc_* the order is alphabetical (base.scs sorts first).
    """
    names = sorted({n for n in file_names if n.lower().endswith(".scs")}, key=str.lower)
    low = {n.lower(): n for n in names}
    out: list[str] = []
    if "core.scs" in low:
        out.append(low["core.scs"])
    out += [n for n in names if n.lower().startswith("base") and n.lower() != "core.scs"]
    if "def.scs" in low:
        out.append(low["def.scs"])
    out += [n for n in names if n.lower().startswith("dlc_")]
    return out


# Command building
def base_args(archives: Sequence[StrPath]) -> list[str]:
    if not archives:
        raise ConverterArgError("no archives given")
    out: list[str] = []
    for a in archives:
        out += ["-b", str(a)]
    return out


def _game_path(path: str) -> str:
    norm = normalize_game_path(path)
    if norm is None:
        raise ConverterArgError(f"unsafe or invalid game path: {path!r}")
    return norm


def _export(export: StrPath | None) -> list[str]:
    if export is None or str(export) == "":
        raise ConverterArgError("an export folder (-e) is required; without it ConverterPIX writes "
                                "into the game folder")
    return ["-e", str(export)]


def listdir_args(archives: Sequence[StrPath], game_dir: str, recursive: bool = False) -> list[str]:
    return base_args(archives) + ["-listdir_r" if recursive else "-listdir", _game_path(game_dir)]


def extract_dir_args(archives: Sequence[StrPath], game_dir: str, export: StrPath) -> list[str]:
    return base_args(archives) + ["-extract_d", _game_path(game_dir)] + _export(export)


def extract_file_args(archives: Sequence[StrPath], game_file: str, export: StrPath) -> list[str]:
    return base_args(archives) + ["-extract_f", _game_path(game_file)] + _export(export)


def model_arg(game_path: str) -> str:
    """"/vehicle/truck/x/cabin.pmd" -> "/vehicle/truck/x/cabin" (validated)."""
    norm = _game_path(game_path)
    head, _, tail = norm.rpartition("/")
    if tail.lower().endswith(".pmd"):
        tail = tail[:-4]
    if not tail:
        raise ConverterArgError(f"no model name in {game_path!r}")
    return f"{head}/{tail}"


def model_args(archives: Sequence[StrPath], game_path: str, export: StrPath) -> list[str]:
    return base_args(archives) + ["-m", model_arg(game_path)] + _export(export)


def tobj_args(archives: Sequence[StrPath], game_path: str, export: StrPath) -> list[str]:
    """Convert an archived SCS texture object into DDS mid-formats."""
    return base_args(archives) + ["-t", _game_path(game_path)] + _export(export)


# Output parsing
@dataclass(frozen=True)
class ListEntry:
    is_dir: bool
    path: str
    encrypted: bool = False


def parse_listdir(stdout: str) -> list[ListEntry]:
    out: list[ListEntry] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if len(line) < 4 or line[0] != "[" or line[2] != "]" or line[1] not in "DF":
            continue
        rest = line[3:].lstrip()
        encrypted = rest.startswith("(encrypted)")
        if encrypted:
            rest = rest[len("(encrypted)"):].lstrip()
        if rest.startswith("/"):
            out.append(ListEntry(line[1] == "D", rest, encrypted))
    return out


def error_lines(stdout: str) -> list[str]:
    return [ln.strip() for ln in stdout.splitlines() if ln.lstrip().startswith("<error>")]


def warning_lines(stdout: str) -> list[str]:
    return [ln.strip() for ln in stdout.splitlines() if ln.lstrip().startswith("<warning>")]


_SOFT_FAILURES = ("Unable to open file for read:", "Failed to load:", "Unable to open file to read:")


def failure_reason(returncode: int, stdout: str) -> str | None:
    """Why a run failed, or None if stdout and exit code look fine.

    Callers must still check that the expected output exists: some failures print nothing.
    """
    if returncode != 0:
        return f"exit code {returncode}"
    errs = error_lines(stdout)
    if errs:
        return errs[0]
    for ln in stdout.splitlines():
        s = ln.strip()
        if s.startswith(_SOFT_FAILURES):
            return s
    return None


def tail(stdout: str, lines: int = 40) -> str:
    return "\n".join(stdout.splitlines()[-lines:])
