"""Lumen 共享核心：供 apps/api 与 apps/worker 同时消费，避免契约漂移。

暴露：
- models: SQLAlchemy ORM 模型（DESIGN §4）
- schemas: Pydantic I/O schemas（DESIGN §5 请求/响应）
- sizing: 尺寸解析器（DESIGN §7.2 + 附录 A）
- constants: 共享常量（枚举、队列名、事件名）
- context_window: 上下文窗口预算与 token 估算
- providers: Provider Pool 解析与旧 env 兼容 fallback
- runtime_settings: 可调系统设置元数据与校验
"""

__version__ = "1.0.2"

from . import (  # noqa: F401
    constants,
    context_window,
    image_signing,
    models,
    providers,
    runtime_settings,
    schemas,
    sizing,
    utils,
)
