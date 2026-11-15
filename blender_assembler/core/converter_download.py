# SPDX-License-Identifier: GPL-3.0-or-later
"""Pinned ConverterPIX downloads and SHA-256 verification helpers."""
from __future__ import annotations

import hashlib
from pathlib import Path


# Immutable upstream snapshot. These hashes were calculated from the files at
# the pinned commit, rather than from a mutable branch URL.
CONVERTER_BUILDS = {
    "win32": {
        "url": "https://raw.githubusercontent.com/mwl4/ConverterPIX/3cd4e73a86d0c6bd28e117664c50a36cabeccf38/bin/win_x64/converter_pix.exe",
        "sha256": "60310ee8e01563145b71a8fb62f63d7a8b03906334e84e1463918f615f192ed2",
        "filename": "converter_pix.exe",
    },
    "linux": {
        "url": "https://raw.githubusercontent.com/mwl4/ConverterPIX/3cd4e73a86d0c6bd28e117664c50a36cabeccf38/bin/linux/converter_pix",
        "sha256": "fba5a52f763d07d3dc110dea4980fe2c7e656a8c0afa05b6b7a4ab4373dc950d",
        "filename": "converter_pix",
    },
}


def sha256_file(path: str | Path) -> str:
    """Return a file's SHA-256 digest without loading it all into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_pinned_digest(digest: str, system: str) -> bool:
    """Whether a digest matches the pinned build for the given OS platform."""
    build = CONVERTER_BUILDS.get(system)
    return bool(build and digest.lower() == build["sha256"])
