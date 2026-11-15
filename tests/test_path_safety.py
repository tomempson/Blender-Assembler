# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from blender_assembler.core.path_safety import (is_safe_segment, is_within, join_game_path,
                                                 normalize_game_path)
from blender_assembler.core.sii import SiiIncludeError, parse_file
from blender_assembler.core.truck import scan_truck


@pytest.mark.parametrize("path, expected", [
    ("/vehicle/truck/x/cabin.pmd", "/vehicle/truck/x/cabin.pmd"),
    ("/def//vehicle/x.sii", "/def/vehicle/x.sii"),
    ("/../outside.sii", None),
    ("/def/../../x", None),
    ("/def/./x", None),
    ("/C:/Windows/win.ini", None),
    ("/def\\..\\x", None),
    ("relative/path", None),
    ("/", None),
    ("/a/b\x00c", None),
    ("//server/share/x", "/server/share/x"),     # collapses to a plain game path under root
])
def test_normalize_game_path(path, expected):
    assert normalize_game_path(path) == expected


def test_join_and_within(tmp_path):
    assert join_game_path(tmp_path, "/a/b.sii") == tmp_path / "a" / "b.sii"
    assert join_game_path(tmp_path, "/a/../../b") is None
    assert is_within(tmp_path, tmp_path / "a" / ".." / "b")
    assert not is_within(tmp_path / "a", tmp_path / "a" / ".." / "b")
    assert is_safe_segment("f_tire") and not is_safe_segment("..") and not is_safe_segment("a/b")
    assert not is_safe_segment("C:") and not is_safe_segment("")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("ref", ["/../../secret.sui", "/C:/secret.sui", "../../secret.sui",
                                 "C:/secret.sui", "/def/..\\..\\secret.sui"])
def test_includes_cannot_leave_the_defs_root(tmp_path, ref):
    defs = tmp_path / "defs"
    _write(tmp_path / "secret.sui", "\tleaked: 1\n")
    main = _write(defs / "def" / "m.sii", f'SiiNunit\n{{\na : b\n{{\n@include "{ref}"\n}}\n}}\n')
    with pytest.raises(SiiIncludeError, match="safe game path|outside the defs root"):
        parse_file(main, defs)


def test_scanner_never_probes_unsafe_def_paths(tmp_path):
    defs = tmp_path / "defs"
    t = defs / "def" / "vehicle" / "truck" / "evil.truck"
    _write(tmp_path / "outside.sii", "SiiNunit { }")
    _write(t / "data.sii", 'SiiNunit\n{\naccessory_truck_data : vehicle.evil.truck\n{\n'
                           '\tfallback[]: "..|../../../outside.sii"\n}\n}\n')
    _write(t / "chassis" / "c.sii", 'SiiNunit\n{\naccessory_chassis_data : c.evil.truck.chassis\n{\n'
                                    '\tdefaults[]: "/def/../../outside.sii"\n'
                                    '\tdefaults[]: "/C:/Windows/win.ini"\n}\n}\n')
    defn = scan_truck(defs, "evil.truck")
    statuses = [r.status for r in defn.chassis[0].defaults]
    assert statuses == ["outside_root", "outside_root"]
    assert any("not a safe path" in w for w in defn.warnings)
