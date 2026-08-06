#!/usr/bin/env python3
"""Low-cost, reproducible Wave 0 performance and fault baselines."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1
RSS_SCENARIOS = ("1mp", "4k", "edit", "dual_race")


def _json_dump(payload: dict[str, Any], output: str | None = None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _run_child(name: str, *args: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), name, *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class _ApiRedis:
    def __init__(self, fail_channel: str | None = None) -> None:
        self.fail_channel = fail_channel
        self.publish_attempts: list[str] = []
        self.commands: Counter[str] = Counter()

    async def eval(self, *_args: Any) -> bytes:
        self.commands["eval"] += 1
        return b"1000-1"

    async def expire(self, *_args: Any) -> bool:
        self.commands["expire"] += 1
        return True

    async def publish(self, channel: str, _payload: str) -> int:
        self.commands["publish"] += 1
        self.publish_attempts.append(channel)
        if channel == self.fail_channel:
            raise ConnectionError(f"injected publish failure: {channel}")
        return 1


async def _api_realtime_child(iterations: int) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "packages" / "core"))
    sys.path.insert(0, str(ROOT / "apps" / "api"))

    from app import sse_publish
    from app.routes import events

    payload = {
        "event": "generation.progress",
        "channel": "task:task-1",
        "event_id": "event-1",
        "sse_id": "1000-1",
        "data": {"task_id": "task-1", "progress": 50},
    }
    encoded = json.dumps(payload, separators=(",", ":"))

    async def is_disconnected() -> bool:
        return False

    state = events._EventStreamState(  # noqa: SLF001
        is_disconnected=is_disconnected,
        redis=SimpleNamespace(),
        user_id="user-1",
        valid_channels=["task:task-1", "user:user-1"],
        replay_channels={"task:task-1", "user:user-1"},
        include_user_channel=True,
        user_channel="user:user-1",
        stream_key="events:user:user-1",
        last_event_id=None,
        connection_slot=None,
    )
    task_message = {"channel": "task:task-1", "data": encoded}
    user_message = {"channel": "user:user-1", "data": encoded}
    first = await events._standard_pubsub_events(state, task_message)  # noqa: SLF001
    second = await events._standard_pubsub_events(state, user_message)  # noqa: SLF001

    replay_deduper = events._ConnectionEventDeduper()  # noqa: SLF001
    replay_deduper.remember(sse_id="1000-1")
    replay_state = events._EventStreamState(  # noqa: SLF001
        is_disconnected=is_disconnected,
        redis=SimpleNamespace(),
        user_id="user-1",
        valid_channels=["task:task-1", "user:user-1"],
        replay_channels={"task:task-1", "user:user-1"},
        include_user_channel=True,
        user_channel="user:user-1",
        stream_key="events:user:user-1",
        last_event_id="999-0",
        connection_slot=None,
        event_deduper=replay_deduper,
    )
    replay_live = await events._standard_pubsub_events(  # noqa: SLF001
        replay_state,
        task_message,
    )

    throughput_state = events._EventStreamState(  # noqa: SLF001
        is_disconnected=is_disconnected,
        redis=SimpleNamespace(),
        user_id="user-1",
        valid_channels=["task:task-1"],
        replay_channels={"task:task-1"},
        include_user_channel=False,
        user_channel="user:user-1",
        stream_key="events:user:user-1",
        last_event_id=None,
        connection_slot=None,
    )
    started = time.perf_counter()
    frames = 0
    for index in range(iterations):
        item = dict(payload)
        item["event_id"] = f"event-{index}"
        item["sse_id"] = f"{1001 + index}-0"
        message = {
            "channel": "task:task-1",
            "data": json.dumps(item, separators=(",", ":")),
        }
        frames += len(
            await events._standard_pubsub_events(  # noqa: SLF001
                throughput_state,
                message,
            )
        )
    elapsed = time.perf_counter() - started

    normal_redis = _ApiRedis()
    await sse_publish.publish_sse_event(
        normal_redis,
        user_id="user-1",
        channel="task:task-1",
        event_name="generation.progress",
        data={"event_id": "api-normal", "progress": 50},
    )

    fault_redis = _ApiRedis(fail_channel="task:task-1")
    fault_error: str | None = None
    try:
        await sse_publish.publish_sse_event(
            fault_redis,
            user_id="user-1",
            channel="task:task-1",
            event_name="generation.progress",
            data={"event_id": "api-fault", "progress": 50},
        )
    except Exception as exc:  # noqa: BLE001
        fault_error = type(exc).__name__

    channels = sse_publish._live_channels("task:task-1", "user-1")  # noqa: SLF001
    return {
        "status": "measured",
        "implementation": "apps/api current code path",
        "parser_iterations": iterations,
        "parser_frames": frames,
        "parser_elapsed_seconds": elapsed,
        "parser_events_per_second": frames / elapsed if elapsed else 0.0,
        "payload_bytes": len(encoded.encode("utf-8")),
        "fanout_channels": sorted(channels),
        "fanout_bytes_per_event": len(encoded.encode("utf-8")) * len(channels),
        "live_live_frames_for_same_sse_id": len(first) + len(second),
        "replay_live_frames_for_same_sse_id": len(replay_live),
        "normal_publish_attempts": normal_redis.publish_attempts,
        "fault_first_channel": "task:task-1",
        "fault_publish_attempts": fault_redis.publish_attempts,
        "fault_error": fault_error,
    }


class _WorkerRedis(_ApiRedis):
    async def lpush(self, *_args: Any) -> int:
        self.commands["lpush"] += 1
        return 1

    async def ltrim(self, *_args: Any) -> bool:
        self.commands["ltrim"] += 1
        return True


async def _worker_realtime_child() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "packages" / "core"))
    sys.path.insert(0, str(ROOT / "apps" / "worker"))

    from app import sse_publish

    normal_redis = _WorkerRedis()
    await sse_publish.publish_event(
        normal_redis,
        "user-1",
        "task:task-1",
        "generation.progress",
        {"event_id": "worker-normal", "progress": 50},
    )

    fault_redis = _WorkerRedis(fail_channel="task:task-1")
    await sse_publish.publish_event(
        fault_redis,
        "user-1",
        "task:task-1",
        "generation.progress",
        {"event_id": "worker-fault", "progress": 50},
    )
    return {
        "status": "measured",
        "implementation": "apps/worker current code path",
        "normal_publish_attempts": normal_redis.publish_attempts,
        "fault_first_channel": "task:task-1",
        "fault_publish_attempts": fault_redis.publish_attempts,
        "fault_user_channel_reached": "user:user-1" in fault_redis.publish_attempts,
    }


class _QueueService:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.provider_cooldowns: dict[str, float] = {}

    def configured_capacity(self) -> int:
        return self.capacity

    async def resolve_capacity(self) -> int:
        return self.capacity


class _QueueRedis:
    def __init__(self) -> None:
        self.commands: Counter[str] = Counter()
        self.dedupe: set[str] = set()
        self.dispatch_active: dict[str, str] = {}
        self.dispatch_revisions: Counter[str] = Counter()
        self.enqueued: list[str] = []

    async def zremrangebyscore(self, *_args: Any) -> int:
        self.commands["zremrangebyscore"] += 1
        return 0

    async def zrange(self, *_args: Any) -> list[str]:
        self.commands["zrange"] += 1
        return []

    async def get(self, key: str) -> str | None:
        self.commands["get"] += 1
        return self.dispatch_active.get(key)

    async def eval(self, script: str, _num_keys: int, *args: Any) -> Any:
        self.commands["eval"] += 1
        if "local revision = redis.call('INCR', revision_key)" in script:
            active_key = str(args[0])
            revision_key = str(args[1])
            attempt = int(args[2])
            self.dispatch_revisions[revision_key] += 1
            value = (
                f"{attempt}|{self.dispatch_revisions[revision_key]}|reserved|"
            )
            self.dispatch_active[active_key] = value
            return [1, value]
        if "local current = redis.call('GET', KEYS[1])" in script:
            active_key = str(args[0])
            expected = str(args[1])
            replacement = str(args[2])
            if self.dispatch_active.get(active_key) != expected:
                return 0
            self.dispatch_active[active_key] = replacement
            return 1
        raise AssertionError("unexpected queue benchmark Redis script")

    async def set(self, key: str, _value: str, **kwargs: Any) -> bool:
        self.commands["set"] += 1
        if kwargs.get("nx"):
            if key in self.dedupe:
                return False
            self.dedupe.add(key)
        return True

    async def enqueue_job(
        self,
        _name: str,
        task_id: str,
        *_args: Any,
        **_kwargs: Any,
    ) -> str:
        self.commands["enqueue_job"] += 1
        self.enqueued.append(task_id)
        return f"job:{task_id}"

    async def delete(self, *_keys: str) -> int:
        self.commands["delete"] += 1
        return 1


async def _queue_child(candidate_counts: list[int], capacity: int) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "packages" / "core"))
    sys.path.insert(0, str(ROOT / "apps" / "worker"))

    from app.tasks.generation_parts import queue

    original = queue.queued_generation_candidates
    measurements: list[dict[str, Any]] = []
    try:
        for candidate_count in candidate_counts:
            candidates = [
                queue.QueuedGenerationCandidate(
                    id=f"task-{index:04d}",
                    queue_lane=(
                        "image:interactive:small"
                        if index % 2 == 0
                        else "image:workflow:large"
                    ),
                )
                for index in range(candidate_count)
            ]

            async def fake_candidates(
                _limit: int,
                _services: Any,
                *,
                values: list[Any] = candidates,
            ) -> list[Any]:
                return list(values)

            queue.queued_generation_candidates = fake_candidates
            redis = _QueueRedis()
            services = SimpleNamespace(queue=_QueueService(capacity))
            started = time.perf_counter()
            await queue.kick_image_queue(redis, services=services)
            elapsed = time.perf_counter() - started
            measurements.append(
                {
                    "candidate_count": candidate_count,
                    "selected_enqueue_count": len(redis.enqueued),
                    "redis_commands": sum(redis.commands.values()),
                    "redis_commands_by_name": dict(sorted(redis.commands.items())),
                    "elapsed_seconds": elapsed,
                }
            )
    finally:
        queue.queued_generation_candidates = original

    first = measurements[0]
    last = measurements[-1]
    candidate_delta = last["candidate_count"] - first["candidate_count"]
    command_delta = last["redis_commands"] - first["redis_commands"]
    return {
        "status": "measured",
        "implementation": "apps/worker generation_parts.queue.kick_image_queue",
        "capacity": capacity,
        "measurements": measurements,
        "redis_command_growth_per_candidate": (
            command_delta / candidate_delta if candidate_delta else 0.0
        ),
    }


def _rss_bytes_from_ru_maxrss(raw: int) -> int:
    if sys.platform == "darwin":
        return raw
    return raw * 1024


def _touch_pages(buffer: bytearray) -> None:
    for index in range(0, len(buffer), 4096):
        buffer[index] = index % 251


def _rss_child(scenario: str) -> dict[str, Any]:
    specs = {
        "1mp": (1024, 1024, 5),
        "4k": (3840, 2160, 4),
        "edit": (2048, 2048, 7),
        "dual_race": (2048, 2048, 10),
    }
    width, height, buffer_count = specs[scenario]
    bytes_per_buffer = width * height * 4
    buffers = [bytearray(bytes_per_buffer) for _ in range(buffer_count)]
    for buffer in buffers:
        _touch_pages(buffer)
    checksum = sum(buffer[0] for buffer in buffers)
    peak = _rss_bytes_from_ru_maxrss(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return {
        "status": "measured",
        "mode": "synthetic_memory_shape",
        "scenario": scenario,
        "width": width,
        "height": height,
        "buffer_count": buffer_count,
        "allocated_payload_bytes": bytes_per_buffer * buffer_count,
        "peak_rss_bytes": peak,
        "checksum": checksum,
    }


def _sample_process_rss(command: str) -> dict[str, Any]:
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        shell=True,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    peak_kib = 0
    process_group = os.getpgid(process.pid)
    while process.poll() is None:
        rows = subprocess.run(
            ["ps", "-axo", "pgid=,rss="],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        sample_kib = 0
        for row in rows:
            parts = row.split()
            if len(parts) != 2:
                continue
            with contextlib.suppress(ValueError):
                pgid, rss_kib = (int(value) for value in parts)
                if pgid == process_group:
                    sample_kib += rss_kib
        peak_kib = max(peak_kib, sample_kib)
        time.sleep(0.05)
    stdout, stderr = process.communicate()
    return {
        "status": "measured" if process.returncode == 0 else "failed",
        "mode": "external_command",
        "rss_scope": "process_group",
        "command": command,
        "exit_code": process.returncode,
        "elapsed_seconds": time.perf_counter() - started,
        "peak_rss_bytes": peak_kib * 1024,
        "stdout_tail": stdout[-1000:],
        "stderr_tail": stderr[-1000:],
    }


def _rss_scenarios() -> dict[str, Any]:
    results: dict[str, Any] = {}
    for scenario in RSS_SCENARIOS:
        env_name = f"LUMEN_WAVE0_RSS_{scenario.upper()}_COMMAND"
        command = os.environ.get(env_name)
        if command:
            results[scenario] = _sample_process_rss(command)
            continue
        results[scenario] = _run_child("_rss_child", scenario)
        results[scenario]["real_workload_env"] = env_name
    return {
        "status": "measured",
        "note": (
            "Synthetic memory shapes are the low-cost default. Set the per-scenario "
            "command environment variable to measure a real generation path."
        ),
        "scenarios": results,
    }


def _timed_http(
    url: str,
    headers: dict[str, str],
    *,
    samples: int,
    warmup: int,
) -> dict[str, Any]:
    timings: list[float] = []
    status_codes: list[int] = []
    response_bytes: list[int] = []
    item_counts: list[int | None] = []
    errors: list[str] = []
    for index in range(samples + warmup):
        request = urllib.request.Request(url, headers=headers)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            errors.append(f"HTTPError:{exc.code}")
        except Exception as exc:  # noqa: BLE001
            body = b""
            status = 0
            errors.append(f"{type(exc).__name__}:{exc}")
        elapsed = time.perf_counter() - started
        if index < warmup:
            continue
        timings.append(elapsed)
        status_codes.append(status)
        response_bytes.append(len(body))
        try:
            parsed = json.loads(body)
            items = parsed.get("items") if isinstance(parsed, dict) else None
            item_counts.append(len(items) if isinstance(items, list) else None)
        except Exception:  # noqa: BLE001
            item_counts.append(None)
    return {
        "url": url,
        "samples": samples,
        "warmup": warmup,
        "p50_seconds": statistics.median(timings) if timings else 0.0,
        "p95_seconds": _percentile(timings, 0.95),
        "max_seconds": max(timings, default=0.0),
        "status_codes": status_codes,
        "response_bytes": response_bytes,
        "item_counts": item_counts,
        "errors": errors,
    }


def _feed_timing(samples: int, warmup: int) -> dict[str, Any]:
    feed_url = os.environ.get("LUMEN_WAVE0_FEED_URL")
    if not feed_url:
        return {
            "status": "gated",
            "reason": "LUMEN_WAVE0_FEED_URL is not set",
            "required_environment": [
                "LUMEN_WAVE0_FEED_URL",
                "LUMEN_WAVE0_AUTHORIZATION (when authentication is required)",
                "LUMEN_WAVE0_COOKIE (optional)",
                "LUMEN_WAVE0_SEARCH_QUERY (optional)",
            ],
        }
    headers = {"Accept": "application/json"}
    authorization = os.environ.get("LUMEN_WAVE0_AUTHORIZATION")
    cookie = os.environ.get("LUMEN_WAVE0_COOKIE")
    if authorization:
        headers["Authorization"] = authorization
    if cookie:
        headers["Cookie"] = cookie
    separator = "&" if "?" in feed_url else "?"
    unfiltered_url = f"{feed_url}{separator}limit=30"
    search_query = os.environ.get("LUMEN_WAVE0_SEARCH_QUERY", "wave0-target")
    search_url = f"{feed_url}{separator}limit=30&q={urllib.parse.quote(search_query)}"
    return {
        "status": "measured",
        "unfiltered": _timed_http(
            unfiltered_url,
            headers,
            samples=samples,
            warmup=warmup,
        ),
        "search": _timed_http(
            search_url,
            headers,
            samples=samples,
            warmup=warmup,
        ),
        "search_query": search_query,
    }


def _browser_baseline() -> dict[str, Any]:
    command = [
        "node",
        str(ROOT / "perf" / "wave0" / "browser_assets.mjs"),
        "--json",
    ]
    browser_url = os.environ.get("LUMEN_WAVE0_ASSET_URL")
    if browser_url:
        command.extend(["--url", browser_url])
    selector = os.environ.get("LUMEN_WAVE0_TILE_SELECTOR")
    if selector:
        command.extend(["--tile-selector", selector])
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {
            "status": "gated",
            "reason": "browser runner failed",
            "exit_code": result.returncode,
            "stderr": result.stderr[-2000:],
        }
    return json.loads(result.stdout)


def _environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "node": subprocess.run(
            ["node", "--version"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "cpu_count": os.cpu_count(),
    }


def _baseline(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "work_base_sha": _git_output("rev-parse", "HEAD"),
        "branch": _git_output("branch", "--show-current"),
        "environment": _environment(),
        "invariants": [
            "Harness imports current production paths but does not mutate business code.",
            "Synthetic scenarios are labeled and are not presented as production SLO proof.",
            "Environment-gated scenarios return a structured gated result instead of passing silently.",
            "Raw measurements are emitted as JSON for same-environment comparisons.",
        ],
        "scenarios": {
            "realtime": {
                "api": _run_child("_api_realtime_child", str(args.iterations)),
                "worker": _run_child("_worker_realtime_child"),
            },
            "queue_tick": _run_child(
                "_queue_child",
                ",".join(str(value) for value in args.candidate_counts),
                str(args.capacity),
            ),
            "generation_rss": _rss_scenarios(),
            "assets_browser": _browser_baseline(),
            "feed_search": _feed_timing(args.http_samples, args.http_warmup),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    baseline = subparsers.add_parser("baseline")
    baseline.add_argument("--output")
    baseline.add_argument("--iterations", type=int, default=5000)
    baseline.add_argument(
        "--candidate-counts",
        type=lambda raw: [int(item) for item in raw.split(",")],
        default=[10, 100, 1000],
    )
    baseline.add_argument("--capacity", type=int, default=4)
    baseline.add_argument("--http-samples", type=int, default=5)
    baseline.add_argument("--http-warmup", type=int, default=1)

    realtime = subparsers.add_parser("realtime")
    realtime.add_argument("--iterations", type=int, default=5000)
    realtime.add_argument("--output")

    queue = subparsers.add_parser("queue")
    queue.add_argument(
        "--candidate-counts",
        type=lambda raw: [int(item) for item in raw.split(",")],
        default=[10, 100, 1000],
    )
    queue.add_argument("--capacity", type=int, default=4)
    queue.add_argument("--output")

    rss = subparsers.add_parser("rss")
    rss.add_argument("--output")

    feed = subparsers.add_parser("feed")
    feed.add_argument("--samples", type=int, default=5)
    feed.add_argument("--warmup", type=int, default=1)
    feed.add_argument("--output")

    browser = subparsers.add_parser("browser")
    browser.add_argument("--output")

    subparsers.add_parser("_worker_realtime_child")
    api_child = subparsers.add_parser("_api_realtime_child")
    api_child.add_argument("iterations", type=int)
    queue_child = subparsers.add_parser("_queue_child")
    queue_child.add_argument("candidate_counts")
    queue_child.add_argument("capacity", type=int)
    rss_child = subparsers.add_parser("_rss_child")
    rss_child.add_argument("scenario", choices=RSS_SCENARIOS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "baseline":
        _json_dump(_baseline(args), args.output)
    elif args.command == "realtime":
        _json_dump(
            {
                "api": _run_child("_api_realtime_child", str(args.iterations)),
                "worker": _run_child("_worker_realtime_child"),
            },
            args.output,
        )
    elif args.command == "queue":
        _json_dump(
            _run_child(
                "_queue_child",
                ",".join(str(value) for value in args.candidate_counts),
                str(args.capacity),
            ),
            args.output,
        )
    elif args.command == "rss":
        _json_dump(_rss_scenarios(), args.output)
    elif args.command == "feed":
        _json_dump(_feed_timing(args.samples, args.warmup), args.output)
    elif args.command == "browser":
        _json_dump(_browser_baseline(), args.output)
    elif args.command == "_api_realtime_child":
        print(json.dumps(asyncio.run(_api_realtime_child(args.iterations))))
    elif args.command == "_worker_realtime_child":
        print(json.dumps(asyncio.run(_worker_realtime_child())))
    elif args.command == "_queue_child":
        counts = [int(item) for item in args.candidate_counts.split(",")]
        print(json.dumps(asyncio.run(_queue_child(counts, args.capacity))))
    elif args.command == "_rss_child":
        print(json.dumps(_rss_child(args.scenario)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
