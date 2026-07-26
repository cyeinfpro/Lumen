from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app import listener  # noqa: E402


class ActiveUsersPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def zremrangebyscore(self, *args: object) -> "ActiveUsersPipeline":
        self.calls.append(("zremrangebyscore", *args))
        return self

    def zrangebyscore(self, *args: object) -> "ActiveUsersPipeline":
        self.calls.append(("zrangebyscore", *args))
        return self

    async def execute(self) -> list[object]:
        return [1, [b"user-1", b"user-2", b""]]


class ActiveUsersRedis:
    def __init__(self) -> None:
        self.pipe = ActiveUsersPipeline()

    def pipeline(self, *, transaction: bool) -> ActiveUsersPipeline:
        assert transaction is False
        return self.pipe

    async def set(self, key: str, _value: object, **kwargs: object) -> bool:
        assert key == listener._FALLBACK_SCAN_LEASE_KEY
        assert kwargs == {
            "nx": True,
            "ex": listener._FALLBACK_SCAN_LEASE_SECONDS,
        }
        return False


class EmptyActivePipeline:
    def zremrangebyscore(self, *_args: object) -> "EmptyActivePipeline":
        return self

    def zrangebyscore(self, *_args: object) -> "EmptyActivePipeline":
        return self

    async def execute(self) -> list[object]:
        return [0, []]


class ExistingTrackerPipeline:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def exists(self, key: str) -> "ExistingTrackerPipeline":
        self.keys.append(key)
        return self

    async def execute(self) -> list[object]:
        return [key.endswith("gen-legacy") for key in self.keys]


class FallbackRedis:
    def __init__(self, *, acquire_lease: bool = True) -> None:
        self.acquire_lease = acquire_lease
        self.pipeline_calls = 0
        self.scan_calls: list[dict[str, object]] = []
        self.xrevrange_calls: list[dict[str, object]] = []
        self.cursor_writes: list[tuple[str, object, dict[str, object]]] = []

    def pipeline(self, *, transaction: bool) -> object:
        assert transaction is False
        self.pipeline_calls += 1
        if self.pipeline_calls == 1:
            return EmptyActivePipeline()
        return ExistingTrackerPipeline()

    async def set(
        self,
        key: str,
        value: object,
        **kwargs: object,
    ) -> bool:
        if key == listener._FALLBACK_SCAN_LEASE_KEY:
            assert value == b"1"
            assert kwargs == {
                "nx": True,
                "ex": listener._FALLBACK_SCAN_LEASE_SECONDS,
            }
            return self.acquire_lease
        self.cursor_writes.append((key, value, kwargs))
        return True

    async def get(self, key: str) -> None:
        assert key in {
            listener._FALLBACK_SCAN_CURSOR_KEY,
            listener._fallback_stream_cursor_key("legacy-user"),
        }
        return None

    async def scan(self, **kwargs: object) -> tuple[int, list[bytes]]:
        if not self.acquire_lease:
            raise AssertionError("lease loser must not scan")
        self.scan_calls.append(kwargs)
        return 0, [b"events:user:legacy-user", b"events:user:web-only:dlq"]

    async def xrevrange(self, key: str, **kwargs: object) -> list[object]:
        self.xrevrange_calls.append({"key": key, **kwargs})
        return [
            (
                b"123-0",
                {
                    b"data": json.dumps(
                        {
                            "event": "generation.progress",
                            "data": {"generation_id": "gen-legacy"},
                        }
                    ).encode(),
                },
            )
        ]


class RecoveringTracker:
    def __init__(self) -> None:
        self.refreshes: list[tuple[str, str]] = []

    async def refresh(self, gen_id: str, user_id: str) -> bool:
        self.refreshes.append((gen_id, user_id))
        return True


class RefreshingDispatchTracker:
    def __init__(self) -> None:
        self.refreshes: list[tuple[str, str]] = []
        self.track = SimpleNamespace(
            chat_id=1,
            status_message_id=2,
            prompt="p",
            batch_id="",
        )

    async def get(self, _gen_id: str) -> object:
        return self.track

    async def refresh(self, gen_id: str, user_id: str) -> bool:
        self.refreshes.append((gen_id, user_id))
        return True


class CursorRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict[str, object]]] = []

    async def set(self, key: str, value: object, **kwargs: object) -> bool:
        self.calls.append((key, value, kwargs))
        return True


class BusyTracker:
    async def begin_delivery(self, _gen_id: str) -> bool:
        return False

    async def is_notified(self, _gen_id: str) -> bool:
        return False


class NotifiedTracker:
    async def begin_delivery(self, _gen_id: str) -> bool:
        return False

    async def is_notified(self, _gen_id: str) -> bool:
        return True

    async def is_delivery_active(self, _gen_id: str) -> bool:
        return False


class ActiveNotifiedTracker(NotifiedTracker):
    async def is_delivery_active(self, _gen_id: str) -> bool:
        return True


class RecordingTracker:
    def __init__(
        self,
        events: list[object],
        *,
        batch_remaining: int | None = 0,
        sent: set[str] | None = None,
    ) -> None:
        self.events = events
        self.batch_remaining = batch_remaining
        self.sent: set[str] = set(sent or ())

    async def sent_images(self, _gen_id: str) -> set[str]:
        return set(self.sent)

    async def mark_image_sent(self, gen_id: str, image_id: str) -> None:
        self.sent.add(image_id)
        self.events.append(("sent_image", gen_id, image_id))

    async def begin_delivery(self, gen_id: str) -> bool:
        self.events.append(("begin", gen_id))
        return True

    async def is_notified(self, _gen_id: str) -> bool:
        return False

    async def is_delivery_active(self, _gen_id: str) -> bool:
        return False

    async def mark_notified(self, gen_id: str, *, release_lock: bool = True) -> bool:
        self.events.append(("mark", gen_id, release_lock))
        return True

    async def clear_delivery(self, gen_id: str) -> None:
        self.events.append(("clear", gen_id))

    async def batch_decr(self, batch_id: str, gen_id: str = "") -> int | None:
        self.events.append(("batch_decr", batch_id, gen_id))
        return self.batch_remaining

    async def batch_remove(self, batch_id: str) -> None:
        self.events.append(("batch_remove", batch_id))


class RecordingBot:
    def __init__(self, events: list[object], *, fail_edit: bool = False) -> None:
        self.events = events
        self.fail_edit = fail_edit

    async def edit_message_text(self, **_kwargs) -> None:
        self.events.append("edit")
        if self.fail_edit:
            raise RuntimeError("edit failed")

    async def send_message(self, **_kwargs) -> None:
        self.events.append("send")

    async def delete_message(self, **_kwargs) -> None:
        self.events.append("delete")


class RecordingApi:
    def __init__(
        self,
        events: list[object],
        tmp_path: Path,
        *,
        detail: dict[str, object] | None = None,
        get_error: Exception | None = None,
        download_errors: set[str] | None = None,
    ) -> None:
        self.events = events
        self.tmp_path = tmp_path
        self.detail = detail
        self.get_error = get_error
        self.download_errors = set(download_errors or ())

    async def get_generation(self, _chat_id: int, gen_id: str) -> dict[str, object]:
        self.events.append(("get_generation", gen_id))
        if self.get_error is not None:
            raise self.get_error
        if self.detail is not None:
            return dict(self.detail)
        return {"edit_url": "", "project_url": ""}

    async def download_image_to_file(
        self, _chat_id: int, image_id: str
    ) -> tuple[Path, str, int]:
        self.events.append(("download", image_id))
        if image_id in self.download_errors:
            raise listener.ApiError("download_failed", "boom")
        path = self.tmp_path / f"{image_id}.png"
        path.write_bytes(b"png")
        return path, "image/png", path.stat().st_size


def _close_created_task(coro, *_args, **_kwargs):
    coro.close()
    return SimpleNamespace(cancel=lambda: None)


@pytest.mark.asyncio
async def test_listener_discovers_only_recent_bot_task_users() -> None:
    redis = ActiveUsersRedis()

    user_ids = await listener._load_active_user_ids(redis)  # type: ignore[arg-type]

    assert user_ids == {"user-1", "user-2"}
    assert redis.pipe.calls[0][0] == "zremrangebyscore"
    assert redis.pipe.calls[1][0] == "zrangebyscore"


@pytest.mark.asyncio
async def test_listener_rebuilds_empty_active_zset_from_legacy_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FallbackRedis()
    recovering_tracker = RecoveringTracker()
    monkeypatch.setattr(listener, "tracker", recovering_tracker)
    monkeypatch.setattr(listener.time, "time", lambda: 200_000.0)

    user_ids = await listener._load_active_user_ids(redis)  # type: ignore[arg-type]

    assert user_ids == {"legacy-user"}
    assert recovering_tracker.refreshes == [("gen-legacy", "legacy-user")]
    assert redis.scan_calls == [
        {
            "cursor": 0,
            "match": "events:user:*",
            "count": listener._FALLBACK_SCAN_COUNT,
        }
    ]
    assert redis.xrevrange_calls == [
        {
            "key": "events:user:legacy-user",
            "max": "+",
            "min": listener._initial_cursor(),
            "count": listener._FALLBACK_EVENTS_PER_STREAM,
        }
    ]
    assert redis.cursor_writes == [
        (
            listener._fallback_stream_cursor_key("legacy-user"),
            "+",
            {"ex": listener._CURSOR_TTL_SECONDS},
        ),
        (
            listener._FALLBACK_SCAN_CURSOR_KEY,
            "0",
            {"ex": listener._CURSOR_TTL_SECONDS},
        ),
    ]


@pytest.mark.asyncio
async def test_listener_fallback_scan_is_cluster_throttled() -> None:
    redis = FallbackRedis(acquire_lease=False)

    user_ids = await listener._load_active_user_ids(redis)  # type: ignore[arg-type]

    assert user_ids == set()
    assert redis.scan_calls == []
    assert redis.pipeline_calls == 1


@pytest.mark.asyncio
async def test_non_terminal_event_refreshes_tracker_and_active_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshing_tracker = RefreshingDispatchTracker()
    progress_calls: list[str] = []
    monkeypatch.setattr(listener, "tracker", refreshing_tracker)
    monkeypatch.setattr(listener, "_should_throttle_progress", lambda _gen_id: False)

    async def fake_on_progress(
        _bot: object, _track: object, data: dict[str, object]
    ) -> None:
        progress_calls.append(str(data["generation_id"]))

    monkeypatch.setattr(listener, "_on_progress", fake_on_progress)

    await listener._dispatch(
        SimpleNamespace(),
        SimpleNamespace(),
        {
            "event": "generation.started",
            "data": {"generation_id": "gen-1"},
        },
        stream_user_id="user-1",
    )

    assert refreshing_tracker.refreshes == [("gen-1", "user-1")]
    assert progress_calls == ["gen-1"]


@pytest.mark.asyncio
async def test_listener_fake_clock_keeps_full_retention_lookback_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 200_000.25
    monkeypatch.setattr(listener.time, "time", lambda: now)
    redis = CursorRedis()

    initial_cursor = listener._initial_cursor()
    await listener._save_cursor(redis, "user-1", "123-0")  # type: ignore[arg-type]

    expected_ms = int(now * 1000) - listener.ACTIVE_USER_STREAM_TTL_SECONDS * 1000
    assert initial_cursor == f"{expected_ms}-0"
    assert listener.ACTIVE_USER_STREAM_TTL_SECONDS >= 48 * 3600
    assert listener._CURSOR_TTL_SECONDS >= 48 * 3600
    assert redis.calls == [
        (
            listener._cursor_key("user-1"),
            "123-0",
            {"ex": listener._CURSOR_TTL_SECONDS},
        )
    ]


@pytest.mark.asyncio
async def test_terminal_delivery_busy_does_not_silently_skip_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener, "tracker", BusyTracker())

    with pytest.raises(listener._TerminalDeliveryBusy):
        await listener._on_succeeded(
            SimpleNamespace(),
            SimpleNamespace(),
            "gen-1",
            SimpleNamespace(
                chat_id=1,
                status_message_id=2,
                prompt="p",
                batch_id="",
                is_bonus=False,
            ),
            {},
        )


@pytest.mark.asyncio
async def test_terminal_delivery_busy_does_not_silently_skip_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener, "tracker", BusyTracker())

    with pytest.raises(listener._TerminalDeliveryBusy):
        await listener._on_failed(
            SimpleNamespace(),
            "gen-1",
            SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
            {},
        )


@pytest.mark.asyncio
async def test_terminal_delivery_notified_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener, "tracker", NotifiedTracker())

    await listener._on_failed(
        SimpleNamespace(),
        "gen-1",
        SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
        {},
    )


@pytest.mark.asyncio
async def test_terminal_delivery_notified_replay_with_active_lock_stays_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener, "tracker", ActiveNotifiedTracker())

    with pytest.raises(listener._TerminalDeliveryBusy):
        await listener._on_failed(
            SimpleNamespace(),
            "gen-1",
            SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
            {},
        )


@pytest.mark.asyncio
async def test_failed_delivery_marks_notified_after_telegram_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    monkeypatch.setattr(listener.asyncio, "create_task", _close_created_task)

    await listener._on_failed(
        RecordingBot(events),
        "gen-1",
        SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
        {},
    )

    assert events[:3] == [
        ("begin", "gen-1"),
        "edit",
        ("mark", "gen-1", False),
    ]
    assert ("clear", "gen-1") in events


@pytest.mark.asyncio
async def test_failed_delivery_leaves_no_sent_marker_when_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    with pytest.raises(RuntimeError, match="edit failed"):
        await listener._on_failed(
            RecordingBot(events, fail_edit=True),
            "gen-1",
            SimpleNamespace(chat_id=1, status_message_id=2, prompt="p", batch_id=""),
            {},
        )

    assert ("mark", "gen-1", False) not in events
    assert ("clear", "gen-1") in events


@pytest.mark.asyncio
async def test_succeeded_delivery_marks_notified_after_all_documents_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def fake_send_document_with_backoff(*_args, **kwargs) -> None:
        events.append(("send_document", kwargs["filename"]))

    async def fake_finish(_bot, gen_id: str, _track) -> None:
        events.append(("finish", gen_id))

    monkeypatch.setattr(
        listener, "_send_document_with_backoff", fake_send_document_with_backoff
    )
    monkeypatch.setattr(listener, "_finish_succeeded_cleanup", fake_finish)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path),
        "gen-1",
        SimpleNamespace(
            chat_id=1,
            status_message_id=2,
            prompt="p",
            batch_id="",
            is_bonus=False,
        ),
        {"images": [{"image_id": "img-1"}]},
    )

    mark_idx = events.index(("mark", "gen-1", False))
    send_idx = next(
        idx
        for idx, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "send_document"
    )
    assert send_idx < mark_idx
    assert ("clear", "gen-1") in events
    assert ("finish", "gen-1") in events


@pytest.mark.asyncio
async def test_succeeded_delivery_leaves_no_sent_marker_when_any_document_send_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))

    async def fake_send_document_with_backoff(*_args, **kwargs) -> None:
        events.append(("send_document", kwargs["filename"]))
        if kwargs["filename"].endswith("-2.png"):
            raise RuntimeError("telegram send failed")

    async def fake_finish(_bot, gen_id: str, _track) -> None:
        events.append(("finish", gen_id))

    monkeypatch.setattr(
        listener, "_send_document_with_backoff", fake_send_document_with_backoff
    )
    monkeypatch.setattr(listener, "_finish_succeeded_cleanup", fake_finish)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            RecordingApi(events, tmp_path),
            "gen-1",
            SimpleNamespace(
                chat_id=1,
                status_message_id=2,
                prompt="p",
                batch_id="",
                is_bonus=False,
            ),
            {"images": [{"image_id": "img-1"}, {"image_id": "img-2"}]},
        )

    assert ("mark", "gen-1", False) not in events
    assert ("clear", "gen-1") in events
    assert ("finish", "gen-1") not in events


def _succeeded_track() -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=1,
        status_message_id=2,
        prompt="p",
        batch_id="",
        is_bonus=False,
    )


def _patch_delivery(monkeypatch: pytest.MonkeyPatch, events: list[object]) -> None:
    async def fake_send_document_with_backoff(*_args, **kwargs) -> None:
        events.append(
            ("send_document", kwargs["filename"], kwargs["caption"] is not None)
        )
        if str(kwargs["filename"]).endswith("-2.png") and kwargs.get("_fail"):
            raise RuntimeError("telegram send failed")

    async def fake_finish(_bot, gen_id: str, _track) -> None:
        events.append(("finish", gen_id))

    monkeypatch.setattr(
        listener, "_send_document_with_backoff", fake_send_document_with_backoff
    )
    monkeypatch.setattr(listener, "_finish_succeeded_cleanup", fake_finish)


@pytest.mark.asyncio
async def test_succeeded_lookup_failure_is_retryable_not_reported_as_no_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """审计 J-3：查不到图 ≠ 没有图。查询失败必须可重投，不能标终态。"""
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            RecordingApi(
                events, tmp_path, get_error=listener.ApiError("upstream", "down")
            ),
            "gen-1",
            _succeeded_track(),
            {},
        )

    # 不能把"没有图片返回"当结论发给用户，也不能 mark_notified 把图判死刑
    assert "edit" not in events
    assert ("mark", "gen-1", False) not in events
    assert ("clear", "gen-1") in events


@pytest.mark.asyncio
async def test_succeeded_falls_back_to_api_when_event_images_unparsable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """images 里没有 image_id 时退回 API 再问一次，而不是直接宣布没有图。"""
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path, detail={"image_ids": ["img-9"]}),
        "gen-1",
        _succeeded_track(),
        {"images": [{"url": "https://example.invalid/x.png"}]},
    )

    assert ("download", "img-9") in events
    assert ("mark", "gen-1", False) in events


@pytest.mark.asyncio
async def test_succeeded_reports_no_images_only_when_api_confirms_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path, detail={"image_ids": []}),
        "gen-1",
        _succeeded_track(),
        {},
    )

    assert "edit" in events
    assert ("mark", "gen-1", False) in events


@pytest.mark.asyncio
async def test_succeeded_partial_send_records_only_delivered_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """审计 J-4：发到一半失败时，只有已送达的图会被记账。"""
    events: list[object] = []
    tracker_double = RecordingTracker(events)
    monkeypatch.setattr(listener, "tracker", tracker_double)

    async def fake_send_document_with_backoff(*_args, **kwargs) -> None:
        events.append(("send_document", kwargs["filename"]))
        if str(kwargs["filename"]).endswith("-2.png"):
            raise RuntimeError("telegram send failed")

    async def fake_finish(_bot, gen_id: str, _track) -> None:
        events.append(("finish", gen_id))

    monkeypatch.setattr(
        listener, "_send_document_with_backoff", fake_send_document_with_backoff
    )
    monkeypatch.setattr(listener, "_finish_succeeded_cleanup", fake_finish)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            RecordingApi(events, tmp_path),
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}, {"image_id": "img-2"}]},
        )

    assert tracker_double.sent == {"img-1"}
    assert ("sent_image", "gen-1", "img-2") not in events


@pytest.mark.asyncio
async def test_succeeded_replay_only_resends_missing_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """审计 J-4：重投不能把已经发出去的图再发一遍（也不能再挂一套按钮）。"""
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events, sent={"img-1"}))
    _patch_delivery(monkeypatch, events)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path),
        "gen-1",
        _succeeded_track(),
        {"images": [{"image_id": "img-1"}, {"image_id": "img-2"}]},
    )

    assert ("download", "img-1") not in events
    assert ("download", "img-2") in events
    sends = [event for event in events if event[0] == "send_document"]
    # 只补发第二张，序号保持整批原始位置，且不再重复挂 caption / 操作键盘
    assert sends == [("send_document", "gen-1-2.png", False)]
    assert ("mark", "gen-1", False) in events


@pytest.mark.asyncio
async def test_succeeded_replay_after_full_delivery_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events, sent={"img-1"}))
    _patch_delivery(monkeypatch, events)

    await listener._on_succeeded(
        RecordingBot(events),
        RecordingApi(events, tmp_path),
        "gen-1",
        _succeeded_track(),
        {"images": [{"image_id": "img-1"}]},
    )

    assert not [event for event in events if event[0] == "send_document"]
    assert ("mark", "gen-1", False) in events
    assert ("finish", "gen-1") in events


@pytest.mark.asyncio
async def test_succeeded_download_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """部分图下载失败 = 用户少拿一张已付费的图，必须重投补发。"""
    events: list[object] = []
    monkeypatch.setattr(listener, "tracker", RecordingTracker(events))
    _patch_delivery(monkeypatch, events)

    with pytest.raises(RuntimeError, match="terminal delivery failed"):
        await listener._on_succeeded(
            RecordingBot(events),
            RecordingApi(events, tmp_path, download_errors={"img-2"}),
            "gen-1",
            _succeeded_track(),
            {"images": [{"image_id": "img-1"}, {"image_id": "img-2"}]},
        )

    assert ("mark", "gen-1", False) not in events


@pytest.mark.asyncio
async def test_dispatch_drop_tells_user_the_task_already_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 J-3：放弃重投前必须说明"已完成、别重新生成"，否则用户会重复付费。"""
    events: list[object] = []
    texts: list[str] = []

    class TrackOnlyTracker:
        async def get(self, _gen_id: str) -> object:
            return _succeeded_track()

    monkeypatch.setattr(listener, "tracker", TrackOnlyTracker())

    async def fake_replace_status(_bot: object, _track: object, text: str) -> None:
        texts.append(text)

    monkeypatch.setattr(listener, "_replace_status", fake_replace_status)

    await listener._notify_dispatch_drop(
        RecordingBot(events), "generation.succeeded", "gen-1"
    )
    await listener._notify_dispatch_drop(
        RecordingBot(events), "generation.failed", "gen-2"
    )

    assert len(texts) == 1
    assert "不用重新生成" in texts[0]
    assert "/tasks" in texts[0]


@pytest.mark.asyncio
async def test_batch_finalize_decrements_once_per_generation_and_deletes_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        listener, "tracker", RecordingTracker(events, batch_remaining=0)
    )

    await listener._maybe_finalize_batch(
        RecordingBot(events),
        SimpleNamespace(chat_id=1, status_message_id=2, batch_id="batch-1"),
        "gen-1",
    )

    assert ("batch_decr", "batch-1", "gen-1") in events
    assert "delete" in events
    assert ("batch_remove", "batch-1") in events


@pytest.mark.asyncio
async def test_batch_finalize_skips_delete_when_counter_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        listener, "tracker", RecordingTracker(events, batch_remaining=None)
    )

    await listener._maybe_finalize_batch(
        RecordingBot(events),
        SimpleNamespace(chat_id=1, status_message_id=2, batch_id="batch-1"),
        "gen-1",
    )

    assert ("batch_decr", "batch-1", "gen-1") in events
    assert "delete" not in events
    assert ("batch_remove", "batch-1") not in events
