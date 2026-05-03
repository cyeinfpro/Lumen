"""Lumen API 异步客户端。

所有 bot → api 调用统一从这里出，自动带：
- X-Bot-Token：service-to-service 共享密钥
- X-Telegram-Chat-Id：标识当前 TG 用户（除 /telegram/bind 走显式 chat_id 之外）

错误处理：把 4xx/5xx 包成 ApiError(code, message, status)，handler 层再翻成中文给用户。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# 单进程并发下载上限：4K PNG 可能十几 MB。allow burst（status_message edit + image
# fetch 同时多 task 并发）但又不至于把 socket / 磁盘 IO 排队卡死。
_DOWNLOAD_CONCURRENCY = 4
_download_sem = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)

# 下载前最低空闲磁盘门槛。低于此值直接拒绝下载，避免撑爆 /tmp 后整个 bot 崩。
_MIN_FREE_DISK_BYTES = 200 * 1024 * 1024  # 200 MB


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class LumenApi:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.lumen_api_base.rstrip("/"),
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"X-Bot-Token": settings.telegram_bot_shared_secret},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _hdr(self, chat_id: int | str | None) -> dict[str, str]:
        if chat_id is None:
            return {}
        return {"X-Telegram-Chat-Id": str(chat_id)}

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                raise ApiError(
                    code=str(err.get("code") or "unknown"),
                    message=str(err.get("message") or resp.text),
                    status=resp.status_code,
                )
        except ValueError:
            pass
        raise ApiError(code="http_error", message=resp.text or resp.reason_phrase, status=resp.status_code)

    async def bind(self, chat_id: int, code: str, tg_username: str | None) -> dict[str, Any]:
        resp = await self._client.post(
            "/telegram/bind",
            json={"chat_id": str(chat_id), "code": code, "tg_username": tg_username},
        )
        self._raise_for(resp)
        return resp.json()

    async def unbind(self, chat_id: int) -> None:
        resp = await self._client.post("/telegram/unbind", headers=self._hdr(chat_id))
        self._raise_for(resp)

    async def me(self, chat_id: int) -> dict[str, Any]:
        resp = await self._client.get("/telegram/me", headers=self._hdr(chat_id))
        self._raise_for(resp)
        return resp.json()

    async def create_generation(self, chat_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        # 生成是 enqueue，立即返回 generation_ids；本身很快。但 worker 4K 任务上限 1500s，
        # 这里只是创建，timeout 30s 足够。
        resp = await self._client.post(
            "/telegram/generations", json=payload, headers=self._hdr(chat_id)
        )
        self._raise_for(resp)
        return resp.json()

    async def get_generation(self, chat_id: int, gen_id: str) -> dict[str, Any]:
        resp = await self._client.get(
            f"/telegram/generations/{gen_id}", headers=self._hdr(chat_id)
        )
        self._raise_for(resp)
        return resp.json()

    async def enhance_prompt(self, chat_id: int, text: str) -> str:
        # enhance 内部要打上游 LLM，给 60s 余量；超时则回退给原文
        resp = await self._client.post(
            "/telegram/prompts/enhance",
            json={"text": text},
            headers=self._hdr(chat_id),
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._raise_for(resp)
        return str(resp.json().get("enhanced") or "").strip()

    async def get_runtime_config(self, avoid: list[str] | None = None) -> dict[str, Any]:
        """bot bootstrap / failover：拿 bot 配置 + pool 选出来的 proxy。"""
        params: dict[str, str] = {}
        if avoid:
            params["avoid"] = ",".join(avoid)
        resp = await self._client.get(
            "/telegram/runtime-config",
            params=params,
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        self._raise_for(resp)
        return resp.json()

    async def report_proxy(self, name: str, *, success: bool = False) -> dict[str, Any]:
        resp = await self._client.post(
            "/telegram/proxy/report",
            json={"name": name, "success": success},
            timeout=httpx.Timeout(5.0, connect=3.0),
        )
        self._raise_for(resp)
        return resp.json()

    async def list_tasks(self, chat_id: int, limit: int = 10) -> dict[str, Any]:
        resp = await self._client.get(
            "/telegram/tasks",
            headers=self._hdr(chat_id),
            params={"limit": limit},
        )
        self._raise_for(resp)
        return resp.json()

    async def download_image_to_file(
        self, chat_id: int, image_id: str
    ) -> tuple[Path, str, int]:
        """流式下载到磁盘临时文件。返回 (path, mime, size_bytes)。

        4K PNG 可能十几 MB，多张同时入内存会让 bot 进程吃满。落盘后用 FSInputFile
        发送，aiogram 内部自己读 + stream up。caller 发完务必 unlink()。

        韧性保护：
        - 全局 _download_sem 限并发，避免 batch 任务一次性下 16 张把 socket / IO 排满
        - 下载前 shutil.disk_usage 检查 free，低于 _MIN_FREE_DISK_BYTES 直接拒绝，
          避免把 /tmp 撑爆导致整个 bot 进程后续操作（FSM redis 写入 / 日志）连带崩
        """
        tmp_root = (settings.download_tmp_dir or "").strip() or tempfile.gettempdir()
        Path(tmp_root).mkdir(parents=True, exist_ok=True)
        try:
            usage = shutil.disk_usage(tmp_root)
        except OSError as exc:
            logger.warning("disk_usage check failed dir=%s err=%s", tmp_root, exc)
        else:
            if usage.free < _MIN_FREE_DISK_BYTES:
                raise ApiError(
                    code="disk_full",
                    message=(
                        f"临时目录空间不足（剩 {usage.free // (1024 * 1024)} MB），"
                        "请稍后再试或联系管理员清理。"
                    ),
                    status=507,
                )
        path = Path(tmp_root) / f"lumen-{image_id[:12]}-{uuid.uuid4().hex[:8]}.bin"
        size = 0
        mime = "image/jpeg"
        async with _download_sem:
            try:
                async with self._client.stream(
                    "GET",
                    f"/telegram/images/{image_id}/binary",
                    headers=self._hdr(chat_id),
                ) as resp:
                    if not resp.is_success:
                        await resp.aread()
                        self._raise_for(resp)
                    mime = resp.headers.get("content-type", "image/jpeg")
                    with path.open("wb") as fp:
                        async for chunk in resp.aiter_bytes():
                            fp.write(chunk)
                            size += len(chunk)
            except Exception:
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass
                raise
        return path, mime, size
