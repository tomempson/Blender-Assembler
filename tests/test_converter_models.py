# SPDX-License-Identifier: GPL-3.0-or-later
"""Model conversion outcomes, safety checks, and parallel execution."""

import json
import threading
from pathlib import Path


def test_conversion_failures_are_reported_and_only_retried_on_request(
        game, tmp_path, converter_factory, converter_calls):
    conv = converter_factory(game, tmp_path)
    paths = [
        "/vehicle/truck/test/cabin/cab_high.pmd",
        "/vehicle/truck/test/acc/f_bumper/b.pmd",
        "/vehicle/truck/test/chassis/chs_6x2.pmd",
        "/vehicle/truck/test/missing.pmd",
        "/vehicle/truck/test/chassis/chs_4x2.pmd",
    ]

    first = conv.convert_models(paths)
    reasons = dict(first.failed)
    assert first.converted == ["/vehicle/truck/test/chassis/chs_4x2.pmd"]
    assert reasons["/vehicle/truck/test/cabin/cab_high.pmd"].startswith("exit code 3")
    assert reasons["/vehicle/truck/test/acc/f_bumper/b.pmd"].startswith("<error> [model]")
    assert reasons["/vehicle/truck/test/chassis/chs_6x2.pmd"].startswith("no .pim written")
    assert reasons["/vehicle/truck/test/missing.pmd"].startswith("<error> [model]")

    manifest = json.loads((tmp_path / "cache/manifest.json").read_text())
    assert manifest["models"]["/vehicle/truck/test/acc/f_bumper/b.pmd"]["status"] == "failed"
    assert "stdout_tail" in manifest["models"]["/vehicle/truck/test/acc/f_bumper/b.pmd"]

    converter_calls.reset()
    second = converter_factory(game, tmp_path).convert_models(paths)
    assert len(second.failed_before) == 4
    assert second.cached == ["/vehicle/truck/test/chassis/chs_4x2.pmd"]
    assert converter_calls("-m") == []

    retried = converter_factory(game, tmp_path).convert_models(paths, retry_failed=True)
    assert len(retried.failed) == 4
    assert len(converter_calls("-m")) == 4


def test_unsafe_model_paths_are_rejected_before_conversion(game, tmp_path, converter_factory, converter_calls):
    result = converter_factory(game, tmp_path).convert_models(
        ["/vehicle/../../evil.pmd", "C:/x.pmd", "relative.pmd"]
    )

    assert len(result.rejected) == 3
    assert converter_calls("-m") == []


def test_missing_converted_output_is_rebuilt(game, tmp_path, converter_factory, converter_calls):
    path = "/vehicle/truck/test/chassis/chs_4x2.pmd"
    converter_factory(game, tmp_path).convert_models([path])
    (tmp_path / "cache/base/vehicle/truck/test/chassis/chs_4x2.pim").unlink()
    converter_calls.reset()

    result = converter_factory(game, tmp_path).convert_models([path])

    assert result.converted == [path]
    assert len(converter_calls("-m")) == 1


def _max_parallel_overlap(times_file: Path) -> int:
    runs = [json.loads(line) for line in times_file.read_text(encoding="utf-8").splitlines()]
    return max(sum(1 for other in runs if other["t0"] <= run["t0"] < other["t1"]) for run in runs)


def test_parallel_conversion_matches_serial_results(game, tmp_path, monkeypatch, converter_factory):
    paths = [
        "/vehicle/truck/upgrade/toy/fresh.pmd",
        "/vehicle/truck/upgrade/horn/ahooga.pmd",
        "/vehicle/truck/test/acc/trlr_cables/stock.pmd",
        "/vehicle/truck/test/acc/f_bumper/a_uk.pmd",
    ]
    serial_root, parallel_root = tmp_path / "serial", tmp_path / "parallel"
    serial_root.mkdir()
    parallel_root.mkdir()
    monkeypatch.setenv("FAKE_CPX_TIMES", str(tmp_path / "serial-times.jsonl"))
    serial = converter_factory(game, serial_root, jobs=1).convert_models(paths)
    monkeypatch.setenv("FAKE_CPX_TIMES", str(tmp_path / "parallel-times.jsonl"))
    callback_threads = []
    main_thread = threading.get_ident()
    parallel_conv = converter_factory(game, parallel_root, jobs=4)
    parallel = parallel_conv.convert_models(
        paths, progress=lambda done, total, key: callback_threads.append(threading.get_ident())
    )

    assert sorted(serial.converted) == sorted(parallel.converted) == sorted(paths)
    assert _max_parallel_overlap(tmp_path / "serial-times.jsonl") == 1
    assert _max_parallel_overlap(tmp_path / "parallel-times.jsonl") >= 2
    assert len(callback_threads) == len(paths)
    assert set(callback_threads) == {main_thread}

    def converted_files(root):
        base = root / "cache/base"
        return {path.relative_to(base).as_posix() for path in base.rglob("*") if path.is_file()}

    assert converted_files(serial_root) == converted_files(parallel_root)
