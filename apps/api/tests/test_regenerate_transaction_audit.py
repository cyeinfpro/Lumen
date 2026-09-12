"""Run the real regeneration route with controlled transaction collaborators."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.routes import regenerate
from lumen_core.schemas import ImageParamsIn, RegenerateIn, RegenerateOut


class ExpiringIdentity:
    """Reject implicit attribute reloads once the fake transaction rolls back."""

    def __init__(self, identity: str, **values):
        self.identity = identity
        self.expired = False
        self.__dict__.update(values)

    @property
    def id(self):
        if self.expired:
            raise AssertionError("Implicit ORM I/O after rollback")
        return self.identity


@pytest.fixture
def route_harness(monkeypatch):
    def make(*, conflict=None, replay_at_lock=False, requested="image_to_image"):
        log = []
        saved = {}
        user = ExpiringIdentity("owner", account_mode="wallet")
        conv = ExpiringIdentity("conv")
        target = SimpleNamespace(intent="image_to_image", status="succeeded")
        parent = SimpleNamespace(content={"text": "original user", "attachments": []})
        source = SimpleNamespace(prompt="actual edit operation", input_image_ids=["source-image"])
        prior = RegenerateOut(
            assistant_message_id="prior", generation_ids=["prior-gen"], completion_id=None,
        )

        class DB:
            async def execute(self, statement):
                assert getattr(statement, "_for_update_arg", None) is not None
                log.append("lock-conversation")
                return SimpleNamespace(scalar_one_or_none=lambda: conv)

            async def commit(self):
                log.append("commit")
                if conflict == "commit":
                    raise IntegrityError("commit", {}, ValueError("duplicate"))

            async def rollback(self):
                log.append("rollback")
                user.expired = conv.expired = True

            async def refresh(self, _value):
                log.append("refresh")

        async def visible(*_args, **_kwargs):
            log.append("visibility")
            return conv, target, parent

        async def lookup(_db, user_id, conv_id, key, **_kwargs):
            assert (user_id, conv_id, key) == ("owner", "conv", "key")
            log.append("lookup")
            if "rollback" in log or (replay_at_lock and "lock-conversation" in log):
                return prior
            return None

        async def lock(*_args, **_kwargs):
            log.append("lock-user")
            return SimpleNamespace(user=user, account_mode="wallet")

        async def rows(*_args, **_kwargs):
            return [source]

        async def attachments(*_args, **kwargs):
            return [item["image_id"] for item in kwargs["user_content"]["attachments"]]

        async def mask(*_args, **_kwargs):
            return "mask"

        async def image_params(*_args, **_kwargs):
            return ImageParamsIn(count=2)

        async def output_format(*_args, **_kwargs):
            return "png"

        async def cancel(*_args, **_kwargs):
            log.append("cancel-old")
            return {}

        async def create(**kwargs):
            log.append("create-and-flush")
            saved.update(kwargs)
            if conflict == "flush":
                raise IntegrityError("flush", {}, ValueError("duplicate"))
            if conflict == "other":
                raise ValueError("unrelated creation failure")
            return SimpleNamespace(
                assistant_msg=SimpleNamespace(id="new"), completion_id=None,
                generation_ids=["g1", "g2"], outbox_payloads=[], outbox_rows=[],
            )

        async def noop(*_args, **_kwargs):
            return None

        async def publish(*_args, **_kwargs):
            assert "commit" in log
            log.append("publish")

        monkeypatch.setattr(regenerate.MESSAGES_LIMITER, "check", noop)
        monkeypatch.setattr(regenerate, "get_redis", object)
        for name, replacement in {
            "_regenerate_messages": visible,
            "_lookup_idempotent_regenerate": lookup,
            "lock_active_user_snapshot": lock,
            "_ordered_target_generations": rows,
            "_validated_attachment_ids": attachments,
            "_image_params_from_target": image_params,
            "_mask_image_id_from_target": mask,
            "_default_image_output_format": output_format,
            "_regenerate_system_prompt": noop,
            "_cancel_regenerate_target_active_tasks": cancel,
            "_create_assistant_task": create,
            "_post_commit_regenerate_cancel_cleanup": noop,
            "_publish_message_appended": publish,
            "_publish_assistant_task": publish,
        }.items():
            monkeypatch.setattr(regenerate, name, replacement)

        async def run():
            return await regenerate.regenerate_message(
                "conv", "target", RegenerateIn(intent=requested, idempotency_key="key"),
                user, DB(),
            )

        return run, log, saved, prior

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["flush", "commit"])
async def test_idempotency_conflict_recovers_without_reading_expired_ids(route_harness, phase):
    run, log, _saved, prior = route_harness(conflict=phase)
    assert await run() is prior
    assert "rollback" in log
    assert "publish" not in log


@pytest.mark.asyncio
async def test_locked_recheck_does_not_create_or_cancel_again(route_harness):
    run, log, _saved, prior = route_harness(replay_at_lock=True)
    assert await run() is prior
    assert log.count("lookup") == 2
    assert "create-and-flush" not in log
    assert "cancel-old" not in log


@pytest.mark.asyncio
async def test_same_intent_keeps_actual_operation_and_mask(route_harness):
    run, log, saved, _prior = route_harness()
    result = await run()
    assert result.generation_ids == ["g1", "g2"]
    assert saved["text"] == "actual edit operation"
    assert saved["attachment_ids"] == ["source-image"]
    assert saved["mask_image_id"] == "mask"
    assert log.index("commit") < log.index("publish")


@pytest.mark.asyncio
async def test_changed_intent_does_not_inherit_unrelated_mask(route_harness):
    run, _log, saved, _prior = route_harness(requested="text_to_image")
    await run()
    assert saved["text"] == "original user"
    assert saved["mask_image_id"] is None


@pytest.mark.asyncio
async def test_other_failure_is_rolled_back_and_not_published(route_harness):
    run, log, _saved, _prior = route_harness(conflict="other")
    with pytest.raises(ValueError, match="unrelated"):
        await run()
    assert "rollback" in log
    assert "publish" not in log
