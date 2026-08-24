from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from PIL import Image as PILImage
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.main as main
from app import observability as api_observability
from app.realtime.channel_policy import ChannelPolicyError, validate_channels
from app.routes import conversations
from app.routes import me_export
from app.routes.billing_parts.orphan_hold_recovery import recovery_action
from app.routes.billing_parts.orphan_hold_safety import _hold_release_proof
from app.services.agent import runs as agent_runs_service
from app.services.agent import sessions as agent_sessions_service
from app.services.agent import tools as agent_tools_service
from app.services.agent import common as agent_common
from app.services.agent import message_submission as agent_message_service
from app.services.agent import session_crud as agent_session_crud_service
from app.services.agent.session_images import session_image_slot_count
from app.services.agent.common import AgentProviderPreflight, AgentTextReservation
from lumen_core.agent_capability import AgentCapabilityClaims
from lumen_core.agent_events import AGENT_TOOL_CREATE_IMAGE, AgentRunStatus
from lumen_core.model_base import Base
from lumen_core.model_entities import (
    AgentRun,
    AgentCapabilityGrant,
    AgentRunReference,
    AgentSession,
    AgentToolCall,
    ApiSupplierTemplate,
    Conversation,
    Generation,
    Image,
    Message,
    OutboxEvent,
    SystemPrompt,
    SystemSetting,
    User,
    UserApiCredential,
    UserMemoryScope,
)
from lumen_core.schema_models import (
    AgentMessageCreateIn,
    AgentSessionCreateIn,
    AgentSessionPatchIn,
    AgentToolCreateImageIn,
)


_TABLE_MODELS = (
    User,
    SystemPrompt,
    UserMemoryScope,
    Conversation,
    Message,
    Image,
    ApiSupplierTemplate,
    UserApiCredential,
    AgentSession,
    AgentRun,
    AgentCapabilityGrant,
    AgentRunReference,
    AgentToolCall,
    OutboxEvent,
    SystemSetting,
)


def test_agent_orphan_hold_requires_persisted_no_dispatch_evidence() -> None:
    safe = SimpleNamespace(
        dispatch_jsonb={"runtime_delivery": "proven_absent"},
        billing_jsonb={"knowledge": "proven_absent"},
    )
    unknown = SimpleNamespace(
        dispatch_jsonb={"runtime_delivery": "provider_dispatched"},
        billing_jsonb={"knowledge": "unknown"},
    )

    assert recovery_action("agent_run") == "release"
    assert _hold_release_proof(safe, ref_type="agent_run") == (
        "runtime_delivery:proven_absent"
    )
    assert _hold_release_proof(unknown, ref_type="agent_run") is None


def _create_generation_table(connection: Any) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE generations (
          id VARCHAR(36) PRIMARY KEY,
          message_id VARCHAR(36) NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
          user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          action VARCHAR(16) NOT NULL,
          model VARCHAR(64) NOT NULL,
          prompt TEXT NOT NULL,
          size_requested VARCHAR(32) NOT NULL,
          aspect_ratio VARCHAR(16) NOT NULL,
          input_image_ids JSON NOT NULL DEFAULT '[]',
          primary_input_image_id VARCHAR(36),
          mask_image_id VARCHAR(36),
          upstream_request JSON,
          user_api_credential_id VARCHAR(36),
          upstream_supplier_id VARCHAR(36),
          status VARCHAR(32) NOT NULL,
          progress_stage VARCHAR(32) NOT NULL,
          attempt INTEGER NOT NULL DEFAULT 0,
          execution_epoch INTEGER NOT NULL DEFAULT 0,
          billing_retry_count INTEGER NOT NULL DEFAULT 0,
          error_code VARCHAR(64),
          error_message TEXT,
          started_at TIMESTAMP,
          finished_at TIMESTAMP,
          cancel_requested_at TIMESTAMP,
          upstream_pixels INTEGER,
          idempotency_key VARCHAR(64) NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE (user_id, idempotency_key)
        )
        """
    )


@pytest_asyncio.fixture
async def db_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[model.__table__ for model in _TABLE_MODELS],
            )
        )
        await connection.run_sync(_create_generation_table)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_session(
    db: AsyncSession,
    *,
    user_id: str = "user-1",
    session_id: str = "agent-session-1",
    conversation_id: str = "conversation-1",
    image_ids: tuple[str, ...] = (),
) -> tuple[User, AgentSession, Conversation]:
    user = await db.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            email=f"{user_id}@example.test",
            email_verified=True,
            display_name=user_id,
            role="member",
            account_mode="wallet",
        )
        db.add(user)
        await db.flush()
    conversation = Conversation(
        id=conversation_id,
        user_id=user_id,
        title="Agent session",
        default_params={},
    )
    session = AgentSession(
        id=session_id,
        user_id=user_id,
        conversation_id=conversation_id,
        runtime_version="",
    )
    db.add(conversation)
    await db.flush()
    db.add(session)
    for index, image_id in enumerate(image_ids):
        db.add(
            Image(
                id=image_id,
                user_id=user_id,
                source="uploaded",
                storage_key=f"u/{user_id}/{image_id}.png",
                mime="image/png",
                width=1024,
                height=1024,
                size_bytes=128,
                sha256=f"{index + 1:064x}",
                visibility="private",
                metadata_jsonb={},
            )
        )
    await db.commit()
    await db.refresh(session)
    await db.refresh(conversation)
    return user, session, conversation


def _patch_agent_message_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async def provider_preflight(*_args: Any, **_kwargs: Any) -> AgentProviderPreflight:
        return AgentProviderPreflight("gpt-agent-test", ("provider-test",))

    async def reserve_text(*_args: Any, **_kwargs: Any) -> AgentTextReservation:
        return AgentTextReservation(0, {})

    async def setting(_db: Any, key: str, _default: int | None = None) -> int:
        return {
            "agent.max_image_tool_calls": 3,
            "agent.max_images_per_run": 4,
            "agent.max_reference_images": 16,
            "agent.max_session_images": 64,
        }[key]

    async def no_audit(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def no_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    class Redis:
        async def set(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(
        agent_message_service,
        "wallet_chat_provider_preflight",
        provider_preflight,
    )
    monkeypatch.setattr(agent_message_service, "reserve_agent_text", reserve_text)

    async def valid_reference(_image: Image) -> None:
        return None

    monkeypatch.setattr(
        agent_message_service,
        "_validate_reference_artifact",
        valid_reference,
    )
    monkeypatch.setattr(agent_message_service, "agent_setting_int", setting)
    monkeypatch.setattr(agent_message_service, "write_audit", no_audit)
    monkeypatch.setattr(
        agent_message_service,
        "publish_agent_events_best_effort",
        no_publish,
    )
    monkeypatch.setattr(agent_session_crud_service, "write_audit", no_audit)
    monkeypatch.setattr(
        agent_session_crud_service,
        "publish_agent_events_best_effort",
        no_publish,
    )
    monkeypatch.setattr(agent_runs_service, "write_audit", no_audit)
    monkeypatch.setattr(agent_runs_service, "get_redis", Redis)
    monkeypatch.setattr(
        agent_runs_service,
        "publish_agent_events_best_effort",
        no_publish,
    )


def _capability(
    *,
    run: AgentRun,
    reference_labels: list[str],
) -> AgentCapabilityClaims:
    now = int(datetime.now(timezone.utc).timestamp())
    return AgentCapabilityClaims(
        capability_id="capability-123456",
        nonce="nonce-1234567890abcdef",
        run_id=run.id,
        user_id=run.user_id,
        agent_session_id=run.agent_session_id,
        execution_epoch=run.execution_epoch,
        allowed_tools=[AGENT_TOOL_CREATE_IMAGE],
        allowed_reference_labels=reference_labels,
        issued_at=now,
        expires_at=now + 120,
    )


@pytest.mark.asyncio
async def test_agent_text_reservation_uses_pi_native_model_and_tool_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def enabled(_db: Any) -> bool:
        return True

    async def pricing_snapshot(_db: Any, *, model: str) -> dict[str, Any]:
        assert model == "gpt-agent-test"
        return {"pricing": "snapshot"}

    def breakdown(
        snapshot: dict[str, Any],
        *,
        model: str,
        tokens: Any,
        rate_multiplier_x10000: int,
    ) -> Any:
        captured.update(
            snapshot=snapshot,
            model=model,
            multiplier=rate_multiplier_x10000,
        )
        captured.setdefault("tokens_seen", []).append(tokens)
        return SimpleNamespace(actual_cost_micro=123_456)

    async def multiplier(_db: Any, _user_id: str) -> int:
        return 10_000

    async def allow_negative(_db: Any) -> bool:
        return False

    async def hold(_db: Any, user_id: str, amount: int, **kwargs: Any) -> Any:
        captured.update(user_id=user_id, amount=amount, hold=kwargs)
        return SimpleNamespace(balance_after=900_000, hold_after=123_456)

    async def no_audit(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(agent_common, "billing_enabled", enabled)
    monkeypatch.setattr(
        agent_common.billing_core,
        "completion_pricing_snapshot",
        pricing_snapshot,
    )
    monkeypatch.setattr(
        agent_common.billing_core,
        "completion_breakdown_from_snapshot",
        breakdown,
    )
    monkeypatch.setattr(agent_common, "user_rate_multiplier_x10000", multiplier)
    monkeypatch.setattr(agent_common, "billing_allow_negative", allow_negative)
    monkeypatch.setattr(agent_common.billing_core, "hold", hold)
    monkeypatch.setattr(agent_common, "write_audit", no_audit)

    reservation = await agent_common.reserve_agent_text(
        SimpleNamespace(),  # type: ignore[arg-type]
        run=SimpleNamespace(id="run-1"),
        user_id="user-1",
        account_mode="wallet",
        model="gpt-agent-test",
        text="create a campaign",
        reference_count=2,
        provider_max_output_tokens=4096,
        max_image_tool_calls=2,
    )
    assert reservation.hold_micro == 123_456
    assert all(tokens.output_tokens == 8 * 4096 for tokens in captured["tokens_seen"])
    assert (
        max(
            tokens.input_tokens
            + tokens.cache_read_tokens
            + tokens.cache_creation_tokens
            + tokens.cache_creation_1h_tokens
            for tokens in captured["tokens_seen"]
        )
        == reservation.billing_snapshot["reserved_input_tokens"]
    )
    assert captured["hold"]["ref_type"] == "agent_run"
    assert captured["hold"]["ref_id"] == "run-1"
    assert reservation.billing_snapshot["execution_policy"] == "pi-native"
    assert reservation.billing_snapshot["native_tool_turns"] == 2
    assert reservation.billing_snapshot["reserved_provider_calls"] == 8
    assert reservation.billing_snapshot["max_output_tokens"] == 4096
    assert reservation.billing_snapshot["context_window"] == 128_000


@pytest.mark.asyncio
async def test_agent_session_crud_keeps_conversation_soft_delete_semantics(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_message_dependencies(monkeypatch)
    async with db_factory() as db:
        user = User(
            id="crud-user",
            email="crud-user@example.test",
            email_verified=True,
            display_name="CRUD",
            role="member",
            account_mode="wallet",
        )
        db.add(user)
        await db.commit()
        created = await agent_sessions_service.create_agent_session(
            db,
            user=user,
            body=AgentSessionCreateIn(
                title="Initial",
                allow_image=False,
                image_defaults={"count": 2, "aspect_ratio": "16:9"},
            ),
            request=None,
        )
        assert created.title == "Initial"
        assert created.allow_image is False
        assert created.image_defaults.count == 2

        patched = await agent_sessions_service.patch_agent_session(
            db,
            session_id=created.id,
            user=user,
            body=AgentSessionPatchIn(
                title="Renamed",
                archived=True,
                allow_image=True,
            ),
            request=None,
        )
        assert patched.title == "Renamed"
        assert patched.archived is True
        assert patched.allow_image is True
        assert patched.image_defaults.count == 2

        result = await agent_sessions_service.delete_agent_session(
            db,
            session_id=created.id,
            user=user,
            request=None,
        )
        assert result == {"ok": True}
        deleted_at = await db.scalar(
            select(Conversation.deleted_at).where(
                Conversation.id == created.conversation_id
            )
        )
        assert deleted_at is not None
        assert await db.get(AgentSession, created.id) is not None
        with pytest.raises(HTTPException):
            await agent_sessions_service.get_owned_agent_session(
                db,
                session_id=created.id,
                user_id=user.id,
            )


@pytest.mark.asyncio
async def test_agent_message_is_idempotent_owned_and_hidden_from_studio(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_message_dependencies(monkeypatch)
    async with db_factory() as db:
        user, session, conversation = await _seed_session(db)
        user_id = user.id
        session_id = session.id
        conversation_id = conversation.id
        body = AgentMessageCreateIn(idempotency_key="message-idem-1", text="hello")

        first = await agent_sessions_service.submit_agent_message(
            db,
            session_id=session_id,
            user=user,
            body=body,
            request=None,
        )
        replay = await agent_sessions_service.submit_agent_message(
            db,
            session_id=session_id,
            user=user,
            body=body,
            request=None,
        )
        assert replay.agent_run.id == first.agent_run.id
        assert replay.user_message.id == first.user_message.id
        assert await db.scalar(select(func.count(AgentRun.id))) == 1
        assert await db.scalar(select(func.count(Message.id))) == 2
        assert sorted((await db.execute(select(OutboxEvent.kind))).scalars().all()) == [
            "agent_run",
            "sse",
        ]
        persisted = await db.get(AgentRun, first.agent_run.id)
        assert persisted is not None
        assert persisted.reasoning_effort == "max"
        assert persisted.request_snapshot_jsonb["execution_policy"] == "pi-native"
        assert persisted.request_snapshot_jsonb["tool_policy"] == {
            "max_image_tool_calls": 3,
            "max_images_per_run": 4,
        }
        assert persisted.request_snapshot_jsonb["reference_policy"] == {
            "max_reference_images": 16,
            "max_session_images": 64,
        }

        with pytest.raises(HTTPException) as conflict:
            await agent_sessions_service.submit_agent_message(
                db,
                session_id=session_id,
                user=user,
                body=body.model_copy(update={"text": "changed"}),
                request=None,
            )
        assert conflict.value.status_code == 409
        assert conflict.value.detail["error"]["code"] == "idempotency_conflict"
        await db.rollback()
        user = await db.get(User, user_id)
        assert user is not None

        with pytest.raises(HTTPException) as active:
            await agent_sessions_service.submit_agent_message(
                db,
                session_id=session_id,
                user=user,
                body=AgentMessageCreateIn(
                    idempotency_key="message-idem-2",
                    text="second",
                ),
                request=None,
            )
        assert active.value.detail["error"]["code"] == "agent_run_active"
        await db.rollback()
        user = await db.get(User, user_id)
        assert user is not None

        with pytest.raises(HTTPException) as studio_hidden:
            await conversations._get_owned_conv(db, conversation_id, user_id)
        assert studio_hidden.value.status_code == 404

        other = User(
            id="user-2",
            email="user-2@example.test",
            email_verified=True,
            display_name="other",
            role="member",
            account_mode="wallet",
        )
        db.add(other)
        await db.commit()
        with pytest.raises(HTTPException) as isolated:
            await agent_sessions_service.get_owned_agent_session(
                db,
                session_id=session_id,
                user_id=other.id,
            )
        assert isolated.value.status_code == 404

        cancelled = await agent_runs_service.cancel_agent_run(
            db,
            run_id=first.agent_run.id,
            user=user,
            request=None,
        )
        assert cancelled.status == "cancelled"
        assert cancelled.execution_epoch == 1
        second = await agent_sessions_service.submit_agent_message(
            db,
            session_id=session_id,
            user=user,
            body=AgentMessageCreateIn(
                idempotency_key="message-idem-2",
                text="second",
            ),
            request=None,
        )
        assert second.agent_run.id != first.agent_run.id
        assert await db.scalar(select(func.count(AgentRun.id))) == 2


@pytest.mark.asyncio
async def test_agent_references_preserve_roles_order_and_ownership(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_message_dependencies(monkeypatch)
    async with db_factory() as db:
        user, session, _conversation = await _seed_session(
            db,
            image_ids=("image-1", "image-2"),
        )
        await _seed_session(
            db,
            user_id="user-2",
            session_id="agent-session-2",
            conversation_id="conversation-2",
            image_ids=("other-image",),
        )
        response = await agent_sessions_service.submit_agent_message(
            db,
            session_id=session.id,
            user=user,
            body=AgentMessageCreateIn(
                idempotency_key="references-1",
                text="use both",
                attachments=[
                    {"image_id": "image-1", "role": "product", "label": "Product"},
                    {"image_id": "image-2", "role": "style", "label": "Style"},
                ],
            ),
            request=None,
        )
        references = list(
            (
                await db.execute(
                    select(AgentRunReference)
                    .where(AgentRunReference.agent_run_id == response.agent_run.id)
                    .order_by(AgentRunReference.ordinal.asc())
                )
            )
            .scalars()
            .all()
        )
        assert [reference.reference_label for reference in references] == [
            "ref_1",
            "ref_2",
        ]
        assert [reference.image_id for reference in references] == [
            "image-1",
            "image-2",
        ]
        assert [reference.role for reference in references] == ["product", "style"]

        _user, third_session, _third_conversation = await _seed_session(
            db,
            user_id=user.id,
            session_id="agent-session-3",
            conversation_id="conversation-3",
        )
        with pytest.raises(HTTPException) as invalid:
            await agent_sessions_service.submit_agent_message(
                db,
                session_id=third_session.id,
                user=user,
                body=AgentMessageCreateIn(
                    idempotency_key="references-other-user",
                    text="steal",
                    attachments=[{"image_id": "other-image"}],
                ),
                request=None,
            )
        assert invalid.value.detail["error"]["code"] == "invalid_attachment"


@pytest.mark.asyncio
async def test_agent_tool_rechecks_reference_retention_before_redemption(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_message_dependencies(monkeypatch)
    async with db_factory() as db:
        user, session, _conversation = await _seed_session(
            db,
            image_ids=("image-expiring",),
        )
        submitted = await agent_sessions_service.submit_agent_message(
            db,
            session_id=session.id,
            user=user,
            body=AgentMessageCreateIn(
                idempotency_key="retention-reference-run",
                text="use the image",
                attachments=[{"image_id": "image-expiring"}],
            ),
            request=None,
        )
        run = await db.get(AgentRun, submitted.agent_run.id)
        assert run is not None
        claims = _capability(run=run, reference_labels=["ref_1"])

        async def hidden_reference(*_args: Any, **_kwargs: Any) -> Any:
            return Image.created_at > datetime.now(timezone.utc) + timedelta(days=1)

        monkeypatch.setattr(
            agent_tools_service,
            "retention_filter",
            hidden_reference,
        )
        with pytest.raises(HTTPException) as hidden:
            await agent_tools_service._reference_map(  # noqa: SLF001
                db,
                run=run,
                claims=claims,
                requested_labels=["ref_1"],
            )
        assert hidden.value.detail["error"]["code"] == "agent_reference_not_found"


@pytest.mark.asyncio
async def test_agent_session_slots_exclude_retention_hidden_images(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as db:
        user, session, _conversation = await _seed_session(
            db,
            image_ids=("image-expired", "image-visible"),
        )
        expired = await db.get(Image, "image-expired")
        assert expired is not None
        expired.created_at = datetime.now(timezone.utc) - timedelta(days=8)
        await db.commit()
        visible_after = datetime.now(timezone.utc) - timedelta(days=3)

        slots = await session_image_slot_count(
            db,
            session_id=session.id,
            user_id=user.id,
            snapshotted_image_ids={"image-expired", "image-visible"},
            image_visibility_filter=Image.created_at >= visible_after,
        )

        assert slots == 1


@pytest.mark.asyncio
async def test_agent_session_inherits_all_prior_uploads_and_generated_images(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_message_dependencies(monkeypatch)
    async with db_factory() as db:
        user, session, _conversation = await _seed_session(
            db,
            image_ids=("image-original",),
        )
        user_id = user.id
        session_id = session.id
        first = await agent_sessions_service.submit_agent_message(
            db,
            session_id=session.id,
            user=user,
            body=AgentMessageCreateIn(
                idempotency_key="session-context-1",
                text="create a revision",
                attachments=[
                    {
                        "image_id": "image-original",
                        "role": "edit_target",
                        "label": "Original",
                    }
                ],
            ),
            request=None,
        )
        first_run = await db.get(AgentRun, first.agent_run.id)
        assert first_run is not None
        first_run.status = AgentRunStatus.SUCCEEDED.value
        generation = Generation(
            id="generation-agent-result",
            message_id=first.assistant_message.id,
            user_id=user.id,
            action="edit",
            model="gpt-image-2",
            prompt="revised image",
            size_requested="1024x1024",
            aspect_ratio="1:1",
            input_image_ids=["image-original"],
            primary_input_image_id="image-original",
            upstream_request={
                "source": "agent",
                "agent_session_id": session.id,
                "agent_run_id": first.agent_run.id,
            },
            status="succeeded",
            progress_stage="finalizing",
            idempotency_key="session-context-generation",
        )
        db.add(generation)
        await db.flush()
        db.add(
            Image(
                id="image-agent-result",
                user_id=user.id,
                owner_generation_id=generation.id,
                source="generated",
                storage_key="u/user-1/image-agent-result.webp",
                mime="image/webp",
                width=1024,
                height=1024,
                size_bytes=256,
                sha256="a" * 64,
                visibility="private",
                metadata_jsonb={},
                artifact_status="ready",
            )
        )
        await db.commit()

        pending_generation = Generation(
            id="generation-agent-pending",
            message_id=first.assistant_message.id,
            user_id=user.id,
            action="edit",
            model="gpt-image-2",
            prompt="pending revision",
            size_requested="1024x1024",
            aspect_ratio="1:1",
            input_image_ids=["image-original"],
            primary_input_image_id="image-original",
            upstream_request={"source": "agent"},
            status="queued",
            progress_stage="queued",
            idempotency_key="session-context-pending",
        )
        db.add(pending_generation)
        await db.commit()
        setting_reader = agent_message_service.agent_setting_int

        async def low_session_limit(db_arg: Any, key: str) -> int:
            if key == "agent.max_session_images":
                return 2
            return await setting_reader(db_arg, key)

        monkeypatch.setattr(
            agent_message_service,
            "agent_setting_int",
            low_session_limit,
        )
        with pytest.raises(HTTPException) as capacity:
            await agent_sessions_service.submit_agent_message(
                db,
                session_id=session.id,
                user=user,
                body=AgentMessageCreateIn(
                    idempotency_key="session-context-2",
                    text="make it less sharp",
                ),
                request=None,
            )
        assert (
            capacity.value.detail["error"]["code"]
            == "agent_session_reference_limit_reached"
        )
        await db.rollback()
        pending_generation = await db.get(Generation, "generation-agent-pending")
        assert pending_generation is not None
        pending_generation.status = "failed"
        await db.commit()
        user = await db.get(User, user_id)
        session = await db.get(AgentSession, session_id)
        assert user is not None
        assert session is not None
        monkeypatch.setattr(
            agent_message_service,
            "agent_setting_int",
            setting_reader,
        )

        second = await agent_sessions_service.submit_agent_message(
            db,
            session_id=session.id,
            user=user,
            body=AgentMessageCreateIn(
                idempotency_key="session-context-2",
                text="make it less sharp",
            ),
            request=None,
        )

        assert second.user_message.content["attachments"] == []
        assert second.agent_run.reasoning_effort == "max"
        assert [reference.image_id for reference in second.agent_run.references] == [
            "image-original",
            "image-agent-result",
        ]
        assert [
            reference.reference_label for reference in second.agent_run.references
        ] == [
            "ref_1",
            "ref_2",
        ]
        persisted = await db.get(AgentRun, second.agent_run.id)
        assert persisted is not None
        assert persisted.request_snapshot_jsonb["reference_policy"] == {
            "max_reference_images": 16,
            "max_session_images": 64,
        }

        persisted.status = AgentRunStatus.SUCCEEDED.value
        db.add(
            Image(
                id="image-new",
                user_id=user.id,
                source="uploaded",
                storage_key="u/user-1/image-new.png",
                mime="image/png",
                width=1024,
                height=1024,
                size_bytes=128,
                sha256="b" * 64,
                visibility="private",
                metadata_jsonb={},
                artifact_status="ready",
            )
        )
        await db.commit()
        third = await agent_sessions_service.submit_agent_message(
            db,
            session_id=session.id,
            user=user,
            body=AgentMessageCreateIn(
                idempotency_key="session-context-3",
                text="use every session image",
                attachments=[
                    {"image_id": "image-new", "role": "subject"},
                    {"image_id": "image-original", "role": "style"},
                ],
            ),
            request=None,
        )
        assert [reference.image_id for reference in third.agent_run.references] == [
            "image-original",
            "image-agent-result",
            "image-new",
        ]
        assert [
            reference.reference_label for reference in third.agent_run.references
        ] == ["ref_1", "ref_2", "ref_3"]
        assert third.agent_run.references[0].role == "style"


@pytest.mark.asyncio
async def test_agent_tool_gateway_creates_one_generation_batch_and_replays_receipt(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_message_dependencies(monkeypatch)

    async def no_audit(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def image_preflight(*_args: Any, **_kwargs: Any) -> tuple[str, ...]:
        return ("image-provider",)

    async def no_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    batch_submissions = 0
    create_generation_batch = agent_tools_service.create_generation_batch_for_message

    async def counted_generation_batch(command: Any) -> Any:
        nonlocal batch_submissions
        batch_submissions += 1
        return await create_generation_batch(command)

    monkeypatch.setattr(agent_tools_service, "write_audit", no_audit)
    monkeypatch.setattr(
        agent_tools_service,
        "wallet_image_provider_preflight",
        image_preflight,
    )
    monkeypatch.setattr(agent_tools_service, "publish_assistant_task", no_publish)
    monkeypatch.setattr(
        agent_tools_service,
        "create_generation_batch_for_message",
        counted_generation_batch,
    )
    monkeypatch.setattr(
        agent_tools_service,
        "publish_agent_events_best_effort",
        no_publish,
    )

    async with db_factory() as db:
        db.add(SystemSetting(key="billing.enabled", value="0"))
        await db.commit()
        user, session, _conversation = await _seed_session(
            db,
            image_ids=("image-1", "image-2"),
        )
        submitted = await agent_sessions_service.submit_agent_message(
            db,
            session_id=session.id,
            user=user,
            body=AgentMessageCreateIn(
                idempotency_key="tool-run-1",
                text="create two images",
                attachments=[
                    {"image_id": "image-1", "role": "product"},
                    {"image_id": "image-2", "role": "style"},
                ],
                image_defaults={
                    "count": 2,
                    "aspect_ratio": "3:4",
                    "quality": "2k",
                    "render_quality": "high",
                    "background": "auto",
                    "output_format": "webp",
                },
            ),
            request=None,
        )
        run = await db.get(AgentRun, submitted.agent_run.id)
        assert run is not None
        run.status = AgentRunStatus.RUNNING.value
        run.execution_epoch = 1
        await db.commit()
        run_id = run.id
        session_id = session.id
        claims = _capability(run=run, reference_labels=["ref_1", "ref_2"])
        db.add(
            AgentCapabilityGrant(
                capability_id=claims.capability_id,
                nonce=claims.nonce,
                agent_run_id=run.id,
                user_id=run.user_id,
                agent_session_id=run.agent_session_id,
                execution_epoch=run.execution_epoch,
                expires_at=datetime.fromtimestamp(
                    claims.expires_at,
                    tz=timezone.utc,
                ),
                max_redemptions=4,
                redeemed_count=0,
            )
        )
        await db.commit()
        request = AgentToolCreateImageIn(
            pi_tool_call_id="pi-tool-1",
            ordinal=0,
            execution_epoch=1,
            arguments={
                "prompt": "clean campaign poster",
                "reference_labels": ["ref_2", "ref_1"],
            },
        )
        with pytest.raises(HTTPException) as stale_epoch:
            await agent_tools_service.submit_create_image_tool(
                db,
                run_id=run_id,
                claims=claims,
                body=request.model_copy(update={"execution_epoch": 0}),
            )
        assert stale_epoch.value.detail["error"]["code"] == (
            "agent_stale_execution_epoch"
        )
        with pytest.raises(HTTPException) as reference_denied:
            await agent_tools_service.submit_create_image_tool(
                db,
                run_id=run_id,
                claims=claims.model_copy(
                    update={"allowed_reference_labels": ["ref_1"]}
                ),
                body=request,
            )
        assert reference_denied.value.detail["error"]["code"] == (
            "agent_reference_not_allowed"
        )
        assert await db.scalar(select(func.count(AgentToolCall.id))) == 0
        first = await agent_tools_service.submit_create_image_tool(
            db,
            run_id=run_id,
            claims=claims,
            body=request,
        )
        assert first.replayed is False
        assert first.mode == "image_to_image"
        assert first.accepted.reference_labels == ["ref_2", "ref_1"]
        assert first.accepted.count == 2
        assert len(first.generation_ids) == 2
        generations = list(
            (await db.execute(select(Generation).order_by(Generation.created_at.asc())))
            .scalars()
            .all()
        )
        assert len(generations) == 2
        assert all(
            item.input_image_ids == ["image-2", "image-1"] for item in generations
        )
        assert all(item.primary_input_image_id == "image-2" for item in generations)
        assert all(item.size_requested == "1248x1664" for item in generations)
        assert all(item.source == "agent" for item in generations)
        assert all(item.upstream_request["n"] == 1 for item in generations)
        assert all(item.agent_session_id == session_id for item in generations)
        assert all(item.agent_run_id == run_id for item in generations)
        assert await db.scalar(select(func.count(AgentToolCall.id))) == 1
        assert batch_submissions == 1

        replay = await agent_tools_service.submit_create_image_tool(
            db,
            run_id=run_id,
            claims=claims,
            body=request,
        )
        assert replay.replayed is True
        assert replay.generation_ids == first.generation_ids
        assert await db.scalar(select(func.count(Generation.id))) == 2
        assert batch_submissions == 1
        grant = await db.get(AgentCapabilityGrant, claims.capability_id)
        assert grant is not None and grant.redeemed_count == 1
        with pytest.raises(HTTPException) as conflict:
            await agent_tools_service.submit_create_image_tool(
                db,
                run_id=run_id,
                claims=claims,
                body=request.model_copy(
                    update={
                        "arguments": request.arguments.model_copy(
                            update={"prompt": "different"}
                        )
                    }
                ),
            )
        assert conflict.value.detail["error"]["code"] == ("agent_tool_ordinal_conflict")
        await db.rollback()

        async def fail_preflight(*_args: Any, **_kwargs: Any) -> tuple[str, ...]:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "code": "agent_image_provider_unavailable",
                        "message": "unavailable",
                    }
                },
            )

        monkeypatch.setattr(
            agent_tools_service,
            "wallet_image_provider_preflight",
            fail_preflight,
        )
        failed_request = AgentToolCreateImageIn(
            pi_tool_call_id="pi-tool-2",
            ordinal=1,
            execution_epoch=1,
            arguments={"prompt": "will fail", "count": 1},
        )
        with pytest.raises(HTTPException) as failed:
            await agent_tools_service.submit_create_image_tool(
                db,
                run_id=run_id,
                claims=claims,
                body=failed_request,
            )
        assert failed.value.status_code == 503
        assert await db.scalar(select(func.count(Generation.id))) == 2
        failed_receipt = (
            await db.execute(select(AgentToolCall).where(AgentToolCall.ordinal == 1))
        ).scalar_one()
        assert failed_receipt.status == "failed"
        assert failed_receipt.error_code == "agent_image_provider_unavailable"

        with pytest.raises(HTTPException) as crossed_receipts:
            await agent_tools_service.submit_create_image_tool(
                db,
                run_id=run_id,
                claims=claims,
                body=request.model_copy(update={"pi_tool_call_id": "pi-tool-2"}),
            )
        assert crossed_receipts.value.detail["error"]["code"] == (
            "agent_tool_ordinal_conflict"
        )

        monkeypatch.setattr(
            agent_tools_service,
            "wallet_image_provider_preflight",
            image_preflight,
        )
        failed_replay = await agent_tools_service.submit_create_image_tool(
            db,
            run_id=run_id,
            claims=claims,
            body=failed_request,
        )
        assert failed_replay.replayed is True
        assert failed_replay.tool_call.status == "failed"
        assert await db.scalar(select(func.count(Generation.id))) == 2
        assert batch_submissions == 1
        await db.refresh(grant)
        assert grant.redeemed_count == 2


@pytest.mark.asyncio
async def test_invalid_reference_is_rejected_before_run_hold_or_messages(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_message_dependencies(monkeypatch)
    reserve_calls = 0

    async def invalid_reference(_image: Image) -> None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "invalid_attachment",
                    "message": "decode failed",
                }
            },
        )

    async def unexpected_reserve(*_args: Any, **_kwargs: Any) -> AgentTextReservation:
        nonlocal reserve_calls
        reserve_calls += 1
        return AgentTextReservation(0, {})

    monkeypatch.setattr(
        agent_message_service,
        "_validate_reference_artifact",
        invalid_reference,
    )
    monkeypatch.setattr(
        agent_message_service,
        "reserve_agent_text",
        unexpected_reserve,
    )
    async with db_factory() as db:
        user, session, _conversation = await _seed_session(
            db,
            image_ids=("invalid-reference",),
        )
        with pytest.raises(HTTPException) as captured:
            await agent_sessions_service.submit_agent_message(
                db,
                session_id=session.id,
                user=user,
                body=AgentMessageCreateIn(
                    idempotency_key="invalid-reference-run",
                    text="use this",
                    attachments=[{"image_id": "invalid-reference"}],
                ),
                request=None,
            )
        assert captured.value.detail["error"]["code"] == "invalid_attachment"
        assert reserve_calls == 0
        assert await db.scalar(select(func.count(AgentRun.id))) == 0
        assert await db.scalar(select(func.count(Message.id))) == 0


@pytest.mark.asyncio
async def test_reference_preflight_decodes_and_matches_declared_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_root = agent_message_service.settings.storage_root
    monkeypatch.setattr(agent_message_service.settings, "storage_root", str(tmp_path))
    path = tmp_path / "references" / "image.png"
    path.parent.mkdir(parents=True)
    PILImage.new("RGB", (8, 8), (10, 20, 30)).save(path, format="PNG")
    image = Image(
        id="reference-artifact",
        user_id="user-1",
        source="uploaded",
        storage_key="references/image.png",
        mime="image/png",
        width=8,
        height=8,
        size_bytes=path.stat().st_size,
        sha256="a" * 64,
        artifact_status="ready",
    )
    try:
        await agent_message_service._validate_reference_artifact(image)
        image.mime = "image/jpeg"
        with pytest.raises(HTTPException) as captured:
            await agent_message_service._validate_reference_artifact(image)
        assert captured.value.detail["error"]["code"] == "invalid_attachment"
    finally:
        agent_message_service.settings.storage_root = original_root


@pytest.mark.asyncio
async def test_agent_privacy_export_omits_capabilities_and_internal_results(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as db:
        user, session, conversation = await _seed_session(db)
        user_message = Message(
            id="export-user-message",
            conversation_id=conversation.id,
            role="user",
            content={"text": "private prompt", "source": "agent"},
        )
        assistant_message = Message(
            id="export-assistant-message",
            conversation_id=conversation.id,
            role="assistant",
            content={"text": "done", "source": "agent"},
        )
        db.add_all([user_message, assistant_message])
        await db.flush()
        run = AgentRun(
            id="export-run",
            agent_session_id=session.id,
            user_id=user.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            status="succeeded",
            idempotency_key="export-idem",
            request_fingerprint="a" * 64,
            request_snapshot_jsonb={"secret_runtime_ticket": "do-not-export"},
            account_mode_snapshot="wallet",
            system_prompt_snapshot="internal system prompt",
            usage_jsonb={"input_tokens": 10, "output_tokens": 5},
        )
        db.add(run)
        await db.flush()
        db.add(
            AgentToolCall(
                id="export-tool",
                agent_run_id=run.id,
                capability_id="capability-do-not-export",
                pi_tool_call_id="pi-tool-export",
                ordinal=0,
                execution_epoch=0,
                name=AGENT_TOOL_CREATE_IMAGE,
                mode="text_to_image",
                status="succeeded",
                request_hash="b" * 64,
                semantic_key="c" * 64,
                arguments_jsonb={
                    "prompt": "exported user image prompt",
                    "reference_labels": [],
                    "callback_url": "http://internal/secret",
                },
                result_jsonb={
                    "generation_ids": ["generation-1"],
                    "provider_response": {"secret": True},
                },
                generation_count=1,
            )
        )
        await db.commit()
        user_id = user.id
        session_id = session.id
        run_id = run.id

        session_batches = [
            batch
            async for batch in me_export.iter_export_agent_session_batches(db, user_id)
        ]
        run_batches = [
            batch
            async for batch in me_export.iter_export_agent_run_batches(db, user_id)
        ]
        tool_batches = [
            batch
            async for batch in me_export.iter_export_agent_tool_call_batches(
                db, user_id
            )
        ]
        assert session_batches[0][0].id == session_id
        exported_run = run_batches[0][0]
        assert exported_run.id == run_id
        assert not hasattr(exported_run, "request_snapshot_jsonb")
        assert not hasattr(exported_run, "system_prompt_snapshot")
        exported_tool = tool_batches[0][0]
        assert exported_tool.arguments == {
            "prompt": "exported user image prompt",
            "reference_labels": [],
        }
        assert exported_tool.generation_ids == ["generation-1"]
        assert not hasattr(exported_tool, "capability_id")


@pytest.mark.asyncio
async def test_agent_channel_requires_session_ownership_and_conv_channel_is_hidden(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as db:
        _user, session, conversation = await _seed_session(db)
        assert await validate_channels([f"agent:{session.id}"], "user-1", db) == [
            f"agent:{session.id}"
        ]
        with pytest.raises(ChannelPolicyError):
            await validate_channels([f"agent:{session.id}"], "user-2", db)
        with pytest.raises(ChannelPolicyError):
            await validate_channels([f"conv:{conversation.id}"], "user-1", db)


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "status"), [("1", 200), ("0", 404), (None, 404)])
async def test_agent_feature_gate_uses_capability_flag_not_navigation_visibility(
    value: str | None,
    status: int,
) -> None:
    seen_keys: list[tuple[str, ...]] = []

    class Flags:
        async def read(self, keys: tuple[str, ...]) -> dict[str, str | None]:
            seen_keys.append(keys)
            return {"agent.enabled": value}

    async def downstream(_scope: Any, _receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = main._NavFeatureGuardMiddleware(downstream, Flags())
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(
        {"type": "http", "method": "GET", "path": "/agent/status", "headers": []},
        receive,
        send,
    )
    response_start = next(
        item for item in sent if item["type"] == "http.response.start"
    )
    assert response_start["status"] == status
    assert seen_keys == [("agent.enabled",)]


def test_agent_routes_and_outbox_contract_are_registered() -> None:
    paths = {route.path for route in main.app.routes if hasattr(route, "path")}
    assert "/agent/sessions/{session_id}/messages" in paths
    assert "/agent/runs/{run_id}/cancel" in paths
    assert "/internal/agent/runs/{run_id}/tools/create-image" in paths

    from app.services.agent.common import stage_agent_run_dispatch

    assert callable(stage_agent_run_dispatch)
    # Keep the dispatch contract explicit even though the Worker implementation
    # belongs to the next implementation wave.
    from app import main as _main

    assert _main._agent_feature_for_api_path("/internal/agent/runs/run-1") == (
        "agent",
        "agent.enabled",
    )


def test_api_agent_sentry_frame_variables_are_scrubbed() -> None:
    event = {
        "threads": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "vars": {
                                    "arguments": {"prompt": "private"},
                                    "capability": "capability-secret",
                                    "proxy_url": "socks5://user:password@proxy:1080",
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }
    scrubbed = api_observability._sentry_before_send(  # noqa: SLF001
        event,  # type: ignore[arg-type]
        {},
    )
    rendered = str(scrubbed)
    assert "private" not in rendered
    assert "capability-secret" not in rendered
    assert "password@proxy" not in rendered
