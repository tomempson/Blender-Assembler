# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the repository's automated checks, package validation, and optional Blender smoke test."""
from __future__ import annotations

import argparse
import compileall
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
BLENDER_TEST = ROOT / "tests" / "blender" / "run_tests.py"
REAL_GAME_TEST = ROOT / "tests" / "blender" / "real_game_e2e.py"
BLEND_VERIFY_TEST = ROOT / "tests" / "blender" / "verify_saved_blend.py"
REQUIRED_ZIP_ENTRIES = {
    "LICENSE",
    "blender_assembler/__init__.py",
    "blender_assembler/blender/operators.py",
    "blender_assembler/core/truck/plan.py",
    "blender_assembler/assets/icon_missing.png",
}


def run_command(label: str, command: list[str], *, timeout: int = 300) -> bool:
    print(f"\n=== {label} ===", flush=True)
    print("$ " + subprocess.list2cmdline(command), flush=True)
    try:
        result = subprocess.run(command, cwd=ROOT, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}", flush=True)
        return False
    if result.returncode:
        print(f"FAIL: command exited with {result.returncode}", flush=True)
        return False
    print("PASS", flush=True)
    return True


def compile_sources() -> bool:
    print("\n=== Python syntax compilation ===", flush=True)
    ok = True
    for relative in ("blender_assembler", "tests", "tools"):
        target = ROOT / relative
        if target.is_dir() and not compileall.compile_dir(str(target), quiet=1, force=True):
            print(f"FAIL: compilation failed under {relative}", flush=True)
            ok = False
    if ok:
        print("PASS", flush=True)
    return ok


def check_package() -> bool:
    print("\n=== Add-on ZIP build and contents ===", flush=True)
    with tempfile.TemporaryDirectory(prefix="blender-assembler-tests-") as temp_dir:
        output = Path(temp_dir) / "blender_assembler.zip"
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "build_zip.py"), "--output", str(output)],
            cwd=ROOT, check=False,
        )
        if result.returncode or not output.is_file():
            print("FAIL: ZIP builder did not produce an archive", flush=True)
            return False
        try:
            with ZipFile(output) as archive:
                corrupt = archive.testzip()
                names = set(archive.namelist())
                missing = REQUIRED_ZIP_ENTRIES - names
                unexpected = [name for name in names
                              if name != "LICENSE" and not name.startswith("blender_assembler/")
                              or "__pycache__" in name or name.endswith(".pyc")]
        except Exception as exc:
            print(f"FAIL: cannot read add-on ZIP: {exc}", flush=True)
            return False
        if corrupt:
            print(f"FAIL: corrupt ZIP member: {corrupt}", flush=True)
            return False
        if missing:
            print("FAIL: missing ZIP members: " + ", ".join(sorted(missing)), flush=True)
            return False
        if unexpected:
            print("FAIL: unexpected ZIP members: " + ", ".join(sorted(unexpected)), flush=True)
            return False
        print(f"PASS: {len(names)} add-on files packaged", flush=True)
        return True


def find_blender(explicit: str | None) -> str | None:
    candidates = [explicit, os.environ.get("BLENDER_EXECUTABLE"), shutil.which("blender"),
                  shutil.which("blender.exe")]
    if os.name == "nt":
        candidates.extend((
            r"C:\Program Files\Blender Foundation\Blender 5.3\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 5.4\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 5.5\blender.exe",
        ))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
        if candidate and shutil.which(candidate):
            return shutil.which(candidate)
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender-executable", help="Blender executable for the background add-on smoke test.")
    parser.add_argument("--require-blender", action="store_true",
                        help="Fail if the Blender smoke test cannot run.")
    parser.add_argument("--skip-blender", action="store_true",
                        help="Skip Blender even if an executable is available.")
    parser.add_argument("--game-dir", type=Path,
                        help="Run a real ETS2 dealer-truck conversion and assembly check.")
    parser.add_argument("--converter-executable", type=Path,
                        help="ConverterPIX executable for --game-dir.")
    parser.add_argument("--integration-dealer", default=
                        "/def/vehicle/truck_dealer/scania/scania_s2016_4x2_460.sii",
                        help="Dealer config game path for the real integration check.")
    parser.add_argument("--integration-cache-root", type=Path,
                        default=Path(os.environ.get(
                            "BLENDER_ASSEMBLER_TEST_CACHE_ROOT",
                            str(Path(tempfile.gettempdir()) / "blender-assembler-test-cache"))),
                        help="Parent directory for disposable real-integration cache/output files.")
    parser.add_argument("--require-integration", action="store_true",
                        help="Fail unless Blender, --game-dir, and --converter-executable are available.")
    return parser.parse_args()


def check_real_game(blender: str, game_dir: Path, converter: Path, dealer: str,
                    cache_root: Path) -> bool:
    print("\n=== Real ETS2 conversion and Blender assembly ===", flush=True)
    if not game_dir.is_dir():
        print(f"FAIL: game directory does not exist: {game_dir}", flush=True)
        return False
    if not converter.is_file():
        print(f"FAIL: ConverterPIX executable does not exist: {converter}", flush=True)
        return False
    if not (game_dir / "base.scs").is_file():
        print(f"FAIL: no base.scs found under {game_dir}", flush=True)
        return False

    cache_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="blender-assembler-e2e-", dir=cache_root) as temp_dir:
            disposable = Path(temp_dir)
            blend_file = disposable / "dealer_assembly.blend"
            command = [blender, "--background", "--factory-startup", "--python-exit-code", "1",
                       "--python", str(REAL_GAME_TEST), "--", "--game-dir", str(game_dir),
                       "--converter-executable", str(converter), "--cache-dir", str(disposable / "cache"),
                       "--output-blend", str(blend_file), "--dealer", dealer]
            if not run_command("Real dealer truck UI-operator flow", command, timeout=1800):
                return False
            if not blend_file.is_file():
                print("FAIL: integration run did not save its .blend file", flush=True)
                return False
            verify = [blender, "--background", "--factory-startup", "--python-exit-code", "1",
                      "--python", str(BLEND_VERIFY_TEST), "--", str(blend_file),
                      "Blender Assembler - scania.s_2016"]
            if not run_command("Reopen and inspect real assembled .blend", verify, timeout=180):
                return False
    except OSError as exc:
        print(f"FAIL: cannot create disposable integration workspace: {exc}", flush=True)
        return False
    return True


def main() -> int:
    args = parse_args()
    results: list[tuple[str, bool]] = []
    results.append(("Syntax compilation", compile_sources()))
    results.append(("Python test suite", run_command(
        "Pure-Python tests", [sys.executable, "-m", "pytest", "-q"])))
    results.append(("Add-on ZIP", check_package()))

    blender = None if args.skip_blender else find_blender(args.blender_executable)
    if blender:
        results.append(("Blender registration smoke", run_command(
            "Blender 5.3+ background smoke", [blender, "--background", "--factory-startup",
                                               "--python-exit-code", "1", "--python", str(BLENDER_TEST)],
            timeout=180)))
    else:
        reason = "explicitly skipped" if args.skip_blender else "Blender executable not found"
        if args.require_blender:
            results.append(("Blender registration smoke", False))
            print(f"\nFAIL: {reason}; pass --blender-executable or install Blender.", flush=True)
        else:
            print(f"\nSKIP: Blender registration smoke ({reason}).", flush=True)

    integration_args = (args.game_dir, args.converter_executable)
    have_integration = all(integration_args)
    if have_integration and blender:
        results.append(("Real ETS2 assembly", check_real_game(
            blender, args.game_dir, args.converter_executable, args.integration_dealer,
            args.integration_cache_root)))
    elif args.require_integration:
        results.append(("Real ETS2 assembly", False))
        print("\nFAIL: real integration requires Blender plus --game-dir and --converter-executable.",
              flush=True)
    else:
        print("\nSKIP: real ETS2 assembly (pass --game-dir and --converter-executable to enable).",
              flush=True)

    print("\n=== Summary ===", flush=True)
    for name, passed in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name}", flush=True)
    if not have_integration:
        print("Real game conversion and SCS model import were not exercised in this run.", flush=True)
    return 0 if all(passed for _, passed in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
