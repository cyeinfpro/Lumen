from __future__ import annotations

from dataclasses import fields
import inspect

from app import realtime
from app.realtime import EventStreamState
from app.routes import events


def test_realtime_capabilities_are_exported_through_public_index() -> None:
    expected = {
        "validate_channels",
        "iter_replay_events",
        "replay_connection_events",
        "stream_events",
        "EventStreamState",
        "ConnectionEventDeduper",
    }

    assert expected.issubset(set(realtime.__all__))


def test_event_stream_state_does_not_retain_fastapi_request() -> None:
    state_fields = {item.name for item in fields(EventStreamState)}

    assert "is_disconnected" in state_fields
    assert "request" not in state_fields


def test_events_route_only_composes_transport_and_realtime_public_api() -> None:
    source = inspect.getsource(events)

    assert "from ..realtime import (" in source
    assert "from ..realtime." not in source
    assert "select(" not in source
    assert ".xadd(" not in source
    assert ".get_message(" not in source
    assert len(source.splitlines()) <= 300
