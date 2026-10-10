"""Fresh-process real generation runner with a local image-provider port."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

# The harness has no reason to open any TCP connection. Redis uses a private
# Unix socket; provider payloads are generated locally. Fail closed on mistakes.
_real_connect = socket.socket.connect
NETWORK_ATTEMPTS = 0


def local_only_connect(self, address):
    if self.family != socket.AF_UNIX:
        global NETWORK_ATTEMPTS
        NETWORK_ATTEMPTS += 1
        raise RuntimeError("stub acceptance prohibits all network connections")
    return _real_connect(self, address)


socket.socket.connect = local_only_connect


async def main(directory: Path) -> None:
    from PIL import Image as PILImage
    from arq.connections import ArqRedis
    from sqlalchemy import select
    from app.db import SessionLocal, engine
    from app.storage import LocalStorage
    from app.tasks.generation import run_generation
    from app.tasks.generation_parts import composition, runner
    from app.tasks.generation_parts.composition_ports import (
        DefaultGenerationProvider,
        DefaultGenerationArtifacts,
    )
    from app.tasks.generation_parts.runtime import ImagePostprocessRuntime
    from app.tasks.generation_parts import post_commit
    from app.tasks import canvas_execution_reconcile as reconcile
    from app.upstream_parts import InlineImageBytes
    from lumen_core.models import Generation, OutboxEvent
    from app.generation_dispatch import enqueue_generation_dispatch
    from app.provider_runtime.errors import UpstreamError
    from app.upstream_parts.delivery_evidence import apply_dispatch_receipt
    from lumen_core.upstream_billing import UPSTREAM_DISPATCH_PROVEN_NO_COST
    from lumen_core.canvas_models import CanvasExecutionTask

    redis = ArqRedis(unix_socket_path=os.environ["REDIS_URL"].removeprefix("unix://"))
    storage = LocalStorage(directory / "artifacts")
    postprocess = ImagePostprocessRuntime()
    calls = []

    class LocalProvider(DefaultGenerationProvider):
        def __init__(self):
            super().__init__(postprocess, storage, None)

        async def resolve_primary_route(self):
            return "image2"

        def endpoint_kind_for_engine(self, _engine):
            return "generations"

        async def emit(self, request, *, action, references=0):
            call = {
                "action": action,
                "task": request.context.quota_task_id,
                "reference_count": references,
            }
            calls.append(call)
            with (directory / "provider-calls.jsonl").open("a") as stream:
                stream.write(json.dumps(call) + "\n")
            await request.progress_callback({"type": "dispatch_ready"})
            if "stub-unknown" in request.prompt:
                raise UpstreamError(
                    "local ambiguous submission", error_code="image_job_result_unknown"
                )
            failure_case = (
                "stub-fail-fast-once"
                if "stub-fail-fast-once" in request.prompt
                else "stub-fail-once"
            )
            marker = directory / (failure_case + "-observed")
            if failure_case in request.prompt and not marker.exists():
                marker.write_text("synthetic provider rejected before cost")
                error = UpstreamError(
                    "local confirmed rejection",
                    status_code=400,
                    error_code="invalid_value",
                )
                apply_dispatch_receipt(error, UPSTREAM_DISPATCH_PROVEN_NO_COST)
                raise error
            await request.progress_callback(
                {"type": "response_received", "upstream_response_status_code": 200}
            )
            stream = io.BytesIO()
            color = (50 if action == "edit" else 49, 104, 160)
            PILImage.new("RGB", (1024, 1024), color).save(stream, format="PNG")
            yield InlineImageBytes(stream.getvalue()), request.prompt

        def generate(self, request):
            return self.emit(request, action="generate")

        def edit(self, request):
            assert request.images, (
                "actual resolved upstream image must reach edit provider"
            )
            return self.emit(
                request.request, action="edit", references=len(request.images)
            )

    async def noop(*_args, **_kwargs):
        return None

    async def reserve(state, _metadata):
        state.reserved_provider = SimpleNamespace(
            name="local-stub", model="gpt-image-2"
        )
        return 0

    # Provider discovery/reservation is an external integration boundary, not a
    # generation or billing replacement. Real claim, epoch CAS, leases, dispatch
    # receipts, artifact commit, wallet settlement and projection remain active.
    runner._attach_provider_pool = noop
    runner._reserve_provider = reserve
    runner.kick_image_queue = noop
    post_commit.enqueue_auto_title = noop
    reconcile.notify_committed_execution = noop
    runtime = composition.build_generation_runtime()
    runtime = replace(
        runtime,
        deps=replace(
            runtime.deps,
            provider=LocalProvider(),
            artifacts=DefaultGenerationArtifacts(storage),
        ),
        postprocess_runtime=postprocess,
    )
    ctx = {
        "redis": redis,
        "worker_id": "local-stub-harness",
        "generation_runtime": runtime,
    }
    async with SessionLocal() as db:
        payloads = list(
            (
                await db.execute(
                    select(OutboxEvent.payload).where(OutboxEvent.kind == "generation")
                )
            ).scalars()
        )
        ids = list(dict.fromkeys(payload["task_id"] for payload in payloads))
    for task_id in ids:
        async with SessionLocal() as db:
            current = await db.get(Generation, task_id)
            attempt = int(current.attempt or 0) + 1
            terminal = current.status in {"succeeded", "failed", "canceled"}
        if terminal:
            await run_generation(ctx, task_id)
        else:
            first, duplicate = await asyncio.gather(
                *[
                    enqueue_generation_dispatch(redis, task_id=task_id, attempt=attempt)
                    for _ in range(2)
                ]
            )
            assert first.identity == duplicate.identity
            # The real preceding worker cleanup may already have kicked this
            # next task. Both callers must share one durable identity either way.
            assert int(first.created) + int(duplicate.created) <= 1
            assert first.accepted and duplicate.accepted
            identity = first.identity
            await run_generation(ctx, task_id, identity.attempt, identity.revision)
            await run_generation(ctx, task_id, identity.attempt, identity.revision)
        async with SessionLocal() as db:
            task = await db.get(Generation, task_id)
            assert task.status in {"succeeded", "failed"}, (
                task_id,
                task.status,
                task.error_code,
                task.error_message,
            )
            execution_ids = list(
                (
                    await db.execute(
                        select(CanvasExecutionTask.execution_id).where(
                            CanvasExecutionTask.generation_id == task_id
                        )
                    )
                ).scalars()
            )
        for execution_id in execution_ids:
            await reconcile.reconcile_canvas_execution(ctx, execution_id)
    assert NETWORK_ATTEMPTS == 0
    await runtime.shutdown()
    await redis.aclose()
    await engine.dispose()
    print(
        json.dumps(
            {"worker": "passed", "delivered_task_ids": ids, "provider_calls": calls}
        )
    )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
