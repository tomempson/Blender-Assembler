# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from blender_assembler.core.sii import SiiEncodedError, SiiIncludeError, SiiSyntaxError, parse_file


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "defs"
    write(root / "def/shared/attrs.sui", '\tfrom_shared: 1\n@include "deeper/more.sui"\n')
    write(root / "def/shared/deeper/more.sui", '\tfrom_deeper[]: "x"\n')
    write(root / "def/shared/units.sui", "extra_data : .extra\n{\n\tk: v\n}\n")
    write(root / "def/truck/t/local.sui", "\tfrom_local: 2\n")
    main = write(root / "def/truck/t/main.sii",
                 "SiiNunit\n{\nthing_data : a.t\n{\n"
                 '@include "/def/shared/attrs.sui"\n'
                 '@include "local.sui"\n'
                 "}\n"
                 '@include "/def/shared/units.sui"\n'
                 "}\n")
    return root, main


def test_nested_includes_across_directories(tree):
    root, main = tree
    sii = parse_file(main, root)
    assert [u.name for u in sii.units] == ["a.t", ".extra"]
    a = sii.units[0].attrs
    assert a == {"from_shared": 1, "from_deeper": ["x"], "from_local": 2}
    extra = sii.units[1]
    assert extra.source == main and extra.line == 8          # include site line in main.sii
    assert extra.origin[0].name == "units.sui" and extra.origin[1] == 1


def test_absolute_include_without_defs_root_fails(tree):
    _, main = tree
    with pytest.raises(SiiIncludeError, match="no defs root"):
        parse_file(main, None)


def test_missing_include_reports_site(tmp_path):
    main = write(tmp_path / "m.sii", 'SiiNunit\n{\na : b\n{\n@include "gone.sui"\n}\n}\n')
    with pytest.raises(SiiIncludeError) as exc:
        parse_file(main, tmp_path)
    assert exc.value.line == 5 and "gone.sui" in str(exc.value)


def test_cycle_raises_with_chain(tmp_path):
    write(tmp_path / "x/a.sui", '@include "../y/b.sui"\n')
    write(tmp_path / "y/b.sui", '@include "../x/a.sui"\n')
    main = write(tmp_path / "m.sii", 'SiiNunit\n{\na : b\n{\n@include "x/a.sui"\n}\n}\n')
    with pytest.raises(SiiIncludeError) as exc:
        parse_file(main, tmp_path)
    msg = str(exc.value)
    assert "include cycle" in msg
    assert "a.sui" in msg and "b.sui" in msg and "m.sii" in msg
    assert "included from" in msg


def test_error_inside_included_file_points_into_it(tmp_path):
    write(tmp_path / "frag.sui", "\tok: 1\n\tbroken \"no colon\"\n")
    main = write(tmp_path / "m.sii", 'SiiNunit\n{\na : b\n{\n@include "frag.sui"\n}\n}\n')
    with pytest.raises(SiiSyntaxError) as exc:
        parse_file(main, tmp_path)
    assert exc.value.path.name == "frag.sui" and exc.value.line == 2
    assert exc.value.chain == ((main, 5),)
    assert "included from" in str(exc.value) and "m.sii:5" in str(exc.value)


def test_encoded_file_raises(tmp_path):
    main = write(tmp_path / "enc.sii", b"ScsC\x01\x00\x00")
    with pytest.raises(SiiEncodedError, match="decode it first"):
        parse_file(main, tmp_path)


def test_encoded_include_raises_with_chain(tmp_path):
    write(tmp_path / "enc.sui", b"BSII\x00\x01")
    main = write(tmp_path / "m.sii", 'SiiNunit\n{\na : b\n{\n@include "enc.sui"\n}\n}\n')
    with pytest.raises(SiiEncodedError) as exc:
        parse_file(main, tmp_path)
    assert exc.value.chain == ((main, 5),)


def test_include_needs_a_quoted_path(tmp_path):
    main = write(tmp_path / "m.sii", "SiiNunit\n{\na : b\n{\n@include bare.sui\n}\n}\n")
    with pytest.raises(SiiIncludeError, match="quoted path"):
        parse_file(main, tmp_path)
