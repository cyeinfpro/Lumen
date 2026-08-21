"""接收提示词 → 调 API → 注册 tracker。

两条入口：
- GenFlow.awaiting_prompt：用户发完提示词文本后落点。
  - params.enhance=False → 直接 submit
  - params.enhance=True  → 调 enhance，进入 confirming_enhanced，让用户在「优化版/原文」
    之间选；选择后由下面的 callback_query handler 落点 submit。
- enh:* 回调：confirming_enhanced 状态下的二选一。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramForbiddenError, TelegramUnauthorizedError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..api_client import ApiError, LumenApi, make_idempotency_key
from ..generation_state import (
    DurableGenerationSubmission,
    SubmissionDisposition,
    SubmissionJournalStatus,
    ensure_generation_flow_epoch,
    generation_submission_idempotency_key,
    generation_flow_is_current,
    pending_generation,
    resolve_or_stage_generation,
)
from ..keyboards import DEFAULT_PARAMS, enhance_choice_keyboard, render_params_summary
from ..states import GenFlow
from ..tracker import TaskTrack, tracker
from ._helpers import (
    is_slash_command,
    message_prompt,
    require_message,
    telegram_user_id,
)

logger = logging.getLogger(__name__)
router = Router()
_PROMPT_ENHANCE_KEY_FIELD = "prompt_enhance_idempotency_key"
_PROMPT_ENHANCE_PENDING_FIELD = "prompt_enhance_pending"


@dataclass
class GenerationRuntime:
    """Process-owned state shared by generation handlers through dispatcher DI."""

    heartbeat_auth_logged: set[int] = field(default_factory=set)
    active_prompt_tasks: dict[int, asyncio.Task[None]] = field(
        default_factory=dict,
        repr=False,
    )
    cancelled_prompt_tasks: set[asyncio.Task[None]] = field(
        default_factory=set,
        repr=False,
    )
    prompt_task_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def begin_prompt(self, chat_id: int, task: asyncio.Task[None]) -> bool:
        async with self.prompt_task_lock:
            active = self.active_prompt_tasks.get(chat_id)
            if active is not None and active is not task and not active.done():
                return False
            self.active_prompt_tasks[chat_id] = task
            return True

    async def cancel_prompt(self, chat_id: int) -> None:
        current = asyncio.current_task()
        async with self.prompt_task_lock:
            task = self.active_prompt_tasks.pop(chat_id, None)
            if task is None or task is current or task.done():
                return
            self.cancelled_prompt_tasks.add(task)
            task.cancel()

    async def prompt_was_cancelled(self, task: asyncio.Task[None]) -> bool:
        async with self.prompt_task_lock:
            return task in self.cancelled_prompt_tasks

    async def finish_prompt(self, chat_id: int, task: asyncio.Task[None]) -> None:
        async with self.prompt_task_lock:
            if self.active_prompt_tasks.get(chat_id) is task:
                self.active_prompt_tasks.pop(chat_id, None)
            self.cancelled_prompt_tasks.discard(task)


@dataclass(frozen=True)
class _PromptFlow:
    message: Message
    state: FSMContext
    api: LumenApi
    runtime: GenerationRuntime
    task: asyncio.Task[None]
    flow_epoch: str
    actor_id: int
    update_token: str

    async def is_current(self) -> bool:
        return await _prompt_flow_is_current(
            self.state,
            self.runtime,
            self.message.chat.id,
            self.task,
            self.flow_epoch,
        )


async def _chat_action_heartbeat(
    message: Message,
    action: ChatAction,
    runtime: GenerationRuntime,
) -> None:
    bot = message.bot
    if bot is None:
        return
    while True:
        try:
            await bot.send_chat_action(message.chat.id, action)
        except (TelegramUnauthorizedError, TelegramForbiddenError) as exc:
            # 401/403：bot token 失效或被踢出 chat。整个进程内 per-chat 只 warn 一次。
            if message.chat.id not in runtime.heartbeat_auth_logged:
                runtime.heartbeat_auth_logged.add(message.chat.id)
                logger.warning(
                    "chat_action heartbeat auth failed chat=%s err=%r",
                    message.chat.id,
                    exc,
                )
        except Exception:  # noqa: BLE001
            # 其它异常按原行为吞掉（network blip / RetryAfter 等）
            pass
        await asyncio.sleep(4.0)


def _generation_payload(
    prompt: str,
    params: dict[str, object],
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "idempotency_key": idempotency_key,
        "prompt": prompt,
        "aspect_ratio": params["aspect_ratio"],
        "render_quality": params["render_quality"],
        "count": params["count"],
        "resolution": params["resolution"],
        "output_format": params["output_format"],
    }


def _generation_result(
    result: object,
) -> tuple[list[str], str] | None:
    if not isinstance(result, dict):
        return None
    raw_ids = result.get("generation_ids")
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or any(not isinstance(value, str) or not value.strip() for value in raw_ids)
    ):
        return None
    user_id = result.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    return [value.strip() for value in raw_ids], user_id.strip()


def _message_update_token(message: Message) -> str:
    return f"message:{message.message_id}"


def _callback_update_token(cb: CallbackQuery, *, choice: str, message_id: int) -> str:
    callback_id = str(getattr(cb, "id", "") or "").strip()
    if callback_id:
        return f"callback:{callback_id}"
    return f"callback-message:{message_id}:{choice}"


async def _prompt_flow_is_current(
    state: FSMContext,
    runtime: GenerationRuntime,
    chat_id: int,
    task: asyncio.Task[None],
    flow_epoch: str,
) -> bool:
    if await runtime.prompt_was_cancelled(task):
        return False
    return await generation_flow_is_current(state, flow_epoch)


async def _stage_durable_submission(
    state: FSMContext,
    *,
    chat_id: int,
    tg_user_id: int,
    update_token: str,
    payload: dict[str, Any],
    answer,
    expected_flow_epoch: str | None = None,
) -> DurableGenerationSubmission | None:
    if (
        expected_flow_epoch is not None
        and not await generation_flow_is_current(state, expected_flow_epoch)
    ):
        return None
    try:
        submission = await tracker.stage_generation_submission(
            chat_id=chat_id,
            tg_user_id=tg_user_id,
            update_token=update_token,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "durable generation journal unavailable chat=%s user=%s err=%r",
            chat_id,
            tg_user_id,
            exc,
        )
        await answer(
            "⚠️ 暂时无法安全记录本次付费提交，请稍后重试。"
            "为避免重复扣费，本次没有创建任务。"
        )
        return None
    if (
        expected_flow_epoch is not None
        and not await generation_flow_is_current(state, expected_flow_epoch)
    ):
        return None
    await state.update_data(pending_generation=dict(submission.payload))
    return submission


async def _finish_durable_submission(
    submission: DurableGenerationSubmission,
    disposition: SubmissionDisposition,
) -> None:
    status = {
        SubmissionDisposition.ACCEPTED: SubmissionJournalStatus.ACCEPTED,
        SubmissionDisposition.REJECTED: SubmissionJournalStatus.REJECTED,
        SubmissionDisposition.AMBIGUOUS: SubmissionJournalStatus.AMBIGUOUS,
    }[disposition]
    try:
        await tracker.finish_generation_submission(submission, status)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "durable generation journal update failed operation=%s status=%s err=%r",
            submission.operation_id,
            status.value,
            exc,
        )


async def _submit_generation(
    chat_id: int,
    tg_user_id: int,
    payload: dict[str, Any],
    api: LumenApi,
    answer,  # callable(text: str) -> Awaitable[Message]
) -> SubmissionDisposition:
    """把 (提示词, params) 提交到 API 并注册 tracker。

    count==1：一条状态消息，listener 走单图编辑流。
    count>1：一条 placeholder 罗列所有 #短ID，所有 gens 共享同一 status_message_id +
            batch_id；listener 不刷状态，终态事件 DECR batch 计数，归零才删除 placeholder。
    """
    try:
        result = await api.create_generation(
            chat_id,
            payload,
            tg_user_id=tg_user_id,
        )
    except ApiError as exc:
        if exc.outcome_unknown:
            await answer(
                "⚠️ 提交结果暂时无法确认。请再次发送同一提示词或点击同一按钮重试；"
                "系统会复用原请求。"
            )
            return SubmissionDisposition.AMBIGUOUS
        await answer(f"❌ 提交失败：{exc.message}")
        return SubmissionDisposition.REJECTED
    except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("generation submission connection lost: %r", exc)
        await answer(
            "⚠️ 连接中断，提交结果暂时无法确认。请再次发送同一提示词或点击同一按钮重试；"
            "系统会复用原请求。"
        )
        return SubmissionDisposition.AMBIGUOUS

    parsed = _generation_result(result)
    if parsed is None:
        logger.error(
            "generation response malformed; preserving retry state response=%r",
            result,
        )
        await answer(
            "⚠️ 服务端返回异常，提交结果暂时无法确认。请重试同一请求；系统会复用原请求。"
        )
        return SubmissionDisposition.AMBIGUOUS
    gen_ids, user_id = parsed

    prompt = str(payload.get("prompt") or "")
    params = {
        key: value
        for key, value in payload.items()
        if key not in {"idempotency_key", "prompt"}
    }
    summary = render_params_summary(params)
    short_ids = " ".join(f"#{g[:8]}" for g in gen_ids)
    if len(gen_ids) == 1:
        status_text = (
            f"⏳ 任务已排队 #{gen_ids[0][:8]}\n\n{summary}\n\n📝 {prompt[:200]}"
        )
        batch_id = ""
    else:
        status_text = (
            f"⏳ 已派发 {len(gen_ids)} 个任务  {short_ids}\n\n"
            f"{summary}\n\n📝 {prompt[:200]}\n\n"
            f"完成的图会逐张推送，全部完成后此消息会自动消失。"
        )
        batch_id = gen_ids[0]

    status = await answer(status_text)
    try:
        if batch_id:
            await tracker.init_batch(batch_id, len(gen_ids))
        for gen_id in gen_ids:
            await tracker.add(
                gen_id,
                TaskTrack(
                    chat_id=chat_id,
                    tg_user_id=tg_user_id,
                    status_message_id=status.message_id,
                    prompt=prompt,
                    params=params,
                    batch_id=batch_id,
                    user_id=user_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracker registration failed ids=%s err=%r", gen_ids, exc)
        await answer("⚠️ 任务已创建，但通知追踪失败；请用 /tasks 查看结果。")
    return SubmissionDisposition.ACCEPTED


async def _submit_durable_generation(
    chat_id: int,
    tg_user_id: int,
    submission: DurableGenerationSubmission,
    api: LumenApi,
    answer,
) -> SubmissionDisposition:
    if submission.status is SubmissionJournalStatus.ACCEPTED:
        await answer("ℹ️ 这条 Telegram 请求已经提交，请用 /tasks 查看。")
        return SubmissionDisposition.ACCEPTED
    if submission.status is SubmissionJournalStatus.REJECTED:
        await answer("ℹ️ 这条 Telegram 请求已被拒绝，请重新发送以创建新请求。")
        return SubmissionDisposition.REJECTED
    disposition = await _submit_generation(
        chat_id,
        tg_user_id,
        submission.payload,
        api,
        answer,
    )
    await _finish_durable_submission(submission, disposition)
    return disposition


async def _delete_notice(notice: Message | None) -> None:
    if notice is None:
        return
    with contextlib.suppress(Exception):
        await notice.delete()


async def _submit_prompt_flow_payload(
    flow: _PromptFlow,
    payload: dict[str, Any],
) -> SubmissionDisposition | None:
    if not await flow.is_current():
        return None
    submission = await _stage_durable_submission(
        flow.state,
        chat_id=flow.message.chat.id,
        tg_user_id=flow.actor_id,
        update_token=flow.update_token,
        payload=payload,
        answer=flow.message.answer,
        expected_flow_epoch=flow.flow_epoch,
    )
    if submission is None:
        return None
    disposition = await _submit_durable_generation(
        flow.message.chat.id,
        flow.actor_id,
        submission,
        flow.api,
        flow.message.answer,
    )
    if (
        disposition is not SubmissionDisposition.AMBIGUOUS
        and await flow.is_current()
    ):
        await flow.state.clear()
    return disposition


async def _resolve_and_submit_prompt_flow(
    flow: _PromptFlow,
    candidate: dict[str, Any],
) -> SubmissionDisposition | None:
    payload = await resolve_or_stage_generation(
        flow.state,
        candidate,
        expected_flow_epoch=flow.flow_epoch,
    )
    if payload is None:
        if await flow.is_current():
            await flow.message.answer(
                "上一笔提交参数与当前请求不一致，请 /cancel 后重新开始。"
            )
        return None
    return await _submit_prompt_flow_payload(flow, payload)


async def _retry_pending_prompt_flow(
    flow: _PromptFlow,
    data: dict[str, Any],
    prompt: str,
) -> bool:
    pending = pending_generation(data)
    if pending is None:
        return False
    if str(pending.get("prompt") or "") != prompt:
        await flow.message.answer(
            "上一笔提交结果仍未确认。请重新发送原提示词重试，或 /cancel 放弃。"
        )
        return True
    await _submit_prompt_flow_payload(flow, pending)
    return True


async def _handle_prompt_enhance_api_error(
    flow: _PromptFlow,
    *,
    notice: Message,
    error: ApiError,
    prompt: str,
    params: dict[str, object],
    idempotency_key: str,
) -> None:
    if not await flow.is_current():
        await _delete_notice(notice)
        return
    logger.warning(
        "enhance failed user=%s err=%s",
        flow.message.chat.id,
        error,
    )
    await _delete_notice(notice)
    if error.outcome_unknown or error.code == "idempotency_in_progress":
        if error.code == "idempotency_in_progress":
            await flow.message.answer(
                "⏳ 上一笔提示词优化仍在处理中。请稍后重新发送同一提示词；"
                "系统会复用原请求。"
            )
        else:
            await flow.message.answer(
                "⚠️ 提示词优化结果暂时无法确认。请重新发送同一提示词；"
                "系统会复用原请求，不会重复扣费。"
            )
        return
    await flow.state.update_data(**{_PROMPT_ENHANCE_PENDING_FIELD: False})
    await flow.message.answer(
        f"⚠️ 优化失败（{error.message}），已用原提示词继续。"
    )
    if await flow.is_current():
        await _resolve_and_submit_prompt_flow(
            flow,
            _generation_payload(prompt, params, idempotency_key),
        )


async def _handle_prompt_enhance_connection_error(
    flow: _PromptFlow,
    *,
    notice: Message,
    error: BaseException,
) -> None:
    if not await flow.is_current():
        await _delete_notice(notice)
        return
    logger.warning(
        "enhance connection lost user=%s err=%r",
        flow.message.chat.id,
        error,
    )
    await _delete_notice(notice)
    await flow.message.answer(
        "⚠️ 提示词优化连接中断，结果暂时无法确认。"
        "请重新发送同一提示词；系统会复用原请求，不会重复扣费。"
    )


async def _publish_enhanced_prompt_choice(
    flow: _PromptFlow,
    *,
    notice: Message,
    enhanced: str,
) -> None:
    if not await flow.is_current():
        await _delete_notice(notice)
        return
    await flow.state.update_data(
        enhanced_prompt=enhanced,
        **{_PROMPT_ENHANCE_PENDING_FIELD: False},
    )
    if not await flow.is_current():
        await _delete_notice(notice)
        return
    await flow.state.set_state(GenFlow.confirming_enhanced)
    if not await flow.is_current():
        await _delete_notice(notice)
        return
    await _delete_notice(notice)
    await flow.message.answer(
        f"✨ 优化后：\n\n{enhanced[:3500]}",
        reply_markup=enhance_choice_keyboard(),
    )


async def _handle_prompt_enhancement(
    flow: _PromptFlow,
    *,
    prompt: str,
    params: dict[str, object],
    idempotency_key: str,
    prompt_enhance_key: str,
) -> None:
    notice = await flow.message.answer("✨ 正在优化提示词…")
    heartbeat = asyncio.create_task(
        _chat_action_heartbeat(
            flow.message,
            ChatAction.TYPING,
            flow.runtime,
        )
    )
    try:
        try:
            enhanced = await flow.api.enhance_prompt(
                flow.message.chat.id,
                prompt,
                idempotency_key=prompt_enhance_key,
                tg_user_id=flow.actor_id,
            )
        except ApiError as exc:
            await _handle_prompt_enhance_api_error(
                flow,
                notice=notice,
                error=exc,
                prompt=prompt,
                params=params,
                idempotency_key=idempotency_key,
            )
            return
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            await _handle_prompt_enhance_connection_error(
                flow,
                notice=notice,
                error=exc,
            )
            return
        await _publish_enhanced_prompt_choice(
            flow,
            notice=notice,
            enhanced=enhanced,
        )
    except asyncio.CancelledError:
        await _delete_notice(notice)
        raise
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _resolve_prompt_input(
    message: Message,
    state: FSMContext,
    runtime: GenerationRuntime,
) -> tuple[str, int, str] | None:
    prompt = message_prompt(message)
    if prompt == "/cancel":
        await runtime.cancel_prompt(message.chat.id)
        await state.clear()
        await message.answer("已取消。/new 重新开始。")
        return None
    if is_slash_command(prompt):
        await message.answer(
            "当前正在等待提示词。请发送普通文本，或先 /cancel 再执行命令。"
        )
        return None
    if not prompt:
        await message.answer("提示词不能为空，请重新发送。")
        return None
    if len(prompt) > 5000:
        await message.answer("提示词太长（>5000 字），请精简后重发。")
        return None
    actor_id = telegram_user_id(message)
    if actor_id is None:
        await message.answer("无法确认 Telegram 用户身份，请重新发送。")
        return None
    return prompt, actor_id, _message_update_token(message)


@router.message(GenFlow.awaiting_prompt)
async def on_prompt(
    message: Message,
    state: FSMContext,
    api: LumenApi,
    generation_runtime: GenerationRuntime,
) -> None:
    prompt_input = await _resolve_prompt_input(
        message,
        state,
        generation_runtime,
    )
    if prompt_input is None:
        return
    prompt, actor_id, update_token = prompt_input

    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("generation handler is not running in an asyncio task")
    if not await generation_runtime.begin_prompt(message.chat.id, task):
        await message.answer("上一条提示词仍在处理中，请稍候或发送 /cancel。")
        return

    try:
        data = await state.get_data()
        flow_epoch = await ensure_generation_flow_epoch(state, data)
        flow = _PromptFlow(
            message=message,
            state=state,
            api=api,
            runtime=generation_runtime,
            task=task,
            flow_epoch=flow_epoch,
            actor_id=actor_id,
            update_token=update_token,
        )
        if await _retry_pending_prompt_flow(flow, data, prompt):
            return

        original_prompt = str(data.get("original_prompt") or "")
        prompt_enhance_pending = data.get(_PROMPT_ENHANCE_PENDING_FIELD) is True
        if prompt_enhance_pending and original_prompt != prompt:
            await message.answer(
                "上一笔提示词优化结果仍未确认。请重新发送原提示词重试，或 /cancel 放弃。"
            )
            return

        params = dict(data.get("params") or DEFAULT_PARAMS)
        same_prompt = original_prompt == prompt
        stored_key = str(data.get("idempotency_key") or "") if same_prompt else ""
        idempotency_key = stored_key or generation_submission_idempotency_key(
            message.chat.id,
            actor_id,
            update_token,
        )
        stored_enhance_key = (
            str(data.get(_PROMPT_ENHANCE_KEY_FIELD) or "") if same_prompt else ""
        )
        if prompt_enhance_pending and not stored_enhance_key:
            await message.answer(
                "上一笔提示词优化缺少稳定请求标识，请 /cancel 后重新开始。"
            )
            return
        prompt_enhance_key = stored_enhance_key or make_idempotency_key(
            "prompt-enhance",
            message.chat.id,
            actor_id,
            flow_epoch,
            message.message_id,
        )
        if not await flow.is_current():
            return
        state_updates: dict[str, object] = {
            "params": params,
            "original_prompt": prompt,
            "idempotency_key": idempotency_key,
        }
        if params.get("enhance"):
            state_updates.update(
                {
                    _PROMPT_ENHANCE_KEY_FIELD: prompt_enhance_key,
                    _PROMPT_ENHANCE_PENDING_FIELD: True,
                }
            )
        await state.update_data(
            **state_updates,
        )

        if params.get("enhance"):
            await _handle_prompt_enhancement(
                flow,
                prompt=prompt,
                params=params,
                idempotency_key=idempotency_key,
                prompt_enhance_key=prompt_enhance_key,
            )
            return

        await _resolve_and_submit_prompt_flow(
            flow,
            _generation_payload(prompt, params, idempotency_key),
        )
    except asyncio.CancelledError:
        if not await generation_runtime.prompt_was_cancelled(task):
            raise
    finally:
        await generation_runtime.finish_prompt(message.chat.id, task)


async def _acquire_submit_guard(idempotency_key: str) -> tuple[bool, bool]:
    try:
        acquired = await tracker.acquire_submit_once(idempotency_key)
        return acquired, acquired
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "submit-once lock unavailable key=%s err=%r",
            idempotency_key,
            exc,
        )
        return True, False


async def _release_submit_guard(idempotency_key: str) -> None:
    try:
        await tracker.release_submit_once(idempotency_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "submit-once release failed key=%s err=%r",
            idempotency_key,
            exc,
        )


async def _submit_enhance_choice_generation(
    cb: CallbackQuery,
    msg: Message,
    state: FSMContext,
    api: LumenApi,
    *,
    choice: str,
    prompt: str,
    params: dict[str, object],
    idempotency_key: str,
    actor_id: int,
) -> None:
    update_token = _callback_update_token(
        cb,
        choice=choice,
        message_id=msg.message_id,
    )
    payload = await resolve_or_stage_generation(
        state,
        _generation_payload(prompt, params, idempotency_key),
    )
    if payload is None:
        await cb.answer(
            "上一笔提交结果仍未确认，请重试原来的选择。",
            show_alert=True,
        )
        return
    submission = await _stage_durable_submission(
        state,
        chat_id=msg.chat.id,
        tg_user_id=actor_id,
        update_token=update_token,
        payload=payload,
        answer=msg.answer,
    )
    if submission is None:
        await cb.answer("安全提交服务暂不可用", show_alert=True)
        return

    acquired, guard_acquired = await _acquire_submit_guard(
        submission.idempotency_key
    )
    if not acquired:
        await cb.answer("已在提交中，请勿重复点击")
        return

    disposition = await _submit_durable_generation(
        msg.chat.id,
        actor_id,
        submission,
        api,
        msg.answer,
    )
    if disposition is SubmissionDisposition.AMBIGUOUS:
        if guard_acquired:
            await _release_submit_guard(submission.idempotency_key)
        await cb.answer("提交结果未知，请再次点击同一选项")
        return

    await state.clear()
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await cb.answer(
        "已提交" if disposition is SubmissionDisposition.ACCEPTED else "提交失败",
    )


@router.callback_query(GenFlow.confirming_enhanced, F.data.startswith("enh:"))
async def on_enhance_choice(
    cb: CallbackQuery, state: FSMContext, api: LumenApi
) -> None:
    choice = (cb.data or "").split(":", 1)[1] if cb.data else ""
    msg = await require_message(cb)
    if msg is None:
        await state.clear()
        return
    data = await state.get_data()
    original = str(data.get("original_prompt") or "")
    enhanced = str(data.get("enhanced_prompt") or "")
    params = dict(data.get("params") or DEFAULT_PARAMS)
    idempotency_key = str(data.get("idempotency_key") or "")

    if choice == "cancel":
        await state.clear()
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await cb.answer("已取消")
        return

    if choice == "edit":
        # 进入手动编辑：先把按钮去掉避免重复点；再发一条单独的 message 让用户复制
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await state.set_state(GenFlow.editing_enhanced)
        # 单独一条只含优化文本的消息，方便长按 → 复制
        if enhanced:
            await msg.answer(enhanced)
        await msg.answer(
            "✏️ 把改好的提示词发回来。\n（直接发送一条新消息即可；/cancel 放弃）"
        )
        await cb.answer()
        return

    if choice == "use":
        prompt = enhanced
    elif choice == "orig":
        prompt = original
    else:
        await cb.answer("无效选择，请重新发起。", show_alert=True)
        return
    if not prompt or not idempotency_key:
        await cb.answer("会话已失效，/new 重开", show_alert=True)
        await state.clear()
        return
    actor_id = telegram_user_id(cb)
    if actor_id is None:
        await cb.answer("无法确认 Telegram 用户身份", show_alert=True)
        return
    await _submit_enhance_choice_generation(
        cb,
        msg,
        state,
        api,
        choice=choice,
        prompt=prompt,
        params=params,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
    )


@router.message(GenFlow.editing_enhanced)
async def on_edited_prompt(message: Message, state: FSMContext, api: LumenApi) -> None:
    text = message_prompt(message)
    if text == "/cancel":
        await state.clear()
        await message.answer("已放弃。/new 重新开始。")
        return
    if is_slash_command(text):
        await message.answer(
            "当前正在等待改好的提示词。请发送普通文本，或 /cancel 放弃。"
        )
        return
    if not text:
        await message.answer("提示词不能为空，重新发送一条；/cancel 放弃。")
        return
    if len(text) > 5000:
        await message.answer("提示词太长（>5000 字），请精简后重发。")
        return
    actor_id = telegram_user_id(message)
    if actor_id is None:
        await message.answer("无法确认 Telegram 用户身份，请重新发送。")
        return
    data = await state.get_data()
    params = dict(data.get("params") or DEFAULT_PARAMS)
    update_token = _message_update_token(message)
    payload = await resolve_or_stage_generation(
        state,
        _generation_payload(
            text,
            params,
            generation_submission_idempotency_key(
                message.chat.id,
                actor_id,
                update_token,
            ),
        ),
    )
    if payload is None:
        await message.answer(
            "上一笔提交结果仍未确认。请重新发送同一提示词重试，或 /cancel 放弃。"
        )
        return
    submission = await _stage_durable_submission(
        state,
        chat_id=message.chat.id,
        tg_user_id=actor_id,
        update_token=update_token,
        payload=payload,
        answer=message.answer,
    )
    if submission is None:
        return
    disposition = await _submit_durable_generation(
        message.chat.id,
        actor_id,
        submission,
        api,
        message.answer,
    )
    if disposition is not SubmissionDisposition.AMBIGUOUS:
        await state.clear()
