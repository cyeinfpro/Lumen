"""arq worker entrypoint and lifecycle hooks.

Task functions receive durable database task IDs through the arq Redis queue.
Startup initializes observability, tokenization, and billing-cache services;
shutdown drains their process resources. Business state remains authoritative in
PostgreSQL, while Redis provides dispatch, leases, counters, and event delivery.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from arq import func
from arq.connections import RedisSettings
from arq.cron import cron

from lumen_core.context_window import warm_tiktoken

from .config import settings
from .jobs.upstream_probe import probe_upstream
from .observability import (
    init_otel,
    init_sentry,
    start_metrics_server,
    stop_metrics_server,
)
from .provider_pool import probe_providers
from .services import billing_cache
from .storage import storage
from .tasks import auto_title as auto_title_tasks
from .tasks import byok_retention as byok_retention_tasks
from .tasks import canvas_execution_reconcile as canvas_reconcile_tasks
from .tasks import completion as completion_tasks
from .tasks import context_summary as context_summary_tasks
from .tasks import generation as generation_tasks
from .tasks import memory_extraction as memory_tasks
from .tasks import outbox as outbox_tasks
from .tasks import storyboard_assembly as storyboard_assembly_tasks
from .tasks import video_generation as video_generation_tasks
from .tasks import volcano_assets as volcano_asset_tasks
from .upstream import close_client, validate_effective_image_job_configuration

_startup_logger = logging.getLogger(__name__)
_PROVIDER_CRON_TIMEOUT_S = 30.0


async def _cleanup_resource(name: str, cleanup: Callable[[], Any]) -> None:
    try:
        result = cleanup()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001
        _startup_logger.warning(
            "worker cleanup failed resource=%s", name, exc_info=True
        )


async def _cleanup_resources() -> None:
    await _cleanup_resource("billing_cache", billing_cache.shutdown)
    await _cleanup_resource("upstream_client", close_client)
    await _cleanup_resource("metrics_server", stop_metrics_server)


async def _on_startup(ctx: dict) -> None:  # type: ignore[type-arg]
    """arq WorkerSettings.on_startup 钩子：初始化观测层（幂等）。"""
    try:
        await validate_effective_image_job_configuration()
        storage.ensure_ready()
        init_sentry(
            settings.sentry_dsn,
            settings.sentry_environment or settings.app_env,
            settings.sentry_traces_sample_rate,
        )
        init_otel(settings.otel_service_name, settings.otel_exporter_endpoint)
        start_metrics_server(settings.worker_metrics_port, settings.worker_metrics_host)
        # P1-4: 预热 tiktoken o200k_base encoding，避免首条请求承担 ~100-200 ms 加载耗时。
        # 失败不阻塞启动——count_tokens 内部会回落到 estimate_text_tokens。
        loaded = warm_tiktoken()
        _startup_logger.info("worker.tiktoken_warm loaded=%s", loaded)
        await billing_cache.configure(ctx.get("redis"))
    except Exception:
        _startup_logger.exception("worker startup failed; cleaning partial resources")
        await _cleanup_resources()
        raise


async def _on_shutdown(ctx: dict) -> None:  # type: ignore[type-arg]
    """arq WorkerSettings.on_shutdown 钩子：独立清理各项进程资源。"""
    await _cleanup_resources()


class WorkerSettings:
    # Redis
    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # Registered task entry points.
    functions = [
        generation_tasks.run_generation,
        video_generation_tasks.run_video_generation,
        video_generation_tasks.run_video_poll,
        storyboard_assembly_tasks.run_storyboard_assembly,
        completion_tasks.run_completion,
        canvas_reconcile_tasks.reconcile_canvas_execution,
        outbox_tasks.publish_outbox,
        auto_title_tasks.auto_title_conversation,
        context_summary_tasks.manual_compact_conversation,
        memory_tasks.memory_extract,
        memory_tasks.memory_reembed,
        func(
            volcano_asset_tasks.process_volcano_asset_operation,
            max_tries=1000,
        ),
    ]
    cron_jobs = (
        outbox_tasks.cron_jobs
        + canvas_reconcile_tasks.cron_jobs
        + video_generation_tasks.cron_jobs
        + [
            # provider probe 可能卡在 Redis、代理或上游 TCP；arq 的 cron timeout
            # 负责取消该 job，避免它占住 cron 槽位和 worker event loop。
            cron(
                probe_providers,
                second={0, 30},
                run_at_startup=False,
                timeout=_PROVIDER_CRON_TIMEOUT_S,
            ),
            cron(
                auto_title_tasks.reconcile_default_titles,
                minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
                run_at_startup=False,
            ),
            # 上游健康/schema 探针：每小时第 5 分钟一次，避开整点 reconcile_default_titles。
            # 故意不在启动时跑——避免 dev / CI 启动顺手烧 token。
            cron(
                probe_upstream,
                hour={i for i in range(24)},
                minute={5},
                run_at_startup=False,
            ),
            cron(
                memory_tasks.cleanup_memory,
                hour={3},
                minute={17},
                run_at_startup=False,
            ),
            cron(
                byok_retention_tasks.cleanup_byok_retention,
                hour={3},
                minute={27},
                run_at_startup=False,
            ),
            # last_used_at 批量 flush: 每分钟 0/30 秒各一次, 把 redis ZSET 累积的
            # 最近注入时间戳写回 user_memories, 避免主对话热路径每轮 N 次 UPDATE.
            cron(
                memory_tasks.flush_memory_last_used,
                second={0, 30},
                run_at_startup=False,
            ),
        ]
    )

    # Keep the arq process wide enough for the runtime image FIFO cap plus
    # cron/outbox jobs. The image queue still owns admission, so this only
    # prevents max_jobs from becoming the bottleneck when admins raise
    # image.generation_concurrency from system settings without restarting.
    max_jobs = 64
    # 4K 图生图（4K 升级后）最糟耗时：主链路 retry × 单次 ~8 min + 备链路 + 解码/落盘
    # 可达 ~20-25 min；给 1800s（30 min）留缓冲。普通小图/文生图远远跑不到这个上限。
    # 保持 > _RUN_GENERATION_TIMEOUT_S（1500s），让 task 自己 raise TimeoutError 释放 lease。
    job_timeout = 1800  # s
    keep_result = 3600

    # Startup hook：观测层 + metrics server
    on_startup = _on_startup
    on_shutdown = _on_shutdown
