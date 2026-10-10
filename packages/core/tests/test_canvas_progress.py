from types import SimpleNamespace
from lumen_core.canvas_progress import canvas_progress_snapshot


def test_progress_unknown_stays_unknown_and_private_fields_never_escape():
    owner = SimpleNamespace(
        id="task",
        status="submit_unknown",
        progress_stage="submitting",
        progress_pct=True,
        upstream_request={"api_key": "secret"},
    )
    digest, items = canvas_progress_snapshot([owner])
    assert items[0]["progress_pct"] is None
    assert "secret" not in str(items)
    assert items[0]["status"] == "submit_unknown"
    assert digest == canvas_progress_snapshot([owner])[0]


def test_only_real_bounded_progress_changes_digest():
    owner = SimpleNamespace(
        id="task", status="running", progress_stage="rendering", progress_pct=20
    )
    before = canvas_progress_snapshot([owner])[0]
    owner.progress_pct = 21
    assert canvas_progress_snapshot([owner])[0] != before
    owner.progress_pct = 101
    assert canvas_progress_snapshot([owner])[1][0]["progress_pct"] is None
