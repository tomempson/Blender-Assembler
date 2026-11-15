# SPDX-License-Identifier: GPL-3.0-or-later
"""Fake ConverterPIX CLI used by tests.

- `-b <dir>` mounts a folder; later mounts take priority.
- Implements `-listdir`, `-listdir_r`, `-extract_d`, `-extract_f`, and `-m`.
- Model fixture contents select success, error, missing-output, or slow behavior.
- Successful model conversions create PIM, PIT, PIC, TOBJ, and DDS fixture outputs.
- Calls are appended as JSON lines to `FAKE_CPX_LOG` when that variable is set.
"""
import json
import os
import sys
import time
from pathlib import Path

BANNER = ("\n ******************************************\n **        Converter PMX to PIX          **\n"
          " **       Copyright (C) 2024 mwl4        **\n ******************************************\n")


def main(argv):
    bases, export, mode, path = [], None, None, None
    it = iter(argv)
    for arg in it:
        if arg == "-b":
            bases.append(Path(next(it)))
        elif arg == "-e":
            export = Path(next(it))
        elif arg in ("-m", "-listdir", "-listdir_r", "-extract_d", "-extract_f"):
            mode, path = arg, next(it)
        else:
            print(f"<error> [system] : unknown argument {arg}")
            return 1
    log = os.environ.get("FAKE_CPX_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"mode": mode, "path": path, "export": str(export) if export else None,
                                "bases": [str(b) for b in bases]}) + "\n")
    print(BANNER)
    if not bases:
        print("<error> [system] : Not specified base path!")
        return 1
    if mode in ("-m", "-extract_d", "-extract_f") and export is None:
        export = Path(str(bases[-1]) + "_exp")      # The real tool can write beside the source archive.

    def find(game_path):
        rel = game_path.lstrip("/")
        for b in reversed(bases):
            p = b / rel
            if p.exists():
                return b, p
        return None, None

    if mode in ("-listdir", "-listdir_r"):
        entries = {}
        for b in bases:
            d = b / path.lstrip("/")
            if d.is_dir():
                children = d.rglob("*") if mode == "-listdir_r" else d.iterdir()
                for c in children:
                    suffix = c.relative_to(d).as_posix()
                    entries[path.rstrip("/") + "/" + suffix] = c.is_dir()
        for p, is_dir in sorted(entries.items()):
            print(f"[{'D' if is_dir else 'F'}] {p}")
        print("-- done --")
        return 0

    if mode == "-extract_f":
        _, src = find(path)
        if src is None or not src.is_file():
            print(f"Unable to open file for read: {path}")
            return 0
        dest = export / path.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        return 0

    if mode == "-extract_d":
        rel = path.lstrip("/")
        files = {}
        for b in bases:
            d = b / rel
            if d.is_dir():
                for f in d.rglob("*"):
                    if f.is_file():
                        files[f.relative_to(b).as_posix()] = f
        for r, f in files.items():
            dest = export / r
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f.read_bytes())
        return 0

    if mode == "-m":
        base, pmd = find(path + ".pmd")
        if pmd is None:
            print(f"<error> [model] {path}: Unable to open descriptor file [.pmd] (No error)!")
            print(f"Failed to load: {path}")
            return 0
        behaviour = pmd.read_text(encoding="utf-8").strip()
        if behaviour == "FAIL_EXIT":
            print("crashed")
            return 3
        if behaviour == "FAIL_ERROR":
            print(f"<error> [model] {path}: Unable to load geometry!")
            return 0
        if behaviour == "NO_OUTPUT":
            return 0
        if behaviour == "SLOW":
            time.sleep(0.3)
        out = export / path.lstrip("/")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".pim").write_text(f"Header {{ }}\n# from {base.name}\n", encoding="utf-8")
        out.with_suffix(".pit").write_text("pit\n", encoding="utf-8")
        out.with_suffix(".pic").write_text("pic\n", encoding="utf-8")
        shared = export / "material" / "shared"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "common.tobj").write_text(f"map 2d\n  /material/shared/common.dds\n# {path}\n", encoding="utf-8")
        (shared / "common.dds").write_bytes(b"DDS " + path.encode())
        (out.parent / (out.name + "_tex.dds")).write_bytes(b"DDS model")
        name = path.rsplit("/", 1)[-1]
        print(f"[model] {name}: pim:yes pit:yes pis:no pic:yes pip:no vertices:3 indices:3 materials:1")
        return 0

    print("<error> [system] : Invalid parameters!")
    return 1


if __name__ == "__main__":
    t0 = time.time()
    rc = main(sys.argv[1:])
    times = os.environ.get("FAKE_CPX_TIMES")
    if times:   # one line per run, so tests can check that parallel runs really overlapped
        with open(times, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t0": t0, "t1": time.time()}) + "\n")
    sys.exit(rc)
