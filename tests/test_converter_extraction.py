# SPDX-License-Identifier: GPL-3.0-or-later
"""Truck definition extraction and its cache."""

import pytest

from blender_assembler.blender.converter import ConverterError


def test_extract_truck_caches_files_and_shared_sui(game, tmp_path, converter_factory, converter_calls):
    conv = converter_factory(game, tmp_path)
    first = conv.extract_truck("test.truck")
    truck_defs = tmp_path / "cache/defs/def/vehicle/truck/test.truck"

    assert first.extracted
    assert first.files > 20
    assert (truck_defs / "data.sii").is_file()
    assert first.shared_files == 2
    assert (tmp_path / "cache/defs/def/vehicle/truck/common_cables_data.sui").is_file()
    assert all(row["export"] and "cache" in row["export"]
               for row in converter_calls() if row["mode"] != "-listdir")

    converter_calls.reset()
    second = conv.extract_truck("test.truck")
    assert not second.extracted
    assert second.shared_files == 0
    assert converter_calls() == []
    assert not list((tmp_path / "cache/tmp").glob("job-*"))


def test_extract_unknown_truck_reports_missing_definitions(game, tmp_path, converter_factory):
    result = converter_factory(game, tmp_path).extract_truck("nope.truck")

    assert not result.extracted
    assert "No valid truck definitions" in result.error


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", "C:x"])
def test_extract_rejects_unsafe_truck_names(game, tmp_path, converter_factory, bad_name):
    with pytest.raises(ConverterError):
        converter_factory(game, tmp_path).extract_truck(bad_name)
