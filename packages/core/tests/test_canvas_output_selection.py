import pytest

from lumen_core.canvas_output_selection import canvas_auto_select_output_index


@pytest.mark.parametrize(
    ("ordinal", "outputs", "expected"),
    [
        (1, [{"ordinal": 0}, {"ordinal": 1}], 1),
        (1, [{"ordinal": 1}], 0),
        (0, [{"ordinal": 1}], None),
        (1, [{"ordinal": 0}], None),
        (1, [{"ordinal": 1}, {"ordinal": 1}], None),
        (1, [{"image_id": "legacy"}], None),
        (True, [{"ordinal": 1}], None),
        (-1, [{"ordinal": -1}], None),
        (10, [{"ordinal": 10}], None),
        ("1", [{"ordinal": 1}], None),
    ],
)
def test_planned_candidate_never_substitutes_another_output(ordinal, outputs, expected):
    snapshot = {"_canvas": {"planned_output_ordinal": ordinal}}
    assert canvas_auto_select_output_index(snapshot, outputs) == expected


def test_legacy_single_node_keeps_first_available_output():
    assert canvas_auto_select_output_index({}, [{"ordinal": 1}]) == 0
    assert canvas_auto_select_output_index(None, []) is None
