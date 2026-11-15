# SPDX-License-Identifier: GPL-3.0-or-later
"""Test preparation from dealer configuration through model conversion."""
import pytest

from blender_assembler.blender.converter import ConverterError
from blender_assembler.blender.pipeline import prepare, seed_report, stale_warning
from blender_assembler.core.truck.dealer import dealer_config_matches_truck
from blender_assembler.core.truck.selection import Selection

DEALER = "/def/vehicle/truck_dealer/test/test_4x2.sii"


def test_prepare_from_dealer_config(game, tmp_path, converter_factory):
    conv = converter_factory(game, tmp_path)
    prep = prepare(conv, dealer=DEALER, log=lambda m: None)
    assert prep.defn.brand_model == "test.truck"
    assert prep.selection.chassis == "4x2.test.truck.chassis"
    assert (tmp_path / "cache/defs/def/vehicle/truck_dealer/test/test_4x2.sii").is_file()
    # cab_high fails (exit 3); badge is absent. Interior model importing is disabled.
    failed = dict(prep.convert.failed)
    assert sorted(failed) == ["/vehicle/truck/test/badge/b1.pmd", "/vehicle/truck/test/cabin/cab_high.pmd"]
    assert sorted(prep.convert.converted) == [
        "/vehicle/truck/test/acc/f_bumper/a.pmd", "/vehicle/truck/test/chassis/chs_4x2.pmd",
        "/vehicle/truck/upgrade/horn/ahooga.pmd", "/vehicle/truck/upgrade/toy/fresh.pmd"]
    assert prep.failures()["/vehicle/truck/test/cabin/cab_high.pmd"].startswith("exit code 3")

    rep = seed_report("t", prep)
    kinds = {(s.slot, s.kind) for s in rep.skipped}
    assert ("(conversion)", "failed") in kinds and ("engine", "skipped") in kinds
    assert ("f_tire", "skipped") in kinds
    assert not rep.warnings and "time_convert" in rep.stats
    prep2 = prepare(converter_factory(game, tmp_path), dealer=DEALER, log=lambda m: None)
    assert prep2.convert.converted == [] and len(prep2.convert.failed_before) == 2


def test_prepare_from_selection_and_argument_errors(game, tmp_path, converter_factory):
    conv = converter_factory(game, tmp_path)
    sel = Selection("test.truck", "4x2.test.truck.chassis", None, None, {})
    prep = prepare(conv, selection=sel, log=lambda m: None)
    assert prep.convert.converted == ["/vehicle/truck/test/chassis/chs_4x2.pmd"]
    for kwargs in ({}, {"dealer": DEALER, "selection": sel}):
        with pytest.raises(ConverterError, match="Choose either"):
            prepare(conv, log=lambda m: None, **kwargs)
    with pytest.raises(ConverterError, match="Extracting"):
        prepare(conv, dealer="/def/vehicle/truck_dealer/test/missing.sii", log=lambda m: None)


def test_stale_cache_is_a_prominent_report_warning(game, tmp_path, converter_factory):
    import os
    prepare(converter_factory(game, tmp_path), dealer=DEALER, log=lambda m: None)
    archive = game / "dlc_x.scs"
    st = archive.stat()
    os.utime(archive, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    logged: list[str] = []
    prep = prepare(converter_factory(game, tmp_path), dealer=DEALER, log=logged.append)
    assert prep.stale and logged[0].startswith("WARNING: the game archives changed")
    rep = seed_report("t", prep)
    assert rep.warnings[0].startswith("STALE CACHE:") and rep.to_text().index("STALE CACHE") < 120
    assert "Clear the cache" in stale_warning(prep.stale)


def test_list_dealer_configs(game, tmp_path, converter_factory):
    assert converter_factory(game, tmp_path).list_dealer_configs("test") == [DEALER]


def test_dealer_filename_filter_uses_selected_truck_model():
    assert dealer_config_matches_truck("/def/vehicle/truck_dealer/scania/scania_s2016_4x2_460.sii",
                                       "scania.s_2016")
    assert not dealer_config_matches_truck("/def/vehicle/truck_dealer/scania/scania_r2016_4x2_460.sii",
                                           "scania.s_2016")
    assert dealer_config_matches_truck("/def/vehicle/truck_dealer/volvo/volvo_fh16_2012_4x2.sii",
                                       "volvo.fh16_2012")
