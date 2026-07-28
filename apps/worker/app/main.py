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
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import BusyLoadingError
from redis.exceptions import ConnectionError as RedisConnectionError

from lumen_core.context_window import warm_tiktoken
from lumen_core.storage_capacity import build_storage_capacity

from .config import settings
from .db import engine
from .jobs.upstream_probe import probe_upstream
from .observability import (
    bind_db_pool_metrics,
    init_otel,
    init_sentry,
    start_metrics_server,
    stop_metrics_server,
)
from .provider_pool import probe_providers
from .provider_runtime.upstream_services import ImageUpstreamRuntime
from . import runtime_settings
from .runtime import RuntimeLifecycle, WorkerRuntime
from .services import billing_cache
from .storage import storage
from .storage_writes import StorageWriteCoordinator
from .tasks import auto_title as auto_title_tasks
from .tasks import byok_retention as byok_retention_tasks
from .tasks import canvas_execution_reconcile as canvas_reconcile_tasks
from .tasks import context_summary as context_summary_tasks
from .tasks import generation as generation_tasks
from .tasks import memory_extraction as memory_tasks
from .tasks import outbox as outbox_tasks
from .tasks import storyboard_assembly as storyboard_assembly_tasks
from .tasks import volcano_assets as volcano_asset_tasks
from .tasks.completion_parts import entrypoints as completion_tasks
from .tasks.completion_parts.default_runtime import build_completion_runtime
from .tasks.generation_parts.default_runtime import build_generation_runtime
from .tasks.video_generation_parts import entrypoints as video_generation_tasks
from .tasks.video_generation_parts.default_runtime import (
    build_video_generation_runtime,
)
from .upstream_parts.entrypoints import (
    close_client,
    validate_effective_image_job_configuration,
)
from .upstream_parts.upstream_impl import build_image_upstream_runtime

_startup_logger = logging.getLogger(__name__)
_PROVIDER_CRON_TIMEOUT_S = 30.0

# RedisSettings.from_dsn 只解析 host/port/db/账号密码，其余全部落在 arq 的库
# 默认值上：conn_timeout=1s、retry_on_timeout=False、retry_on_error=None、
# max_connections=None。后果是主从切换或几百毫秒的网络抖动就让命令直接抛错、
# 在途任务批量失败，而连接池又没有上界（max_jobs=64 + cron 并发足以把
# Redis 的 maxclients 顶穿）。下面显式接管这几个旋钮。
_REDIS_CONN_TIMEOUT_S = 10
_REDIS_CONN_RETRIES = 10
_REDIS_CONN_RETRY_DELAY_S = 2
# max_jobs=64 + cron + 事件订阅，128 留一倍余量且远低于默认 maxclients。
_REDIS_MAX_CONNECTIONS = 128
_REDIS_COMMAND_RETRIES = 3


def build_redis_settings(dsn: str) -> RedisSettings:
    """DSN 解析出的连接信息 + 显式的重连/连接池策略。

    命令级重试只覆盖 ConnectionError / TimeoutError / BusyLoadingError：
    arq 不设置 socket_timeout，因此 TimeoutError 只可能来自建连阶段（命令还没
    发出去）；ConnectionError / BusyLoadingError 则是断连与 Redis 载入 RDB。
    worker 打到 Redis 的写操作要么是 CAS Lua（锁、slot）、要么按 task_id 定键
    （ZADD / SET NX / arq enqueue 的 job_id 去重），重放都是幂等的；真正的
    钱账在 PostgreSQL，不受这里的重试影响。

    刻意不做审计建议的「关键操作前 ping」：ping 与真正的命令之间仍有 TOCTOU
    窗口，却给每次操作加一个 RTT——命令级重试才是对症的做法。
    """
    resolved = RedisSettings.from_dsn(dsn)
    resolved.conn_timeout = _REDIS_CONN_TIMEOUT_S
    resolved.conn_retries = _REDIS_CONN_RETRIES
    resolved.conn_retry_delay = _REDIS_CONN_RETRY_DELAY_S
    resolved.max_connections = _REDIS_MAX_CONNECTIONS
    resolved.retry_on_timeout = True
    resolved.retry_on_error = [RedisConnectionError, BusyLoadingError]
    resolved.retry = Retry(
        ExponentialBackoff(cap=1.0, base=0.05),
        _REDIS_COMMAND_RETRIES,
    )
    return resolved


async def _cleanup_resource(name: str, cleanup: Callable[[], Any]) -> None:
    try:
        result = cleanup()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001
        _startup_logger.warning(
            "worker cleanup failed resource=%s", name, exc_info=True
        )


async def _cleanup_resources(ctx: dict[str, Any]) -> None:
    worker_runtime = ctx.pop("worker_runtime", None)
    if isinstance(worker_runtime, WorkerRuntime):
        for key, value in worker_runtime.context_values().items():
            if ctx.get(key) is value:
                ctx.pop(key, None)
        await worker_runtime.close()
        return

    partial_lifecycle = ctx.pop("_worker_runtime_lifecycle", None)
    if isinstance(partial_lifecycle, RuntimeLifecycle):
        await partial_lifecycle.close()
        return

    await _cleanup_resource("billing_cache", billing_cache.shutdown)
    cache = ctx.pop("runtime_settings_cache", None)
    if isinstance(cache, runtime_settings.RuntimeSettingsCache):
        await _cleanup_resource(
            "runtime_settings_cache",
            lambda: runtime_settings.shutdown_cache(cache),
        )
    generation_runtime = ctx.pop("generation_runtime", None)
    if isinstance(generation_runtime, generation_tasks.GenerationRuntime):
        await _cleanup_resource("generation_runtime", generation_runtime.shutdown)
    image_upstream_runtime = ctx.pop("image_upstream_runtime", None)
    ctx.pop("completion_runtime", None)
    ctx.pop("video_generation_runtime", None)
    ctx.pop("storage_write_coordinator", None)
    if isinstance(image_upstream_runtime, ImageUpstreamRuntime):
        await _cleanup_resource(
            "upstream_client",
            lambda: close_client(runtime=image_upstream_runtime),
        )
    await _cleanup_resource("metrics_server", stop_metrics_server)
    # 引擎最后释放：上面的清理动作（billing_cache flush、generation_runtime
    # 收尾）都可能还要用连接。不 dispose 会在 PG 侧留下 IDLE 连接，
    # 反复重启后耗尽 max_connections。
    await _cleanup_resource("db_engine", engine.dispose)


async def _on_startup(ctx: dict) -> None:  # type: ignore[type-arg]
    """arq WorkerSettings.on_startup 钩子：初始化观测层（幂等）。"""
    lifecycle = RuntimeLifecycle("worker", logger=_startup_logger)
    lifecycle.own("db_engine", engine.dispose)
    ctx["_worker_runtime_lifecycle"] = lifecycle
    try:
        runtime_settings_cache = runtime_settings.configure_cache()
        ctx["runtime_settings_cache"] = runtime_settings_cache
        lifecycle.own(
            "runtime_settings_cache",
            lambda: runtime_settings.shutdown_cache(runtime_settings_cache),
        )
        image_upstream_runtime = build_image_upstream_runtime()
        ctx["image_upstream_runtime"] = image_upstream_runtime
        lifecycle.own(
            "upstream_client",
            lambda: close_client(runtime=image_upstream_runtime),
        )
        await validate_effective_image_job_configuration(
            runtime=image_upstream_runtime,
        )
        storage.ensure_ready()
        configured_policy = settings.image_upload_capacity_degraded_policy.strip()
        degraded_policy = configured_policy or (
            "scaled_local"
            if settings.app_env.strip().lower()
            in {"dev", "development", "local", "test"}
            else "fail_closed"
        )
        storage_writes = StorageWriteCoordinator(
            storage=storage,
            capacity=build_storage_capacity(
                ctx["redis"],
                settings.storage_root,
                minimum_free_bytes=settings.minimum_storage_free_bytes,
                lease_ttl_seconds=settings.image_upload_lease_ttl_seconds,
                degraded_policy=degraded_policy,
            ),
            lease_ttl_seconds=settings.image_upload_lease_ttl_seconds,
        )
        ctx["storage_write_coordinator"] = storage_writes
        generation_runtime = build_generation_runtime(
            storage_writes=storage_writes,
            image_upstream_runtime=image_upstream_runtime,
        )
        ctx["generation_runtime"] = generation_runtime
        lifecycle.own("generation_runtime", generation_runtime.shutdown)
        completion_runtime = build_completion_runtime(
            storage_writes=storage_writes,
            image_upstream_runtime=image_upstream_runtime,
        )
        ctx["completion_runtime"] = completion_runtime
        video_generation_runtime = build_video_generation_runtime(
            storage_writes=storage_writes
        )
        ctx["video_generation_runtime"] = video_generation_runtime
        init_sentry(
            settings.sentry_dsn,
            settings.sentry_environment or settings.app_env,
            settings.sentry_traces_sample_rate,
        )
        init_otel(settings.otel_service_name, settings.otel_exporter_endpoint)
        # 必须在 metrics server 起来之前挂好采样函数，否则第一轮 scrape 拿到的是空值。
        bind_db_pool_metrics(engine)
        start_metrics_server(settings.worker_metrics_port, settings.worker_metrics_host)
        lifecycle.own("metrics_server", stop_metrics_server)
        # P1-4: 预热 tiktoken o200k_base encoding，避免首条请求承担 ~100-200 ms 加载耗时。
        # 失败不阻塞启动——count_tokens 内部会回落到 estimate_text_tokens。
        loaded = warm_tiktoken()
        _startup_logger.info("worker.tiktoken_warm loaded=%s", loaded)
        await billing_cache.configure(ctx.get("redis"))
        lifecycle.own("billing_cache", billing_cache.shutdown)

        worker_runtime = WorkerRuntime(
            _runtime_settings=runtime_settings_cache,
            _image_upstream=image_upstream_runtime,
            _storage_writes=storage_writes,
            _generation=generation_runtime,
            _completion=completion_runtime,
            _video=video_generation_runtime,
            _lifecycle=lifecycle,
        )
        ctx["worker_runtime"] = worker_runtime
        ctx.update(worker_runtime.context_values())
        ctx.pop("_worker_runtime_lifecycle", None)
        worker_runtime.start(logger=_startup_logger)
    except Exception:
        _startup_logger.exception("worker startup failed; cleaning partial resources")
        await _cleanup_resources(ctx)
        raise


async def _on_shutdown(ctx: dict) -> None:  # type: ignore[type-arg]
    """arq WorkerSettings.on_shutdown 钩子：独立清理各项进程资源。"""
    await _cleanup_resources(ctx)


class WorkerSettings:
    # Redis
    redis_settings = build_redis_settings(settings.redis_url)

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
