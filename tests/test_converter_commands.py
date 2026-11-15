# SPDX-License-Identifier: GPL-3.0-or-later
"""ConverterPIX command construction, parsing, and model path mapping."""

import pytest

from blender_assembler.core import converter_pix as cpx
from blender_assembler.core.truck.paths import model_key, resolve_model

REAL_LISTDIR = """
 ******************************************
 **        Converter PMX to PIX          **
 **       Copyright (C) 2024 mwl4        **
 ******************************************

[D] /def/vehicle/truck/daf.xf
[F] /def/vehicle/truck/common_trlr_cables.sui
[F] (encrypted) /def/vehicle/truck/secret.sii
-- done --
"""


def test_default_archive_order():
    names = ["dlc_b.scs", "effect.scs", "def.scs", "base_share.scs", "base.scs", "core.scs", "dlc_a.scs",
             "locale.scs", "version.scs", "base_cfg.scs", "notes.txt"]
    assert cpx.default_archive_order(names) == ["core.scs", "base.scs", "base_cfg.scs", "base_share.scs", "def.scs",
                                                "dlc_a.scs", "dlc_b.scs"]


def test_command_building_keeps_archive_order_and_requires_export():
    archives = ["A.scs", "B.scs"]
    assert cpx.listdir_args(archives, "/def/vehicle/truck") == ["-b", "A.scs", "-b", "B.scs", "-listdir",
                                                                "/def/vehicle/truck"]
    assert cpx.model_args(archives, "/vehicle/truck/x/cabin.pmd", "out") == [
        "-b", "A.scs", "-b", "B.scs", "-m", "/vehicle/truck/x/cabin", "-e", "out"]
    assert cpx.extract_dir_args(archives, "/def/vehicle/truck/x", "o")[-4:] == ["-extract_d", "/def/vehicle/truck/x",
                                                                                "-e", "o"]
    assert cpx.extract_file_args(archives, "/def/a.sui", "o")[-4:] == ["-extract_f", "/def/a.sui", "-e", "o"]
    for build in (lambda: cpx.model_args(archives, "/a.pmd", ""), lambda: cpx.extract_dir_args(archives, "/d", None)):
        with pytest.raises(cpx.ConverterArgError, match="export folder"):
            build()
    with pytest.raises(cpx.ConverterArgError):
        cpx.base_args([])


@pytest.mark.parametrize("bad", ["/vehicle/../../x.pmd", "C:/x.pmd", "vehicle/x.pmd", "/C:/x.pmd", "/a\\b.pmd"])
def test_unsafe_model_paths_rejected(bad):
    with pytest.raises(cpx.ConverterArgError, match="unsafe"):
        cpx.model_arg(bad)


def test_parse_listdir():
    entries = cpx.parse_listdir(REAL_LISTDIR)
    assert entries == [cpx.ListEntry(True, "/def/vehicle/truck/daf.xf"),
                       cpx.ListEntry(False, "/def/vehicle/truck/common_trlr_cables.sui"),
                       cpx.ListEntry(False, "/def/vehicle/truck/secret.sii", encrypted=True)]


def test_failure_detection():
    ok = "\n[model] cab: pim:yes pit:yes\n"
    assert cpx.failure_reason(0, ok) is None
    assert cpx.failure_reason(3, ok) == "exit code 3"
    assert cpx.failure_reason(0, "<error> [model] /x: Unable to open descriptor file\nFailed to load: /x").startswith(
        "<error> [model] /x")
    assert cpx.failure_reason(0, "Unable to open file for read: /def/nope.sui") == "Unable to open file for read: /def/nope.sui"
    assert cpx.warning_lines("<warning> [tobj] x: y\n") == ["<warning> [tobj] x: y"]


def test_resolve_model_and_key(tmp_path):
    assert resolve_model("/vehicle/truck/x/cabin.pmd", tmp_path) == tmp_path / "vehicle/truck/x/cabin.pim"
    assert resolve_model("/vehicle/../x.pmd", tmp_path) is None
    assert model_key("/vehicle//truck/x.pmd") == "/vehicle/truck/x.pmd"
