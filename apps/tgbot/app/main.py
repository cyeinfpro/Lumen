"""Bot 进程入口。

- aiogram 3 Dispatcher + Bot
- RedisStorage 优先，MemoryStorage 只兜底菜单状态；付费生成另有 Redis journal，
  journal 不可用时 fail closed，不会用易失 FSM 代替幂等身份
- 单 worker：listener task + polling 在同一 event loop
- DI：把 LumenApi 实例 inject 给 handler
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.utils.token import TokenValidationError
from aiohttp import ClientError
from prometheus_client import Counter, start_http_server
from redis import asyncio as aioredis

from .api_client import ApiError, LumenApi
from .config import settings
from .handlers import GenerationRuntime, build_root_router
from .listener import run_listener
from .middlewares import AccessGate
from .proxy_manager import FailoverSession, ProxyManager, normalize_proxy_url
from .tgbot_health import (
    TgbotHealthReporter,
    TgbotRuntimeStatus,
)
from .tracker import tracker


# FSM 状态过期时间。/new 走完一个生成或丢弃后状态被 clear()，正常路径不会留垃圾。
# 但用户中途退出（关闭 TG / 长时间不回复）的状态需要兜底过期，避免 redis 里
# 沉积上百万条死状态。1h 比 enhance/iter 流程的合理交互窗口宽十几倍。
_FSM_STATE_TTL_SEC = 3600


_CONTROL_CHANNEL = "admin:tgbot:control"
_PAUSED_CONFIG_REFRESH_INTERVAL_SEC = 20.0
_POLLING_NETWORK_BACKOFF_MAX_SEC = 60.0

TGBOT_POLLING_FAILURES = Counter(
    "tgbot_polling_failures_total",
    "Telegram polling terminations classified by the process supervisor.",
    labelnames=("class",),
)


class PollingTerminationClass(StrEnum):
    NORMAL_STOP = "normal_stop"
    NORMAL_CANCEL = "normal_cancel"
    INVALID_CONFIGURATION = "invalid_configuration"
    RECOVERABLE_NETWORK = "recoverable_network"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PollingTermination:
    classification: PollingTerminationClass
    error: BaseException | None = None


class PollingSupervisorFailure(RuntimeError):
    pass


def _set_runtime_health(
    runtime_health: TgbotHealthReporter,
    status: TgbotRuntimeStatus,
    reason: str,
) -> None:
    runtime_health.transition(status, reason)


async def _sleep_or_stop(
    stop_event: asyncio.Event,
    seconds: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    if stop_event.is_set():
        return True
    stop_task = asyncio.create_task(stop_event.wait(), name="lumen-backoff-stop")
    sleep_task = asyncio.ensure_future(sleep(seconds))
    done, _pending = await asyncio.wait(
        (stop_task, sleep_task),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if stop_task in done:
        sleep_task.cancel()
        await asyncio.gather(sleep_task, return_exceptions=True)
        return True
    stop_task.cancel()
    await asyncio.gather(stop_task, return_exceptions=True)
    await sleep_task
    return False


def _record_polling_failure(classification: PollingTerminationClass) -> None:
    if classification not in {
        PollingTerminationClass.NORMAL_STOP,
        PollingTerminationClass.NORMAL_CANCEL,
    }:
        TGBOT_POLLING_FAILURES.labels(classification.value).inc()


def _classify_polling_error(error: BaseException) -> PollingTerminationClass:
    if isinstance(
        error,
        (
            TokenValidationError,
            TelegramUnauthorizedError,
            TelegramForbiddenError,
        ),
    ):
        return PollingTerminationClass.INVALID_CONFIGURATION
    if isinstance(
        error,
        (
            TelegramNetworkError,
            TelegramServerError,
            ClientError,
            TimeoutError,
            OSError,
        ),
    ):
        return PollingTerminationClass.RECOVERABLE_NETWORK
    return PollingTerminationClass.UNKNOWN


async def _wait_for_polling_termination(
    polling: asyncio.Task[None],
    stop_event: asyncio.Event,
) -> PollingTermination:
    stop_wait = asyncio.create_task(stop_event.wait(), name="lumen-stopwait")
    done, _pending = await asyncio.wait(
        (polling, stop_wait),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if stop_wait in done:
        polling.cancel()
        try:
            await polling
        except asyncio.CancelledError:
            return PollingTermination(PollingTerminationClass.NORMAL_STOP)
        except Exception as exc:  # noqa: BLE001
            return PollingTermination(_classify_polling_error(exc), exc)
        return PollingTermination(PollingTerminationClass.NORMAL_STOP)

    stop_wait.cancel()
    try:
        await stop_wait
    except asyncio.CancelledError:
        pass
    if polling.cancelled():
        error = PollingSupervisorFailure(
            "Telegram polling was cancelled before a stop was requested"
        )
        return PollingTermination(PollingTerminationClass.UNKNOWN, error)
    error = polling.exception()
    if error is None:
        error = PollingSupervisorFailure(
            "Telegram polling returned before a stop was requested"
        )
    return PollingTermination(_classify_polling_error(error), error)


async def _run_polling_supervisor(
    *,
    start_polling: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event,
    logger: logging.Logger,
    runtime_health: TgbotHealthReporter,
    pause_invalid_configuration: Callable[[BaseException], Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PollingTerminationClass:
    network_backoff = 1.0
    while not stop_event.is_set():
        _set_runtime_health(
            runtime_health,
            TgbotRuntimeStatus.POLLING_STARTING,
            "polling_attempt_start",
        )
        polling = asyncio.create_task(
            start_polling(),
            name="lumen-polling",
        )
        termination = await _wait_for_polling_termination(polling, stop_event)
        classification = termination.classification
        if classification in {
            PollingTerminationClass.NORMAL_STOP,
            PollingTerminationClass.NORMAL_CANCEL,
        }:
            _set_runtime_health(
                runtime_health,
                TgbotRuntimeStatus.STOPPING,
                "polling_stop_requested",
            )
            logger.info("polling terminated class=%s", classification.value)
            return classification

        assert termination.error is not None
        _record_polling_failure(classification)
        if classification is PollingTerminationClass.INVALID_CONFIGURATION:
            _set_runtime_health(
                runtime_health,
                TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
                "polling_invalid_configuration",
            )
            logger.error(
                "polling terminated class=%s error=%s",
                classification.value,
                termination.error,
            )
            await pause_invalid_configuration(termination.error)
            return classification
        if classification is PollingTerminationClass.RECOVERABLE_NETWORK:
            _set_runtime_health(
                runtime_health,
                TgbotRuntimeStatus.POLLING_BACKOFF,
                "polling_network_backoff",
            )
            logger.warning(
                "polling terminated class=%s error=%s; retry in %.1fs",
                classification.value,
                termination.error,
                network_backoff,
            )
            if await _sleep_or_stop(
                stop_event,
                network_backoff,
                sleep=sleep,
            ):
                return PollingTerminationClass.NORMAL_STOP
            network_backoff = min(
                network_backoff * 2,
                _POLLING_NETWORK_BACKOFF_MAX_SEC,
            )
            continue

        _set_runtime_health(
            runtime_health,
            TgbotRuntimeStatus.FAILED,
            "polling_unknown_failure",
        )
        logger.error(
            "polling terminated class=%s",
            classification.value,
            exc_info=(
                type(termination.error),
                termination.error,
                termination.error.__traceback__,
            ),
        )
        raise PollingSupervisorFailure(
            f"Telegram polling failed: {termination.error}"
        ) from termination.error
    return PollingTerminationClass.NORMAL_STOP


async def _runtime_config_has_replacement_token(
    api: LumenApi,
    rejected_token: str,
) -> bool:
    access_cfg = await api.get_access_config()
    if not bool(access_cfg.get("bot_enabled", True)):
        return False
    cfg = await api.get_runtime_config(avoid=[])
    token = (cfg.get("bot_token") or settings.telegram_bot_token).strip()
    return bool(token) and token != rejected_token


def _start_metrics_server() -> object | None:
    if settings.telegram_metrics_port <= 0:
        return None
    server, _thread = start_http_server(
        settings.telegram_metrics_port,
        addr=settings.telegram_metrics_host,
    )
    return server


def _stop_metrics_server(server: object | None) -> None:
    if server is None:
        return
    shutdown = getattr(server, "shutdown", None)
    server_close = getattr(server, "server_close", None)
    if callable(shutdown):
        shutdown()
    if callable(server_close):
        server_close()


async def _run_control_listener(
    stop_event: asyncio.Event,
    *,
    sleep_or_stop: Callable[[asyncio.Event, float], Awaitable[bool]] = _sleep_or_stop,
) -> None:
    """订阅 admin 通道；收到 restart 命令则 clean-exit，systemd Restart=always 会拉起。

    任何错误（包括 Redis 抖动）都不应该让进程退出；记 warning 后继续重连。
    """
    from redis import asyncio as aioredis

    logger = logging.getLogger("lumen-tgbot.control")
    backoff = 1.0
    consecutive_failures = 0
    # control 通道丢消息只影响管理面（一键重启），重要性低于 listener；上限 60s
    # 重试，连续 50 次失败后告警一次便于排查，但继续重试不退出。
    backoff_max = 60.0
    alert_threshold = 50
    while not stop_event.is_set():
        pubsub = None
        client = None
        try:
            client = aioredis.from_url(settings.redis_url, decode_responses=False)
            pubsub = client.pubsub()
            await pubsub.subscribe(_CONTROL_CHANNEL)
            logger.info("control: subscribed to %s", _CONTROL_CHANNEL)
            backoff = 1.0
            consecutive_failures = 0
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
            consecutive_failures += 1
            level = (
                logging.ERROR
                if consecutive_failures >= alert_threshold
                else logging.WARNING
            )
            logger.log(
                level,
                "control listener error: %s; reconnect in %.1fs (failures=%d)",
                exc,
                backoff,
                consecutive_failures,
            )
            if await sleep_or_stop(stop_event, backoff):
                return
            backoff = min(backoff * 2, backoff_max)
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


def _install_stop_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows fallback


async def _cancel_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


async def _runtime_config_is_runnable(api: LumenApi) -> bool:
    access_cfg = await api.get_access_config()
    if not bool(access_cfg.get("bot_enabled", True)):
        return False
    if settings.telegram_bot_token.strip():
        return True

    cfg = await api.get_runtime_config(avoid=[])
    bot_enabled = bool(cfg.get("bot_enabled", True))
    bot_token = (cfg.get("bot_token") or "").strip()
    return bot_enabled and bool(bot_token)


async def _run_paused_config_refresh(
    stop_event: asyncio.Event,
    logger: logging.Logger,
    recovery_check: Callable[[], Awaitable[bool]],
    *,
    refresh_interval_sec: float,
) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=refresh_interval_sec,
            )
            return
        except TimeoutError:
            pass

        try:
            if await recovery_check():
                logger.info(
                    "runtime configuration is runnable; exiting paused state "
                    "for supervisor restart"
                )
                stop_event.set()
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "paused runtime-config refresh failed: %s; retry in %.1fs",
                exc,
                refresh_interval_sec,
            )


async def _pause_until_restart_or_stop(
    logger: logging.Logger,
    diagnostic: str,
    *,
    level: int,
    recovery_check: Callable[[], Awaitable[bool]] | None = None,
    refresh_interval_sec: float = _PAUSED_CONFIG_REFRESH_INTERVAL_SEC,
) -> None:
    """Stay active until stopped, restarted, or runtime configuration recovers."""
    stop_event = asyncio.Event()
    _install_stop_signal_handlers(stop_event)
    tasks = [
        asyncio.create_task(
            _run_control_listener(stop_event),
            name="lumen-control-paused",
        )
    ]
    if recovery_check is not None:
        tasks.append(
            asyncio.create_task(
                _run_paused_config_refresh(
                    stop_event,
                    logger,
                    recovery_check,
                    refresh_interval_sec=refresh_interval_sec,
                ),
                name="lumen-config-refresh-paused",
            )
        )
    logger.log(
        level,
        "%s; bot polling is paused until configuration recovery, "
        "an admin restart, or service stop",
        diagnostic,
    )
    try:
        await stop_event.wait()
    finally:
        for task in tasks:
            await _cancel_task(task)


async def _amain(runtime_health: TgbotHealthReporter) -> None:
    _setup_logging()
    logger = logging.getLogger("lumen-tgbot")

    if not settings.telegram_bot_shared_secret.strip():
        _set_runtime_health(
            runtime_health,
            TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
            "missing_shared_secret",
        )
        await _pause_until_restart_or_stop(
            logger,
            "configuration error: TELEGRAM_BOT_SHARED_SECRET is empty",
            level=logging.ERROR,
        )
        return
    async with AsyncExitStack() as stack:
        api = LumenApi()
        stack.push_async_callback(api.aclose)
        stack.push_async_callback(tracker.aclose)
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
            if exc.status in {401, 403}:
                _set_runtime_health(
                    runtime_health,
                    TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
                    "shared_secret_rejected",
                )
                await _pause_until_restart_or_stop(
                    logger,
                    "configuration error: TELEGRAM_BOT_SHARED_SECRET was "
                    "rejected by lumen-api",
                    level=logging.ERROR,
                )
                return
            logger.warning(
                "runtime-config load failed (will use env fallbacks): %s", exc
            )

        # bootstrap fallbacks
        if not bot_token:
            bot_token = settings.telegram_bot_token
        if not initial_proxy_url:
            initial_proxy_url = settings.telegram_proxy_url.strip()
        initial_proxy_url = normalize_proxy_url(initial_proxy_url)

        if not bot_enabled:
            _set_runtime_health(
                runtime_health,
                TgbotRuntimeStatus.PAUSED_INTENTIONAL,
                "runtime_disabled",
            )
            await _pause_until_restart_or_stop(
                logger,
                "telegram.bot_enabled=0 in runtime configuration",
                level=logging.INFO,
                recovery_check=lambda: _runtime_config_is_runnable(api),
            )
            return
        if not bot_token:
            _set_runtime_health(
                runtime_health,
                TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
                "empty_bot_token",
            )
            await _pause_until_restart_or_stop(
                logger,
                "configuration error: bot token is empty in runtime configuration and "
                "TELEGRAM_BOT_TOKEN",
                level=logging.ERROR,
                recovery_check=lambda: _runtime_config_is_runnable(api),
            )
            return

        if initial_proxy_url:
            logger.info(
                "outbound proxy: name=%s url=%s",
                proxy_mgr.current_name or "(env fallback)",
                _redact_proxy(initial_proxy_url),
            )
        else:
            logger.warning(
                "no outbound proxy configured; TG calls will go direct "
                "(likely fail in CN)"
            )

        session = (
            FailoverSession(proxy_mgr, proxy=initial_proxy_url)
            if initial_proxy_url
            else None
        )
        defaults = DefaultBotProperties(parse_mode=None)
        try:
            bot = (
                Bot(token=bot_token, default=defaults, session=session)
                if session is not None
                else Bot(token=bot_token, default=defaults)
            )
        except TokenValidationError as exc:
            _record_polling_failure(PollingTerminationClass.INVALID_CONFIGURATION)
            _set_runtime_health(
                runtime_health,
                TgbotRuntimeStatus.PAUSED_CONFIGURATION_ERROR,
                "invalid_bot_token",
            )
            logger.error(
                "polling initialization failed class=%s error=%s",
                PollingTerminationClass.INVALID_CONFIGURATION.value,
                exc,
            )
            await _pause_until_restart_or_stop(
                logger,
                f"invalid Telegram bot token: {exc}",
                level=logging.ERROR,
                recovery_check=lambda: _runtime_config_has_replacement_token(
                    api,
                    bot_token,
                ),
            )
            return
        stack.push_async_callback(bot.session.close)

        # FSM storage 优先 Redis（进程重启 /new 菜单状态不丢）；连接失败时
        # MemoryStorage 只维持非付费交互。generation handler 在每次付费提交前
        # 必须写独立 Redis journal，journal 不可用会拒绝创建任务。
        fsm_redis: aioredis.Redis | None = None
        storage: MemoryStorage | RedisStorage
        try:
            fsm_redis = aioredis.from_url(
                settings.redis_url,
                decode_responses=False,
            )
            stack.push_async_callback(fsm_redis.aclose)
            await fsm_redis.ping()
            storage = RedisStorage(
                redis=fsm_redis,
                key_builder=DefaultKeyBuilder(prefix="tg:bot:fsm", with_bot_id=True),
                state_ttl=_FSM_STATE_TTL_SEC,
                data_ttl=_FSM_STATE_TTL_SEC,
            )
            logger.info("fsm: using RedisStorage (ttl=%ds)", _FSM_STATE_TTL_SEC)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fsm: redis unavailable (%s); fallback to MemoryStorage",
                exc,
            )
            if fsm_redis is not None:
                try:
                    await fsm_redis.aclose()
                except Exception:  # noqa: BLE001
                    pass
                fsm_redis = None
            storage = MemoryStorage()
        dp = Dispatcher(storage=storage)

        # DI：handler 用 `api: LumenApi` 注解就能拿到
        dp["api"] = api
        dp["generation_runtime"] = GenerationRuntime()

        # 全局准入：拒非私聊 + 可选 TG user_id 白名单
        gate = AccessGate(api)
        dp.message.middleware(gate)
        dp.callback_query.middleware(gate)

        dp.include_router(build_root_router())

        stop_event = asyncio.Event()
        listener_task = asyncio.create_task(
            run_listener(bot, api, stop_event),
            name="lumen-listener",
        )
        stack.push_async_callback(_cancel_task, listener_task)
        control_task = asyncio.create_task(
            _run_control_listener(stop_event),
            name="lumen-control",
        )
        stack.push_async_callback(_cancel_task, control_task)
        stack.callback(stop_event.set)

        _install_stop_signal_handlers(stop_event)
        logger.info("starting polling; api=%s", settings.lumen_api_base)

        async def start_polling() -> None:
            await bot.me()
            _set_runtime_health(
                runtime_health,
                TgbotRuntimeStatus.POLLING,
                "telegram_identity_verified",
            )
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                close_bot_session=False,
            )

        async def pause_invalid_configuration(error: BaseException) -> None:
            stop_event.set()
            for task in (listener_task, control_task):
                await _cancel_task(task)
            await _pause_until_restart_or_stop(
                logger,
                f"invalid Telegram polling configuration: {error}",
                level=logging.ERROR,
                recovery_check=lambda: _runtime_config_has_replacement_token(
                    api,
                    bot_token,
                ),
            )

        await _run_polling_supervisor(
            start_polling=start_polling,
            stop_event=stop_event,
            logger=logger,
            runtime_health=runtime_health,
            pause_invalid_configuration=pause_invalid_configuration,
        )


def main() -> None:
    async def run() -> None:
        runtime_health = TgbotHealthReporter.from_environment()
        await runtime_health.start()
        metrics_server = None
        failed = False
        try:
            metrics_server = _start_metrics_server()
            await _amain(runtime_health)
        except BaseException:
            failed = True
            raise
        finally:
            try:
                _stop_metrics_server(metrics_server)
            finally:
                await runtime_health.stop(
                    (
                        TgbotRuntimeStatus.FAILED
                        if failed
                        else TgbotRuntimeStatus.STOPPING
                    ),
                    "unhandled_process_failure" if failed else "process_exit",
                )

    asyncio.run(run())


if __name__ == "__main__":
    main()
