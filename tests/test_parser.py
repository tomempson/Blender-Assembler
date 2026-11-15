# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from blender_assembler.core.sii import Placement, SiiError, SiiSyntaxError, Token, parse_text


def one_unit(body: str, cls="test_data", name="u.test"):
    sii = parse_text(f"SiiNunit\n{{\n{cls} : {name}\n{{\n{body}\n}}\n}}\n", "f.sii")
    assert len(sii.units) == 1
    return sii.units[0], sii.warnings


def test_value_types():
    u, w = one_unit("""
        s: "text"
        t: some.token
        i: 42
        neg: -7
        f: 1.5
        h: &3f800000
        v: (1, 2.5, -3)
        p: (1, 2, 3) (1; 0, 0, 0)
        n: nil
        yes: true
        no: false
        ff: -0.008f
    """)
    a = u.attrs
    assert a["s"] == "text" and not isinstance(a["s"], Token)
    assert a["t"] == "some.token" and isinstance(a["t"], Token)
    assert a["i"] == 42 and isinstance(a["i"], int)
    assert a["neg"] == -7
    assert a["f"] == 1.5
    assert a["h"] == 1.0
    assert a["v"] == (1, 2.5, -3)
    assert a["p"] == Placement((1, 2, 3), (1, 0, 0, 0))
    assert a["n"] is None and a["yes"] is True and a["no"] is False
    assert a["ff"] == pytest.approx(-0.008)
    assert w == []


def test_unit_header_and_line():
    sii = parse_text("SiiNunit\n{\n\naccessory_addon_data :  x.y.z\n{\n}\n}", "f.sii")
    u = sii.units[0]
    assert (u.class_name, u.name, u.line) == ("accessory_addon_data", "x.y.z", 4)


def test_array_forms_are_identical():
    appended, _ = one_unit('k[]: "a"\nk[]: "b"\nk[]: "c"')
    indexed, _ = one_unit('k: 3\nk[0]: "a"\nk[1]: "b"\nk[2]: "c"')
    count_append, w = one_unit('k: 3\nk[]: "a"\nk[]: "b"\nk[]: "c"')
    assert appended.attrs["k"] == indexed.attrs["k"] == count_append.attrs["k"] == ["a", "b", "c"]
    assert w == []


def test_count_with_missing_indices_fills_none_and_warns():
    u, w = one_unit('k: 3\nk[0]: "a"\nk[2]: "c"')
    assert u.attrs["k"] == ["a", None, "c"]
    assert any("missing" in x for x in w)


def test_count_mismatch_warns():
    u, w = one_unit('k: 2\nk[]: "a"\nk[]: "b"\nk[]: "c"')
    assert u.attrs["k"] == ["a", "b", "c"]
    assert any("declares 2 item(s) but 3" in x for x in w)


def test_short_count_pads_with_none():
    u, w = one_unit('k: 3\nk[]: "a"')
    assert u.attrs["k"] == ["a", None, None]
    assert w


def test_scalar_redefinition_last_wins_with_both_lines():
    u, w = one_unit("a: 1\na: 2")
    assert u.attrs["a"] == 2
    assert len(w) == 1 and "redefined" in w[0] and "first at line 5" in w[0] and "f.sii:6" in w[0]


def test_get_list():
    u, _ = one_unit('one: "x"\nmany[]: "a"')
    assert u.get_list("one") == ["x"] and u.get_list("many") == ["a"] and u.get_list("absent") == []


def test_local_units_and_multiple_units():
    sii = parse_text("SiiNunit { a : x.y { } b : .local { k: v } }", "f.sii")
    assert [u.name for u in sii.units] == ["x.y", ".local"]
    assert sii.units[1].is_local


@pytest.mark.parametrize("text, line", [
    ("SiiNunit\n{\na : b\n{\n  key \"v\"\n}\n}", 5),           # missing colon
    ("SiiNunit\n{\na : b\n{\n  k: 1\n", 6),                     # truncated
    ("NotSii\n{\n}", 1),                                         # wrong magic
    ("SiiNunit\n{\na b\n{\n}\n}", 3),                            # missing colon in header
    ("SiiNunit\n{\na : b\n{\n  k[x]: 1\n}\n}", 5),              # bad index
    ("SiiNunit\n{\n}\nextra", 4),                                # trailing junk
])
def test_errors_carry_path_and_line(text, line):
    with pytest.raises(SiiSyntaxError) as exc:
        parse_text(text, "bad.sii")
    assert exc.value.line == line
    assert f"bad.sii:{line}" in str(exc.value)


VALID = """SiiNunit
{
accessory_addon_data : a.test.truck.f_bumper
{
	name: "@@x@@"
	v: (1, 2, 3)
	p: (1, 2, 3) (1; 0, 0, 0)
	k: 2
	k[]: "a"
	k[1]: "b"
}
}
"""


def test_truncated_files_only_ever_raise_sii_errors_with_a_line():
    for cut in range(len(VALID)):
        try:
            parse_text(VALID[:cut], "trunc.sii")
        except SiiError as exc:
            assert exc.line >= 1, (cut, exc)
            assert "trunc.sii" in str(exc)
    assert parse_text(VALID, "ok.sii").units
