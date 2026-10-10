"""Disposable real Canvas video submit/poll/store/GET acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

# Import first to install the TCP deny guard before app dependencies.
import lumen_canvas_stub_api as base
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from lumen_core.models import (
    PricingRule,
    SystemSetting,
    UserWallet,
    Video,
    VideoGeneration,
    WalletTransaction,
)
from app.canvas_services import execution_service


async def configure_video(factory):
    values = {
        "video.enabled": "1",
        "video.providers": json.dumps(
            [
                {
                    "name": "local-video-stub",
                    "kind": "fake",
                    "base_url": "https://stub.invalid",
                    "enabled": True,
                    "supports_idempotency": False,
                    "models": {"seedance-2.0": "seedance-2.0"},
                }
            ]
        ),
        "video.token_hold_estimates": json.dumps(
            {
                "seedance-2.0": {"t2v": {"720p:5": 1000}},
            }
        ),
    }
    async with factory() as db:
        db.add_all(
            [SystemSetting(key=key, value=value) for key, value in values.items()]
        )
        db.add(
            PricingRule(
                scope="video",
                key="seedance-2.0",
                unit="per_mtoken",
                variant="t2v_720p",
                price_micro=100_000,
            )
        )
        await db.commit()


def make_video(directory):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Existing ffmpeg required; no install attempted")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x264b63:s=1280x720:r=12",
            "-t",
            "5",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(directory / "provider-video.mp4"),
        ],
        check=True,
        timeout=30,
    )


def graph(prompt):
    return {
        "nodes": [
            {
                "id": "prompt",
                "type": "prompt",
                "position": {"x": 0, "y": 0},
                "config": {"text": prompt},
            },
            {
                "id": "video",
                "type": "video_text_generate",
                "position": {"x": 320, "y": 0},
                "config": {
                    "model": "seedance-2.0",
                    "resolution": "720p",
                    "duration_s": 5,
                    "aspect_ratio": "16:9",
                },
            },
        ],
        "edges": [
            {
                "id": "pv",
                "source_node_id": "prompt",
                "target_node_id": "video",
                "source_handle": "text",
                "target_handle": "prompt",
                "data_type": "text",
            }
        ],
    }


async def start(h, key, prompt):
    document = await h.request("POST", "", {"title": key, "graph": graph(prompt)})
    prefix = "/" + document["id"]
    body = {
        "document_revision": document["revision"],
        "kind": "all",
        "budget_micro": 200,
        "output_indices": {},
        "failure_policy": "continue_independent",
        "auto_select_on_success": True,
    }
    preview = await h.request("POST", prefix + "/plans/preview", body)
    start_body = {
        **body,
        "plan_hash": preview["plan"]["plan_hash"],
        "idempotency_key": key,
    }
    run = await h.request("POST", prefix + "/plans/run", start_body)
    assert (await h.request("POST", prefix + "/plans/run", start_body))["id"] == run[
        "id"
    ]
    assert await h.dispatch(run["id"])
    await h.worker("lumen_canvas_stub_video_worker.py")
    await h.dispatch(run["id"])
    detail = await h.request("GET", prefix + "/runs/" + run["id"])
    return prefix, body, start_body, detail


async def snapshot(h):
    async with h.factory() as db:
        wallet = await db.get(UserWallet, "stub-owner")
        counts = [
            (await db.execute(select(func.count()).select_from(model))).scalar_one()
            for model in (VideoGeneration, Video, WalletTransaction)
        ]
        return [*counts, wallet.balance_micro, wallet.hold_micro]


async def success(h):
    prefix, _, start_body, detail = await start(
        h, "video-success", "stub-video-success"
    )
    assert detail["status"] == "succeeded", detail
    output = detail["executions"][0]["outputs"][0]
    async with h.factory() as db:
        video = await db.get(Video, output["video_id"])
        raw = (h.directory / "artifacts" / video.storage_key).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == video.sha256 == output["sha256"]
        assert video.width == 1280 and video.height == 720
        assert 4900 <= video.duration_ms <= 5100
        assert video.poster_storage_key
        assert (h.directory / "artifacts" / video.poster_storage_key).is_file()
        rows = list((await db.execute(select(WalletTransaction))).scalars())
        assert len([row for row in rows if row.kind == "settle"]) == 1
    document = await h.request("GET", prefix)
    assert len(document["selections"]) == 1
    before = await snapshot(h)
    assert before[0:2] == [1, 1] and before[-2:] == [99_900, 0], before
    await h.request("POST", prefix + "/plans/run", start_body)
    await h.worker("lumen_canvas_stub_video_worker.py")
    await h.request("GET", prefix)
    assert await snapshot(h) == before
    h.owner_id = "stub-other"
    await h.request("GET", prefix, expected=404)
    h.owner_id = "stub-owner"
    return {"detail": detail, "snapshot": before, "artifact_sha": output["sha256"]}


async def unknown(h):
    prefix, body, start_body, detail = await start(
        h, "video-unknown", "stub-video-unknown"
    )
    execution = detail["executions"][0]
    assert not execution["outputs"]
    async with h.factory() as db:
        rows = list(
            (
                await db.execute(
                    select(VideoGeneration).where(
                        VideoGeneration.status == "submit_unknown"
                    )
                )
            ).scalars()
        )
        assert len(rows) == 1 and not rows[0].provider_task_id
    before = await snapshot(h)
    await h.request("POST", prefix + "/plans/preview", body, expected=409)
    await h.request(
        "POST",
        prefix + "/runs/" + detail["id"] + "/retry-failed",
        {
            "execution_ids": [execution["id"]],
            "additional_budget_micro": 200,
            "idempotency_key": "unsafe-video-repair",
        },
        expected=409,
    )
    await h.request("POST", prefix + "/plans/run", start_body)
    await h.worker("lumen_canvas_stub_video_worker.py")
    await h.request("GET", prefix)
    assert await snapshot(h) == before
    return {"detail": detail, "snapshot": before, "unknown_replay_unchanged": True}


async def main(directory):
    from lumen_canvas_stub_sqlite import install_sqlite_utc_adapter

    engine = create_async_engine(os.environ["DATABASE_URL"])
    install_sqlite_utc_adapter(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await base.bootstrap(engine, factory)
        await configure_video(factory)
        make_video(directory)
        h = base.Harness(directory, factory)
        execution_service.publish_canvas_video_task = base.noop
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=base.application(h)),
            base_url="http://stub.local",
        ) as client:
            h.client = client
            succeeded = await success(h)
            uncertain = await unknown(h)
        calls = [
            json.loads(line)
            for line in (directory / "video-provider-calls.jsonl")
            .read_text()
            .splitlines()
        ]
        assert [call["action"] for call in calls].count("submit") == 2, calls
        assert [call["action"] for call in calls].count("poll") == 1, calls
        assert [call["action"] for call in calls].count("download") == 1, calls
        assert base.NETWORK_ATTEMPTS == 0
        evidence = {
            "result": "passed",
            "http": h.http,
            "success": succeeded,
            "unknown": uncertain,
            "calls": calls,
            "api_tcp_attempts": base.NETWORK_ATTEMPTS,
            "boundaries": [
                "synthetic HTTP auth/CSRF",
                "locally driven outbox and ARQ",
                "in-process provider submit/poll and local-file download",
                "SQLite copied ARRAY defaults and UTC timestamp codec adapted only",
            ],
            "real_paths": [
                "video capability/pricing/admission",
                "durable outbox",
                "video worker lease/submit receipt/poll/artifact fence",
                "ffmpeg validation/poster/storage",
                "wallet settlement",
                "Canvas reconcile and HTTP GET",
            ],
        }
        path = directory / "video-evidence.json"
        path.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
        print(
            json.dumps(
                {"result": "passed", "evidence": str(path), "provider_submits": 2}
            )
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
