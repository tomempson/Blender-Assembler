# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python assembly planning, locator matching, PIM reading, and report tests."""
import math
import struct
from pathlib import Path

import pytest

from blender_assembler.core.pim import read_pim
from blender_assembler.core.report import PlacedItem, Report, SkippedItem
from blender_assembler.core.truck import scan_truck
from blender_assembler.core.truck.dealer import parse_dealer_file, selection_from_dealer
from blender_assembler.core.truck.locator_index import LocatorEntry, LocatorIndex
from blender_assembler.core.truck.plan import PlanError, PlanOptions, build_plan
from blender_assembler.core.truck.selection import Selection
from blender_assembler.core.truck.slot_match import SlotMatcher

from conftest import FIXTURE_DEFS

DEALER = FIXTURE_DEFS / "def/vehicle/truck_dealer/test/test_4x2.sii"


@pytest.fixture(scope="module")
def defn():
    return scan_truck(FIXTURE_DEFS, "test.truck")


# Slot matching
def test_slot_matching_exact_prefix_and_ties():
    m = SlotMatcher({"mirror", "doorstep", "toyhang", "badge", "badge_a", "f_light_chs", "f_light_top"})
    assert m.slot_for("mirror") == "mirror"                 # exact
    assert m.slot_for("mirror_s") == "mirror"               # prefix (scania cabin)
    assert m.slot_for("doorsteps") == "doorstep"            # prefix without underscore (volvo chassis)
    assert m.slot_for("toyhang_1") == "toyhang"
    assert m.slot_for("badge_a") == "badge_a"               # exact beats the shorter slot's prefix
    assert m.slot_for("badge_a2") == "badge_a"              # longest slot wins
    assert m.slot_for("badge_x") == "badge"
    assert m.slot_for("f_light") is None                    # a locator shorter than the slot never matches
    assert m.slot_for("wheel_f_0") is None and m.slot_for("cabin") is None
    assert m.matches("mirror_s", "mirror") and not m.matches("mirror_s", "badge")


# Locator index
def test_index_owner_order_and_no_double_fill():
    idx = LocatorIndex(SlotMatcher({"f_bumper", "mirror", "lamp"}))
    idx.add([LocatorEntry("f_bumper", "chassis", payload="c1"), LocatorEntry("mirror_s", "chassis", payload="c2")])
    idx.add([LocatorEntry("mirror_s", "cabin", payload="k1"), LocatorEntry("mirror_s", "cabin", payload="k2")])
    assert [e.payload for e in idx.find("mirror")] == ["k1", "k2"]            # cabin before chassis, all copies
    assert [e.payload for e in idx.find("f_bumper")] == ["c1"]
    assert idx.find("lamp") == []
    idx.add([LocatorEntry("lamp_1", "accessory:b", payload="a1")])
    idx.add([LocatorEntry("lamp_1", "accessory:a", payload="a0")])
    assert [e.payload for e in idx.find("lamp")] == ["a1"]                    # first accessory placed wins
    assert idx.find("lamp", side="interior") == []
    assert idx.slots_present() == {"f_bumper", "mirror", "lamp"}


# Dealer loader
def test_dealer_selection(defn):
    cfg = parse_dealer_file(DEALER, FIXTURE_DEFS)
    assert cfg.brand_model == "test.truck"
    assert [e.local for e in cfg.entries][:3] == [".data", ".chassis", ".cabin"]
    assert any("ghost" in p for p in cfg.problems)
    ds = selection_from_dealer(cfg, defn)
    sel = ds.selection
    assert (sel.chassis, sel.cabin, sel.interior) == ("4x2.test.truck.chassis", "high.test.truck.cabin",
                                                      "int_a.test.truck.interior")
    assert sel.accessories == {"f_bumper": "a.test.truck.f_bumper", "l_horn": "ahooga.test.truck.l_horn",
                               "toyhang": "fresh.test.truck.toyhang", "badge": "b1.test.truck.badge"}
    reasons = {s.local: s.reason for s in ds.skipped}
    assert reasons[".engine"].startswith("engines have no model")
    assert reasons[".transm"].startswith("transmissions have no model")
    assert reasons[".f_tire"] == "wheel accessories are not assembled"
    assert reasons[".paint"].startswith("paint jobs are not modelled")
    assert "already holds a.test.truck.f_bumper" in reasons[".bumper2"]
    assert reasons[".gone"].startswith("def file not scanned or not found")
    assert reasons[".other"].startswith("belongs to another truck")
    assert ".data" not in reasons and ".chassis" not in reasons


# Plan
def sel_from_dealer(defn):
    return selection_from_dealer(parse_dealer_file(DEALER, FIXTURE_DEFS), defn).selection


def test_plan_base_pending_and_skips(defn):
    plan = build_plan(defn, sel_from_dealer(defn))
    assert [(b.role, b.model_path) for b in plan.base] == [
        ("chassis", "/vehicle/truck/test/chassis/chs_4x2.pmd"),
        ("cabin", "/vehicle/truck/test/cabin/cab_high.pmd")]
    assert sorted(plan.pending) == ["badge", "f_bumper", "l_horn", "toyhang"]
    assert plan.pending["f_bumper"][0].model_path == "/vehicle/truck/test/acc/f_bumper/a.pmd"
    assert plan.pending["f_bumper"][0].look == "chrome"
    assert [(m.side, m.model_path) for m in plan.pending["toyhang"]] == [
        ("exterior", "/vehicle/truck/upgrade/toy/fresh.pmd")]
    assert all(m.side == "exterior" for models in plan.pending.values() for m in models)
    assert any("interior model importing is disabled" in skip.reason for skip in plan.skipped)
    assert len(plan.model_paths()) == len(set(plan.model_paths()))


def test_plan_rhd_and_logic_only_and_unknown_slot(defn):
    sel = Selection("test.truck", "4x2.test.truck.chassis", None, None,
                    {"f_bumper": "a.test.truck.f_bumper", "r_bumper": "c.test.truck.r_bumper",
                     "nosuch": "a.test.truck.f_bumper", "l_horn": "missing.unit"})
    plan = build_plan(defn, sel, PlanOptions(rhd=True))
    assert plan.pending["f_bumper"][0].model_path.endswith("a_uk.pmd")
    reasons = {s.slot: s.reason for s in plan.skipped}
    assert reasons["nosuch"] == "'nosuch' is not a slot of test.truck"
    assert reasons["l_horn"] == "unit not found in test.truck"
    assert "r_bumper" not in plan.pending                     # c.sii sits in f_bumper/, slot not selected


def test_plan_uses_cabin_uk_variant_when_rhd(defn):
    sel = Selection("test.truck", "4x2.test.truck.chassis", "high.test.truck.cabin")
    plan = build_plan(defn, sel, PlanOptions(rhd=True))
    assert next(item for item in plan.base if item.role == "cabin").variant == "uk"


def test_plan_always_skips_interior_models(defn):
    sel = sel_from_dealer(defn)
    plan = build_plan(defn, sel)
    assert not any(base.role in {"interior", "external_interior"} for base in plan.base)
    assert not any(m.side == "interior" for models in plan.pending.values() for m in models)
    assert any("interior model importing is disabled" in skip.reason for skip in plan.skipped)


def test_plan_logic_only_accessory(defn):
    sel = Selection("test.truck", "4x2.test.truck.chassis", None, None, {"f_bumper": "c.test.truck.r_bumper"})
    plan = build_plan(defn, sel)
    assert plan.skipped[0].reason == "no model defined (logic-only accessory)" and not plan.pending


@pytest.mark.parametrize("sel", [Selection("test.truck"), Selection("test.truck", "nope.chassis"),
                                 Selection("test.truck", "high.test.truck.cabin")])
def test_plan_rejects_missing_or_invalid_chassis_without_a_cabin(defn, sel):
    with pytest.raises(PlanError):
        build_plan(defn, sel)


def test_plan_accepts_cabin_without_chassis(defn):
    sel = Selection("test.truck", cabin="high.test.truck.cabin")
    plan = build_plan(defn, sel)
    assert [(base.role, base.unit_name) for base in plan.base] == [
        ("cabin", "high.test.truck.cabin")]


# Pim reader
def hexf(x: float) -> str:
    return "&" + struct.pack(">f", x).hex()


def test_read_pim(tmp_path):
    s = math.sqrt(0.5)
    text = f"""Header {{
     FormatVersion: 5
}}
Part {{
     Name: "eu_part"
     Locators: 0 2
}}
Part {{
     Name: "uk_part"
     Locators: 1
}}
Locator {{
     Name: "mirror"
     Index: 0
     Position: ( {hexf(1.5)}  {hexf(-2.0)}  {hexf(0.25)} )
     Rotation: ( {hexf(s)}  {hexf(0.0)}  {hexf(0.0)}  {hexf(s)} )
     Scale: ( {hexf(1)}  {hexf(1)}  {hexf(1)} )
}}
Locator {{
     Name: "mirror"
     Index: 1
     Position: ( {hexf(-1.5)}  {hexf(-2.0)}  {hexf(0.25)} )
     Rotation: ( {hexf(1)}  {hexf(0)}  {hexf(0)}  {hexf(0)} )
     Scale: ( {hexf(1)}  {hexf(1)}  {hexf(1)} )
}}
Locator {{
     Name: "ml1"
     Hookup: "license.plate"
     Index: 2
     Position: ( {hexf(0)}  {hexf(0)}  {hexf(0)} )
     Rotation: ( {hexf(1)}  {hexf(0)}  {hexf(0)}  {hexf(0)} )
     Scale: ( {hexf(1)}  {hexf(1)}  {hexf(1)} )
}}
"""
    f = tmp_path / "m.pim"
    f.write_text(text, encoding="utf-8")
    info = read_pim(f)
    assert [(l.name, l.part, l.hookup, l.is_hookup) for l in info.locators] == [
        ("mirror", "eu_part", "", False), ("mirror", "uk_part", "", False), ("ml1", "eu_part", "license.plate", True)]
    assert info.locators[0].position == pytest.approx((1.5, -2.0, 0.25))
    assert info.locators[0].rotation_wxyz == pytest.approx((s, 0, 0, s))
    assert info.names() == {"mirror", "ml1"} and info.parts == {"eu_part": [0, 2], "uk_part": [1]}


# Report
def test_report_text_and_counts():
    r = Report("Assembly of x")
    r.warnings.append("STALE CACHE: rebuild")
    r.placed.append(PlacedItem("mirror", "painted.x.mirror", "/m.pmd", "mirror_s", "cabin", "eu_part", "interior"))
    r.skipped.append(SkippedItem("engine", "e", None, "engines have no model in v1"))
    r.skipped.append(SkippedItem("tank", "t", "/t.pmd", "import failed", kind="failed"))
    r.hookups["chassis"] = ["wp0", "wp0", "ml1"]
    assert r.summary() == "1 placed, 1 skipped, 1 failed"
    text = r.to_text()
    assert text.index("WARNING: STALE CACHE") < text.index("1 placed")
    for needle in ("| interior | mirror_s [eu_part] | cabin |", "FAILED (1)", "SKIPPED (1)", "reason: import failed",
                   "ml1, wp0 x2"):
        assert needle in text
    assert r.to_dict()["summary"] == r.summary()
