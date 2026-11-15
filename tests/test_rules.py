# SPDX-License-Identifier: GPL-3.0-or-later
from blender_assembler.core.truck import scan_truck
from blender_assembler.core.truck.model import UnitRef
from blender_assembler.core.truck.rules import evaluate_options, reset_invalid_accessories, validity
from blender_assembler.core.truck.selection import Selection
from blender_assembler.core.truck.names import find_name

from conftest import FIXTURE_DEFS


def test_suitable_for_wildcard_and_two_way_conflicts(fixture_defs):
    defn = scan_truck(fixture_defs, "test.truck")
    units = {a.unit_name: a for a in defn.all_accessories()}
    candidate = units["b.test.truck.f_bumper"]
    selection = Selection("test.truck", "4x2.test.truck.chassis", "high.test.truck.cabin",
                          accessories={"f_bumper": "a.test.truck.f_bumper"})
    selected = {selection.chassis, selection.cabin, *selection.accessories.values()}
    assert validity(candidate, {selection.cabin}, units).valid  # wildcard resolves to the cabin
    assert validity(candidate, selected, units).reasons == ("conflicts with: a.test.truck.f_bumper",)

    # Reverse direction is also honoured if the selected item declares the conflict.
    candidate.conflict_with.clear()
    units["a.test.truck.f_bumper"].conflict_with = [UnitRef(candidate.unit_name, "resolved")]
    assert validity(candidate, selected, units).reasons == ("conflicts with: a.test.truck.f_bumper",)


def test_unresolved_and_paint_job_references_do_not_invalidate(fixture_defs):
    defn = scan_truck(fixture_defs, "test.truck")
    acc = next(a for a in defn.slots["r_chs_cover"].accessories if a.unit_name == "stock.test.truck.r_chs_cover")
    selection = Selection("test.truck", "4x2.test.truck.chassis", "high.test.truck.cabin")
    assert evaluate_options(defn, selection)[acc.unit_name].valid is False  # resolved engine/transmission refs
    for ref in acc.suitable_for:
        if ref.status != "resolved":
            continue
        ref.status = "unresolved"
    verdict = validity(acc, {selection.chassis, selection.cabin}, {a.unit_name: a for a in defn.all_accessories()})
    assert verdict.valid


def test_strict_reset_lists_invalid_chosen_items(fixture_defs):
    defn = scan_truck(fixture_defs, "test.truck")
    selection = Selection("test.truck", "4x2.test.truck.chassis", "high.test.truck.cabin",
                          accessories={"f_bumper": "a.test.truck.f_bumper",
                                       "r_chs_cover": "stock.test.truck.r_chs_cover"})
    reset, notes = reset_invalid_accessories(defn, selection)
    assert reset.accessories == {"f_bumper": "a.test.truck.f_bumper"}
    assert notes == ["r_chs_cover: stock.test.truck.r_chs_cover (needs one of: e1.test.truck.engine, "
                     "*.test.truck.transmission)"]
    assert selection.accessories["r_chs_cover"] == "stock.test.truck.r_chs_cover"  # input untouched


def test_wildcard_matching_requires_a_full_name_match(fixture_defs):
    defn = scan_truck(fixture_defs, "test.truck")
    candidate = next(a for a in defn.slots["r_chs_cover"].accessories
                     if a.unit_name == "stock.test.truck.r_chs_cover")
    units = {a.unit_name: a for a in defn.all_accessories()}
    candidate.suitable_for = [UnitRef("*.test.truck.engine", "resolved")]
    assert validity(candidate, {"x.test.truck.engine"}, units).valid
    assert not validity(candidate, {"x.test.truck.transmission"}, units).valid


def test_unresolved_and_paint_job_conflicts_are_ignored(fixture_defs):
    defn = scan_truck(fixture_defs, "test.truck")
    candidate = next(a for a in defn.slots["r_chs_cover"].accessories
                     if a.unit_name == "stock.test.truck.r_chs_cover")
    units = {a.unit_name: a for a in defn.all_accessories()}
    candidate.suitable_for = [UnitRef("missing.unit", "unresolved"),
                              UnitRef("paint.test.truck.paint_job", "not_indexed")]
    candidate.conflict_with = [UnitRef("also.missing", "unresolved"),
                               UnitRef("paint.test.truck.paint_job", "not_indexed")]
    assert validity(candidate, {"4x2.test.truck.chassis"}, units).valid


def test_variant_name_lookup_is_case_insensitive():
    assert find_name("DEFAULT", ["uk", "default"]) == "default"
    assert find_name("missing", ["uk", "default"]) is None
