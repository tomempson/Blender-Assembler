# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FIXTURES = REPO / "tests" / "fixtures"
FIXTURE_DEFS = FIXTURES / "defs"
FAKE = FIXTURES / "fake_converter" / "fake_converter_pix.py"


class ConverterCallLog:
    """Read and reset the fake converter's JSONL invocation log."""

    def __init__(self, path: Path):
        self.path = path

    def __call__(self, mode=None):
        if not self.path.exists():
            return []
        records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        return [record for record in records if mode is None or record["mode"] == mode]

    def reset(self):
        self.path.unlink(missing_ok=True)


@pytest.fixture
def converter_factory():
    from blender_assembler.blender.converter import Converter, ConverterSettings

    def make(game: Path, tmp_path: Path, jobs: int = 1, archives=None):
        if archives is None:
            archives = [game / "base.scs", game / "dlc_x.scs"]
        return Converter(ConverterSettings([sys.executable, str(FAKE)], archives,
                                           tmp_path / "cache", jobs=jobs))

    return make


@pytest.fixture
def converter_calls(tmp_path, monkeypatch):
    log = tmp_path / "converter-calls.jsonl"
    monkeypatch.setenv("FAKE_CPX_LOG", str(log))
    return ConverterCallLog(log)

# Map each model fixture to its fake-converter behavior.
MODELS = {
    "vehicle/truck/test/chassis/chs_4x2.pmd": "ok",
    "vehicle/truck/test/chassis/chs_6x2.pmd": "NO_OUTPUT",
    "vehicle/truck/test/cabin/cab_low.pmd": "ok",
    "vehicle/truck/test/cabin/cab_high.pmd": "FAIL_EXIT",
    "vehicle/truck/test/acc/f_bumper/a.pmd": "ok",
    "vehicle/truck/test/acc/f_bumper/a_uk.pmd": "SLOW",
    "vehicle/truck/test/acc/f_bumper/b.pmd": "FAIL_ERROR",
    "vehicle/truck/upgrade/toy/fresh.pmd": "SLOW",
    "vehicle/truck/upgrade/horn/ahooga.pmd": "SLOW",
    "vehicle/truck/test/acc/trlr_cables/stock.pmd": "SLOW",
}


@pytest.fixture
def fixture_defs() -> Path:
    return FIXTURE_DEFS


@pytest.fixture
def game(tmp_path):
    """A fake game folder: base.scs (defs + models) and dlc_x.scs (overrides cab_low)."""
    g = tmp_path / "game"
    base = g / "base.scs"
    shutil.copytree(FIXTURE_DEFS / "def", base / "def")
    for rel, content in MODELS.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    dlc = g / "dlc_x.scs" / "vehicle/truck/test/cabin/cab_low.pmd"
    dlc.parent.mkdir(parents=True)
    dlc.write_text("ok", encoding="utf-8")
    return g
