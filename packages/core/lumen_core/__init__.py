"""Lumen 共享核心：供 apps/api 与 apps/worker 同时消费，避免契约漂移。

暴露：
- models: SQLAlchemy ORM 模型（DESIGN §4）
- schemas: Pydantic I/O schemas（DESIGN §5 请求/响应）
- sizing: 尺寸解析器（DESIGN §7.2 + 附录 A）
- constants: 共享常量（枚举、队列名、事件名）
- context_window: 上下文窗口预算与 token 估算
- providers: Provider Pool 解析与旧 env 兼容 fallback
- pricing: cache-aware token usage and cost breakdown helpers
- runtime_settings: 可调系统设置元数据与校验
- chat_tools: chat tool status normalization
"""

__version__ = "1.2.170"

from importlib import import_module as _import_module
from typing import TYPE_CHECKING as _TYPE_CHECKING

# Preserve historical exports without loading ORM/provider modules for callers
# that only need a version or a small, independent utility.
__all__ = (
    "agent_capability",
    "agent_events",
    "canvas",
    "canvas_models",
    "canvas_schemas",
    "capacity_leases",
    "chat_tools",
    "constants",
    "context_window",
    "image_signing",
    "models",
    "pricing",
    "pricing_fallback",
    "pricing_resolver",
    "providers",
    "runtime_settings",
    "schemas",
    "sizing",
    "sse_durable",
    "storage_capacity",
    "utils",
    "video_billing",
    "video_providers",
    "volcano_assets",
)

if _TYPE_CHECKING:
    from . import (  # noqa: F401
        agent_capability,
        agent_events,
        canvas,
        canvas_models,
        canvas_schemas,
        capacity_leases,
        chat_tools,
        constants,
        context_window,
        image_signing,
        models,
        pricing,
        pricing_fallback,
        pricing_resolver,
        providers,
        runtime_settings,
        schemas,
        sizing,
        sse_durable,
        storage_capacity,
        utils,
        video_billing,
        video_providers,
        volcano_assets,
    )


def __getattr__(name: str):
    if name in __all__:
        # importlib synchronizes and binds submodules on this package; avoid a
        # second mutable cache or an alternate SQLAlchemy model registry.
        return _import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
