"""Assistant task construction and post-commit publication services."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    EV_COMP_QUEUED,
    EV_CONV_MSG_APPENDED,
    EV_GEN_QUEUED,
    CompletionStage,
    CompletionStatus,
    GenerationAction,
    GenerationStage,
    GenerationStatus,
    IMAGE_MULTI_GEN_STAGGER_CAP_S,
    IMAGE_MULTI_GEN_STAGGER_S,
    MAX_PROMPT_CHARS,
    Intent,
    MessageStatus,
    Role,
    conv_channel,
    task_channel,
)
from lumen_core.models import (
    ApiSupplierTemplate,
    Completion,
    Conversation,
    Generation,
    Message,
    OutboxEvent,
    SystemSetting,
    SystemPrompt,
    UserApiCredential,
    new_uuid7,
)
from lumen_core.queue_metadata import generation_queue_metadata
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,
    MessageAttachmentIn,
    PostMessageIn,
)
from lumen_core.sizing import ResolvedSize, resolve_size

from ..arq_pool import get_arq_pool
from ..audit import write_audit
from ..byok_service import read_byok_settings_cached
from ..runtime_settings import get_setting
from ..sse_publish import publish_sse_event, publish_sse_events
from ..task_billing import (
    ChatWalletPreflight,
    apply_rate_multiplier_micro,
    requested_image_billing_tier,
    resolve_image_render_quality,
    user_rate_multiplier_x10000,
)


logger = logging.getLogger(__name__)

SYSTEM_PROMPT_SOURCE_LIMIT = MAX_PROMPT_CHARS
IMAGE_OUTPUT_FORMAT_VALUES = {"png", "jpeg", "webp"}
DEFAULT_IMAGE_OUTPUT_FORMAT = "jpeg"
GENERATION_FAST_DEFAULT_KEY = "generation.fast_default"

_VECTOR_STORE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_IMAGE_BACKGROUND_VALUES = {"auto", "opaque", "transparent"}
_IMAGE_MODERATION_VALUES = {"auto", "low"}
_TRANSPARENT_BACKGROUND_RE = re.compile(
    r"透明(?:底|背景|底色)|去背|抠图|免抠|无背景|"
    r"transparent\s+(?:background|bg)|background\s+transparent|"
    r"cutout|isolated\s+subject|"
    r"(?<!\w)(?:no|without)\s+(?:a\s+)?background\b",
    re.IGNORECASE,
)
_TRANSPARENT_BACKGROUND_NEGATIVE_RE = re.compile(
    r"不(?:要|需要|用)?透明(?:底|背景|底色)?|非透明|"
    r"不要(?:去背|抠图|免抠|无背景|移除背景|去掉背景)|"
    r"保留背景|不要(?:删除|移除|去掉).{0,6}背景|"
    r"opaque\s+background|no\s+transparent\s+(?:background|bg)",
    re.IGNORECASE,
)
_TRANSPARENT_BACKGROUND_NEGATIVE_CONTEXT_RE = re.compile(
    r"(?<!\w)(?:no|without)\s+(?:a\s+)?background\s+"
    r"(?:blur|bokeh|noise|characters?|people|persons?|subjects?|"
    r"objects?|details?|change|changes|music|story|context|scene|"
    r"scenery|lighting|shadows?|text|pattern|elements?)\b",
    re.IGNORECASE,
)
_PROMPT_CONTROL_TRANSLATION = {i: " " for i in range(32) if i not in (9, 10, 13)}
_PROMPT_CONTROL_TRANSLATION[127] = " "
_SYSTEM_PROMPT_SECTION_TAG_RE = re.compile(r"(\[/?)(SYSTEM_[A-Z0-9_]+)(\])")
_SYSTEM_PROMPT_SECTION_TAG_ESCAPE = "\u200b"
_CHAT_TOOL_BUDGET_SETTINGS: dict[str, tuple[str, str]] = {
    "web_search": ("chat.tool_web_search_micro", "CHAT_TOOL_WEB_SEARCH_MICRO"),
    "file_search": ("chat.tool_file_search_micro", "CHAT_TOOL_FILE_SEARCH_MICRO"),
    "code_interpreter": (
        "chat.tool_code_interpreter_micro",
        "CHAT_TOOL_CODE_INTERPRETER_MICRO",
    ),
    "image_generation": (
        "chat.tool_image_generation_micro",
        "CHAT_TOOL_IMAGE_GENERATION_MICRO",
    ),
}
_MAX_TOOL_INVOCATIONS_DEFAULT = 8

AsyncCallable = Callable[..., Awaitable[Any]]


@dataclass
class AssistantTaskResult:
    assistant_msg: Message
    completion_id: str | None
    generation_ids: list[str]
    outbox_payloads: list[dict[str, Any]]
    outbox_rows: list[OutboxEvent]


@dataclass(frozen=True)
class TaskCredentialPin:
    credential_id: str
    supplier_id: str
    default_chat_model: str
    fast_chat_model: str | None
    default_image_model: str | None


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


def idempotency_lock_key(
    user_id: str,
    conv_id: str,
    idempotency_key: str,
) -> str:
    return f"{user_id}:{conv_id}:{idempotency_key}"


def stored_idempotency_key(conv_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"{conv_id}:{idempotency_key}".encode("utf-8", errors="replace")
    ).hexdigest()
    return f"cv:{digest[:61]}"


def generation_child_idempotency_key(base_key: str, index: int) -> str:
    if index <= 1:
        return base_key
    suffix = f":g{index}"
    prefix_len = 64 - len(suffix)
    return f"{base_key[:prefix_len]}{suffix}"


def image_multi_generation_defer_s(index: int) -> int:
    if index <= 1:
        return 0
    return min(IMAGE_MULTI_GEN_STAGGER_CAP_S, (index - 1) * IMAGE_MULTI_GEN_STAGGER_S)


def idempotency_lookup_keys(
    conv_id: str,
    idempotency_key: str,
) -> tuple[str, str]:
    return (idempotency_key, stored_idempotency_key(conv_id, idempotency_key))


async def billing_setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    try:
        return await get_setting(db, spec)
    except (AssertionError, IndexError):
        if key.startswith("billing."):
            return None
        raise


async def billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await billing_setting_raw(db, "billing.enabled"),
        False,
    )


async def billing_allow_negative(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await billing_setting_raw(db, "billing.allow_negative_balance"),
        False,
    )


def _parse_nonnegative_micro(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _enabled_chat_tools(chat_params: ChatParamsIn | None) -> list[str]:
    if chat_params is None:
        return []
    tools: list[str] = []
    if chat_params.web_search:
        tools.append("web_search")
    if chat_params.file_search:
        tools.append("file_search")
    if chat_params.code_interpreter:
        tools.append("code_interpreter")
    if chat_params.image_generation:
        tools.append("image_generation")
    return tools


async def chat_tool_budget_setting_micro(
    db: AsyncSession,
    tool_name: str,
) -> int:
    setting = _CHAT_TOOL_BUDGET_SETTINGS.get(tool_name)
    if setting is None:
        return 0
    setting_key, env_key = setting
    raw: object | None = None
    try:
        raw = (
            await db.execute(
                select(SystemSetting.value).where(SystemSetting.key == setting_key)
            )
        ).scalar_one_or_none()
    except Exception:
        logger.warning(
            "chat tool budget setting lookup failed key=%s",
            setting_key,
            exc_info=True,
        )
    if raw in (None, ""):
        raw = os.environ.get(env_key)
    return _parse_nonnegative_micro(raw)


async def chat_max_tool_invocations(db: AsyncSession) -> int:
    raw: object | None = None
    try:
        raw = (
            await db.execute(
                select(SystemSetting.value).where(
                    SystemSetting.key == "chat.max_tool_invocations"
                )
            )
        ).scalar_one_or_none()
    except Exception:
        logger.warning("chat max_tool_invocations lookup failed", exc_info=True)
    if raw in (None, ""):
        raw = os.environ.get("CHAT_MAX_TOOL_INVOCATIONS")
    if isinstance(raw, bool):
        return _MAX_TOOL_INVOCATIONS_DEFAULT
    if isinstance(raw, int):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = int(raw.strip())
        except ValueError:
            return _MAX_TOOL_INVOCATIONS_DEFAULT
    else:
        return _MAX_TOOL_INVOCATIONS_DEFAULT
    return min(64, max(1, parsed))


async def _estimate_chat_tool_budget_micro(
    db: AsyncSession,
    chat_params: ChatParamsIn | None,
    *,
    chat_tool_budget_setting_fn: AsyncCallable = chat_tool_budget_setting_micro,
    chat_max_tool_invocations_fn: AsyncCallable = chat_max_tool_invocations,
) -> tuple[int, dict[str, int]]:
    budget_by_tool: dict[str, int] = {}
    max_tool_invocations = int(await chat_max_tool_invocations_fn(db))
    for tool_name in _enabled_chat_tools(chat_params):
        amount = int(await chat_tool_budget_setting_fn(db, tool_name))
        if amount > 0:
            budget_by_tool[tool_name] = amount * max_tool_invocations
    return sum(budget_by_tool.values()), budget_by_tool


async def billing_image_thresholds(db: AsyncSession) -> dict[str, int]:
    return billing_core.parse_thresholds(
        await billing_setting_raw(db, "billing.image_size_thresholds")
    )


def _billing_http_error(exc: billing_core.BillingError) -> HTTPException:
    return _http(exc.code, exc.message, exc.status_code)


async def ensure_chat_wallet_preflight(
    db: AsyncSession,
    *,
    user_id: str,
    user_email: str | None,
    account_mode: str,
    model: str,
    chat_params: ChatParamsIn | None = None,
    billing_enabled_fn: AsyncCallable = billing_enabled,
    billing_allow_negative_fn: AsyncCallable = billing_allow_negative,
    user_rate_multiplier_fn: AsyncCallable = user_rate_multiplier_x10000,
    chat_tool_budget_setting_fn: AsyncCallable = chat_tool_budget_setting_micro,
    chat_max_tool_invocations_fn: AsyncCallable = chat_max_tool_invocations,
) -> ChatWalletPreflight | None:
    _ = user_email
    if account_mode != "wallet" or not await billing_enabled_fn(db):
        return None
    wallet = await billing_core.get_wallet(db, user_id, lock=True)
    if wallet is None:
        raise _http("WALLET_UNAVAILABLE", "wallet could not be initialized", 503)
    rate_multiplier_x10000 = int(await user_rate_multiplier_fn(db, user_id))
    if rate_multiplier_x10000 > 0 and wallet.balance_micro < 10_000:
        raise _http(
            "INSUFFICIENT_BALANCE",
            "insufficient wallet balance",
            402,
            required_micro=10_000,
            balance_micro=int(wallet.balance_micro),
        )
    try:
        pricing_snapshot = await billing_core.completion_pricing_snapshot(
            db,
            model=model,
        )
        cost_preview = billing_core.completion_breakdown_from_snapshot(
            pricing_snapshot,
            model=model,
            tokens=billing_core.UsageTokens(input_tokens=1, output_tokens=1),
            rate_multiplier_x10000=rate_multiplier_x10000,
        ).actual_cost_micro
    except billing_core.BillingError as exc:
        raise _billing_http_error(exc) from exc
    if cost_preview <= 0 and rate_multiplier_x10000 > 0:
        raise _billing_http_error(
            billing_core.BillingError(
                "PRICING_MISSING",
                f"missing enabled chat pricing rule for {model}",
                503,
            )
        )
    tool_budget_micro, budget_by_tool = await _estimate_chat_tool_budget_micro(
        db,
        chat_params,
        chat_tool_budget_setting_fn=chat_tool_budget_setting_fn,
        chat_max_tool_invocations_fn=chat_max_tool_invocations_fn,
    )
    budget_by_tool = {
        tool_name: apply_rate_multiplier_micro(amount, rate_multiplier_x10000)
        for tool_name, amount in budget_by_tool.items()
    }
    tool_budget_micro = sum(budget_by_tool.values())
    preauth_micro = (
        0
        if rate_multiplier_x10000 == 0
        else max(10_000, int(cost_preview or 0) + tool_budget_micro)
    )
    if wallet.balance_micro < preauth_micro and not await billing_allow_negative_fn(db):
        raise _http(
            "INSUFFICIENT_BALANCE",
            "insufficient wallet balance",
            402,
            required_micro=preauth_micro,
            balance_micro=int(wallet.balance_micro),
            estimated_model_micro=int(cost_preview or 0),
            tool_budget_micro=tool_budget_micro,
        )
    return ChatWalletPreflight(
        estimated_model_micro=int(cost_preview or 0),
        tool_budget_micro=tool_budget_micro,
        preauth_micro=preauth_micro,
        tool_budget_by_tool=budget_by_tool,
        pricing_snapshot=pricing_snapshot,
        rate_multiplier_x10000=rate_multiplier_x10000,
    )


async def resolve_fast_default(
    db: AsyncSession,
    *,
    get_spec_fn: Callable[[str], Any] = get_spec,
    get_setting_fn: AsyncCallable = get_setting,
) -> bool:
    spec = get_spec_fn(GENERATION_FAST_DEFAULT_KEY)
    if spec is None:
        return True
    raw = await get_setting_fn(db, spec)
    if raw in {"0", "1"}:
        return raw == "1"
    return True


def image_params_with_fast_default(
    image_params: ImageParamsIn,
    fast_default: bool,
) -> ImageParamsIn:
    if image_params.fast is not None:
        return image_params
    return image_params.model_copy(update={"fast": fast_default})


def chat_params_with_fast_default(
    chat_params: ChatParamsIn,
    fast_default: bool,
) -> ChatParamsIn:
    if chat_params.fast is not None:
        return chat_params
    return chat_params.model_copy(update={"fast": fast_default})


def wants_transparent_background(prompt: str | None) -> bool:
    if not prompt:
        return False
    if _TRANSPARENT_BACKGROUND_NEGATIVE_RE.search(prompt):
        return False
    if _TRANSPARENT_BACKGROUND_NEGATIVE_CONTEXT_RE.search(prompt):
        return False
    return bool(_TRANSPARENT_BACKGROUND_RE.search(prompt))


def _resolve_image_background(
    image_params: ImageParamsIn,
    prompt: str | None,
) -> str:
    background = (
        image_params.background
        if image_params.background in _IMAGE_BACKGROUND_VALUES
        else "auto"
    )
    if background == "auto" and wants_transparent_background(prompt):
        return "transparent"
    return background


def _transparent_background_prompt_suffix() -> str:
    return (
        "\n\nRender the subject as a clean cutout on a true transparent alpha "
        "background. Do not paint a white, gray, checkerboard, wall, floor, or "
        "studio backdrop."
    )


def image_upstream_request(
    image_params: ImageParamsIn,
    resolved_size: ResolvedSize,
    *,
    prompt: str | None = None,
    default_output_format: str = DEFAULT_IMAGE_OUTPUT_FORMAT,
) -> dict[str, Any]:
    render_quality = resolve_image_render_quality(image_params, resolved_size)
    background = _resolve_image_background(image_params, prompt)
    output_format_is_explicit = image_params.output_format in IMAGE_OUTPUT_FORMAT_VALUES
    output_format = (
        image_params.output_format
        if output_format_is_explicit
        else default_output_format
        if default_output_format in IMAGE_OUTPUT_FORMAT_VALUES
        else DEFAULT_IMAGE_OUTPUT_FORMAT
    )
    output_format_source = "request" if output_format_is_explicit else "system_default"
    if background == "transparent":
        output_format = "png"
        output_format_source = "transparent_background"
    upstream_request: dict[str, Any] = {
        "fast": bool(image_params.fast),
        "responses_model": (
            DEFAULT_IMAGE_RESPONSES_MODEL_FAST
            if image_params.fast
            else DEFAULT_IMAGE_RESPONSES_MODEL
        ),
        "render_quality": render_quality,
        "output_format": output_format,
        "output_format_source": output_format_source,
        "background": background,
        "moderation": (
            image_params.moderation
            if image_params.moderation in _IMAGE_MODERATION_VALUES
            else "low"
        ),
    }
    billing_tier = requested_image_billing_tier(image_params)
    if billing_tier is not None:
        upstream_request["billing_tier"] = billing_tier
        upstream_request["billing_tier_source"] = "request_quality"
    if (
        output_format in {"jpeg", "webp"}
        and image_params.output_compression is not None
    ):
        upstream_request["output_compression"] = image_params.output_compression
    return upstream_request


def _chat_param_vector_store_ids(chat_params: ChatParamsIn) -> list[str]:
    vector_store_ids: list[str] = []
    seen: set[str] = set()
    for raw in chat_params.vector_store_ids:
        value = raw.strip()
        if not value:
            continue
        if not _VECTOR_STORE_ID_RE.fullmatch(value):
            raise _http(
                "invalid_vector_store_id",
                "invalid vector_store_ids entry",
                422,
            )
        if value not in seen:
            seen.add(value)
            vector_store_ids.append(value)
    return vector_store_ids


def _chat_upstream_request(chat_params: ChatParamsIn) -> dict[str, Any] | None:
    req: dict[str, Any] = {}
    if chat_params.web_search:
        req["web_search"] = True
    if chat_params.file_search:
        vector_store_ids = _chat_param_vector_store_ids(chat_params)
        req["file_search"] = True
        if vector_store_ids:
            req["vector_store_ids"] = vector_store_ids
    if chat_params.code_interpreter:
        req["code_interpreter"] = True
    if chat_params.image_generation:
        req["image_generation"] = True
    return req or None


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _default_attachment_role(intent: Intent) -> str:
    return "ask_target" if intent == Intent.VISION_QA else "reference"


def _message_attachment_roles(
    body: PostMessageIn,
    *,
    attachment_ids: list[str],
    intent: Intent,
) -> list[dict[str, Any]]:
    roles: list[dict[str, Any]] = []
    items: list[MessageAttachmentIn | str]
    if body.attachments:
        items = list(body.attachments)
    else:
        items = list(attachment_ids)
    default_role = _default_attachment_role(intent)
    for item in items:
        if isinstance(item, str):
            roles.append({"image_id": item, "role": default_role})
            continue
        role: dict[str, Any] = {
            "image_id": item.image_id,
            "role": item.role,
        }
        if item.label:
            role["label"] = item.label
        if item.weight is not None:
            role["weight"] = item.weight
        roles.append(role)
    return roles


def message_request_metadata(
    body: PostMessageIn,
    *,
    attachment_ids: list[str],
    mask_image_id: str | None,
    intent: Intent,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    source = _clean_optional_text(body.source)
    action_source = _clean_optional_text(body.action_source)
    trace_id = _clean_optional_text(body.trace_id)
    attachment_roles = _message_attachment_roles(
        body,
        attachment_ids=attachment_ids,
        intent=intent,
    )
    if source:
        metadata["source"] = source
    if action_source:
        metadata["action_source"] = action_source
    if trace_id:
        metadata["trace_id"] = trace_id
    if attachment_roles:
        metadata["attachment_roles"] = attachment_roles
    if attachment_ids:
        metadata["input_images"] = [dict(item) for item in attachment_roles]
        metadata["primary_input_image_id"] = attachment_ids[0]
        metadata["source_image_id"] = attachment_ids[0]
    if mask_image_id:
        metadata["mask_image_id"] = mask_image_id
        input_images = list(metadata.get("input_images") or [])
        input_images.append({"image_id": mask_image_id, "role": "mask"})
        metadata["input_images"] = input_images
    return metadata


def _merge_request_metadata(
    upstream_request: dict[str, Any] | None,
    request_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(upstream_request or {})
    for key, value in (request_metadata or {}).items():
        out.setdefault(key, value)
    return out


def _task_payload_context(
    upstream_request: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(upstream_request, dict):
        return {}
    context: dict[str, Any] = {}
    for key in ("trace_id", "source", "action_source"):
        value = upstream_request.get(key)
        if isinstance(value, str) and value:
            context[key] = value
    input_images = upstream_request.get("input_images")
    if isinstance(input_images, list):
        context["input_images"] = input_images
    return context


def _image_queue_metadata(
    image_params: ImageParamsIn,
    resolved_size: ResolvedSize,
    *,
    action: str | None,
    mask_image_id: str | None,
    size_px: int,
    billing_tier: str | None,
) -> dict[str, Any]:
    _ = image_params, billing_tier
    safe_action = action or (
        GenerationAction.EDIT.value
        if mask_image_id
        else GenerationAction.GENERATE.value
    )
    return generation_queue_metadata(
        upstream_request=None,
        action=safe_action,
        size_requested=resolved_size.size,
        mask_image_id=mask_image_id,
        upstream_pixels=size_px,
    )


async def ensure_file_search_configured(
    db: AsyncSession,
    chat_params: ChatParamsIn,
    *,
    get_spec_fn: Callable[[str], Any] = get_spec,
    get_setting_fn: AsyncCallable = get_setting,
) -> None:
    if not chat_params.file_search:
        return
    if _chat_param_vector_store_ids(chat_params):
        return
    spec = get_spec_fn("chat.file_search_vector_store_ids")
    raw = await get_setting_fn(db, spec) if spec is not None else None
    if raw and any(part.strip() for part in raw.split(",")):
        return
    raise _http(
        "FILE_SEARCH_NOT_CONFIGURED",
        "file_search requires vector_store_ids or a configured default vector store",
        400,
    )


def _non_blank(text: str | None) -> str | None:
    if text is None:
        return None
    return text if text.strip() else None


def _sanitize_system_prompt_source(text: str | None) -> str | None:
    prompt = _non_blank(text)
    if prompt is None:
        return None
    normalized = unicodedata.normalize("NFKC", prompt)
    cleaned = normalized.translate(_PROMPT_CONTROL_TRANSLATION).strip()
    if not cleaned:
        return None
    if len(cleaned) > SYSTEM_PROMPT_SOURCE_LIMIT:
        logger.warning(
            "system prompt source truncated: original_len=%d limit=%d",
            len(cleaned),
            SYSTEM_PROMPT_SOURCE_LIMIT,
        )
        cleaned = cleaned[:SYSTEM_PROMPT_SOURCE_LIMIT]
    return cleaned


def _escape_system_prompt_section_body(text: str) -> str:
    return _SYSTEM_PROMPT_SECTION_TAG_RE.sub(
        lambda match: (
            f"{match.group(1)}{_SYSTEM_PROMPT_SECTION_TAG_ESCAPE}"
            f"{match.group(2)}{match.group(3)}"
        ),
        text,
    )


def build_structured_system_prompt(
    *,
    explicit_prompt: str | None,
    conversation_prompt: str | None,
    legacy_conversation_prompt: str | None,
    global_prompt: str | None,
) -> str | None:
    sections: list[str] = []
    for tag, candidate in (
        ("SYSTEM_GLOBAL", global_prompt),
        ("SYSTEM_CONVERSATION_LEGACY", legacy_conversation_prompt),
        ("SYSTEM_CONVERSATION", conversation_prompt),
        ("SYSTEM_EXPLICIT", explicit_prompt),
    ):
        prompt = _sanitize_system_prompt_source(candidate)
        if prompt is not None:
            safe_prompt = _escape_system_prompt_section_body(prompt)
            sections.append(f"[{tag}]\n{safe_prompt}\n[/{tag}]")
    if not sections:
        return None
    return "\n".join(("[SYSTEM_PROMPTS]", *sections, "[/SYSTEM_PROMPTS]"))


async def _load_owned_prompt_content(
    db: AsyncSession,
    *,
    user_id: str,
    prompt_id: str | None,
) -> str | None:
    if not prompt_id:
        return None
    return (
        await db.execute(
            select(SystemPrompt.content).where(
                SystemPrompt.id == prompt_id,
                SystemPrompt.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def resolve_system_prompt_for_message(
    db: AsyncSession,
    *,
    user_id: str,
    default_system_prompt_id: str | None,
    conv: Conversation,
    explicit_prompt: str | None,
) -> str | None:
    conversation_prompt = await _load_owned_prompt_content(
        db,
        user_id=user_id,
        prompt_id=conv.default_system_prompt_id,
    )
    global_prompt = await _load_owned_prompt_content(
        db,
        user_id=user_id,
        prompt_id=default_system_prompt_id,
    )
    return build_structured_system_prompt(
        explicit_prompt=explicit_prompt,
        conversation_prompt=conversation_prompt,
        legacy_conversation_prompt=conv.default_system,
        global_prompt=global_prompt,
    )


async def resolve_task_credential_pin(
    db: AsyncSession,
    user_id: str,
    required_purpose: str,
    account_mode: str,
    *,
    read_byok_settings_cached_fn: AsyncCallable = read_byok_settings_cached,
) -> TaskCredentialPin | None:
    if account_mode != "byok":
        return None
    byok_settings = await read_byok_settings_cached_fn(db)
    if not byok_settings.mode_enabled:
        raise _http("byok_disabled", "BYOK is disabled", 403)
    active_row = (
        await db.execute(
            select(UserApiCredential, ApiSupplierTemplate)
            .join(
                ApiSupplierTemplate,
                ApiSupplierTemplate.id == UserApiCredential.supplier_id,
            )
            .where(
                UserApiCredential.user_id == user_id,
                UserApiCredential.status == "active",
                UserApiCredential.deleted_at.is_(None),
                ApiSupplierTemplate.deleted_at.is_(None),
                ApiSupplierTemplate.enabled.is_(True),
            )
            .order_by(UserApiCredential.created_at.desc())
            .limit(1)
        )
    ).first()
    if active_row is not None:
        active, supplier = active_row
        rate_limited_until = getattr(active, "rate_limited_until", None)
        if rate_limited_until is not None:
            if rate_limited_until.tzinfo is None:
                rate_limited_until = rate_limited_until.replace(tzinfo=timezone.utc)
            if rate_limited_until > datetime.now(timezone.utc):
                raise _http(
                    "NO_ACTIVE_API_KEY",
                    "your API key is currently rate limited",
                    412,
                )
        if required_purpose not in set(supplier.purposes or []):
            raise _http(
                "NO_ACTIVE_API_KEY",
                "your current API Key does not support this task type",
                412,
            )
        return TaskCredentialPin(
            credential_id=active.id,
            supplier_id=active.supplier_id,
            default_chat_model=supplier.default_chat_model or DEFAULT_CHAT_MODEL,
            fast_chat_model=supplier.fast_chat_model,
            default_image_model=getattr(supplier, "default_image_model", None),
        )
    raise _http(
        "NO_ACTIVE_API_KEY",
        "please upload an active API key before starting new tasks",
        412,
    )


def _select_chat_task_model(
    credential_pin: TaskCredentialPin | None,
    chat_params: ChatParamsIn,
) -> str:
    return (
        credential_pin.fast_chat_model
        if credential_pin and chat_params.fast and credential_pin.fast_chat_model
        else credential_pin.default_chat_model
        if credential_pin
        else DEFAULT_CHAT_MODEL
    )


async def _create_completion_task(
    *,
    db: AsyncSession,
    user_id: str,
    user_email: str | None,
    account_mode: str,
    assistant_msg: Message,
    intent: Intent,
    stored_key: str,
    attachment_ids: list[str],
    chat_params: ChatParamsIn,
    system_prompt: str | None,
    request_metadata: dict[str, Any] | None,
    credential_pin: TaskCredentialPin | None,
    chat_wallet_preflight_done: bool,
    chat_wallet_preflight: ChatWalletPreflight | None,
    ensure_chat_wallet_preflight_fn: AsyncCallable,
    billing_allow_negative_fn: AsyncCallable,
    write_audit_fn: AsyncCallable,
) -> tuple[str, list[dict[str, Any]]]:
    task_chat_model = _select_chat_task_model(credential_pin, chat_params)
    if not chat_wallet_preflight_done:
        chat_wallet_preflight = await ensure_chat_wallet_preflight_fn(
            db,
            user_id=user_id,
            user_email=user_email,
            account_mode=account_mode,
            model=task_chat_model,
            chat_params=chat_params,
        )
    comp_upstream_request = _merge_request_metadata(
        _chat_upstream_request(chat_params),
        request_metadata,
    )
    if chat_wallet_preflight is not None:
        comp_upstream_request.update(chat_wallet_preflight.upstream_metadata())
    comp = Completion(
        message_id=assistant_msg.id,
        user_id=user_id,
        model=task_chat_model,
        input_image_ids=attachment_ids if intent == Intent.VISION_QA else [],
        system_prompt=system_prompt,
        text="",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        attempt=0,
        idempotency_key=stored_key,
        upstream_request=comp_upstream_request or None,
        user_api_credential_id=(
            credential_pin.credential_id if credential_pin else None
        ),
        upstream_supplier_id=credential_pin.supplier_id if credential_pin else None,
    )
    db.add(comp)
    await db.flush()
    if chat_wallet_preflight is not None and chat_wallet_preflight.preauth_micro > 0:
        try:
            tx = await billing_core.hold(
                db,
                user_id,
                chat_wallet_preflight.preauth_micro,
                ref_type="completion",
                ref_id=comp.id,
                idempotency_key=f"hold:{comp.id}",
                allow_negative=await billing_allow_negative_fn(db),
                meta=chat_wallet_preflight.hold_metadata(),
            )
        except billing_core.BillingError as exc:
            raise _billing_http_error(exc) from exc
        if tx is not None:
            await write_audit_fn(
                db,
                event_type="wallet.hold.chat",
                user_id=user_id,
                details={
                    "completion_id": comp.id,
                    "amount_micro": chat_wallet_preflight.preauth_micro,
                    **chat_wallet_preflight.audit_metadata(),
                    "balance_after": tx.balance_after,
                    "hold_after": tx.hold_after,
                },
                autocommit=False,
            )
    completion_payload: dict[str, Any] = {
        "task_id": comp.id,
        "user_id": user_id,
        "kind": "completion",
    }
    completion_payload.update(_task_payload_context(comp_upstream_request))
    return comp.id, [completion_payload]


async def _create_generation_tasks(
    *,
    db: AsyncSession,
    user_id: str,
    account_mode: str,
    assistant_msg: Message,
    intent: Intent,
    stored_key: str,
    attachment_ids: list[str],
    image_params: ImageParamsIn,
    text: str,
    resolved_size: ResolvedSize,
    prompt_suffix: str,
    default_image_output_format: str,
    mask_image_id: str | None,
    credential_pin: TaskCredentialPin | None,
    request_metadata: dict[str, Any] | None,
    billing_enabled_fn: AsyncCallable,
    billing_allow_negative_fn: AsyncCallable,
    billing_image_thresholds_fn: AsyncCallable,
    user_rate_multiplier_fn: AsyncCallable,
    apply_rate_multiplier_fn: Callable[[int, int], int],
    requested_image_billing_tier_fn: Callable[[ImageParamsIn], str | None],
    write_audit_fn: AsyncCallable,
) -> tuple[list[str], list[dict[str, Any]]]:
    requested_count = max(1, min(10, image_params.count))
    action = (
        GenerationAction.EDIT.value
        if intent == Intent.IMAGE_TO_IMAGE
        else GenerationAction.GENERATE.value
    )
    primary = attachment_ids[0] if attachment_ids else None
    prompt_full = (text or "") + prompt_suffix
    upstream_request = image_upstream_request(
        image_params,
        resolved_size,
        prompt=prompt_full,
        default_output_format=default_image_output_format,
    )
    billing_is_enabled = account_mode == "wallet" and await billing_enabled_fn(db)
    billing_thresholds = (
        await billing_image_thresholds_fn(db) if billing_is_enabled else {}
    )
    size_px = (
        (resolved_size.width or 0) * (resolved_size.height or 0)
        if resolved_size.width and resolved_size.height
        else billing_core.DEFAULT_IMAGE_SIZE_THRESHOLDS["1k"]
    )
    billing_tier = requested_image_billing_tier_fn(image_params)
    base_upstream_request = _merge_request_metadata(
        upstream_request,
        request_metadata,
    )
    base_upstream_request.update(
        _image_queue_metadata(
            image_params,
            resolved_size,
            action=action,
            mask_image_id=mask_image_id,
            size_px=size_px,
            billing_tier=billing_tier,
        )
    )
    if not billing_is_enabled:
        estimated_micro, estimated_tier = (0, "free")
        base_estimated_micro = 0
    elif billing_tier is not None:
        (
            base_estimated_micro,
            estimated_tier,
        ) = await billing_core.estimate_image_cost_for_tier(
            db,
            tier=billing_tier,
            n=1,
        )
    else:
        base_estimated_micro, estimated_tier = await billing_core.estimate_image_cost(
            db,
            size_px=size_px,
            n=1,
            thresholds=billing_thresholds or None,
        )
    if billing_is_enabled:
        rate_multiplier_x10000 = int(await user_rate_multiplier_fn(db, user_id))
        estimated_micro = apply_rate_multiplier_fn(
            base_estimated_micro,
            rate_multiplier_x10000,
        )
        base_upstream_request["billing_pricing_snapshot"] = {
            "kind": "image",
            "tier": estimated_tier,
            "unit_price_micro": int(base_estimated_micro),
            "captured_size_px": int(size_px),
        }
        base_upstream_request["billing_rate_multiplier_x10000"] = rate_multiplier_x10000
    if credential_pin:
        base_upstream_request["responses_model"] = (
            credential_pin.default_image_model or credential_pin.default_chat_model
        )
    if base_upstream_request.get("background") == "transparent":
        prompt_full += _transparent_background_prompt_suffix()

    request_trace_id = base_upstream_request.get("trace_id")
    allow_negative_billing = (
        await billing_allow_negative_fn(db)
        if billing_is_enabled and estimated_micro > 0
        else False
    )
    generation_ids: list[str] = []
    outbox_payloads: list[dict[str, Any]] = []
    for image_index in range(1, requested_count + 1):
        gen_upstream_request = dict(base_upstream_request)
        gen_upstream_request["n"] = 1
        if requested_count > 1:
            gen_upstream_request["batch_task_index"] = image_index
            gen_upstream_request["batch_task_count"] = requested_count
            gen_upstream_request["requested_image_count"] = requested_count
            if isinstance(request_trace_id, str) and request_trace_id:
                gen_upstream_request["request_trace_id"] = request_trace_id
            gen_upstream_request["trace_id"] = f"gen_{new_uuid7()}"
        else:
            gen_upstream_request.setdefault("trace_id", f"gen_{new_uuid7()}")
        gen = Generation(
            message_id=assistant_msg.id,
            user_id=user_id,
            action=action,
            prompt=prompt_full,
            size_requested=resolved_size.size,
            aspect_ratio=image_params.aspect_ratio,
            input_image_ids=attachment_ids,
            primary_input_image_id=primary,
            mask_image_id=(mask_image_id if intent == Intent.IMAGE_TO_IMAGE else None),
            status=GenerationStatus.QUEUED.value,
            progress_stage=GenerationStage.QUEUED.value,
            attempt=0,
            idempotency_key=generation_child_idempotency_key(
                stored_key,
                image_index,
            ),
            upstream_request=gen_upstream_request,
            user_api_credential_id=(
                credential_pin.credential_id if credential_pin else None
            ),
            upstream_supplier_id=(
                credential_pin.supplier_id if credential_pin else None
            ),
        )
        db.add(gen)
        await db.flush()
        if billing_is_enabled and estimated_micro > 0:
            try:
                tx = await billing_core.hold(
                    db,
                    user_id,
                    estimated_micro,
                    ref_type="generation",
                    ref_id=gen.id,
                    idempotency_key=f"hold:{gen.id}",
                    allow_negative=allow_negative_billing,
                    meta={
                        "tier": estimated_tier,
                        "size_requested": resolved_size.size,
                        "pixels_estimated": size_px,
                        "image_count": 1,
                        "batch_task_index": image_index,
                        "batch_task_count": requested_count,
                        "pricing_snapshot": gen_upstream_request.get(
                            "billing_pricing_snapshot"
                        ),
                    },
                )
            except billing_core.BillingError as exc:
                raise _billing_http_error(exc) from exc
            if tx is not None:
                await write_audit_fn(
                    db,
                    event_type="wallet.hold.image",
                    user_id=user_id,
                    details={
                        "generation_id": gen.id,
                        "amount_micro": estimated_micro,
                        "tier": estimated_tier,
                        "image_count": 1,
                        "batch_task_index": image_index,
                        "batch_task_count": requested_count,
                        "balance_after": tx.balance_after,
                        "hold_after": tx.hold_after,
                    },
                    autocommit=False,
                )
        generation_ids.append(gen.id)
        generation_payload: dict[str, Any] = {
            "task_id": gen.id,
            "user_id": user_id,
            "kind": "generation",
        }
        defer_s = image_multi_generation_defer_s(image_index)
        if defer_s > 0:
            generation_payload["defer_s"] = defer_s
        generation_payload.update(_task_payload_context(gen_upstream_request))
        outbox_payloads.append(generation_payload)
    return generation_ids, outbox_payloads


async def _create_outbox_rows(
    db: AsyncSession,
    outbox_payloads: list[dict[str, Any]],
) -> list[OutboxEvent]:
    outbox_rows: list[OutboxEvent] = []
    for payload in outbox_payloads:
        row = OutboxEvent(
            kind=payload["kind"],
            payload=payload,
            published_at=None,
        )
        db.add(row)
        outbox_rows.append(row)
    if outbox_rows:
        await db.flush()
        for payload, row in zip(outbox_payloads, outbox_rows, strict=False):
            payload["outbox_id"] = str(row.id)
            row.payload = dict(payload)
    return outbox_rows


async def create_assistant_task(
    *,
    db: AsyncSession,
    user_id: str,
    account_mode: str,
    conv: Conversation,
    user_msg: Message,
    intent: Intent,
    idempotency_key: str,
    image_params: ImageParamsIn,
    chat_params: ChatParamsIn,
    system_prompt: str | None,
    attachment_ids: list[str],
    text: str,
    user_email: str | None = None,
    default_image_output_format: str = DEFAULT_IMAGE_OUTPUT_FORMAT,
    mask_image_id: str | None = None,
    credential_pin: TaskCredentialPin | None = None,
    credential_pin_resolved: bool = False,
    chat_wallet_preflight_done: bool = False,
    chat_wallet_preflight: ChatWalletPreflight | None = None,
    request_metadata: dict[str, Any] | None = None,
    resolve_task_credential_pin_fn: AsyncCallable = resolve_task_credential_pin,
    ensure_chat_wallet_preflight_fn: AsyncCallable = ensure_chat_wallet_preflight,
    billing_enabled_fn: AsyncCallable = billing_enabled,
    billing_allow_negative_fn: AsyncCallable = billing_allow_negative,
    billing_image_thresholds_fn: AsyncCallable = billing_image_thresholds,
    user_rate_multiplier_fn: AsyncCallable = user_rate_multiplier_x10000,
    apply_rate_multiplier_fn: Callable[[int, int], int] = apply_rate_multiplier_micro,
    requested_image_billing_tier_fn: Callable[
        [ImageParamsIn], str | None
    ] = requested_image_billing_tier,
    write_audit_fn: AsyncCallable = write_audit,
) -> AssistantTaskResult:
    """Build assistant/task/outbox rows inside the caller's transaction."""
    produces_image = intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE)
    if intent == Intent.IMAGE_TO_IMAGE and not attachment_ids:
        raise _http(
            "missing_reference_image",
            "image_to_image requires at least one reference image",
            400,
        )

    resolved_size: ResolvedSize | None = None
    prompt_suffix = ""
    if produces_image:
        try:
            resolved_size = resolve_size(
                aspect=image_params.aspect_ratio,
                mode=image_params.size_mode,
                fixed=image_params.fixed_size,
            )
            prompt_suffix = resolved_size.prompt_suffix
        except Exception as exc:  # noqa: BLE001
            raise _http("invalid_size", f"size resolve failed: {exc}", 422) from exc

    if not credential_pin_resolved:
        credential_pin = await resolve_task_credential_pin_fn(
            db,
            user_id,
            "image" if produces_image else "chat",
            account_mode,
        )

    assistant_msg = Message(
        conversation_id=conv.id,
        role=Role.ASSISTANT.value,
        content={},
        parent_message_id=user_msg.id,
        intent=intent.value,
        status=MessageStatus.PENDING.value,
    )
    db.add(assistant_msg)
    await db.flush()

    stored_key = stored_idempotency_key(conv.id, idempotency_key)
    completion_id: str | None = None
    generation_ids: list[str] = []
    if intent in (Intent.CHAT, Intent.VISION_QA):
        completion_id, outbox_payloads = await _create_completion_task(
            db=db,
            user_id=user_id,
            user_email=user_email,
            account_mode=account_mode,
            assistant_msg=assistant_msg,
            intent=intent,
            stored_key=stored_key,
            attachment_ids=attachment_ids,
            chat_params=chat_params,
            system_prompt=system_prompt,
            request_metadata=request_metadata,
            credential_pin=credential_pin,
            chat_wallet_preflight_done=chat_wallet_preflight_done,
            chat_wallet_preflight=chat_wallet_preflight,
            ensure_chat_wallet_preflight_fn=ensure_chat_wallet_preflight_fn,
            billing_allow_negative_fn=billing_allow_negative_fn,
            write_audit_fn=write_audit_fn,
        )
    else:
        assert resolved_size is not None
        generation_ids, outbox_payloads = await _create_generation_tasks(
            db=db,
            user_id=user_id,
            account_mode=account_mode,
            assistant_msg=assistant_msg,
            intent=intent,
            stored_key=stored_key,
            attachment_ids=attachment_ids,
            image_params=image_params,
            text=text,
            resolved_size=resolved_size,
            prompt_suffix=prompt_suffix,
            default_image_output_format=default_image_output_format,
            mask_image_id=mask_image_id,
            credential_pin=credential_pin,
            request_metadata=request_metadata,
            billing_enabled_fn=billing_enabled_fn,
            billing_allow_negative_fn=billing_allow_negative_fn,
            billing_image_thresholds_fn=billing_image_thresholds_fn,
            user_rate_multiplier_fn=user_rate_multiplier_fn,
            apply_rate_multiplier_fn=apply_rate_multiplier_fn,
            requested_image_billing_tier_fn=requested_image_billing_tier_fn,
            write_audit_fn=write_audit_fn,
        )
    outbox_rows = await _create_outbox_rows(db, outbox_payloads)
    return AssistantTaskResult(
        assistant_msg=assistant_msg,
        completion_id=completion_id,
        generation_ids=generation_ids,
        outbox_payloads=outbox_payloads,
        outbox_rows=outbox_rows,
    )


async def publish_message_appended(
    *,
    redis: Any,
    user_id: str,
    conv_id: str,
    message_ids: list[str],
    publish_sse_event_fn: AsyncCallable = publish_sse_event,
    publish_sse_events_fn: AsyncCallable = publish_sse_events,
    log: logging.Logger = logger,
) -> None:
    """Best-effort publish for cross-device message list synchronization."""
    if not message_ids:
        return
    try:
        if len(message_ids) == 1:
            message_id = message_ids[0]
            await publish_sse_event_fn(
                redis,
                user_id=user_id,
                channel=conv_channel(conv_id),
                event_name=EV_CONV_MSG_APPENDED,
                data={
                    "conversation_id": conv_id,
                    "message_id": message_id,
                },
            )
        else:
            await publish_sse_events_fn(
                redis,
                [
                    {
                        "user_id": user_id,
                        "channel": conv_channel(conv_id),
                        "event_name": EV_CONV_MSG_APPENDED,
                        "data": {
                            "conversation_id": conv_id,
                            "message_id": message_id,
                        },
                    }
                    for message_id in message_ids
                ],
            )
    except Exception:
        log.warning(
            "publish_message_appended failed user=%s conv=%s messages=%s",
            user_id,
            conv_id,
            message_ids,
            exc_info=True,
        )


async def publish_assistant_task(
    *,
    db: AsyncSession,
    redis: Any,
    user_id: str,
    conv_id: str,
    assistant_msg_id: str,
    outbox_payloads: list[dict[str, Any]],
    outbox_rows: list[OutboxEvent],
    get_arq_pool_fn: AsyncCallable = get_arq_pool,
    publish_sse_event_fn: AsyncCallable = publish_sse_event,
    log: logging.Logger = logger,
) -> None:
    """Best-effort enqueue and publish after the caller commits."""
    try:
        pool = await get_arq_pool_fn()
        for payload in outbox_payloads:
            fn_name = (
                "run_completion"
                if payload["kind"] == "completion"
                else "run_generation"
            )
            enqueue_kwargs: dict[str, Any] = {}
            defer_s = payload.get("defer_s")
            if isinstance(defer_s, (int, float)) and defer_s > 0:
                enqueue_kwargs["_defer_by"] = float(defer_s)
            enqueue_kwargs["_job_id"] = arq_job_id(
                payload["kind"],
                payload["task_id"],
                payload.get("outbox_id"),
            )
            await pool.enqueue_job(
                fn_name,
                payload["task_id"],
                **enqueue_kwargs,
            )
            ev_name = (
                EV_COMP_QUEUED if payload["kind"] == "completion" else EV_GEN_QUEUED
            )
            id_field = (
                "completion_id" if payload["kind"] == "completion" else "generation_id"
            )
            event_data: dict[str, Any] = {
                id_field: payload["task_id"],
                "message_id": assistant_msg_id,
                "conversation_id": conv_id,
                "kind": payload["kind"],
            }
            for key in ("trace_id", "source", "action_source"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    event_data[key] = value
            input_images = payload.get("input_images")
            if isinstance(input_images, list):
                event_data["input_images"] = input_images
            await publish_sse_event_fn(
                redis,
                user_id=user_id,
                channel=task_channel(payload["task_id"]),
                event_name=ev_name,
                data=event_data,
            )
    except Exception:
        log.warning(
            "publish_assistant_task failed user=%s conv=%s msg=%s",
            user_id,
            conv_id,
            assistant_msg_id,
            exc_info=True,
        )
        return

    if outbox_rows:
        try:
            published_at = datetime.now(timezone.utc)
            for row in outbox_rows:
                row.published_at = published_at
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                log.warning(
                    "outbox row rollback failed user=%s msg=%s",
                    user_id,
                    assistant_msg_id,
                    exc_info=True,
                )
            log.warning(
                "outbox row mark-published failed user=%s msg=%s",
                user_id,
                assistant_msg_id,
                exc_info=True,
            )
