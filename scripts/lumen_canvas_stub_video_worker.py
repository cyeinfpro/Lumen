"""Local video provider port around real submit/poll/artifact/billing runtime."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import sys

_real_connect = socket.socket.connect
NETWORK_ATTEMPTS = 0


def local_only_connect(self, address):
    if self.family != socket.AF_UNIX:
        global NETWORK_ATTEMPTS
        NETWORK_ATTEMPTS += 1
        raise RuntimeError("video stub prohibits all TCP connections")
    return _real_connect(self, address)


socket.socket.connect = local_only_connect


async def main(directory):
    from arq.connections import ArqRedis
    from sqlalchemy import select
    from app.db import SessionLocal, engine
    from lumen_canvas_stub_sqlite import install_sqlite_utc_adapter
    from app.tasks import canvas_execution_reconcile as reconcile
    from app.tasks.video_generation_parts.default_runtime import (
        build_video_generation_runtime,
    )
    from app.tasks.video_generation_parts.entrypoints import (
        run_video_generation,
        run_video_poll,
    )
    from app.video_upstream_parts.adapters import FakeVideoAdapter
    from app.video_upstream_service import VideoUpstreamError
    from app.video_artifacts import DownloadedVideo
    from lumen_core.models import OutboxEvent, VideoGeneration
    from lumen_core.canvas_models import CanvasExecutionTask

    install_sqlite_utc_adapter(engine)
    redis = ArqRedis(unix_socket_path=os.environ["REDIS_URL"].removeprefix("unix://"))
    journal = directory / "video-provider-calls.jsonl"

    def record(action, task):
        with journal.open("a") as stream:
            stream.write(json.dumps({"action": action, "task": task}) + "\n")

    class LocalVideoAdapter(FakeVideoAdapter):
        async def submit(self, request):
            record("submit", request.task_id)
            if "stub-video-unknown" in request.prompt:
                raise VideoUpstreamError(
                    "synthetic ambiguous video acknowledgement",
                    error_code="upstream_unknown",
                    status_code=504,
                    raw={"synthetic": True, "delivery": "unknown"},
                )
            return await super().submit(request)

        async def poll(self, task_id):
            record("poll", task_id)
            return await super().poll(task_id)

        async def download_result(self, url, *, ensure_active=None):
            if ensure_active:
                ensure_active()
            record("download", url)
            path = directory / "provider-video.mp4"
            return DownloadedVideo(
                path=path,
                mime="video/mp4",
                extension=".mp4",
                size_bytes=path.stat().st_size,
                declared_mime="video/mp4",
                temporary=False,
            )

    async def noop(*_args, **_kwargs):
        return None

    reconcile.notify_committed_execution = noop
    runtime = build_video_generation_runtime()
    runtime = replace(
        runtime,
        ports=replace(
            runtime.ports,
            provider=replace(
                runtime.ports.provider,
                adapter_for_provider=lambda provider: LocalVideoAdapter(provider),
            ),
        ),
    )
    ctx = {
        "redis": redis,
        "worker_id": "local-video-stub",
        "video_generation_runtime": runtime,
    }
    try:
        async with SessionLocal() as db:
            payloads = list(
                (
                    await db.execute(
                        select(OutboxEvent.payload).where(
                            OutboxEvent.kind == "video_generation"
                        )
                    )
                ).scalars()
            )
            ids = list(dict.fromkeys(payload["task_id"] for payload in payloads))
        for task_id in ids:
            # Real lease/idempotency checks, not an in-memory duplicate filter.
            await run_video_generation(ctx, task_id)
            await run_video_generation(ctx, task_id)
            async with SessionLocal() as db:
                row = await db.get(VideoGeneration, task_id)
                known_provider_task = bool(row.provider_task_id)
            if known_provider_task:
                await run_video_poll(ctx, task_id)
                await run_video_poll(ctx, task_id)
            async with SessionLocal() as db:
                row = await db.get(VideoGeneration, task_id)
                assert row.status in {"succeeded", "submit_unknown"}, (
                    row.id,
                    row.status,
                    row.error_code,
                    row.error_message,
                )
                executions = list(
                    (
                        await db.execute(
                            select(CanvasExecutionTask.execution_id).where(
                                CanvasExecutionTask.video_generation_id == task_id
                            )
                        )
                    ).scalars()
                )
            for execution_id in executions:
                await reconcile.reconcile_canvas_execution(ctx, execution_id)
        assert NETWORK_ATTEMPTS == 0
        print(
            json.dumps(
                {
                    "video_worker": "passed",
                    "tasks": ids,
                    "tcp_attempts": NETWORK_ATTEMPTS,
                }
            )
        )
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
