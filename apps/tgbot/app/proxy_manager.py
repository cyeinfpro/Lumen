"""Bot 出网代理管理：启动拉 runtime-config，发送出错自动 failover。

机制：
- 进程内一个 ProxyManager 单例，记 current_name + failed_names 本地冷却
- aiogram 的 session 是自定义 FailoverSession：包了 AiohttpSession，发请求时
  catch (TelegramNetworkError, aiohttp.ClientError) → 调 manager.failover() →
  manager 调 API report 失败 + 拿新 proxy URL → 直接 hot swap session._proxy →
  下一次 request 自动用新连接器
- 全部 proxy 都试过仍失败时让原 exception 抛出，handler 层应该走静默重试或丢弃
- 成功路径走 report_success：节流到 _SUCCESS_REPORT_INTERVAL_SEC（默认 60s）一次，
  调 API 清服务端 cooldown + 清本进程当前代理的失败记录。其它失败代理靠本地
  TTL 重新进入候选，避免长跑 bot 在全池短暂故障后把所有代理永久放进 avoid。

注意：aiogram 的 AiohttpSession.proxy setter 内部会把 _should_reset_connector
置 True，下一次 create_session() 会关闭旧的、用新参数重建。所以这一套整个
透明，handler 层不需要改任何代码。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiohttp import ClientError

from .api_client import ApiError, LumenApi

logger = logging.getLogger(__name__)

# 节流：成功上报最低间隔。TG bot QPS 远高于这个，没必要每发一条都打 API。
_SUCCESS_REPORT_INTERVAL_SEC = 60.0
_FAILED_NAME_COOLDOWN_SEC = 60.0


def _normalize_proxy_url(url: str) -> str:
    """aiogram 用 aiohttp_socks，python-socks 不认 `socks5h://`，只认 `socks5://`。
    runtime-config 返回的是 `socks5h://...`（h = remote DNS），把 h 去掉转成 `socks5://`。
    httpx 那边两种都接受，所以仅在 bot 入口做这一次归一就够了。
    """
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("socks5h://"):
        value = "socks5://" + value[len("socks5h://") :]
    parts = urlsplit(value)
    if parts.scheme and parts.scheme.lower() not in {"socks4", "socks5", "http", "https"}:
        logger.warning("unsupported telegram proxy scheme: %s", parts.scheme)
        return ""
    return value


class ProxyManager:
    def __init__(self, api: LumenApi) -> None:
        self._api = api
        self._lock = asyncio.Lock()
        self.current_name: str = ""
        self.current_url: str = ""
        self._failed_names: dict[str, float] = {}
        self._session: "FailoverSession | None" = None
        # bot 启动时缓存的 runtime-config 其它字段（token/username/whitelist 等）
        self.config: dict[str, Any] = {}
        # 上次 report_success 的 monotonic 时间戳；0 = 从未上报
        self._last_success_report: float = 0.0

    def _active_failed_names_locked(self, now: float) -> list[str]:
        expired = [name for name, until in self._failed_names.items() if until <= now]
        for name in expired:
            self._failed_names.pop(name, None)
        return sorted(self._failed_names)

    def attach(self, session: "FailoverSession") -> None:
        self._session = session

    async def initial_load(self) -> dict[str, Any]:
        cfg = await self._api.get_runtime_config(avoid=[])
        self.config = cfg
        proxy = cfg.get("proxy")
        if proxy and isinstance(proxy, dict):
            self.current_name = str(proxy.get("name") or "")
            self.current_url = _normalize_proxy_url(str(proxy.get("url") or ""))
        return cfg

    async def failover(self) -> bool:
        """切换到下一个 proxy。返回 True 代表已切换；False 代表无可用代理。"""
        async with self._lock:
            now = time.monotonic()
            failed = self.current_name
            if failed:
                self._failed_names[failed] = now + _FAILED_NAME_COOLDOWN_SEC
                try:
                    await self._api.report_proxy(failed, success=False)
                except ApiError as exc:
                    logger.warning("report_proxy failed name=%s err=%s", failed, exc)

            try:
                cfg = await self._api.get_runtime_config(
                    avoid=self._active_failed_names_locked(time.monotonic())
                )
            except ApiError as exc:
                logger.error("failover get_runtime_config failed: %s", exc)
                return False
            self.config = cfg

            proxy = cfg.get("proxy")
            if not isinstance(proxy, dict) or not proxy.get("url"):
                logger.error(
                    "proxy pool exhausted (failed=%s); bot cannot send",
                    self._active_failed_names_locked(time.monotonic()),
                )
                return False

            new_name = str(proxy.get("name") or "")
            new_url = _normalize_proxy_url(str(proxy.get("url") or ""))
            if new_name == self.current_name:
                # 没换到新的，避免空转
                return False

            self.current_name = new_name
            self.current_url = new_url
            if self._session is not None:
                self._session.proxy = new_url  # 触发 _should_reset_connector
            logger.info(
                "proxy failover → %s (failed=%s)",
                new_name,
                self._active_failed_names_locked(time.monotonic()),
            )
            return True

    async def report_success(self) -> None:
        """周期性上报当前 proxy 成功，给服务端 cooldown 复位机会。

        节流到 _SUCCESS_REPORT_INTERVAL_SEC：FailoverSession 每个成功请求都会调用，
        但实际打 API 最多每分钟一次，避免高频 TG 流量打爆 lumen-api。
        """
        now = time.monotonic()
        async with self._lock:
            self._active_failed_names_locked(now)
            if now - self._last_success_report < _SUCCESS_REPORT_INTERVAL_SEC:
                return
            self._last_success_report = now
            current_name = self.current_name
            self._failed_names.pop(current_name, None)
        if current_name:
            try:
                await self._api.report_proxy(current_name, success=True)
            except ApiError:
                pass


class FailoverSession(AiohttpSession):
    """aiohttp session 子类：发请求遇到网络错误时自动调 manager.failover() 重试一次。"""

    def __init__(self, manager: ProxyManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manager = manager
        manager.attach(self)

    async def make_request(self, bot, method, timeout=None):  # type: ignore[override]
        last_exc: Exception | None = None
        method_name = str(
            getattr(method, "__api_method__", "")
            or getattr(method, "method", "")
            or method.__class__.__name__
        )
        retry_safe = method_name.lower().startswith("get")
        max_attempts = 3 if retry_safe else 1
        for attempt in range(max_attempts):
            try:
                result = await super().make_request(bot, method, timeout=timeout)
            except (TelegramNetworkError, ClientError) as exc:
                last_exc = exc
                logger.warning(
                    "tg request error attempt=%d via %s err=%r",
                    attempt + 1, self._manager.current_name, exc,
                )
                swapped = await self._manager.failover()
                if not swapped or not retry_safe:
                    raise
            else:
                # 通路 OK：节流地通知 manager 清失败缓存。失败本身不影响主路径。
                try:
                    await self._manager.report_success()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("report_success ignored err=%r", exc)
                return result
        # 极端情况：3 次仍失败
        if last_exc is not None:
            raise last_exc
