# SPDX-License-Identifier: GPL-3.0-or-later
"""Converter discovery, archive ordering, and truck inventory."""

from blender_assembler.blender.converter import default_archives, exe_name, find_converter


def test_find_converter_prefers_configured_then_wrapper_then_addon_copy(tmp_path):
    pref = tmp_path / "pref" / exe_name()
    wrapper = tmp_path / "blender" / "config" / "ConverterPIXWrapper" / exe_name()
    own = tmp_path / "own" / exe_name()
    for path in (pref, wrapper, own):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"converter")

    assert find_converter(str(pref), tmp_path / "blender", tmp_path / "own") == pref
    assert find_converter(None, tmp_path / "blender", tmp_path / "own") == wrapper
    assert find_converter(str(tmp_path / "missing"), tmp_path / "nowhere", tmp_path / "own") == own
    assert find_converter(None, None, None) is None


def test_default_archives_uses_supported_game_archives_in_priority_order(tmp_path):
    for name in ("def.scs", "base.scs", "dlc_b.scs", "core.scs", "locale.scs", "dlc_a.scs", "base_share.scs"):
        (tmp_path / name).write_bytes(b"")

    assert [path.name for path in default_archives(tmp_path)] == [
        "core.scs", "base.scs", "base_share.scs", "def.scs", "dlc_a.scs", "dlc_b.scs"
    ]


def test_list_trucks_reads_truck_definitions(game, tmp_path, converter_factory):
    assert converter_factory(game, tmp_path).list_trucks() == ["test.truck"]
