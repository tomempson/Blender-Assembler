# SPDX-License-Identifier: GPL-3.0-or-later
"""Selection serialization and input validation."""

import pytest

from blender_assembler.core.truck.selection import Selection


def test_selection_json_roundtrip_and_validation(tmp_path):
    selection = Selection("t", "c", None, None, {"s": "u"}, rhd=True)
    path = tmp_path / "selection.json"
    path.write_text(selection.to_json(), encoding="utf-8")
    assert Selection.load(path) == selection

    invalid_values = (
        {},
        {"truck": 1},
        {"truck": "t", "accessories": {"s": 1}},
        {"truck": "t", "chassis": 5},
    )
    for value in invalid_values:
        with pytest.raises(ValueError):
            Selection.from_dict(value)
