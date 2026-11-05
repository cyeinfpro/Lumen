"""Vision SVG admission and local-only repair contracts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from lumen_core.constants import Intent
from lumen_core.schemas import ChatParamsIn, PostMessageIn
from app.services import message_submission as submission
from app.services.message_request import build_user_content
from app.services.message_idempotency import message_request_fingerprint


def request(**updates):
    values = dict(
        idempotency_key="same-key",
        text="Reconstruct as vector shapes",
        intent="vision_qa",
        attachment_image_ids=["source"],
        chat_params={"artifact_format": "svg"},
    )
    values.update(updates)
    return PostMessageIn(**values)


@pytest.mark.parametrize(
    "field", ["web_search", "file_search", "code_interpreter", "image_generation"]
)
def test_tools_rejected(field):
    with pytest.raises(ValidationError):
        request(chat_params={"artifact_format": "svg", field: True})


@pytest.mark.parametrize(
    "updates",
    [
        {"intent": "chat"},
        {"intent": "text_to_image"},
        {"attachment_image_ids": []},
        {"attachment_image_ids": ["a", "b"]},
        {"mask_image_id": "mask"},
        {"markup_image_id": "markup"},
        {"advanced": {"stream_partial_image": True}},
        {"chat_params": {"artifact_format": "svg", "max_output_tokens": 16001}},
        {"chat_params": {"artifact_format": "svg", "system_prompt": "unrelated"}},
        {"chat_params": {"artifact_format": "svg", "vector_store_ids": ["vs"]}},
        {"image_params": {"count": 2}},
        {"image_params": {"output_format": "png"}},
    ],
)
def test_ambiguous_or_unbounded_requests_rejected(updates):
    with pytest.raises(ValidationError):
        request(**updates)


def test_local_contract_and_current_message():
    body = request()
    assert submission._chat_upstream_request(body.chat_params) == {
        "artifact_format": "svg",
        "artifact_status": "pending",
        "max_output_tokens": 2048,
    }
    content = build_user_content(
        body,
        request_metadata={},
        attachment_ids=["source"],
        chat_params=body.chat_params,
        intent=Intent.VISION_QA,
        allowed_reasoning_efforts=set(),
        http_error=RuntimeError,
    )
    assert content["artifact_format"] == "svg"
    assert content["attachments"] == [{"image_id": "source"}]
    assert not any(
        content.get(k) for k in ("web_search", "file_search", "image_generation")
    )


def test_idempotency_covers_mode_and_payload():
    assert message_request_fingerprint(request()) != message_request_fingerprint(
        request(chat_params={})
    )
    assert message_request_fingerprint(request()) != message_request_fingerprint(
        request(text="other")
    )
    assert message_request_fingerprint(request()) == message_request_fingerprint(
        request()
    )


def source(**updates):
    fields = dict(
        id="source",
        user_id="owner",
        deleted_at=None,
        artifact_status="ready",
        mime="image/png",
        width=100,
        height=100,
        size_bytes=1024,
        sha256="a" * 64,
        metadata_jsonb={},
    )
    fields.update(updates)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"user_id": "other"},
        {"deleted_at": "yesterday"},
        {"artifact_status": "processing"},
        {"mime": "image/svg+xml"},
        {"mime": "image/gif"},
        {"metadata_jsonb": {"is_animated": True}},
        {"metadata_jsonb": {"frame_count": 2}},
        {"metadata_jsonb": {"frame_count": "bad"}},
        {"width": 0},
        {"height": -1},
        {"width": 4001, "height": 4000},
        {"size_bytes": 20 * 1024 * 1024 + 1},
        {"size_bytes": 0},
    ],
)
async def test_owned_static_bounded_source_required(updates):
    db = SimpleNamespace(get=AsyncMock(return_value=source(**updates)))
    with pytest.raises(HTTPException):
        await submission._validate_vision_svg_source(
            db, user_id="owner", attachment_ids=["source"], intent=Intent.VISION_QA
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/webp"])
async def test_valid_source(mime):
    image = source(mime=mime)
    db = SimpleNamespace(get=AsyncMock(return_value=image))
    assert (
        await submission._validate_vision_svg_source(
            db, user_id="owner", attachment_ids=["source"], intent=Intent.VISION_QA
        )
        is image
    )


@pytest.mark.asyncio
async def test_creation_persists_text_pricing_provenance():
    from lumen_core.models import Message, Completion

    msg = Message(id="assistant", content={})
    added = []
    db = SimpleNamespace(
        get=AsyncMock(return_value=source()), add=added.append, flush=AsyncMock()
    )
    preflight = AsyncMock(return_value=None)
    command = submission.CompletionTaskCommand(
        user_id="owner",
        user_email=None,
        account_mode="byok",
        assistant_msg=msg,
        intent=Intent.VISION_QA,
        stored_key="key",
        attachment_ids=["source"],
        chat_params=ChatParamsIn(artifact_format="svg"),
        system_prompt=None,
        request_metadata=None,
        credential_pin=None,
        chat_wallet_preflight_done=False,
        chat_wallet_preflight=None,
    )
    await submission._create_completion_task(
        db,
        command,
        submission.CompletionTaskServices(
            ensure_chat_wallet_preflight=preflight,
            billing_allow_negative=AsyncMock(),
            write_audit=AsyncMock(),
        ),
    )
    comp = next(x for x in added if isinstance(x, Completion))
    assert comp.upstream_request["source_image_sha256"] == "a" * 64
    assert comp.upstream_request["source_image_id"] == "source"
    assert comp.input_image_ids == ["source"]
    assert msg.content == {"artifact_format": "svg", "artifact_status": "pending"}
    assert preflight.call_args.kwargs["model"] == comp.model
    assert "image_generation" not in comp.upstream_request


def completed():
    return SimpleNamespace(
        id="completion",
        user_id="owner",
        message_id="message",
        status="succeeded",
        attempt=2,
        execution_epoch=3,
        cancel_requested_at=None,
        text="<svg/>",
        upstream_request={
            "artifact_format": "svg",
            "artifact_status": "processing_failed",
            "completion_checkpoint_response_id": "resp",
            "completion_checkpoint_usage_exact": True,
            "completion_checkpoint_state": "billing_ready",
            "completion_checkpoint_execution_epoch": 3,
            "completion_checkpoint_attempt_epoch": 2,
        },
    )


async def repair_setup(monkeypatch, comp):
    from app.routes import tasks

    user = SimpleNamespace(id="owner")
    monkeypatch.setattr(
        tasks,
        "lock_authenticated_user_snapshot",
        AsyncMock(return_value=SimpleNamespace(user=user)),
    )
    monkeypatch.setattr(tasks, "durable_session_id", lambda req: None)
    publish = AsyncMock()
    monkeypatch.setattr(tasks, "_publish_queued", publish)
    rows = []

    async def flush():
        for row in rows:
            row.id = "outbox"

    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: comp)
        ),
        add=rows.append,
        flush=flush,
        commit=AsyncMock(),
    )
    return tasks, user, db, rows, publish


@pytest.mark.asyncio
async def test_repair_retains_terminal_task_and_no_hold(monkeypatch):
    comp = completed()
    before = (
        comp.status,
        comp.attempt,
        comp.execution_epoch,
        comp.text,
        dict(comp.upstream_request),
    )
    tasks, user, db, rows, publish = await repair_setup(monkeypatch, comp)
    result = await tasks.retry_completion_artifact(comp.id, user, db)
    assert result == {"status": "succeeded", "artifact_status": "processing_failed"}
    assert before == (
        comp.status,
        comp.attempt,
        comp.execution_epoch,
        comp.text,
        comp.upstream_request,
    )
    assert len(rows) == 1 and rows[0].kind == "completion"
    assert rows[0].payload["artifact_repair"] is True
    assert rows[0].payload["execution_epoch"] == 3
    publish.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "canceled",
        "streaming",
        "invalid",
        "ready",
        "no_receipt",
        "inexact",
        "stale_epoch",
        "stale_attempt",
    ],
)
async def test_repair_rejects_nonlocal_or_stale_work(monkeypatch, kind):
    comp = completed()
    if kind == "missing":
        comp = None
    elif kind == "canceled":
        comp.cancel_requested_at = "now"
    elif kind == "streaming":
        comp.status = "streaming"
    elif kind in {"invalid", "ready"}:
        comp.upstream_request["artifact_status"] = kind
    elif kind == "no_receipt":
        comp.upstream_request.pop("completion_checkpoint_response_id")
    elif kind == "inexact":
        comp.upstream_request["completion_checkpoint_usage_exact"] = False
    elif kind == "stale_epoch":
        comp.upstream_request["completion_checkpoint_execution_epoch"] = 1
    elif kind == "stale_attempt":
        comp.upstream_request["completion_checkpoint_attempt_epoch"] = 1
    tasks, user, db, rows, publish = await repair_setup(monkeypatch, comp)
    with pytest.raises(HTTPException):
        await tasks.retry_completion_artifact("completion", user, db)
    assert not rows
    publish.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_svg_fast_mode_rejected_and_default_cannot_override():
    with pytest.raises(ValidationError):
        request(chat_params={"artifact_format": "svg", "fast": True})
    params = ChatParamsIn(artifact_format="svg")
    assert params.fast is False
    assert submission.chat_params_with_fast_default(params, True).fast is False
    pin = SimpleNamespace(
        default_chat_model="normal-model", fast_chat_model="priority-model"
    )
    assert submission._select_chat_task_model(pin, params) == "normal-model"


def test_legacy_default_none_fingerprint_unchanged():
    from app.services.message_idempotency import (
        canonical_request_fingerprint,
        MESSAGE_CREATE_IDEMPOTENCY_OPERATION,
    )

    body = request(chat_params={})
    payload = body.model_dump(mode="json", exclude={"idempotency_key"})
    payload.pop("markup_image_id")
    payload["chat_params"].pop("artifact_format")
    for key in ("complexity", "frame_count", "frame_duration_ms", "svg"):
        payload["image_params"].pop(key)
    assert message_request_fingerprint(body) == canonical_request_fingerprint(
        MESSAGE_CREATE_IDEMPOTENCY_OPERATION, payload
    )


def test_same_key_svg_payload_conflict_is_rejected():
    from app.services.message_idempotency import (
        require_matching_task_idempotency,
        MESSAGE_CREATE_IDEMPOTENCY_OPERATION,
    )

    first = request()
    task = SimpleNamespace(
        upstream_request={
            "idempotency_operation_namespace": MESSAGE_CREATE_IDEMPOTENCY_OPERATION,
            "idempotency_request_fingerprint": message_request_fingerprint(first),
        }
    )

    def error(code, message, status):
        return HTTPException(status, {"code": code})

    with pytest.raises(HTTPException) as exc:
        require_matching_task_idempotency(
            [task],
            operation_namespace=MESSAGE_CREATE_IDEMPOTENCY_OPERATION,
            request_fingerprint=message_request_fingerprint(request(text="different")),
            http_error=error,
        )
    assert exc.value.status_code == 409


@pytest.mark.parametrize("status", ["pending", "invalid", "processing_failed", "ready"])
def test_svg_message_projection_never_exposes_markdown_source(status):
    from lumen_core.message_content import public_message_content

    raw = "<svg><script>alert(1)</script></svg>"
    projected = public_message_content(
        {
            "artifact_format": "svg",
            "artifact_status": status,
            "text": raw,
            "thinking": raw,
            "artifact_source_text": raw,
            "_private": raw,
        }
    )
    assert "<svg" not in projected["text"]
    assert "thinking" not in projected and "_private" not in projected
    assert projected["artifact_source_text"] == raw
    assert (
        public_message_content({"artifact_format": "svg", "text": "User prompt"})[
            "text"
        ]
        == "User prompt"
    )


def test_completion_projection_never_uses_raw_svg_as_text():
    from lumen_core.schemas import CompletionOut
    from datetime import datetime, timezone

    values = dict(
        id="id",
        message_id="msg",
        model="configured-model",
        input_image_ids=["source"],
        text="<svg/>",
        tokens_in=1307,
        tokens_out=214,
        status="succeeded",
        progress_stage="finalizing",
        attempt=1,
        error_code=None,
        error_message=None,
        started_at=None,
        finished_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        upstream_request={
            "artifact_format": "svg",
            "artifact_status": "invalid",
            "completion_checkpoint_response_id": "resp",
            "raw_response": "<svg/>",
        },
    )
    projected = CompletionOut.model_validate(values)
    assert projected.artifact_source_text == "<svg/>"
    assert "<svg" not in projected.text
    assert projected.artifact_status == "invalid"
    assert "raw_response" not in projected.upstream_request


def test_regenerate_preserves_explicit_svg_contract():
    from app.routes.regenerate_options import chat_params_from_user_content

    params = chat_params_from_user_content(
        {"artifact_format": "svg", "max_output_tokens": 4096}
    )
    assert params.artifact_format == "svg" and params.max_output_tokens == 4096
    assert not any(
        (
            params.web_search,
            params.file_search,
            params.image_generation,
            params.code_interpreter,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checkpoint", ["exact", "missing_id", "inexact_zero_usage", "nested"]
)
async def test_ordinary_retry_cannot_replay_completed_svg(monkeypatch, checkpoint):
    comp = completed()
    comp.status = "failed"
    if checkpoint != "exact":
        comp.upstream_request = {"artifact_format": "svg"}
        if checkpoint == "nested":
            comp.upstream_request["completion_checkpoint"] = {
                "version": 2,
                "usage_exact": False,
            }
        else:
            comp.upstream_request["completion_checkpoint_version"] = 2
            if checkpoint == "inexact_zero_usage":
                comp.upstream_request["completion_checkpoint_usage_exact"] = False
                comp.upstream_request["completion_checkpoint_usage"] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
    tasks, user, db, rows, publish = await repair_setup(monkeypatch, comp)
    with pytest.raises(HTTPException) as exc:
        await tasks.retry_completion(comp.id, user, db)
    assert exc.value.status_code == 409
    assert exc.value.detail["error"]["code"] == "artifact_local_retry_required"
    assert comp.status == "failed" and comp.execution_epoch == 3 and comp.attempt == 2
    assert not rows
    publish.assert_not_awaited()


def billing_command(mode):
    from lumen_core.models import Message

    return submission.CompletionTaskCommand(
        user_id="owner",
        user_email=None,
        account_mode=mode,
        assistant_msg=Message(id="assistant", content={}),
        intent=Intent.VISION_QA,
        stored_key="key",
        attachment_ids=["source"],
        chat_params=ChatParamsIn(artifact_format="svg", max_output_tokens=4096),
        system_prompt=None,
        request_metadata=None,
        credential_pin=SimpleNamespace(
            default_chat_model="configured-vision",
            fast_chat_model="priority-vision",
            credential_id="credential",
            supplier_id="supplier",
        ),
        chat_wallet_preflight_done=False,
        chat_wallet_preflight=None,
    )


@pytest.mark.asyncio
async def test_svg_wallet_preflight_forwards_model_budget_and_holds_only_text(
    monkeypatch,
):
    from app.task_billing import ChatWalletPreflight
    from lumen_core.models import Completion

    rows = []

    async def flush():
        for row in rows:
            if isinstance(row, Completion):
                row.id = "comp"

    db = SimpleNamespace(
        get=AsyncMock(return_value=source()), add=rows.append, flush=flush
    )
    snapshot = {"model": "configured-vision", "test": "text-token-pricing"}
    preflight = AsyncMock(
        return_value=ChatWalletPreflight(
            estimated_model_micro=23000,
            tool_budget_micro=0,
            preauth_micro=23000,
            tool_budget_by_tool={},
            pricing_snapshot=snapshot,
        )
    )
    hold = AsyncMock(return_value=None)
    monkeypatch.setattr(submission.billing_core, "hold", hold)
    command = billing_command("wallet")
    await submission._create_completion_task(
        db,
        command,
        submission.CompletionTaskServices(
            ensure_chat_wallet_preflight=preflight,
            billing_allow_negative=AsyncMock(return_value=False),
            write_audit=AsyncMock(),
        ),
    )
    args = preflight.call_args.kwargs
    assert args["model"] == "configured-vision"
    assert args["account_mode"] == "wallet"
    assert args["chat_params"].max_output_tokens == 4096
    assert (
        args["chat_params"].artifact_format == "svg"
        and args["chat_params"].fast is False
    )
    assert not args["chat_params"].image_generation
    comp = next(row for row in rows if isinstance(row, Completion))
    assert comp.model == "configured-vision"
    assert comp.upstream_request["max_output_tokens"] == 4096
    assert comp.upstream_request["billing_pricing_snapshot"] == snapshot
    hold.assert_awaited_once()
    assert hold.call_args.args[2] == 23000
    assert hold.call_args.kwargs["ref_type"] == "completion"
    assert hold.call_args.kwargs["idempotency_key"] == "hold:comp"
    assert hold.call_args.kwargs["meta"]["tool_budget_micro"] == 0


@pytest.mark.asyncio
async def test_svg_missing_prices_fail_before_completion_or_hold(monkeypatch):
    from app.services import message_submission_billing as billing

    wallet = AsyncMock(return_value=SimpleNamespace(balance_micro=1_000_000))
    pricing = AsyncMock(
        side_effect=billing.billing_core.BillingError(
            "PRICING_MISSING", "missing configured vision model pricing", 503
        )
    )
    hold = AsyncMock()
    monkeypatch.setattr(billing.billing_core, "get_wallet", wallet)
    monkeypatch.setattr(billing.billing_core, "completion_pricing_snapshot", pricing)
    monkeypatch.setattr(billing.billing_core, "hold", hold)

    async def preflight(db, **kwargs):
        return await billing.ensure_chat_wallet_preflight(
            db,
            **kwargs,
            billing_enabled_fn=AsyncMock(return_value=True),
            user_rate_multiplier_fn=AsyncMock(return_value=10000),
        )

    rows = []
    db = SimpleNamespace(
        get=AsyncMock(return_value=source()), add=rows.append, flush=AsyncMock()
    )
    with pytest.raises(HTTPException) as exc:
        await submission._create_completion_task(
            db,
            billing_command("wallet"),
            submission.CompletionTaskServices(
                ensure_chat_wallet_preflight=preflight,
                billing_allow_negative=AsyncMock(),
                write_audit=AsyncMock(),
            ),
        )
    assert exc.value.status_code == 503
    assert exc.value.detail["error"]["code"] == "PRICING_MISSING"
    assert pricing.call_args.kwargs["model"] == "configured-vision"
    assert not rows
    hold.assert_not_awaited()


@pytest.mark.asyncio
async def test_svg_byok_uses_real_preflight_without_wallet_or_synthetic_image_fee(
    monkeypatch,
):
    from app.services import message_submission_billing as billing
    from lumen_core.models import Completion

    wallet, pricing, hold = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(billing.billing_core, "get_wallet", wallet)
    monkeypatch.setattr(billing.billing_core, "completion_pricing_snapshot", pricing)
    monkeypatch.setattr(billing.billing_core, "hold", hold)
    rows = []
    db = SimpleNamespace(
        get=AsyncMock(return_value=source()), add=rows.append, flush=AsyncMock()
    )
    await submission._create_completion_task(
        db,
        billing_command("byok"),
        submission.CompletionTaskServices(
            ensure_chat_wallet_preflight=billing.ensure_chat_wallet_preflight,
            billing_allow_negative=AsyncMock(),
            write_audit=AsyncMock(),
        ),
    )
    comp = next(row for row in rows if isinstance(row, Completion))
    assert comp.model == "configured-vision"
    assert comp.user_api_credential_id == "credential"
    assert comp.upstream_supplier_id == "supplier"
    assert "billing_pricing_snapshot" not in comp.upstream_request
    assert "image_generation" not in comp.upstream_request
    wallet.assert_not_awaited()
    pricing.assert_not_awaited()
    hold.assert_not_awaited()
