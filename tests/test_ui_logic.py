# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure Python logic used by the Blender UI."""
from blender_assembler.core.converter_pix import enabled_archive_paths
from blender_assembler.core.truck import scan_truck
from blender_assembler.core.truck.selection import filtered_slot_names, options_for_slot

from conftest import FIXTURE_DEFS


def test_enabled_archives_preserve_order_and_skip_unticked_or_empty():
    entries = [("core.scs", True), ("base.scs", False), ("", True), ("mod.scs", True),
               ("dlc.scs", False), ("late_mod.scs", True)]
    assert enabled_archive_paths(entries) == ["core.scs", "mod.scs", "late_mod.scs"]


def test_slot_options_are_scoped_and_filter_case_insensitively():
    defn = scan_truck(FIXTURE_DEFS, "test.truck")
    all_bumpers = options_for_slot(defn, "f_bumper")
    assert all_bumpers
    assert all(option in defn.slots["f_bumper"].accessories for option in all_bumpers)
    assert options_for_slot(defn, "f_bumper", "BUMPER_B") == [
        option for option in all_bumpers
        if "bumper_b" in option.unit_name.casefold() or "bumper_b" in option.display_name.casefold()
    ]
    assert options_for_slot(defn, "no_such_slot") == []


def test_slot_name_filter_is_case_insensitive_substring_and_keeps_order():
    names = ["f_bumper", "f_mirror", "r_bumper", "mirror_s"]
    assert filtered_slot_names(names, "BUMPER") == ["f_bumper", "r_bumper"]
    assert filtered_slot_names(names, "") == names
    assert filtered_slot_names(names, "missing") == []
