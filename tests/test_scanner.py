# SPDX-License-Identifier: GPL-3.0-or-later
import json
import subprocess
import sys

import pytest

from blender_assembler.core.truck import scan_truck
from blender_assembler.core.truck.classes import ADDON_CLASSES, EXCLUDED_CLASSES
from blender_assembler.core.truck.names import find_name, same_name
from blender_assembler.core.truck.scanner import list_trucks, split_truck_folder, wildcard_regex

from conftest import FIXTURE_DEFS, REPO


@pytest.fixture(scope="module")
def defn():
    return scan_truck(FIXTURE_DEFS, "test.truck")


def acc(defn, unit_name):
    return next(a for a in defn.all_accessories() if a.unit_name == unit_name)


def warnings_with(defn, *needles):
    return [w for w in defn.warnings if all(n in w for n in needles)]


def test_counts(defn):
    assert [a.unit_name for a in defn.chassis] == ["4x2.test.truck.chassis", "6x2.test.truck.chassis"]
    assert {a.unit_name for a in defn.cabins} == {"high.test.truck.cabin", "low.test.truck.cabin"}
    assert [a.unit_name for a in defn.interiors] == ["int_a.test.truck.interior"]
    assert sorted(defn.slots) == ["bad", "badge", "beacon", "f_bumper", "l_horn", "r_chs_cover", "toyhang",
                                  "trlr_cables"]
    assert defn.slots["badge"].legacy and not defn.slots["f_bumper"].legacy
    assert len(defn.slots["f_bumper"].accessories) == 3


def test_chassis_and_cabin_use_model_collision_and_variant_uk(defn):
    ch = acc(defn, "4x2.test.truck.chassis")
    assert ch.models.exterior == "/vehicle/truck/test/chassis/chs_4x2.pmd"
    assert ch.models.collision == "/vehicle/truck/test/chassis/chs_4x2.pmc"
    cab = acc(defn, "high.test.truck.cabin")
    assert (cab.variant, cab.variant_uk) == ("Default", "uk")
    assert cab.models.exterior.endswith("cab_high.pmd") and cab.models.collision.endswith("cab_high.pmc")


def test_interior_model_mapping(defn):
    it = defn.interiors[0]
    assert it.models.interior.endswith("int_a.pmd") and it.models.exterior.endswith("int_a_ext.pmd")
    assert (it.ext_variant, it.ext_look, it.look) == ("ext_default", "Black", "black")


def test_addon_models_coll_and_uk(defn):
    a = acc(defn, "a.test.truck.f_bumper")
    assert a.models.exterior.endswith("a.pmd") and a.models.exterior_uk.endswith("a_uk.pmd")
    assert a.models.collision.endswith("a.pmc")
    assert (a.look, a.variant) == ("chrome", "v1")


def test_count_plus_append_from_relative_include(defn):
    raw = acc(defn, "4x2.test.truck.chassis").raw
    assert len(raw.attrs["sounds"]) == 2
    assert not warnings_with(defn, "sounds")


def test_class_filter_includes_horn_painted_int_and_cables(defn):
    classes = {a.class_name for a in defn.all_accessories()}
    assert {"accessory_horn_addon_data", "accessory_addon_int_data", "accessory_addon_painted_data",
            "accessory_addon_trailer_cables_data"} <= classes
    names = {a.unit_name for a in defn.all_accessories()}
    assert ".fresh.phys_data" not in names and ".cable_a" not in names and ".pane.front" not in names


def test_absolute_includes_in_body_and_at_file_level(defn):
    cables = acc(defn, "stock.test.truck.trlr_cables")
    assert [str(c) for c in cables.raw.attrs["cables"]] == [".cable_a", ".cable_b"]
    assert defn.stats["excluded_units"] >= 4     # 2 cable units + physics toy + glass pane


def test_unknown_classes(defn):
    future = acc(defn, "future.test.truck.beacon")
    assert future.class_name == "accessory_addon_future_data"
    assert warnings_with(defn, "[class]", "accessory_addon_future_data", "included as accessory")
    assert warnings_with(defn, "[class]", "mystery_data", "ignored")


def test_bad_files_become_warnings_and_scan_continues(defn):
    assert acc(defn, "good.test.truck.bad")
    assert defn.stats["files_failed"] == 4      # 3 in accessory/bad + engine/broken.sii
    assert warnings_with(defn, "[parse]", "malformed.sii", "SiiSyntaxError", ":6")
    assert warnings_with(defn, "[parse]", "encoded.sii", "SiiEncodedError")
    assert warnings_with(defn, "[parse]", "missing_include.sii", "SiiIncludeError", "does_not_exist.sui")


def test_skipped_folders_and_files_are_never_parsed(defn):
    # paint_job/ and accessory/<slot>/paint_job/ hold deliberately broken files
    assert not warnings_with(defn, "[parse]", "paint_job")
    assert defn.stats["nested_dirs_skipped"] == 1
    assert defn.stats["toplevel_files_skipped"] == 1     # lamp_setup.sii


def test_slot_name_is_the_folder_and_suffix_mismatch_warns(defn):
    c = acc(defn, "c.test.truck.r_bumper")
    assert c.slot == "f_bumper"
    assert warnings_with(defn, "[name]", "c.test.truck.r_bumper", "folder wins")


def test_display_name_and_sorting(defn):
    assert [a.display_name for a in defn.slots["f_bumper"].accessories] == [
        "@@bumper_a@@", "@@bumper_b@@", "@@bumper_c@@"]


def test_rule_lists_are_typed_and_resolved(defn):
    b = acc(defn, "b.test.truck.f_bumper")
    sf = {r.name: r for r in b.suitable_for}
    assert sf["*.test.truck.cabin"].is_pattern and sf["*.test.truck.cabin"].resolved
    assert not sf["ghost.test.truck.f_bumper"].resolved
    cw = {r.name: r for r in b.conflict_with}
    assert cw["a.test.truck.f_bumper"].resolved
    assert not cw["*.test.truck.nothing"].resolved
    assert b.overrides[0].status == "resolved" and b.overrides[0].units == ["a.test.truck.f_bumper"]


def test_defaults_are_def_paths_with_status(defn):
    status = {r.path.rsplit("/", 1)[-1]: r.status for r in acc(defn, "4x2.test.truck.chassis").defaults}
    assert status == {"low.sii": "resolved", "a.sii": "resolved", "color0.sii": "skipped_folder",
                      "nope.sii": "missing"}
    assert warnings_with(defn, "[ref]", "nope.sii", "does not exist")


def test_truck_data_require_and_fallback(defn):
    assert defn.truck_data.name == "vehicle.test.truck"
    assert [(r.name, r.known) for r in defn.requires] == [("f_bumper", True), ("mirror", False)]
    assert warnings_with(defn, "require[]", "'mirror'")
    assert [(f.folder, f.file) for f in defn.fallbacks][:3] == [
        ("chassis", "4x2.sii"), ("cabin", "low.sii"), ("accessory", "f_bumper/a.sii")]
    assert defn.fallbacks[2].def_path == "/def/vehicle/truck/test.truck/accessory/f_bumper/a.sii"
    assert warnings_with(defn, "[wheel]", "f_tire")


def test_unresolved_refs_summarised(defn):
    assert warnings_with(defn, "[ref]", "match no scanned unit")
    assert defn.stats["wildcard_unit_refs"] == 3


def test_engine_and_transmission_names_are_indexed_not_accessories(defn):
    assert defn.indexed_names == {"e1.test.truck.engine": "engine", "t1.test.truck.transmission": "transmission"}
    assert "e1.test.truck.engine" not in {a.unit_name for a in defn.all_accessories()}
    assert warnings_with(defn, "[parse]", "engine/broken.sii")      # recorded, scan carried on


def test_reference_statuses(defn):
    st = {r.name: r.status for r in acc(defn, "stock.test.truck.r_chs_cover").suitable_for}
    assert st == {"e1.test.truck.engine": "resolved", "*.test.truck.transmission": "resolved",
                  "racing.test.truck.paint_job": "not_indexed"}
    assert defn.stats["not_indexed_unit_refs"] == 1
    assert not warnings_with(defn, "match no scanned", "paint_job")


def test_missing_truck_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_truck(tmp_path, "nope.truck")


def test_helpers():
    assert same_name("Default", "default") and not same_name("uk", "default")
    assert find_name("DEFAULT", ["uk", "default"]) == "default" and find_name("x", ["y"]) is None
    rx = wildcard_regex("*.scania.s_2016.r_horn")
    assert rx.fullmatch("a.scania.s_2016.r_horn") and not rx.fullmatch("a.scaniaXs_2016.r_horn")
    defs, bm = split_truck_folder(FIXTURE_DEFS / "def" / "vehicle" / "truck" / "test.truck")
    assert defs == FIXTURE_DEFS and bm == "test.truck"
    assert list_trucks(FIXTURE_DEFS) == ["test.truck"]
    assert not ADDON_CLASSES & EXCLUDED_CLASSES


def test_dump_truck_cli_json_and_summary():
    tool = str(REPO / "tools" / "dump_truck.py")
    out = subprocess.run([sys.executable, tool, str(FIXTURE_DEFS), "test.truck"],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    data = json.loads(out)
    assert data["brand_model"] == "test.truck" and "raw" not in data["chassis"][0]
    summary = subprocess.run([sys.executable, tool, str(FIXTURE_DEFS / "def/vehicle/truck/test.truck"),
                              "--summary"], capture_output=True, text=True, check=True).stdout
    assert summary.startswith("test.truck: 2 chassis, 2 cabins, 1 interiors, 8 slots (1 legacy)")
