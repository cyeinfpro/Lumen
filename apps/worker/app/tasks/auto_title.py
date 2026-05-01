"""自动会话标题：在第一条 assistant 任务完成后，调用 gpt-5.4-mini 生成短标题。

触发：
- run_completion / run_generation 在 succeeded 末尾调用 `maybe_enqueue_auto_title`
- 该函数检查 conversation.title 是否为空（或仍是默认占位）；若是则 enqueue 本任务

任务行为：
- 拉该会话最近 4 条消息的文本摘要
- 调上游 /v1/responses model=gpt-5.4-mini 一次性生成不超过 12 字的中文标题
- UPDATE conversations.title
- publish SSE conv.renamed 事件让 sidebar 实时刷新
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update

from lumen_core.constants import EV_CONV_RENAMED, GenerationErrorCode as EC, conv_channel
from lumen_core.models import Conversation, Message
from lumen_core.providers import ProviderProxyDefinition, resolve_provider_proxy_url

import httpx

from ..db import SessionLocal
from ..sse_publish import publish_event
from ..upstream import UpstreamError, _auth_headers

logger = logging.getLogger(__name__)


_TITLE_MODEL = "gpt-5.4-mini"
_TITLE_INSTRUCTIONS = (
    "你是一个会话标题生成助手。根据用户与助手的前几轮对话，生成一个 4-12 字"
    "的中文短标题，能概括对话主题。直接输出标题文字，不要带引号、标点、"
    "前缀（如'标题：'）或任何额外说明。"
)
_HISTORY_WINDOW = 4
_MAX_PROMPT_CHARS = 1200  # 每条消息文本最大保留长度，避免 prompt 爆炸

# 内存缓存：避免对已有标题的会话反复查 DB
# 正条目（title 已非默认）存 _TITLE_CONFIRMED_SENTINEL，永不过期；
# 负条目（title 仍是占位符）存时间戳，TTL 后允许重新检查。
_title_cache: dict[str, float] = {}
_TITLE_CONFIRMED_SENTINEL: float = -1.0
_TITLE_CACHE_TTL_S = 60.0

# 单次 auto_title 调用的失败重试预算（每个 provider 内部）
_PER_PROVIDER_RETRY_ATTEMPTS = 2
_PER_PROVIDER_RETRY_BACKOFF_S = 2.0
# Title 生成应该秒级完成（gpt-5.4-mini 一次性输出 < 12 字）；不能用 upstream 主路径的
# settings.upstream_read_timeout_s（默认 180s，给 4K 生图留的）——单 worker 卡 3 分钟
# 等一个 title 会让 4K 任务都排队。30s 足够，超了视为故障切下一个 provider。
_TITLE_HTTP_TIMEOUT_S = 30.0
_TITLE_TOTAL_TIMEOUT_S = 75.0

# 巡检相关
_RECONCILE_LOOKBACK_HOURS = 24       # 只扫近 24h 内的会话，避免遍历全表
_RECONCILE_STABLE_AFTER_S = 60       # last_activity_at < now - 60s 才算"已稳定"
_RECONCILE_BATCH_LIMIT = 50          # 单轮最多 enqueue 50 个，限速
_DEFAULT_RECONCILE_INTERVAL_S = 300  # 5 分钟一次
_RECONCILE_LOCK_KEY = "lumen:auto_title:reconcile:lock"
_RECONCILE_LOCK_TTL_S = 60           # 锁 TTL 必须比单轮巡检最坏耗时长（实测 < 5s）

# 定义"默认 / 待生成"的 title 占位集合，DB 查询和 Python 校验共享一份真相源。
# - ""：Conversation.title 的 DB 默认值（models.py:152 default=""）
# - "New Canvas"：前端 sidebar fallback（apps/web/src/components/ui/sidebar/ConversationItem.tsx
#   等三处），万一被某个路径写进 DB 也要识别为可被 auto_title 覆盖
# - "未命名" / "新会话" / "untitled" / "Untitled"：历史兼容（老版本可能写过）
_DEFAULT_TITLE_PLACEHOLDERS = (
    "",
    "New Canvas",
    "未命名",
    "新会话",
    "untitled",
    "Untitled",
)


def _is_default_title(title: str | None) -> bool:
    if title is None:
        return True
    t = title.strip()
    if not t:
        return True
    # 历史/手动占位
    return t in set(_DEFAULT_TITLE_PLACEHOLDERS)


async def maybe_enqueue_auto_title(redis: Any, conversation_id: str) -> None:
    """由 run_completion / run_generation 在 succeeded 后调用。

    幂等：标题已设置则跳过；任务失败也不抛异常（不能阻塞主流程）。

    P1 fix (BUG-024): 正缓存条目用 _TITLE_CONFIRMED_SENTINEL（永不过期），
    避免每次 generation/completion succeeded 都 SELECT title FROM conversations。
    仅当缓存 miss 或缓存为旧时间戳（待确认状态）时才查 DB。
    """
    now = time.monotonic()
    cache_entry = _title_cache.get(conversation_id)
    if cache_entry is not None:
        if cache_entry == _TITLE_CONFIRMED_SENTINEL:
            return  # permanently cached: title is set
        if now - cache_entry < _TITLE_CACHE_TTL_S:
            return  # recently checked, still in grace period
    try:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(Conversation.title).where(Conversation.id == conversation_id)
                )
            ).scalar_one_or_none()
            if row is None or not _is_default_title(row):
                _title_cache[conversation_id] = _TITLE_CONFIRMED_SENTINEL
                return
        # enqueue 异步任务，避免阻塞 worker 当前任务收尾
        await redis.enqueue_job("auto_title_conversation", conversation_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("maybe_enqueue_auto_title failed conv=%s err=%s", conversation_id, exc)


def _extract_text(content: dict[str, Any] | None) -> str:
    if not content:
        return ""
    text = content.get("text") if isinstance(content, dict) else None
    if isinstance(text, str) and text.strip():
        return text.strip()
    # 助手图像消息也可能没 text；用 [图片] 占位
    images = content.get("images") if isinstance(content, dict) else None
    if isinstance(images, list) and images:
        return "[图片]"
    return ""


async def _build_summary(session: Any, conversation_id: str) -> list[dict[str, Any]]:
    """读最近 N 条消息（按 created_at DESC 取最新），转成 /v1/responses input 列表。

    BUG-010: 原先用 .asc() 取了最早的 4 条消息生成标题，导致标题质量低。
    改为 .desc() 取最新的 4 条，reverse 后按时间正序送给 LLM。
    """
    rows = list(
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc())
                .limit(_HISTORY_WINDOW)
            )
        ).scalars()
    )
    # 取最新 N 条后反转为时间正序交给 LLM
    rows.reverse()
    items: list[dict[str, Any]] = []
    for m in rows:
        text = _extract_text(m.content if isinstance(m.content, dict) else None)
        if not text:
            continue
        text = text[:_MAX_PROMPT_CHARS]
        if m.role == "user":
            items.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            )
        elif m.role == "assistant":
            items.append(
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            )
    return items


def _parse_response_text(text: str, content_type: str) -> str:
    """从上游 /v1/responses 响应体里抽出完整 title 文本。

    历史 bug：之前只解析 `response.output_text.done` 和
    `response.content_part.done` 两个事件——但上游在某些版本下只发
    `response.output_text.delta` + `response.completed`，不发 done。
    那时 title 永远是空，conversation 永远停在默认占位。

    现在策略：四路兜底，命中即返回（优先级降序）：
    1. 累积所有 `response.output_text.delta` 的 delta 字段 → 拼接全文（最稳）
    2. 任何 `*.done` 事件里直接给的 text 字段
    3. SSE 解析失败时退化为整体 JSON：output[].content[].text 或 output_text
    4. 都拿不到 → 返回 ""
    """
    if not text:
        return ""

    # ---- SSE 路径：行级解析 ----
    accumulated_delta = ""
    done_text = ""

    is_sse = "text/event-stream" in content_type or text.startswith("event:") or "\ndata:" in text or text.startswith("data:")
    if is_sse:
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[len("data:") :].lstrip()
            if raw == "[DONE]":
                continue
            try:
                ev = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(ev, dict):
                continue
            evtype = ev.get("type")
            if evtype == "response.output_text.delta":
                d = ev.get("delta")
                if isinstance(d, str):
                    accumulated_delta += d
            elif evtype == "response.output_text.done":
                t = ev.get("text")
                if isinstance(t, str) and t.strip():
                    done_text = t.strip()
            elif evtype == "response.content_part.done":
                part = ev.get("part") if isinstance(ev.get("part"), dict) else None
                if isinstance(part, dict):
                    t = part.get("text")
                    if isinstance(t, str) and t.strip():
                        done_text = t.strip()
            elif evtype == "response.completed":
                # 兜底：从 response.output[].content[].text 提取完整文本
                resp_obj = ev.get("response")
                if isinstance(resp_obj, dict) and not done_text:
                    for item in (resp_obj.get("output") or []):
                        if not isinstance(item, dict):
                            continue
                        for part in (item.get("content") or []):
                            if isinstance(part, dict):
                                t = part.get("text")
                                if isinstance(t, str) and t.strip():
                                    done_text = t.strip()
                                    break
                        if done_text:
                            break

        if done_text:
            return done_text
        if accumulated_delta.strip():
            return accumulated_delta.strip()

    # ---- 兜底：整体 JSON 解析（上游可能切回非 SSE） ----
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ""
    if isinstance(payload, dict):
        for item in (payload.get("output") or []):
            if not isinstance(item, dict):
                continue
            for part in (item.get("content") or []):
                if isinstance(part, dict):
                    t = part.get("text")
                    if isinstance(t, str) and t.strip():
                        return t.strip()
        ot = payload.get("output_text")
        if isinstance(ot, str) and ot.strip():
            return ot.strip()
    return ""


async def _call_upstream_one(
    input_list: list[dict[str, Any]],
    *,
    base_url: str,
    api_key: str,
    proxy: ProviderProxyDefinition | None = None,
) -> str:
    """对单个 provider 跑一次 title 生成；返回原始 title（未 sanitize），失败抛 UpstreamError。

    用临时 httpx.AsyncClient 而不是 upstream._get_client() 单例：
    - 单例 client 的 read timeout 是 settings.upstream_read_timeout_s（180s 给 4K 留的）
    - title 生成秒级完成，30s 足够；超时直接抛网络错让 caller failover 切号
    """
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    url = f"{base}/responses"
    body = {
        "model": _TITLE_MODEL,
        "input": input_list,
        "instructions": _TITLE_INSTRUCTIONS,
        "stream": False,
        "store": False,
    }
    headers = {**_auth_headers(api_key), "content-type": "application/json"}
    try:
        proxy_url = await resolve_provider_proxy_url(proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=_TITLE_HTTP_TIMEOUT_S,
                write=_TITLE_HTTP_TIMEOUT_S,
                pool=10.0,
            ),
            proxy=proxy_url,
        ) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise UpstreamError(
            f"auto_title upstream timeout after {_TITLE_HTTP_TIMEOUT_S:.0f}s",
            error_code=EC.UPSTREAM_TIMEOUT.value,
            status_code=None,
        ) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(
            f"auto_title upstream network error: {exc}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=None,
        ) from exc
    if resp.status_code >= 400:
        logger.warning(
            "auto_title upstream %s body=%.500s",
            resp.status_code,
            resp.text or "",
        )
        raise UpstreamError(
            f"auto_title upstream http {resp.status_code}",
            error_code=EC.UPSTREAM_ERROR.value,
            status_code=resp.status_code,
        )
    return _parse_response_text(
        resp.text or "", resp.headers.get("content-type", "")
    )


async def _call_upstream(input_list: list[dict[str, Any]]) -> str:
    """走 provider_pool failover 调用 /v1/responses 拿 title。

    失败处理：
    - 单 provider 内部 retriable 错（5xx / 网络）→ 短 backoff 重试一次
    - 单 provider terminal 失败 → 立刻 failover 下一个候选
    - 所有候选都失败 → 抛 UpstreamError 让 caller 判定不重试
    """
    from ..provider_pool import get_pool
    from ..retry import is_retriable as classify_retriable

    pool = await get_pool()
    # text route 调度（不是 image）；也避开 image_rate_limited 维度，单纯走 text 路径
    providers = await pool.select(route="text")
    last_exc: BaseException | None = None
    attempted_providers: list[str] = []

    for provider in providers:
        attempted_providers.append(provider.name)
        for attempt in range(_PER_PROVIDER_RETRY_ATTEMPTS):
            try:
                kwargs: dict[str, Any] = {
                    "base_url": provider.base_url,
                    "api_key": provider.api_key,
                }
                proxy = getattr(provider, "proxy", None)
                if proxy is not None:
                    kwargs["proxy"] = proxy
                return await _call_upstream_one(input_list, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                decision = classify_retriable(
                    getattr(exc, "error_code", None),
                    getattr(exc, "status_code", None),
                    error_message=str(exc),
                )
                if not decision.retriable:
                    # terminal：换一个 provider 也无意义但 spec 类的 4xx 可能是该号 auth 坏，
                    # 故仍跳出内层循环，让外层 try 下一个 provider。
                    break
                if attempt + 1 < _PER_PROVIDER_RETRY_ATTEMPTS:
                    await asyncio.sleep(
                        _PER_PROVIDER_RETRY_BACKOFF_S * (2 ** attempt)
                    )
    if last_exc is not None:
        # 失败摘要：让运维一眼看到尝试了哪些号、最后死在什么错。message 截短
        # 避免大 stack trace 撑爆 log。
        logger.warning(
            "auto_title all providers failed providers=%s last_err_code=%s "
            "last_status=%s last_msg=%.300s",
            ",".join(attempted_providers) or "<none>",
            getattr(last_exc, "error_code", None),
            getattr(last_exc, "status_code", None),
            str(last_exc),
        )
        raise UpstreamError(
            f"auto_title all providers failed: {last_exc}",
            error_code=EC.ALL_PROVIDERS_FAILED.value,
            status_code=getattr(last_exc, "status_code", None) or 503,
        ) from last_exc
    return ""


def _sanitize_title(raw: str) -> str:
    """裁剪上游可能的多余符号；限制长度上限。"""
    t = raw.strip()
    # 去掉常见包裹符号
    for ch in ("「", "」", "『", "』", '"', "'", "“", "”", "‘", "’", "《", "》"):
        t = t.replace(ch, "")
    # 去掉常见前缀
    for prefix in ("标题：", "标题:", "Title:", "title:", "Title：", "title："):
        if t.startswith(prefix):
            t = t[len(prefix) :].strip()
    return _truncate_title_display_width(t)


def _truncate_title_display_width(title: str, max_width: int = 24) -> str:
    """Limit to 12 full-width CJK chars, or 24 half-width ASCII chars."""
    width = 0
    out: list[str] = []
    for ch in title:
        ch_width = 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
        if width + ch_width > max_width:
            break
        out.append(ch)
        width += ch_width
    return "".join(out)


async def auto_title_conversation(ctx: dict[str, Any], conversation_id: str) -> None:
    """arq 任务函数：生成并写入会话标题。

    并发安全：进入时再检一次 title 是否仍为空；非空直接返回。
    """
    redis = ctx["redis"]

    try:
        async with SessionLocal() as session:
            conv = await session.get(Conversation, conversation_id)
            if conv is None:
                return
            if not _is_default_title(conv.title):
                return  # 用户可能手动改过；尊重用户
            user_id = conv.user_id
            input_list = await _build_summary(session, conversation_id)
            if not input_list:
                return  # 没足够内容生成标题

        # 调上游
        try:
            async with asyncio.timeout(_TITLE_TOTAL_TIMEOUT_S):
                raw = await _call_upstream(input_list)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "auto_title upstream failed conv=%s err_type=%s",
                conversation_id,
                type(exc).__name__,
            )
            return

        title = _sanitize_title(raw)
        if not title:
            return

        # 写库
        async with SessionLocal() as session:
            # 再次幂等检查（双重安全）
            row = (
                await session.execute(
                    select(Conversation.title).where(
                        Conversation.id == conversation_id
                    )
                )
            ).scalar_one_or_none()
            if not _is_default_title(row):
                return
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(title=title)
            )
            await session.commit()

        # 推 SSE conv.renamed 给前端 sidebar 实时刷新
        try:
            await publish_event(
                redis,
                user_id,
                conv_channel(conversation_id),
                EV_CONV_RENAMED,
                {"conversation_id": conversation_id, "title": title},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_title publish_event failed conv=%s err=%s", conversation_id, exc)

        _title_cache[conversation_id] = _TITLE_CONFIRMED_SENTINEL
        logger.info("auto_title set conv=%s title=%r", conversation_id, title)
    except Exception as exc:  # noqa: BLE001
        # 不抛异常以免触发 arq 重试（一次失败就算了）
        logger.warning("auto_title task failed conv=%s err=%s", conversation_id, exc)


async def reconcile_default_titles(ctx: dict[str, Any]) -> int:
    """巡检兜底：把"应该有标题但还是默认占位"的会话重新 enqueue auto_title。

    场景：
    - 上游一时不可达让 maybe_enqueue_auto_title 提交但任务执行时上游 503
    - auto_title_conversation 内部 catch + return 不重试（design choice），漏网
    - 历史会话从老版本迁移过来还没标题

    扫描规则：
    - 限近 _RECONCILE_LOOKBACK_HOURS 小时（默认 24h）内 last_activity_at 的会话，
      避免遍历全表
    - last_activity_at <= now - _RECONCILE_STABLE_AFTER_S（默认 60s）才算"已稳定"，
      免得撞上正在生成的会话
    - title 必须是默认占位之一
    - 必须至少有 1 条 SUCCEEDED 状态的 user 消息（否则没料给上游生标题）
    - 单轮 limit _RECONCILE_BATCH_LIMIT（默认 50）

    Returns: 本轮 enqueue 的会话数；没抢到分布式锁时返回 -1 表示跳过。
    """
    redis = ctx.get("redis")
    if redis is None:
        return 0

    # 分布式锁：HA 多 worker 部署时只让一个 worker 跑这一轮。auto_title_conversation
    # 内部已有 _is_default_title 双重检查（重复 enqueue 不会写双份），此锁只是减少
    # 冗余 enqueue / 减轻 arq 队列压力。
    try:
        got_lock = await redis.set(
            _RECONCILE_LOCK_KEY,
            "1",
            nx=True,
            ex=_RECONCILE_LOCK_TTL_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile_default_titles lock failed: %s", exc)
        # 锁失败不阻断巡检——按"不抢锁"模式继续跑，最坏情况是双 worker 都跑一遍
        got_lock = True
    if not got_lock:
        return -1

    now = datetime.now(timezone.utc)
    lookback_start = now - timedelta(hours=_RECONCILE_LOOKBACK_HOURS)
    stable_before = now - timedelta(seconds=_RECONCILE_STABLE_AFTER_S)

    enqueued = 0
    try:
        async with SessionLocal() as session:
            # 子查询：该会话至少有 1 条 succeeded user 消息
            has_user_msg = (
                exists()
                .where(
                    and_(
                        Message.conversation_id == Conversation.id,
                        Message.role == "user",
                        Message.status == "succeeded",
                    )
                )
            )
            stmt = (
                select(Conversation.id)
                .where(
                    Conversation.last_activity_at >= lookback_start,
                    Conversation.last_activity_at <= stable_before,
                    or_(
                        Conversation.title.is_(None),
                        Conversation.title.in_(_DEFAULT_TITLE_PLACEHOLDERS),
                    ),
                    Conversation.archived.is_(False),
                    has_user_msg,
                )
                .order_by(Conversation.last_activity_at.desc())
                .limit(_RECONCILE_BATCH_LIMIT)
            )
            rows = (await session.execute(stmt)).all()

        for row in rows:
            conv_id = row[0] if isinstance(row, tuple) else row.id
            try:
                await redis.enqueue_job("auto_title_conversation", conv_id)
                enqueued += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile_default_titles enqueue failed conv=%s err=%s",
                    conv_id,
                    exc,
                )
        if enqueued > 0:
            logger.info(
                "reconcile_default_titles: re-enqueued %d conversations", enqueued
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile_default_titles failed: %s", exc)
    return enqueued


__all__ = [
    "auto_title_conversation",
    "maybe_enqueue_auto_title",
    "reconcile_default_titles",
]
