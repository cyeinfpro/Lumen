from copy import deepcopy
from types import SimpleNamespace
from lumen_core.canvas import canvas_node_definition_hash
from lumen_core.canvas_freshness import execution_freshness
from lumen_core.canvas_schemas import CanvasGraph


def fixture():
    def node(identifier, kind, config=None):
        return {
            "id": identifier,
            "type": kind,
            "position": {"x": 0, "y": 0},
            "config": config or {},
        }

    def edge(identifier, source, target, handle, data_type, **extra):
        return {
            "id": identifier,
            "source_node_id": source,
            "target_node_id": target,
            "source_handle": "text" if data_type == "text" else "image",
            "target_handle": handle,
            "data_type": data_type,
            **extra,
        }

    graph = CanvasGraph.model_validate(
        {
            "nodes": [
                node("p", "prompt", {"text": "draw"}),
                node("q", "prompt", {"text": "edit"}),
                node("a", "image_generate"),
                node("b", "image_edit"),
                node("c", "image_edit"),
            ],
            "edges": [
                edge("pa", "p", "a", "prompt", "text"),
                edge("qb", "q", "b", "prompt", "text"),
                edge("qc", "q", "c", "prompt", "text"),
                edge("ab", "a", "b", "source", "image"),
                edge(
                    "ac",
                    "a",
                    "c",
                    "source",
                    "image",
                    binding_mode="pinned",
                    pinned_execution_id="exec-a",
                    pinned_output_index=0,
                ),
            ],
        }
    ).model_dump(mode="json")
    rows = []
    for node in graph["nodes"][2:]:
        incoming = sorted(
            [edge for edge in graph["edges"] if edge["target_node_id"] == node["id"]],
            key=lambda edge: (edge["target_handle"], edge["order"], edge["id"]),
        )
        bindings = []
        for edge in incoming:
            binding = {
                key: edge[key]
                for key in (
                    "source_node_id",
                    "target_handle",
                    "role",
                    "order",
                    "binding_mode",
                )
            }
            binding["order"] = int(edge["order"] or 0)
            binding["edge_id"] = edge["id"]
            if edge["data_type"] == "text":
                binding["text"] = "draw" if node["id"] == "a" else "edit"
            else:
                binding.update(
                    source_execution_id="exec-a",
                    output_index=0,
                    asset={"image_id": "image-a"},
                )
            bindings.append(binding)
        rows.append(
            SimpleNamespace(
                id="exec-" + node["id"],
                node_id=node["id"],
                definition_hash=canvas_node_definition_hash(node),
                input_snapshot_jsonb={
                    "prompt": "draw" if node["id"] == "a" else "edit",
                    "bindings": bindings,
                },
            )
        )
    selections = [
        SimpleNamespace(node_id=row.node_id, execution_id=row.id, output_index=0)
        for row in rows
    ]
    return graph, rows, selections


def test_layout_is_not_semantic_stale():
    graph, rows, selections = fixture()
    graph["nodes"][2]["position"]["x"] = 100
    result = execution_freshness(graph, rows, selections)
    assert result["stale_node_ids"] == []
    assert {item["state"] for item in result["execution_freshness"].values()} == {
        "fresh"
    }


def test_changed_prompt_propagates_follow_active_but_not_pinned():
    graph, rows, selections = fixture()
    graph["nodes"][0]["config"]["text"] = "new prompt"
    result = execution_freshness(graph, rows, selections)
    assert result["stale_node_ids"] == ["a", "b"]
    assert result["execution_freshness"]["exec-a"]["reason"] == "inputs_changed"
    assert result["execution_freshness"]["exec-b"]["reason"] == "upstream_changed"
    assert result["execution_freshness"]["exec-c"]["state"] == "fresh"


def test_selection_change_compares_exact_execution_and_output():
    graph, rows, selections = fixture()
    selections[0].output_index = 1
    result = execution_freshness(graph, rows, selections)
    assert result["execution_freshness"]["exec-b"]["reason"] == "inputs_changed"
    assert result["execution_freshness"]["exec-c"]["state"] == "fresh"


def test_old_snapshot_is_unknown_and_old_candidate_does_not_taint_new_selection():
    graph, rows, selections = fixture()
    previous = deepcopy(rows[0])
    previous.id = "old-a"
    previous.definition_hash = "old"
    rows.append(previous)
    result = execution_freshness(graph, rows, selections)
    assert result["stale_node_ids"] == []
    assert result["execution_freshness"]["old-a"]["state"] == "stale"
    rows[0].input_snapshot_jsonb = {}
    result = execution_freshness(graph, rows, selections)
    assert result["execution_freshness"]["exec-a"]["state"] == "unknown"
