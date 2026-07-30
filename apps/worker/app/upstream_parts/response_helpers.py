"""Responses validation and image result extraction helpers."""

from __future__ import annotations

import logging
from typing import Any

from lumen_core.constants import GenerationErrorCode as EC

from ..provider_runtime.errors import UpstreamError
from ..provider_runtime.upstream_services import (
    ImageUpstreamRuntime,
    resolve_image_upstream_services,
)
from .image_execution import ImageRequestContext

logger = logging.getLogger(__name__)
RESPONSES_SUCCESS_TERMINAL_EVENTS = frozenset({"response.completed", "response.done"})
RESPONSES_ERROR_TERMINAL_EVENTS = frozenset(
    {"response.failed", "response.incomplete", "error"}
)


def runtime_services(runtime: ImageUpstreamRuntime | None):
    return resolve_image_upstream_services(runtime)


def is_responses_success_terminal(event_type: Any) -> bool:
    return (
        isinstance(event_type, str) and event_type in RESPONSES_SUCCESS_TERMINAL_EVENTS
    )


def is_responses_error_terminal(event_type: Any) -> bool:
    return isinstance(event_type, str) and event_type in RESPONSES_ERROR_TERMINAL_EVENTS


def validate_responses_body(body: dict[str, Any]) -> None:
    """请求 schema 预校验——参考 probe report §2.C1 的硬约束：

    - `instructions` 必须存在且为字符串（可为空串；完全缺失 = 上游 400 `Instructions are required`）
    - `input` 必须是 list（不是 = 上游 400 `Input must be a list`）
    - 有 `tools` 时，必须同时带 `parallel_tool_calls` / `tool_choice`，否则上游可能 4xx

    所有错误都按 4xx terminal 处理（重试无意义）。
    """
    instructions = body.get("instructions")
    if not isinstance(instructions, str):
        # 防御性兜底：调用方组 body 时若漏掉 instructions（None / 缺失 / 非 string），
        # 注入空串保持字段存在；图像路径标准模板用 "" 与 Codex CLI 一致，不影响上游接受。
        body["instructions"] = ""
        logger.warning(
            "upstream body missing instructions string; injected empty fallback"
        )
    input_field = body.get("input")
    if not isinstance(input_field, list):
        raise UpstreamError(
            "upstream body.input must be a list",
            status_code=400,
            error_code=EC.INVALID_REQUEST_ERROR.value,
            payload={"input_type": type(input_field).__name__},
        )
    tools = body.get("tools")
    if tools:
        if not isinstance(tools, list):
            raise UpstreamError(
                "upstream body.tools must be a list",
                status_code=400,
                error_code=EC.INVALID_REQUEST_ERROR.value,
            )
        if "tool_choice" not in body:
            raise UpstreamError(
                "upstream body.tools requires tool_choice",
                status_code=400,
                error_code=EC.INVALID_REQUEST_ERROR.value,
            )
        if "parallel_tool_calls" not in body:
            # 上游对该字段在多 tool 场景下要求显式给出；保守默认 False（图像 / chat 场景实际都不并行）。
            body["parallel_tool_calls"] = False


def stable_sort_tools(tools: list[Any]) -> list[Any]:
    """按工具 name（缺省回退 type）排序——保证 prompt cache 前缀稳定。

    上游 prompt cache 命中要求请求体逐字节相同；tools 数组顺序抖动会让 cache miss。
    本函数不会修改输入 list，返回新副本；非 dict / 没有 name & type 的元素排在尾部。
    """
    return sorted(
        tools,
        key=lambda tool: (
            (
                0,
                str(tool.get("name") or tool.get("type") or ""),
            )
            if isinstance(tool, dict) and (tool.get("name") or tool.get("type"))
            else (1, "")
        ),
    )


def parse_error(payload: dict[str, Any], status_code: int) -> UpstreamError:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or "upstream_error"
        msg = err.get("message") or "upstream error"
        return UpstreamError(
            msg, status_code=status_code, error_code=code, payload=payload
        )
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail:
        return UpstreamError(
            detail,
            status_code=status_code,
            error_code=EC.UPSTREAM_ERROR.value,
            payload=payload,
        )
    return UpstreamError(
        f"upstream http {status_code}",
        status_code=status_code,
        error_code=EC.UPSTREAM_ERROR.value,
        payload=payload if isinstance(payload, dict) else {},
    )


def with_error_context(
    exc: UpstreamError,
    *,
    path: str,
    method: str,
    url: str,
) -> UpstreamError:
    payload = dict(exc.payload)
    payload.setdefault("path", path)
    payload.setdefault("method", method)
    payload.setdefault("url", url)
    exc.payload = payload
    return exc


async def extract_image_results(
    payload: Any,
    status_code: int,
    *,
    proxy_url: str | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> list[tuple[str, str | None]]:
    runtime = runtime or (
        request_context.upstream_runtime if request_context is not None else None
    )
    services = runtime_services(runtime)
    fetch_image_url_as_bytes = services.direct.fetch_image_url_as_bytes
    if request_context is not None:

        async def fetch_image_url_as_bytes(
            image_url: str,
            *,
            proxy_url: str | None = None,
        ) -> bytes:
            return await services.direct.fetch_image_url_as_bytes(
                image_url,
                proxy_url=proxy_url,
                request_context=request_context,
            )

    return await services.direct.extract_image_results(
        payload,
        status_code,
        fetch_image_url_as_bytes=fetch_image_url_as_bytes,
        upstream_error_type=services.infrastructure.UpstreamError,
        bad_response_error_code=services.infrastructure.EC.BAD_RESPONSE.value,
        no_image_returned_error_code=(
            services.infrastructure.EC.NO_IMAGE_RETURNED.value
        ),
        proxy_url=proxy_url,
    )


async def extract_image_result(
    payload: Any,
    status_code: int,
    *,
    proxy_url: str | None = None,
    request_context: ImageRequestContext | None = None,
    runtime: ImageUpstreamRuntime | None = None,
) -> tuple[str, str | None]:
    runtime = runtime or (
        request_context.upstream_runtime if request_context is not None else None
    )
    services = runtime_services(runtime)
    kwargs: dict[str, Any] = {"proxy_url": proxy_url}
    if request_context is not None:
        kwargs["request_context"] = request_context
    return (
        await services.core.extract_image_results(
            payload,
            status_code,
            **kwargs,
        )
    )[0]


def extract_response_image_b64(
    event: dict[str, Any],
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    services = runtime_services(runtime)
    return services.responses.extract_response_image_b64(event)


def extract_response_revised_prompt(
    event: dict[str, Any],
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    services = runtime_services(runtime)
    return services.responses.extract_response_revised_prompt(event)


def b64_value_if_str(
    value: Any,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    services = runtime_services(runtime)
    return services.responses.b64_value_if_str(value)


def extract_image_b64_from_payload(
    payload: Any,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> str | None:
    services = runtime_services(runtime)
    return services.responses.extract_image_b64_from_payload(
        payload,
        b64_value_if_str=services.core.b64_value_if_str,
    )


def extract_image_billable_count(
    payload: Any,
    *,
    runtime: ImageUpstreamRuntime | None = None,
) -> int | None:
    services = runtime_services(runtime)
    return services.responses.extract_image_billable_count(payload)
