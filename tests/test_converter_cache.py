# SPDX-License-Identifier: GPL-3.0-or-later
"""Cache invalidation and explicit cache clearing."""

import json
import os


def test_archive_changes_and_order_are_reported_then_cache_can_be_cleared(
        game, tmp_path, converter_factory):
    conv = converter_factory(game, tmp_path)
    conv.extract_truck("test.truck")
    conv.convert_models([
        "/vehicle/truck/test/chassis/chs_4x2.pmd",
        "/vehicle/truck/test/acc/f_bumper/b.pmd",
    ])
    assert conv.stale_reasons() == []

    archive = game / "dlc_x.scs"
    stat = archive.stat()
    os.utime(archive, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    changed = converter_factory(game, tmp_path)
    assert changed.stale_reasons() == [f"archive changed: {archive}"]
    result = changed.convert_models([
        "/vehicle/truck/test/chassis/chs_4x2.pmd",
        "/vehicle/truck/test/acc/f_bumper/b.pmd",
    ])
    assert result.stale
    assert result.cached == ["/vehicle/truck/test/chassis/chs_4x2.pmd"]
    assert [key for key, _ in result.failed] == ["/vehicle/truck/test/acc/f_bumper/b.pmd"]
    assert changed.extract_truck("test.truck").extracted

    order_cache = tmp_path / "order-cache"
    original_order = converter_factory(game, order_cache)
    original_order.extract_truck("test.truck")
    reversed_order = converter_factory(
        game, order_cache, archives=[game / "dlc_x.scs", game / "base.scs"]
    )
    assert "archive order changed" in reversed_order.stale_reasons()

    changed.clear_cache()
    cache = tmp_path / "cache"
    assert not (cache / "defs").exists()
    assert not (cache / "base").exists()
    assert json.loads((cache / "manifest.json").read_text())["models"] == {}
    assert converter_factory(game, tmp_path).stale_reasons() == []
