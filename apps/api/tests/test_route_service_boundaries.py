from app.routes import billing, conversations, events
from app.services import conversation_cleanup, event_replay
from app.services.billing import wallet_activity


def test_billing_route_reexports_wallet_activity_helpers() -> None:
    assert billing._money is wallet_activity.money_out
    assert billing._wallet_activity_24h is wallet_activity.wallet_activity_24h
    assert (
        billing._wallet_activity_window_end
        is wallet_activity.wallet_activity_window_end
    )


def test_conversation_route_reexports_cleanup_helpers() -> None:
    assert (
        conversations._cancel_conversation_memory_extractions
        is conversation_cleanup.cancel_conversation_memory_extractions
    )
    assert (
        conversations._conversation_wallet_exists
        is conversation_cleanup.conversation_wallet_exists
    )
    assert (
        conversations._release_conversation_generation_queue_state
        is conversation_cleanup.release_conversation_generation_queue_state
    )


def test_events_route_reexports_replay_helpers() -> None:
    assert events._normalize_event_id is event_replay.normalize_event_id
    assert (
        events._replay_payload_matches_channels
        is event_replay.replay_payload_matches_channels
    )
    assert events._stream_high_water_id is event_replay.stream_high_water_id
