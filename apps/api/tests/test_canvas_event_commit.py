from types import SimpleNamespace
import pytest
from app.canvas_services import event_commit


class Session:
    def __init__(self, *, fail=False):
        self.info = {}
        self.transaction = object()
        self.sync_session = self
        self.committed = False
        self.fail = fail

    def get_transaction(self):
        return self.transaction

    async def commit(self):
        if self.fail:
            raise RuntimeError("commit failed")
        self.committed = True
        self.transaction = None


def remember(db, seq):
    event_commit.remember_canvas_notice(
        db,
        run=SimpleNamespace(id="run", canvas_id="canvas", user_id="user"),
        event=SimpleNamespace(
            seq=seq, execution_id="execution", event_type="canvas.execution.progress"
        ),
    )


@pytest.mark.asyncio
async def test_only_publish_after_commit_and_coalesce_same_run(monkeypatch):
    db = Session()
    seen = []

    async def publish(redis, **notice):
        assert db.committed
        seen.append(notice)

    monkeypatch.setattr(event_commit, "get_redis", lambda: object())
    monkeypatch.setattr(event_commit, "publish_canvas_run_notification", publish)
    remember(db, 1)
    remember(db, 2)
    assert not seen
    await event_commit.commit_canvas_events(db)
    assert len(seen) == 1 and seen[0]["seq"] == 2
    assert "canvas_committed_notices" not in db.info


@pytest.mark.asyncio
async def test_commit_failure_never_publishes(monkeypatch):
    db = Session(fail=True)

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not publish")

    monkeypatch.setattr(event_commit, "publish_canvas_run_notification", forbidden)
    remember(db, 1)
    with pytest.raises(RuntimeError, match="commit failed"):
        await event_commit.commit_canvas_events(db)


@pytest.mark.asyncio
async def test_rollback_transaction_notice_is_not_published_by_later_commit(
    monkeypatch,
):
    db = Session()
    seen = []

    async def publish(redis, **notice):
        seen.append(notice)

    monkeypatch.setattr(event_commit, "get_redis", lambda: object())
    monkeypatch.setattr(event_commit, "publish_canvas_run_notification", publish)
    remember(db, 1)
    db.transaction = object()  # Old transaction was rolled back.
    await event_commit.commit_canvas_events(db)
    assert not seen


@pytest.mark.asyncio
async def test_notification_failure_does_not_undo_commit(monkeypatch):
    db = Session()
    monkeypatch.setattr(event_commit, "get_redis", lambda: object())

    async def fail(*args, **kwargs):
        raise ConnectionError("not part of transaction")

    monkeypatch.setattr(event_commit, "publish_canvas_run_notification", fail)
    remember(db, 1)
    await event_commit.commit_canvas_events(db)
    assert db.committed
