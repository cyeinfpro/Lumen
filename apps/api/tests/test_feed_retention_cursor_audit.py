"""LUM-01: time advances, policy identity stays stable, visibility does not."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.routes import generations
from lumen_core import byok_retention


@pytest.fixture
def feed(monkeypatch):
    now = [datetime(2026, 10, 1, 12, tzinfo=timezone.utc)]
    policy = SimpleNamespace(retention_hide_enabled=True, retention_hide_days=3,
                             retention_delete_enabled=False, retention_delete_days=7)
    rows = [SimpleNamespace(id=f"gen-{i}", created_at=created, message_id=f"msg-{i}")
            for i, created in enumerate([
                now[0] - timedelta(minutes=1), now[0] - timedelta(minutes=2),
                now[0] - timedelta(days=3) + timedelta(seconds=1),
            ])]
    seen_cutoffs = []
    counts = []

    async def settings(_db):
        return policy

    async def page(_db, *, visible_after, cursor_ts, cursor_id, limit, **_filters):
        seen_cutoffs.append(visible_after)
        visible = [row for row in rows if visible_after is None or row.created_at >= visible_after]
        if cursor_ts is not None:
            visible = [row for row in visible if (row.created_at, row.id) < (cursor_ts, cursor_id)]
        return visible[:limit], len(visible) > limit

    async def images(*_args, **_kwargs):
        return {}

    class Db:
        async def execute(self, statement):
            compiled = statement.compile(dialect=postgresql.dialect())
            cutoff = next((v for v in compiled.params.values() if isinstance(v, datetime)), None)
            if policy.retention_hide_enabled:
                assert "generations.created_at >=" in str(compiled)
                assert cutoff == now[0] - timedelta(days=policy.retention_hide_days)
            total = sum(cutoff is None or row.created_at >= cutoff for row in rows)
            counts.append(total)
            return SimpleNamespace(scalar=lambda: total)

    monkeypatch.setattr(byok_retention, "utcnow", lambda: now[0])
    monkeypatch.setattr(generations, "read_byok_settings_cached", settings)
    monkeypatch.setattr(generations, "_generation_feed_page", page)
    monkeypatch.setattr(generations, "_feed_images", images)
    monkeypatch.setattr(generations, "_feed_variant_states", images)
    monkeypatch.setattr(generations, "_feed_conversation_ids", images)
    return SimpleNamespace(now=now, policy=policy, db=Db(), counts=counts, cutoffs=seen_cutoffs,
                           user=SimpleNamespace(id="byok-user", account_mode="byok"))


@pytest.mark.asyncio
async def test_next_page_accepts_time_drift_and_recounts_expired_items(feed):
    first = await generations.list_generation_feed(feed.user, feed.db, limit=1)
    assert first.total == 3 and first.next_cursor
    feed.now[0] += timedelta(seconds=2, microseconds=317)
    second = await generations.list_generation_feed(feed.user, feed.db, cursor=first.next_cursor, limit=1)
    assert second.total == 2
    assert second.next_cursor is None
    assert feed.counts == [3, 2]
    assert feed.cutoffs[1] > feed.cutoffs[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["ratio", "has_ref", "q", "user", "hide_days", "hide_enabled"])
async def test_cursor_rejects_real_filter_or_policy_changes(feed, changed):
    first = await generations.list_generation_feed(feed.user, feed.db, limit=1)
    kwargs = {}
    if changed == "user":
        feed.user.id = "another-user"
    elif changed == "hide_days":
        feed.policy.retention_hide_days = 2
    elif changed == "hide_enabled":
        feed.policy.retention_hide_enabled = False
    else:
        kwargs[changed] = {"ratio": "1:1", "has_ref": True, "q": "other"}[changed]
    with pytest.raises(HTTPException) as error:
        await generations.list_generation_feed(feed.user, feed.db, cursor=first.next_cursor, limit=1, **kwargs)
    assert error.value.status_code == 400
    assert error.value.detail["error"]["code"] == "invalid_cursor"
    assert feed.counts == [3]


@pytest.mark.asyncio
async def test_legacy_unbound_cursor_still_rechecks_current_visibility(feed):
    first = await generations.list_generation_feed(feed.user, feed.db, limit=1)
    timestamp, identity, total, _signature = generations._decode_cursor(first.next_cursor)
    import base64
    legacy = base64.urlsafe_b64encode(f"v2|{timestamp.isoformat()}|{identity}|{total}".encode()).decode()
    feed.now[0] += timedelta(seconds=2)
    second = await generations.list_generation_feed(feed.user, feed.db, cursor=legacy, limit=1)
    assert second.total == 2 and second.next_cursor is None
    assert feed.counts == [3, 2]
