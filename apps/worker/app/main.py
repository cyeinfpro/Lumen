"""arq WorkerSettings 入口。实际任务函数由 Agent C 在 tasks/ 下填充。

DESIGN §6.1：两条 Redis Stream `queue:generations` / `queue:completions`。
arq 内置用自己的队列；Agent C 可以选择：
(a) 用 arq 的 default queue 作为任务调度，把 task_id 当入参；或
(b) 用原生 Redis Stream + XAUTOCLAIM 自写消费循环。
本骨架用 (a) 起步，符合 DESIGN 的 `task_id` 传递精神；Agent C 可在 V1.1 升级到 (b)。"""

from __future__ import annotations

import logging

from arq.connections import RedisSettings
from arq.cron import cron

from lumen_core.context_window import warm_tiktoken

from .config import settings
from .jobs.upstream_probe import probe_upstream
from .observability import init_otel, init_sentry, start_metrics_server
from .provider_pool import probe_providers
from .tasks import auto_title as auto_title_tasks
from .tasks import completion as completion_tasks
from .tasks import context_summary as context_summary_tasks
from .tasks import generation as generation_tasks
from .tasks import outbox as outbox_tasks
from .upstream import close_client

_startup_logger = logging.getLogger(__name__)


async def _on_startup(ctx: dict) -> None:  # type: ignore[type-arg]
    """arq WorkerSettings.on_startup 钩子：初始化观测层（幂等）。"""
    init_sentry(
        settings.sentry_dsn,
        settings.sentry_environment or settings.app_env,
        settings.sentry_traces_sample_rate,
    )
    init_otel(settings.otel_service_name, settings.otel_exporter_endpoint)
    start_metrics_server(settings.worker_metrics_port)
    # P1-4: 预热 tiktoken o200k_base encoding，避免首条请求承担 ~100-200 ms 加载耗时。
    # 失败不阻塞启动——count_tokens 内部会回落到 estimate_text_tokens。
    loaded = warm_tiktoken()
    _startup_logger.info("worker.tiktoken_warm loaded=%s", loaded)


async def _on_shutdown(ctx: dict) -> None:  # type: ignore[type-arg]
    """arq WorkerSettings.on_shutdown 钩子：清理 httpx 连接池。"""
    await close_client()


class WorkerSettings:
    # Redis
    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # 注册任务（Agent C 填充）
    functions = [
        generation_tasks.run_generation,
        completion_tasks.run_completion,
        outbox_tasks.publish_outbox,
        auto_title_tasks.auto_title_conversation,
        context_summary_tasks.manual_compact_conversation,
    ]

    # 定时任务：每 2s publisher、每 60s reconciler、每 30s 统计刷入+条件探活、
    # 每 5 分钟巡检默认标题（auto_title 兜底）、每小时第 5 分钟做一次上游 schema 探针
    cron_jobs = outbox_tasks.cron_jobs + [
        # run_at_startup=False：probe 内部对 provider 没强制 timeout，某个 provider TCP
        # 长时间无响应时会把启动钩子卡死，导致整个 worker event loop 静默——cron 心跳停、
        # job 队列不消费。让首轮 probe 等到第一次 30s tick，至少 worker 已经在跑。
        cron(probe_providers, second={0, 30}, run_at_startup=False),
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
    ]

    # Keep the arq process wide enough for the image FIFO cap plus cron/outbox
    # jobs. The image queue still owns admission, so this only removes the
    # process-level bottleneck when IMAGE_GENERATION_CONCURRENCY is raised.
    max_jobs = max(8, min(64, settings.image_generation_concurrency + 4))
    # 4K 图生图（4K 升级后）最糟耗时：主链路 retry × 单次 ~8 min + 备链路 + 解码/落盘
    # 可达 ~20-25 min；给 1800s（30 min）留缓冲。普通小图/文生图远远跑不到这个上限。
    # 保持 > _RUN_GENERATION_TIMEOUT_S（1500s），让 task 自己 raise TimeoutError 释放 lease。
    job_timeout = 1800  # s
    keep_result = 3600

    # Startup hook：观测层 + metrics server
    on_startup = _on_startup
    on_shutdown = _on_shutdown
