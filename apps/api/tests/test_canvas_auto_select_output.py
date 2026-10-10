from types import SimpleNamespace

import pytest

from app.canvas_services import read_repair


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self.value


class SelectionSession:
    def __init__(self, selection):
        self.selection = selection
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        if self.calls == 1:
            return Result(SimpleNamespace(graph_jsonb={"nodes": [{"id": "a"}]}))
        return Result([self.selection])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outputs", "ordinal", "locked", "expected"),
    [
        ([{"ordinal": 0}, {"ordinal": 1}], 1, False, 1),
        ([{"ordinal": 1}], 1, False, 0),
        ([{"ordinal": 1}], 0, False, None),
        ([{"ordinal": 0}, {"ordinal": 1}], 1, True, None),
    ],
)
async def test_read_repair_selects_exact_candidate_and_preserves_cas(
    monkeypatch, outputs, ordinal, locked, expected
):
    monkeypatch.setattr(
        read_repair, "canvas_node_definition_hash", lambda _node: "definition"
    )
    monkeypatch.setattr(
        read_repair, "canvas_input_snapshot_matches_graph", lambda *_a, **_kw: True
    )
    selection = SimpleNamespace(
        node_id="a", execution_id="old", output_index=0, revision=7, locked=locked
    )
    db = SelectionSession(selection)
    execution = SimpleNamespace(
        id="new",
        node_id="a",
        canvas_id="canvas",
        definition_hash="definition",
        selection_base_revision=7,
        input_snapshot_jsonb={},
        config_snapshot_jsonb={
            "_canvas": {
                "auto_select_on_success": True,
                "planned_output_ordinal": ordinal,
            }
        },
    )
    changed = await read_repair._auto_select(
        db, user_id="user", execution=execution, outputs=outputs
    )
    assert changed is (expected is not None)
    if expected is None:
        assert selection.execution_id == "old" and selection.revision == 7
    else:
        assert selection.execution_id == "new" and selection.output_index == expected
        assert selection.revision == 8
