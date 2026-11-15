# SPDX-License-Identifier: GPL-3.0-or-later
"""Run ConverterPIX and manage its on-disk cache.

Every ConverterPIX run in the addon goes through this module. It does not import bpy:
callers pass in Blender-specific locations (user resource folder, addon config folder), so
the module can be tested with a fake converter.

Cache layout:
    <cache_root>/defs/def/vehicle/truck/...   extracted defs (one truck at a time + shared *.sui)
    <cache_root>/base/...                     converted models and textures (SCS project base)
    <cache_root>/tmp/                         per-run scratch, emptied on start
    <cache_root>/manifest.json                archives fingerprint, extractions, conversions
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from ..core import converter_pix as cpx
from ..core.path_safety import is_within, join_game_path, normalize_game_path
from ..core.truck.paths import model_key, resolve_model

MANIFEST_VERSION = 1
TRUCK_DEFS_DIR = "/def/vehicle/truck"


def exe_name() -> str:
    return "converter_pix.exe" if sys.platform == "win32" else "converter_pix"


# Executable lookup
def find_converter(pref_path: str | None = None, blender_user_dir: Path | None = None,
                   addon_config_dir: Path | None = None) -> Path | None:
    """Return the first existing executable from preferences, the wrapper, or this add-on.

    `blender_user_dir` is the Blender user resource directory. `addon_config_dir` is this
    add-on's configuration directory.
    """
    candidates: list[Path] = []
    if pref_path:
        candidates.append(Path(pref_path))
    if blender_user_dir is not None:
        candidates.append(Path(blender_user_dir) / "config" / "ConverterPIXWrapper" / exe_name())
    if addon_config_dir is not None:
        candidates.append(Path(addon_config_dir) / exe_name())
    for c in candidates:
        if c.is_file():
            return c
    return None


def default_archives(game_dir: Path) -> list[Path]:
    """Return supported game archives in the add-on's default priority order."""
    names = [p.name for p in Path(game_dir).iterdir() if p.is_file()]
    return [Path(game_dir) / n for n in cpx.default_archive_order(names)]


# Settings and results
@dataclass
class ConverterSettings:
    command: list[str]              # e.g. [".../converter_pix.exe"], or [python, fake.py] in tests
    archives: list[Path]            # ticked archives, in mount order
    cache_root: Path
    jobs: int = 1
    timeout: float = 900.0
    custom_top_level_paths: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    args: list[str]
    returncode: int
    stdout: str
    seconds: float

    @property
    def failure(self) -> str | None:
        return cpx.failure_reason(self.returncode, self.stdout)


@dataclass
class ExtractResult:
    brand_model: str
    extracted: bool                 # False: already cached
    seconds: float = 0.0
    files: int = 0
    shared_files: int = 0
    shared_seconds: float = 0.0
    error: str | None = None


@dataclass
class ConvertReport:
    converted: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)          # (game path, reason)
    failed_before: list[tuple[str, str]] = field(default_factory=list)   # skipped: failed in an earlier run
    rejected: list[str] = field(default_factory=list)                    # unsafe game paths
    shared_kept: int = 0
    seconds: float = 0.0
    stale: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{len(self.converted)} converted, {len(self.cached)} cached, {len(self.failed)} failed, "
                f"{len(self.failed_before)} failed earlier (not retried), {len(self.rejected)} rejected, "
                f"{self.seconds:.1f}s")


class ConverterError(RuntimeError):
    pass


# Manifest
def archive_fingerprint(archives: Sequence[Path]) -> list[dict]:
    out = []
    for a in archives:
        try:
            st = Path(a).stat()
            out.append({"path": str(a), "size": st.st_size, "mtime_ns": st.st_mtime_ns})
        except OSError:
            out.append({"path": str(a), "size": None, "mtime_ns": None})
    return out


def fingerprint_hash(fingerprint: list[dict]) -> str:
    return hashlib.sha1(json.dumps(fingerprint, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"version": MANIFEST_VERSION, "archives": None, "defs": {}, "shared_sui": None,
                           "files": {}, "models": {}}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("version") == MANIFEST_VERSION:
                    self.data.update(loaded)
            except (OSError, ValueError):
                pass   # a corrupt manifest is treated as empty; files on disk are re-validated
        self._lock = threading.Lock()

    @property
    def models(self) -> dict:
        return self.data["models"]

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)

    def stale_reasons(self, current: list[dict]) -> list[str]:
        """Explain why cached data differs from the current archive set."""
        old = self.data.get("archives")
        if old is None:
            return []
        reasons: list[str] = []
        old_by = {a["path"]: a for a in old}
        new_by = {a["path"]: a for a in current}
        for p in new_by.keys() - old_by.keys():
            reasons.append(f"archive added: {p}")
        for p in old_by.keys() - new_by.keys():
            reasons.append(f"archive removed: {p}")
        for p in new_by.keys() & old_by.keys():
            if (new_by[p]["size"], new_by[p]["mtime_ns"]) != (old_by[p]["size"], old_by[p]["mtime_ns"]):
                reasons.append(f"archive changed: {p}")
        if not reasons and [a["path"] for a in old] != [a["path"] for a in current]:
            reasons.append("archive order changed")
        return sorted(reasons)


# The converter
class Converter:
    def __init__(self, settings: ConverterSettings, log: Callable[[str], None] | None = None):
        if not settings.command:
            raise ConverterError("Missing converter script.")
        self.s = settings
        self.root = Path(settings.cache_root)
        self.defs = self.root / "defs"
        self.base = self.root / "base"
        self.tmp = self.root / "tmp"
        self.manifest = Manifest(self.root / "manifest.json")
        self.log = log or (lambda msg: None)
        self._move_lock = threading.Lock()
        self._truck_def_sources: dict[str, str] = {}

    # State
    def fingerprint(self) -> list[dict]:
        return archive_fingerprint(self.s.archives)

    def stale_reasons(self) -> list[str]:
        return self.manifest.stale_reasons(self.fingerprint())

    def is_stale(self) -> bool:
        return bool(self.stale_reasons())

    def _claim_fingerprint(self) -> None:
        if self.manifest.data.get("archives") is None:
            self.manifest.data["archives"] = self.fingerprint()

    def clear_cache(self) -> None:
        """Delete cached definitions, converted models, temporary files, and manifest records."""
        for d in (self.defs, self.base, self.tmp):
            if d.exists():
                if not is_within(self.root, d):
                    raise ConverterError(f"Cannot delete {d} as it is outside the cache root.")
                shutil.rmtree(d)
        manifest_path = self.root / "manifest.json"
        manifest_path.unlink(missing_ok=True)
        self.manifest = Manifest(manifest_path)
        self.manifest.save()

    # Running
    def run(self, args: list[str]) -> RunResult:
        cmd = list(self.s.command) + args
        self.log("converter_pix " + " ".join(args[-4:]))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  timeout=self.s.timeout, creationflags=flags)
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode("utf-8", errors="replace")
            return RunResult(args, -1, out + f"\n<error> [wrapper] timed out after {self.s.timeout}s",
                             time.perf_counter() - t0)
        except OSError as exc:
            raise ConverterError(f"Cannot run {cmd[0]}: {exc}") from exc
        return RunResult(args, proc.returncode, proc.stdout.decode("utf-8", errors="replace"),
                         time.perf_counter() - t0)

    def _scratch(self) -> Path:
        d = self.tmp / f"job-{uuid.uuid4().hex[:12]}"
        d.mkdir(parents=True)
        return d

    def reset_tmp(self) -> None:
        if self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)

    # Listing
    def list_dir(self, game_dir: str, recursive: bool = False) -> list[cpx.ListEntry]:
        if game_dir == "/":
            args = cpx.base_args(self.s.archives) + ["-listdir_r" if recursive else "-listdir", "/"]
        else:
            args = cpx.listdir_args(self.s.archives, game_dir, recursive=recursive)
        res = self.run(args)
        if res.failure:
            raise ConverterError(f"Listing {game_dir} failed: {res.failure}; {cpx.tail(res.stdout)}")
        return cpx.parse_listdir(res.stdout)

    @staticmethod
    def _top_level_pattern(value: str) -> str:
        segment = (value or "").strip().replace("\\", "/").strip("/")
        if not segment or "/" in segment or segment in (".", "..") or ":" in segment or "\x00" in segment:
            raise ConverterError(f"Invalid top-level path: {value}.")
        return "/" + segment

    def custom_top_level_roots(self) -> list[str]:
        """Expand configured top-level folder patterns against the mounted archive root."""
        patterns = [self._top_level_pattern(value) for value in self.s.custom_top_level_paths if value.strip()]
        if not patterns:
            return []
        root_dirs = [entry.path.rstrip("/") for entry in self.list_dir("/") if entry.is_dir]
        roots: list[str] = []
        for pattern in patterns:
            for root in root_dirs:
                if fnmatchcase(root.casefold(), pattern.casefold()) and root not in roots:
                    roots.append(root)
        return roots

    def _definition_roots(self) -> list[str]:
        return ["", *self.custom_top_level_roots()]

    @staticmethod
    def _under_root(root: str, logical_path: str) -> str:
        return f"{root}{logical_path}" if root else logical_path

    def list_trucks(self) -> list[str]:
        sources: dict[str, str] = {}
        for root in self._definition_roots():
            directory = self._under_root(root, TRUCK_DEFS_DIR)
            try:
                entries = self.list_dir(directory)
            except ConverterError as exc:
                if "readDir returned null!" in str(exc):
                    continue
                raise
            for entry in entries:
                if entry.is_dir:
                    sources.setdefault(entry.path.rsplit("/", 1)[-1], entry.path)
        self._truck_def_sources = sources
        return sorted(sources)

    # Defs
    def truck_defs_dir(self, brand_model: str) -> Path:
        target = join_game_path(self.defs, f"{TRUCK_DEFS_DIR}/{brand_model}")
        if target is None or "/" in brand_model:
            raise ConverterError(f"Invalid truck name: {brand_model}.")
        return target

    def extract_truck(self, brand_model: str, force: bool = False) -> ExtractResult:
        """Extract one truck's defs, plus the shared /def/vehicle/truck/*.sui files, into defs/."""
        target = self.truck_defs_dir(brand_model)
        source_dir = self._truck_def_sources.get(brand_model)
        if source_dir is None:
            self.list_trucks()
            source_dir = self._truck_def_sources.get(brand_model)
        rec = self.manifest.data["defs"].get(brand_model)
        fp = fingerprint_hash(self.fingerprint())
        result = ExtractResult(brand_model, extracted=False)
        if source_dir is None:
            result.error = f"No valid truck definitions were found for {brand_model}."
            return result

        # Re-extract when forced, never extracted, missing on disk, or made from other archives.
        if force or rec is None or rec.get("fp") != fp or rec.get("source") != source_dir or not target.is_dir():
            scratch = self._scratch()
            try:
                res = self.run(cpx.extract_dir_args(self.s.archives, source_dir, scratch))
                produced = join_game_path(scratch, source_dir)
                files = [p for p in produced.rglob("*") if p.is_file()] if produced and produced.is_dir() else []
                reason = res.failure or (None if files else "no files extracted")
                if reason:
                    result.error = f"{reason}\n{cpx.tail(res.stdout)}"
                    return result
                if target.exists():
                    shutil.rmtree(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(produced), str(target))
                result.extracted, result.seconds, result.files = True, res.seconds, len(files)
                self.manifest.data["defs"][brand_model] = {"files": len(files), "seconds": round(res.seconds, 2),
                                                           "fp": fp, "source": source_dir,
                                                           "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            finally:
                shutil.rmtree(scratch, ignore_errors=True)

        shared = self.manifest.data.get("shared_sui")
        roots = self._definition_roots()
        if force or shared is None or shared.get("fp") != fp or shared.get("roots") != roots:
            result.shared_files, result.shared_seconds = self._extract_shared_sui(fp, roots)
        self._claim_fingerprint()
        self.manifest.save()
        return result

    def _extract_shared_sui(self, fp: str, roots: list[str]) -> tuple[int, float]:
        t0 = time.perf_counter()
        logical_sources: dict[str, str] = {}
        for root in roots:
            directory = self._under_root(root, TRUCK_DEFS_DIR)
            try:
                entries = self.list_dir(directory)
            except ConverterError as exc:
                if "readDir returned null!" in str(exc):
                    continue
                raise
            for entry in entries:
                if not entry.is_dir and entry.path.lower().endswith(".sui"):
                    logical_path = entry.path[len(root):] if root else entry.path
                    logical_sources.setdefault(logical_path, entry.path)
        done: list[str] = []
        failed: list[str] = []
        for logical_path, source_path in logical_sources.items():
            dest = join_game_path(self.defs, logical_path)
            if dest is None:
                failed.append(logical_path)
                continue
            scratch = self._scratch()
            try:
                res = self.run(cpx.extract_file_args(self.s.archives, source_path, scratch))
                produced = join_game_path(scratch, source_path)
                if res.failure or produced is None or not produced.is_file():
                    failed.append(logical_path)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(produced, dest)
                done.append(logical_path)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        seconds = time.perf_counter() - t0
        self.manifest.data["shared_sui"] = {"files": sorted(done), "failed": sorted(failed),
                                            "seconds": round(seconds, 2), "fp": fp, "roots": roots}
        return len(done), seconds

    def extract_file(self, game_path: str, force: bool = False) -> Path:
        """Extract one def file (e.g. a dealer config) into defs/. Returns its path in the cache.

        Re-extracted when forced, missing, or last extracted from a different archive set."""
        dest = join_game_path(self.defs, game_path)
        key = normalize_game_path(game_path)
        if dest is None or key is None:
            raise ConverterError(f"Game path is unsafe: {game_path}.")
        fp = fingerprint_hash(self.fingerprint())
        files = self.manifest.data.setdefault("files", {})
        rec = files.get(key)
        if not force and dest.is_file() and rec and rec.get("fp") == fp:
            return dest
        scratch = self._scratch()
        try:
            res = self.run(cpx.extract_file_args(self.s.archives, key, scratch))
            produced = join_game_path(scratch, key)
            if res.failure or produced is None or not produced.is_file():
                raise ConverterError(f"Extracting {key} failed: {res.failure or 'no file written'}; "
                                     f"{cpx.tail(res.stdout)}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(produced, dest)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        files[key] = {"fp": fp, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._claim_fingerprint()
        self.manifest.save()
        return dest

    def list_dealer_configs(self, brand: str) -> list[str]:
        """Game paths of one brand's dealer configs under truck_dealer."""
        logical_dir = f"/def/vehicle/truck_dealer/{brand}"
        sources: dict[str, str] = {}
        for root in self._definition_roots():
            try:
                entries = self.list_dir(self._under_root(root, logical_dir), recursive=True)
            except ConverterError as exc:
                if "readDir returned null!" in str(exc):
                    continue
                raise
            for entry in entries:
                if not entry.is_dir and entry.path.lower().endswith(".sii"):
                    logical_path = entry.path[len(root):] if root else entry.path
                    sources.setdefault(logical_path, entry.path)
        return sorted(sources.values())

    # Models
    def convert_models(self, game_paths: Sequence[str], retry_failed: bool = False,
                       progress: Callable[[int, int, str], None] | None = None) -> ConvertReport:
        """Convert every model not already in the cache. Never raises for a single model."""
        t0 = time.perf_counter()
        report = ConvertReport(stale=self.stale_reasons())
        stale = bool(report.stale)
        fp = fingerprint_hash(self.fingerprint())
        todo: list[str] = []
        for gp in dict.fromkeys(game_paths):
            key = model_key(gp)
            pim = resolve_model(gp, self.base) if key else None
            if key is None or pim is None:
                report.rejected.append(gp)
                continue
            rec = self.manifest.models.get(key)
            if rec and rec.get("status") == "ok" and pim.is_file():
                report.cached.append(key)      # stale caches are still used until the user clears them
            elif rec and rec.get("status") == "failed" and not retry_failed and rec.get("fp") == fp:
                report.failed_before.append((key, rec.get("reason", "")))
            else:
                todo.append(key)

        self.reset_tmp()
        total = len(todo)
        done = 0

        def record(key: str, result: tuple[str | None, list[str], int, float]) -> None:
            nonlocal done
            reason, outputs, kept, seconds = result
            done += 1
            if reason is None:
                report.converted.append(key)
                report.shared_kept += kept
                self.manifest.models[key] = {"status": "ok", "seconds": round(seconds, 2), "fp": fp,
                                             "outputs": outputs, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            else:
                report.failed.append((key, reason))
                self.manifest.models[key] = {"status": "failed", "reason": reason.splitlines()[0][:300], "fp": fp,
                                             "stdout_tail": reason[-2000:], "seconds": round(seconds, 2),
                                             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            # Called by the caller thread, including when conversion workers are enabled.
            if progress:
                progress(done, total, key)

        jobs = max(1, min(int(self.s.jobs), 4))
        if jobs == 1:
            for key in todo:
                record(key, self._convert_one(key, overwrite=stale))
        else:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = {pool.submit(self._convert_one, key, stale): key for key in todo}
                for future in as_completed(futures):
                    record(futures[future], future.result())

        if todo:
            self._claim_fingerprint()
        self.manifest.save()
        self.reset_tmp()
        report.seconds = time.perf_counter() - t0
        return report

    def _convert_one(self, key: str, overwrite: bool) -> tuple[str | None, list[str], int, float]:
        """Convert one model into a scratch folder, then move results into base/.
        Returns (failure reason or None, output paths, shared files kept, seconds)."""
        scratch = self._scratch()
        try:
            try:
                res = self.run(cpx.model_args(self.s.archives, key, scratch))
            except ConverterError as exc:
                return str(exc), [], 0, 0.0
            expected = resolve_model(key, scratch)
            reason = res.failure
            if reason is None and (expected is None or not expected.is_file()):
                reason = f"no .pim written for {key}"
            if reason is not None:
                return f"{reason}\n{cpx.tail(res.stdout)}", [], 0, res.seconds
            outputs: list[str] = []
            kept = 0
            with self._move_lock:
                for src in sorted(p for p in scratch.rglob("*") if p.is_file()):
                    rel = src.relative_to(scratch).as_posix()
                    dest = join_game_path(self.base, "/" + rel)
                    if dest is None:
                        continue           # never write outside base/
                    if dest.exists() and not overwrite and dest.suffix.lower() not in (".pim", ".pit", ".pic"):
                        kept += 1          # shared texture/material already cached
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(src, dest)
                    outputs.append("/" + rel)
            return None, outputs, kept, res.seconds
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # Status
    def model_status(self, game_path: str) -> str:
        key = normalize_game_path(game_path)
        rec = self.manifest.models.get(key) if key else None
        pim = resolve_model(game_path, self.base)
        if rec and rec.get("status") == "ok" and pim is not None and pim.is_file():
            return "cached"
        if rec and rec.get("status") == "failed":
            return "failed"
        return "missing"
