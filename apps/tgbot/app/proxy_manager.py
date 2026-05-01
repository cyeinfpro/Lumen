"""Bot 出网代理管理：启动拉 runtime-config，发送出错自动 failover。

机制：
- 进程内一个 ProxyManager 单例，记 current_name + failed_names
- aiogram 的 session 是自定义 FailoverSession：包了 AiohttpSession，发请求时
  catch (TelegramNetworkError, aiohttp.ClientError) → 调 manager.failover() →
  manager 调 API report 失败 + 拿新 proxy URL → 直接 hot swap session._proxy →
  下一次 request 自动用新连接器
- 全部 proxy 都试过仍失败时让原 exception 抛出，handler 层应该走静默重试或丢弃

注意：aiogram 的 AiohttpSession.proxy setter 内部会把 _should_reset_connector
置 True，下一次 create_session() 会关闭旧的、用新参数重建。所以这一套整个
透明，handler 层不需要改任何代码。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiohttp import ClientError

from .api_client import ApiError, LumenApi

logger = logging.getLogger(__name__)


def _normalize_proxy_url(url: str) -> str:
    """aiogram 用 aiohttp_socks，python-socks 不认 `socks5h://`，只认 `socks5://`。
    runtime-config 返回的是 `socks5h://...`（h = remote DNS），把 h 去掉转成 `socks5://`。
    httpx 那边两种都接受，所以仅在 bot 入口做这一次归一就够了。
    """
    if url.startswith("socks5h://"):
        return "socks5://" + url[len("socks5h://") :]
    return url


class ProxyManager:
    def __init__(self, api: LumenApi) -> None:
        self._api = api
        self._lock = asyncio.Lock()
        self.current_name: str = ""
        self.current_url: str = ""
        self._failed_names: set[str] = set()
        self._session: "FailoverSession | None" = None
        # bot 启动时缓存的 runtime-config 其它字段（token/username/whitelist 等）
        self.config: dict[str, Any] = {}

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
            failed = self.current_name
            if failed:
                self._failed_names.add(failed)
                try:
                    await self._api.report_proxy(failed, success=False)
                except ApiError as exc:
                    logger.warning("report_proxy failed name=%s err=%s", failed, exc)

            try:
                cfg = await self._api.get_runtime_config(
                    avoid=sorted(self._failed_names)
                )
            except ApiError as exc:
                logger.error("failover get_runtime_config failed: %s", exc)
                return False
            self.config = cfg

            proxy = cfg.get("proxy")
            if not isinstance(proxy, dict) or not proxy.get("url"):
                logger.error(
                    "proxy pool exhausted (failed=%s); bot cannot send", self._failed_names
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
                "proxy failover → %s (failed=%s)", new_name, sorted(self._failed_names)
            )
            return True

    async def report_success(self) -> None:
        """周期性清掉 failed 集，给曾经故障的代理重新进入候选的机会。"""
        if self.current_name:
            try:
                await self._api.report_proxy(self.current_name, success=True)
            except ApiError:
                pass
        # 清掉 _failed_names 但保留 current（防止下次 failover 又选回它）
        async with self._lock:
            self._failed_names = {n for n in self._failed_names if n == self.current_name}


class FailoverSession(AiohttpSession):
    """aiohttp session 子类：发请求遇到网络错误时自动调 manager.failover() 重试一次。"""

    def __init__(self, manager: ProxyManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manager = manager
        manager.attach(self)

    async def make_request(self, bot, method, timeout=None):  # type: ignore[override]
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return await super().make_request(bot, method, timeout=timeout)
            except (TelegramNetworkError, ClientError) as exc:
                last_exc = exc
                logger.warning(
                    "tg request error attempt=%d via %s err=%r",
                    attempt + 1, self._manager.current_name, exc,
                )
                swapped = await self._manager.failover()
                if not swapped:
                    raise
        # 极端情况：3 次仍失败
        if last_exc is not None:
            raise last_exc
