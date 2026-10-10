from __future__ import annotations

import pytest

from lumen_core.canvas_realtime import CANVAS_RUN_EVENT, canvas_run_notification


def test_canvas_notice_is_minimal_stable_and_sequence_specific():
    args = dict(
        canvas_id="canvas-1",
        run_id="run-1",
        seq=3,
        execution_id="execution-1",
        event_type="execution.succeeded",
    )
    first = canvas_run_notification(**args)
    assert CANVAS_RUN_EVENT == "canvas.run.updated"
    assert first == canvas_run_notification(**args)
    assert set(first) == {
        "schema_version",
        "canvas_id",
        "run_id",
        "seq",
        "execution_id",
        "event_type",
        "event_id",
    }
    assert first["event_id"] == "canvas-run:run-1:3"
    assert (
        canvas_run_notification(**{**args, "seq": 4})["event_id"] != first["event_id"]
    )
    assert (
        canvas_run_notification(
            canvas_id="c", run_id="r", seq=1, event_type="run.created"
        )["execution_id"]
        is None
    )


@pytest.mark.parametrize("seq", [0, -1, True, 1.5, "1", 2**53])
def test_canvas_notice_rejects_unsafe_sequence(seq):
    with pytest.raises(ValueError):
        canvas_run_notification(
            canvas_id="c", run_id="r", seq=seq, event_type="run.created"
        )


@pytest.mark.parametrize(
    "override",
    [
        {"canvas_id": ""},
        {"run_id": None},
        {"execution_id": ""},
        {"event_type": "https://provider.invalid/private"},
        {"event_type": "succeeded\nprivate"},
        {"event_type": "x" * 81},
    ],
)
def test_canvas_notice_rejects_invalid_identity_and_raw_event_content(override):
    with pytest.raises(ValueError):
        canvas_run_notification(
            **{
                "canvas_id": "c",
                "run_id": "r",
                "seq": 1,
                "event_type": "run.created",
                **override,
            }
        )
