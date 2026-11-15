# SPDX-License-Identifier: GPL-3.0-or-later
"""Build an installable Blender add-on zip."""
from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "blender_assembler"
INSTALL_PACKAGE = "blender_assembler"


def build(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                archive.write(path, Path(INSTALL_PACKAGE) / path.relative_to(PACKAGE))
        archive.write(ROOT / "LICENSE", "LICENSE")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "blender_assembler.zip")
    args = parser.parse_args()
    print(build(args.output.resolve()))


if __name__ == "__main__":
    main()
