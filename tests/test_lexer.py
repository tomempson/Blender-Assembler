# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from blender_assembler.core.sii.errors import SiiEncodedError, SiiSyntaxError
from blender_assembler.core.sii.lexer import (EOF, HEXFLOAT, NUMBER, STRING, SYM, WORD, check_encoded,
                                                 decode_text, tokenize)

P = Path("t.sii")


def kinds_values(text):
    return [(lx.kind, lx.value) for lx in tokenize(text, P) if lx.kind != EOF]


def test_basic_tokens():
    assert kinds_values('key: "val" 12 -3.5 1e3 &3f800000 { } ( ) [ ] , ;') == [
        (WORD, "key"), (SYM, ":"), (STRING, "val"), (NUMBER, "12"), (NUMBER, "-3.5"), (NUMBER, "1e3"),
        (HEXFLOAT, "&3f800000"), (SYM, "{"), (SYM, "}"), (SYM, "("), (SYM, ")"), (SYM, "["), (SYM, "]"),
        (SYM, ","), (SYM, ";"),
    ]


@pytest.mark.parametrize("word", [
    "scania.s_2016.cabin_a", ".air_cable_b", "4x2.scania.s_2016.chassis", "_nameless.3446.8ec0",
    "6x2_4_ml.scania.s_2016.chassis", "mx11_270",
])
def test_dotted_and_digit_leading_words_stay_one_token(word):
    assert kinds_values(word) == [(WORD, word)]


def test_c_style_float_suffix_is_a_number():
    assert kinds_values("(0.0f, -0.008f, 1f)") == [
        (SYM, "("), (NUMBER, "0.0f"), (SYM, ","), (NUMBER, "-0.008f"), (SYM, ","), (NUMBER, "1f"), (SYM, ")"),
    ]


def test_comments_all_three_kinds_and_comment_markers_inside_strings_kept():
    text = 'a: 1 # hash\nb: 2 // slashes\n/* block\n comment */ c: "x // y # z"'
    assert kinds_values(text) == [
        (WORD, "a"), (SYM, ":"), (NUMBER, "1"), (WORD, "b"), (SYM, ":"), (NUMBER, "2"),
        (WORD, "c"), (SYM, ":"), (STRING, "x // y # z"),
    ]


def test_string_escapes():
    assert kinds_values(r'"a\"b\\c\nd \q"') == [(STRING, 'a"b\\c\nd \\q')]


def test_line_numbers_counted_across_block_comments():
    toks = tokenize("a\n/* x\ny\n*/\nb", P)
    assert [(t.value, t.line) for t in toks if t.kind == WORD] == [("a", 1), ("b", 5)]


def test_unterminated_string_reports_line():
    with pytest.raises(SiiSyntaxError) as exc:
        tokenize('a: 1\nb: "oops\n', P)
    assert exc.value.line == 2 and "unterminated string" in str(exc.value)
    assert "t.sii:2" in str(exc.value)


def test_unterminated_block_comment():
    with pytest.raises(SiiSyntaxError, match="unterminated /\\*"):
        tokenize("a /* never closed", P)


def test_unexpected_character_reports_line():
    with pytest.raises(SiiSyntaxError) as exc:
        tokenize("a\nb\n  $", P)
    assert exc.value.line == 3


def test_bom_is_stripped():
    text = decode_text(b"\xef\xbb\xbfSiiNunit", P)
    assert kinds_values(text) == [(WORD, "SiiNunit")]


@pytest.mark.parametrize("data", [b"ScsC\x01\x02", b"BSII\x00", b"3nK\x01", b"  \r\nScsC", b"SiiNunit\x00{}"])
def test_encoded_files_rejected(data):
    with pytest.raises(SiiEncodedError):
        check_encoded(data, P)


def test_invalid_utf8_is_a_syntax_error_with_line():
    with pytest.raises(SiiSyntaxError) as exc:
        decode_text(b"SiiNunit\n{\n\xff}", P)
    assert exc.value.line == 3
