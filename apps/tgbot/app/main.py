"""Bot 进程入口。

- aiogram 3 Dispatcher + Bot
- MemoryStorage（FSM）；进程重启会丢菜单状态，但绑定 + 任务 tracker 各自有持久化路径
- 单 worker：listener task + polling 在同一 event loop
- DI：把 LumenApi 实例 inject 给 handler
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from .api_client import ApiError, LumenApi
from .config import settings
from .handlers import build_root_router
from .listener import run_listener
from .middlewares import AccessGate
from .proxy_manager import FailoverSession, ProxyManager, _normalize_proxy_url


_CONTROL_CHANNEL = "admin:tgbot:control"


async def _run_control_listener(stop_event: asyncio.Event) -> None:
    """订阅 admin 通道；收到 restart 命令则 clean-exit，systemd Restart=always 会拉起。

    任何错误（包括 Redis 抖动）都不应该让进程退出；记 warning 后继续重连。
    """
    from redis import asyncio as aioredis

    logger = logging.getLogger("lumen-tgbot.control")
    backoff = 1.0
    while not stop_event.is_set():
        pubsub = None
        client = None
        try:
            client = aioredis.from_url(settings.redis_url, decode_responses=False)
            pubsub = client.pubsub()
            await pubsub.subscribe(_CONTROL_CHANNEL)
            logger.info("control: subscribed to %s", _CONTROL_CHANNEL)
            backoff = 1.0
            async for msg in pubsub.listen():
                if stop_event.is_set():
                    break
                if msg.get("type") != "message":
                    continue
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                cmd = (str(data) or "").strip().lower()
                if cmd == "restart":
                    logger.info("control: restart received → clean exit")
                    stop_event.set()
                    # 让 main 走 finally 清理；最外层 _amain 会 return，进程退出码 0，
                    # systemd 拉起。这里不直接 sys.exit，避免和 main 关闭逻辑打架。
                    return
        except Exception as exc:  # noqa: BLE001
            logger.warning("control listener error: %s; reconnect in %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        finally:
            try:
                if pubsub is not None:
                    await pubsub.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if client is not None:
                    await client.aclose()
            except Exception:  # noqa: BLE001
                pass


def _redact_proxy(url: str) -> str:
    # 日志里隐去用户名/密码段
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        return f"{scheme}://***@{host}"
    return url


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


async def _amain() -> None:
    _setup_logging()
    logger = logging.getLogger("lumen-tgbot")

    if not settings.telegram_bot_shared_secret.strip():
        logger.error("no TELEGRAM_BOT_SHARED_SECRET, refusing to start")
        return
    if settings.bot_mode == "webhook" and not settings.webhook_url.strip():
        logger.error("WEBHOOK_URL is required when BOT_MODE=webhook")
        return

    api = LumenApi()
    proxy_mgr = ProxyManager(api)

    # 先去 API 拉 runtime-config（DB 优先，env 兜底）
    bot_token = ""
    initial_proxy_url = ""
    bot_enabled = True
    try:
        cfg = await proxy_mgr.initial_load()
        bot_token = (cfg.get("bot_token") or "").strip()
        bot_enabled = bool(cfg.get("bot_enabled", True))
        proxy_info = cfg.get("proxy") or {}
        if isinstance(proxy_info, dict):
            initial_proxy_url = str(proxy_info.get("url") or "")
    except ApiError as exc:
        logger.warning("runtime-config load failed (will use env fallbacks): %s", exc)

    # bootstrap fallbacks
    if not bot_token:
        bot_token = settings.telegram_bot_token
    if not initial_proxy_url:
        initial_proxy_url = settings.telegram_proxy_url.strip()
    initial_proxy_url = _normalize_proxy_url(initial_proxy_url)

    if not bot_enabled:
        logger.info("telegram.bot_enabled=0 in DB → exit cleanly")
        await api.aclose()
        return
    if not bot_token:
        logger.error("no bot token (DB empty + env empty), refusing to start")
        await api.aclose()
        return

    if initial_proxy_url:
        logger.info(
            "outbound proxy: name=%s url=%s",
            proxy_mgr.current_name or "(env fallback)",
            _redact_proxy(initial_proxy_url),
        )
    else:
        logger.warning("no outbound proxy configured; TG calls will go direct (likely fail in CN)")

    session = FailoverSession(proxy_mgr, proxy=initial_proxy_url) if initial_proxy_url else None
    bot_kwargs: dict[str, object] = {
        "token": bot_token,
        "default": DefaultBotProperties(parse_mode=None),
    }
    if session is not None:
        bot_kwargs["session"] = session
    bot = Bot(**bot_kwargs)
    dp = Dispatcher(storage=MemoryStorage())

    # DI：handler 用 `api: LumenApi` 注解就能拿到
    dp["api"] = api

    # 全局准入：拒非私聊 + 可选 TG user_id 白名单
    gate = AccessGate()
    dp.message.middleware(gate)
    dp.callback_query.middleware(gate)

    dp.include_router(build_root_router())

    stop_event = asyncio.Event()
    listener_task = asyncio.create_task(run_listener(bot, api, stop_event), name="lumen-listener")
    control_task = asyncio.create_task(_run_control_listener(stop_event), name="lumen-control")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows fallback

    try:
        if settings.bot_mode == "polling":
            logger.info("starting polling; api=%s", settings.lumen_api_base)
            polling = asyncio.create_task(
                dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
                name="lumen-polling",
            )
            stop_wait = asyncio.create_task(stop_event.wait(), name="lumen-stopwait")
            await asyncio.wait(
                [polling, stop_wait], return_when=asyncio.FIRST_COMPLETED
            )
            polling.cancel()
            try:
                await polling
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        else:
            # webhook mode：交给 nginx + aiohttp。MVP 不内置，托管在外层。
            logger.error(
                "webhook mode not implemented in this entrypoint; deploy via nginx + a webhook server"
            )
            stop_event.set()
    finally:
        stop_event.set()
        for t in (listener_task, control_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await bot.session.close()
        await api.aclose()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
