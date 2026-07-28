#!/usr/bin/env python3
"""Reproducible Wave 2 generation performance and fault harness."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import gc
import json
import math
import os
import platform
import resource
import subprocess
import sys
import tempfile
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1
MIB = 1024 * 1024
FOUR_K_WIDTH = 3840
FOUR_K_HEIGHT = 2160
SCHEDULER_SCAN_RTT_LIMIT_100 = 8
SCHEDULER_SCAN_RTT_GROWTH_LIMIT = 0.05
STAGED_PAYLOAD_REDUCTION_TARGET_PERCENT = 30.0


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


def _run_child(name: str, *args: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), name, *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _rss_bytes(raw: int) -> int:
    return raw if sys.platform == "darwin" else raw * 1024


def _touch_pages(buffer: bytearray) -> None:
    for index in range(0, len(buffer), 4096):
        buffer[index] = index % 251


class _QueueService:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.provider_cooldowns: dict[str, float] = {}

    def configured_capacity(self) -> int:
        return self.capacity

    async def resolve_capacity(self) -> int:
        return self.capacity


class _CountingRedis:
    """Redis/ARQ adapter that counts logical commands and network round trips."""

    def __init__(self, *, unknown_first_enqueue: bool = False) -> None:
        self.commands: Counter[str] = Counter()
        self.round_trips: Counter[str] = Counter()
        self.strings: dict[str, str] = {}
        self.active_members: list[str] = []
        self.enqueue_calls: list[dict[str, Any]] = []
        self.accepted_job_ids: set[str] = set()
        self.revision_counters: dict[str, int] = {}
        self.unknown_first_enqueue = unknown_first_enqueue
        self._unknown_injected = False

    def _record(self, command: str) -> None:
        self.commands[command] += 1
        self.round_trips[command] += 1

    async def zremrangebyscore(self, *_args: Any) -> int:
        self._record("zremrangebyscore")
        return 0

    async def zrange(self, *_args: Any) -> list[str]:
        self._record("zrange")
        return list(self.active_members)

    async def get(self, key: str) -> str | None:
        self._record("get")
        return self.strings.get(key)

    async def mget(self, keys: list[str] | tuple[str, ...]) -> list[str | None]:
        self._record("mget")
        return [self.strings.get(key) for key in keys]

    async def set(self, key: str, value: Any, **kwargs: Any) -> bool:
        self._record("set")
        if kwargs.get("nx") and key in self.strings:
            return False
        self.strings[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        self._record("delete")
        deleted = 0
        for key in keys:
            deleted += int(key in self.strings)
            self.strings.pop(key, None)
        return deleted

    async def incrby(self, key: str, amount: int) -> int:
        self._record("incrby")
        value = int(self.strings.get(key, "0")) + int(amount)
        self.strings[key] = str(value)
        return value

    async def expire(self, *_args: Any) -> bool:
        self._record("expire")
        return True

    async def enqueue_job(
        self,
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> SimpleNamespace | None:
        self._record("enqueue_job")
        task_id = str(args[0])
        requested_job_id = kwargs.get("_job_id")
        job_id = str(requested_job_id or f"legacy-call:{len(self.enqueue_calls) + 1}")
        accepted = job_id not in self.accepted_job_ids
        if accepted:
            self.accepted_job_ids.add(job_id)
        self.enqueue_calls.append(
            {
                "name": name,
                "task_id": task_id,
                "args": list(args),
                "job_id": job_id,
                "accepted": accepted,
                "kwargs": dict(kwargs),
            }
        )
        if self.unknown_first_enqueue and not self._unknown_injected:
            self._unknown_injected = True
            raise TimeoutError("injected enqueue result unknown after acceptance")
        return SimpleNamespace(job_id=job_id) if accepted else None

    async def eval(
        self,
        script: str,
        _key_count: int,
        *args: str,
    ) -> Any:
        self._record("eval")
        if "local revision_key = KEYS[2]" in script:
            active_key = args[0]
            revision_key = args[1]
            attempt = int(args[2])
            replace_value = args[5]
            current = self.strings.get(active_key)
            if current is not None:
                current_attempt = int(current.split("|", 1)[0])
                if current_attempt > attempt:
                    return [0, current]
                if current_attempt == attempt and current != replace_value:
                    return [0, current]
            revision = self.revision_counters.get(revision_key, 0) + 1
            self.revision_counters[revision_key] = revision
            value = f"{attempt}|{revision}|reserved|"
            self.strings[active_key] = value
            return [1, value]
        if "if current ~= ARGV[1]" in script and "'EX', tonumber(ARGV[3])" in script:
            active_key = args[0]
            reserved_value = args[1]
            enqueued_value = args[2]
            if self.strings.get(active_key) != reserved_value:
                return 0
            self.strings[active_key] = enqueued_value
            return 1
        raise AssertionError("unsupported Redis script in Wave 2 harness")


def _mixed_candidates(candidate_count: int, queue_module: Any) -> list[Any]:
    lanes = (
        "image:interactive:small",
        "image:workflow:small",
        "image:interactive:large",
        "image:workflow:large",
        "image:interactive:edit",
        "image:workflow:mask_edit",
    )
    return [
        queue_module.QueuedGenerationCandidate(
            id=f"mixed-{index:04d}",
            queue_lane=lanes[index % len(lanes)],
            size_bucket=("1mp", "4k", "edit", "dual_race")[index % 4],
            cost_class=("small", "large", "edit", "race")[index % 4],
        )
        for index in range(candidate_count)
    ]


async def _scheduler_characterization(
    candidate_counts: list[int],
    capacity: int,
) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "packages" / "core"))
    sys.path.insert(0, str(ROOT / "apps" / "worker"))

    from app.tasks.generation_parts import queue

    original = queue.queued_generation_candidates
    measurements: list[dict[str, Any]] = []
    try:
        for candidate_count in candidate_counts:
            candidates = _mixed_candidates(candidate_count, queue)

            async def fake_candidates(
                _limit: int,
                _services: Any,
                *,
                values: list[Any] = candidates,
            ) -> list[Any]:
                return list(values)

            queue.queued_generation_candidates = fake_candidates
            redis = _CountingRedis()
            services = SimpleNamespace(queue=_QueueService(capacity))
            started = time.perf_counter()
            await queue.kick_image_queue(redis, services=services)
            elapsed = time.perf_counter() - started
            measurements.append(
                {
                    "candidate_count": candidate_count,
                    "mixed_lane_count": len(
                        {candidate.queue_lane for candidate in candidates}
                    ),
                    "selected_enqueue_count": sum(
                        int(call["accepted"]) for call in redis.enqueue_calls
                    ),
                    "redis_commands": sum(redis.commands.values()),
                    "redis_commands_by_name": dict(sorted(redis.commands.items())),
                    "redis_round_trips": sum(redis.round_trips.values()),
                    "redis_round_trips_by_name": dict(
                        sorted(redis.round_trips.items())
                    ),
                    "candidate_scan_round_trips": sum(
                        redis.round_trips[name]
                        for name in (
                            "delete",
                            "get",
                            "mget",
                            "zrange",
                            "zremrangebyscore",
                        )
                    ),
                    "elapsed_seconds": elapsed,
                }
            )
    finally:
        queue.queued_generation_candidates = original

    first = measurements[0]
    last = measurements[-1]
    candidate_delta = last["candidate_count"] - first["candidate_count"]
    rtt_delta = last["candidate_scan_round_trips"] - first["candidate_scan_round_trips"]
    growth = rtt_delta / candidate_delta if candidate_delta else 0.0
    measurement_100 = next(
        (item for item in measurements if item["candidate_count"] == 100),
        None,
    )
    acceptance = {
        "fixed_candidate_scan_rtt_limit_for_100_candidates": (
            SCHEDULER_SCAN_RTT_LIMIT_100
        ),
        "fixed_candidate_scan_rtt_growth_limit_per_candidate": (
            SCHEDULER_SCAN_RTT_GROWTH_LIMIT
        ),
        "candidate_scan_rtt_count_100_met": bool(
            measurement_100
            and measurement_100["candidate_scan_round_trips"]
            <= SCHEDULER_SCAN_RTT_LIMIT_100
        ),
        "non_linear_growth_met": growth <= SCHEDULER_SCAN_RTT_GROWTH_LIMIT,
    }
    acceptance["status"] = (
        "met"
        if acceptance["candidate_scan_rtt_count_100_met"]
        and acceptance["non_linear_growth_met"]
        else "not_met"
    )
    return {
        "status": "measured",
        "mode": "current_production_facade",
        "implementation": "generation_parts.queue.kick_image_queue",
        "capacity": capacity,
        "measurements": measurements,
        "candidate_scan_rtt_growth_per_candidate": growth,
        "acceptance": acceptance,
    }


async def _enqueue_unknown_characterization() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "packages" / "core"))
    sys.path.insert(0, str(ROOT / "apps" / "worker"))

    from app.tasks.generation_parts import queue

    redis = _CountingRedis(unknown_first_enqueue=True)
    services = SimpleNamespace(queue=_QueueService(4))
    task_id = "enqueue-unknown"
    first = await queue.enqueue_generation_once(
        redis,
        task_id,
        services=services,
    )
    dedupe_key_fn = getattr(queue, "image_queue_enqueue_dedupe_key", None)
    dedupe_key = dedupe_key_fn(task_id) if callable(dedupe_key_fn) else None
    dedupe_present_after_unknown = bool(dedupe_key and dedupe_key in redis.strings)
    second = await queue.enqueue_generation_once(
        redis,
        task_id,
        services=services,
    )
    active_revisions = len(redis.accepted_job_ids)
    return {
        "status": "measured",
        "mode": "current_production_facade",
        "implementation": "generation_parts.queue.enqueue_generation_once",
        "first_return": repr(first),
        "second_return": repr(second),
        "dedupe_present_after_unknown": dedupe_present_after_unknown,
        "enqueue_calls": redis.enqueue_calls,
        "accepted_active_dispatch_revisions": active_revisions,
        "duplicate_active_dispatch_revisions": max(0, active_revisions - 1),
        "acceptance": {
            "max_active_revision_per_attempt": 1,
            "status": "met" if active_revisions == 1 else "not_met",
        },
    }


def _enqueue_unknown_target_model() -> dict[str, Any]:
    active_revision = "enqueue-unknown:attempt-1:revision-1"
    attempts = [
        {
            "kicker": 1,
            "revision": active_revision,
            "enqueue_outcome": "unknown_after_accept",
        },
        {
            "kicker": 2,
            "revision": active_revision,
            "enqueue_outcome": "reused_active_revision",
        },
    ]
    return {
        "status": "modeled",
        "mode": "stable_dispatch_revision_oracle",
        "attempts": attempts,
        "accepted_active_dispatch_revisions": 1,
        "duplicate_active_dispatch_revisions": 0,
        "acceptance": {
            "max_active_revision_per_attempt": 1,
            "status": "met",
        },
    }


@dataclass(frozen=True, slots=True)
class ResourceDemand:
    pixel_units: int
    reference_units: int
    postprocess_units: int
    external_lane_units: int
    output_units: int

    @property
    def total(self) -> int:
        return (
            self.pixel_units
            + self.reference_units
            + self.postprocess_units
            + self.external_lane_units
            + self.output_units
        )


@dataclass(frozen=True, slots=True)
class Workload:
    task_id: str
    kind: str
    user_id: str
    demand: ResourceDemand
    active_ticks: int


def _resource_demand(
    *,
    width: int,
    height: int,
    reference_bytes: int = 0,
    edit: bool = False,
    transparent: bool = False,
    outputs: int = 1,
    dual_race: bool = False,
) -> ResourceDemand:
    pixels = width * height
    if pixels <= 1_600_000:
        pixel_units = 1
    elif pixels <= 4_000_000:
        pixel_units = 2
    else:
        pixel_units = 4
    return ResourceDemand(
        pixel_units=pixel_units,
        reference_units=math.ceil(reference_bytes / (32 * MIB)),
        postprocess_units=int(edit) + int(transparent),
        external_lane_units=2 if dual_race else 1,
        output_units=max(1, outputs),
    )


def _mixed_resource_workload() -> list[Workload]:
    tasks: list[Workload] = []
    users = ("user-a", "user-b", "user-c", "user-d")
    for index in range(8):
        tasks.append(
            Workload(
                task_id=f"1mp-{index + 1:02d}",
                kind="1mp",
                user_id=users[index % len(users)],
                demand=_resource_demand(width=1024, height=1024),
                active_ticks=2,
            )
        )
    for index in range(2):
        tasks.append(
            Workload(
                task_id=f"4k-{index + 1:02d}",
                kind="4k",
                user_id=users[index],
                demand=_resource_demand(width=FOUR_K_WIDTH, height=FOUR_K_HEIGHT),
                active_ticks=5,
            )
        )
    for index in range(2):
        tasks.append(
            Workload(
                task_id=f"multi-ref-edit-{index + 1:02d}",
                kind="multi_reference_edit",
                user_id=users[index + 2],
                demand=_resource_demand(
                    width=1536,
                    height=1536,
                    reference_bytes=96 * MIB,
                    edit=True,
                ),
                active_ticks=6,
            )
        )
    tasks.append(
        Workload(
            task_id="dual-race-01",
            kind="dual_race",
            user_id="user-a",
            demand=_resource_demand(
                width=1024,
                height=1024,
                dual_race=True,
            ),
            active_ticks=5,
        )
    )
    return tasks


def _resource_budget_simulation() -> dict[str, Any]:
    global_budget = 14
    external_lane_budget = 4
    per_user_budget = 10
    queued = deque((task, 0) for task in _mixed_resource_workload())
    active: list[dict[str, Any]] = []
    completed: list[str] = []
    timeline: list[dict[str, Any]] = []
    peak_units = 0
    peak_external_lanes = 0
    max_wait_ticks = 0
    tick = 0

    def active_units() -> int:
        return sum(item["task"].demand.total for item in active)

    def active_external_lanes() -> int:
        return sum(item["task"].demand.external_lane_units for item in active)

    def user_units(user_id: str) -> int:
        return sum(
            item["task"].demand.total
            for item in active
            if item["task"].user_id == user_id
        )

    while queued or active:
        tick += 1
        released: list[str] = []
        for item in list(active):
            item["remaining"] -= 1
            if item["remaining"] <= 0:
                active.remove(item)
                completed.append(item["task"].task_id)
                released.append(item["task"].task_id)

        admitted: list[str] = []
        for _ in range(len(queued)):
            task, waited = queued.popleft()
            fits = (
                active_units() + task.demand.total <= global_budget
                and active_external_lanes() + task.demand.external_lane_units
                <= external_lane_budget
                and user_units(task.user_id) + task.demand.total <= per_user_budget
            )
            if fits:
                active.append({"task": task, "remaining": task.active_ticks})
                admitted.append(task.task_id)
                max_wait_ticks = max(max_wait_ticks, waited)
            else:
                queued.append((task, waited + 1))

        units = active_units()
        external_lanes = active_external_lanes()
        peak_units = max(peak_units, units)
        peak_external_lanes = max(peak_external_lanes, external_lanes)
        timeline.append(
            {
                "tick": tick,
                "admitted": admitted,
                "released": released,
                "active": [item["task"].task_id for item in active],
                "active_weighted_units": units,
                "active_external_lane_units": external_lanes,
                "queued_count": len(queued),
            }
        )
        if tick > 100:
            raise RuntimeError("resource simulation did not drain")

    workloads = _mixed_resource_workload()
    counts = Counter(task.kind for task in workloads)
    demand_by_kind = {
        kind: asdict(next(task.demand for task in workloads if task.kind == kind))
        | {"total": next(task.demand.total for task in workloads if task.kind == kind)}
        for kind in counts
    }
    cleanup = _resource_cleanup_oracle()
    invariants = {
        "active_weighted_units_within_budget": peak_units <= global_budget,
        "external_lane_units_within_budget": (
            peak_external_lanes <= external_lane_budget
        ),
        "dual_race_uses_two_external_lanes": (
            demand_by_kind["dual_race"]["external_lane_units"] == 2
        ),
        "all_tasks_completed": len(completed) == len(workloads),
        "permit_leaks_after_cancel_or_lease_lost": cleanup["permit_leaks"] == 0,
    }
    return {
        "status": "modeled",
        "mode": "deterministic_resource_demand_oracle",
        "fixed_weights": {
            "pixels_le_1_6mp": 1,
            "pixels_1_6_to_4mp": 2,
            "pixels_above_4mp_or_4k": 4,
            "reference_chunk_bytes": 32 * MIB,
            "edit_or_mask_postprocess": 1,
            "normal_external_lane": 1,
            "dual_race_external_lanes": 2,
            "output_unit_per_output": 1,
        },
        "budgets": {
            "global_weighted_units": global_budget,
            "external_lane_units": external_lane_budget,
            "per_user_weighted_units": per_user_budget,
        },
        "workload_counts": dict(sorted(counts.items())),
        "demand_by_kind": demand_by_kind,
        "peak_active_weighted_units": peak_units,
        "peak_external_lane_units": peak_external_lanes,
        "max_wait_ticks": max_wait_ticks,
        "completed_count": len(completed),
        "timeline": timeline,
        "cleanup_faults": cleanup,
        "invariants": invariants,
        "acceptance": {
            "status": "met" if all(invariants.values()) else "not_met",
        },
    }


def _resource_cleanup_oracle() -> dict[str, Any]:
    permits = {
        "cancel-edit": _resource_demand(
            width=1536,
            height=1536,
            reference_bytes=96 * MIB,
            edit=True,
        ),
        "lease-lost-dual": _resource_demand(
            width=1024,
            height=1024,
            dual_race=True,
        ),
    }
    releases: list[dict[str, Any]] = []
    for task_id, reason in (
        ("cancel-edit", "cancel"),
        ("cancel-edit", "cancel_idempotent_repeat"),
        ("lease-lost-dual", "lease_lost_after_loser_grace"),
    ):
        released = permits.pop(task_id, None)
        releases.append(
            {
                "task_id": task_id,
                "reason": reason,
                "released_units": released.total if released else 0,
            }
        )
    return {
        "releases": releases,
        "permit_leaks": len(permits),
        "idempotent_repeat_released_units": releases[1]["released_units"],
    }


def _payload_legacy_child(payload_bytes: int) -> dict[str, Any]:
    raw = bytearray(payload_bytes)
    _touch_pages(raw)
    encoded = base64.b64encode(raw).decode("ascii")
    decoded = base64.b64decode(encoded)
    pixels = bytearray(FOUR_K_WIDTH * FOUR_K_HEIGHT * 4)
    _touch_pages(pixels)
    variant_sizes = (
        payload_bytes,
        max(1, payload_bytes // 2),
        max(1, payload_bytes // 6),
        max(1, payload_bytes // 48),
    )
    variants = [bytearray(size) for size in variant_sizes]
    for variant in variants:
        _touch_pages(variant)
    logical_live_bytes = (
        len(raw)
        + len(encoded)
        + len(decoded)
        + len(pixels)
        + sum(len(variant) for variant in variants)
    )
    peak = _rss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return {
        "status": "measured",
        "mode": "synthetic_legacy_url_bytes_base64_shape",
        "source_payload_bytes": payload_bytes,
        "base64_characters": len(encoded),
        "base64_expansion_ratio": len(encoded) / payload_bytes,
        "pixel_buffer_bytes": len(pixels),
        "variant_bytes": list(variant_sizes),
        "logical_peak_live_bytes": logical_live_bytes,
        "peak_rss_bytes": peak,
        "checksum": raw[0] + decoded[0] + pixels[0] + sum(v[0] for v in variants),
    }


def _payload_staged_child(payload_bytes: int) -> dict[str, Any]:
    chunk_size = min(MIB, payload_bytes)
    chunk = bytearray(chunk_size)
    _touch_pages(chunk)
    root_path: str
    with tempfile.TemporaryDirectory(prefix="lumen-wave2-staged-") as temp_dir:
        root_path = temp_dir
        staged_path = Path(temp_dir) / "source.bin"
        remaining = payload_bytes
        with staged_path.open("wb") as handle:
            while remaining:
                size = min(remaining, len(chunk))
                handle.write(memoryview(chunk)[:size])
                remaining -= size

        pixels = bytearray(FOUR_K_WIDTH * FOUR_K_HEIGHT * 4)
        _touch_pages(pixels)
        variant_sizes = (
            payload_bytes,
            max(1, payload_bytes // 2),
            max(1, payload_bytes // 6),
            max(1, payload_bytes // 48),
        )
        max_live_variant = 0
        for size in variant_sizes:
            variant = bytearray(size)
            _touch_pages(variant)
            max_live_variant = max(max_live_variant, len(variant))
            del variant
            gc.collect()
        logical_live_bytes = len(chunk) + len(pixels) + max_live_variant
        staged_size = staged_path.stat().st_size
        peak = _rss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return {
        "status": "measured",
        "mode": "synthetic_staged_payload_shape",
        "source_payload_bytes": payload_bytes,
        "staged_file_bytes": staged_size,
        "write_chunk_bytes": len(chunk),
        "pixel_buffer_bytes": len(pixels),
        "max_live_variant_bytes": max_live_variant,
        "logical_peak_live_bytes": logical_live_bytes,
        "peak_rss_bytes": peak,
        "temporary_path_removed": not Path(root_path).exists(),
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


def _reduction_percent(before: int, after: int) -> float:
    if before <= 0:
        return 0.0
    return (before - after) / before * 100.0


def _payload_characterization(payload_mib: int) -> dict[str, Any]:
    payload_bytes = payload_mib * MIB
    legacy = _run_child("_payload_child", "legacy", str(payload_bytes))
    staged = _run_child("_payload_child", "staged", str(payload_bytes))
    logical_reduction = _reduction_percent(
        legacy["logical_peak_live_bytes"],
        staged["logical_peak_live_bytes"],
    )
    rss_reduction = _reduction_percent(
        legacy["peak_rss_bytes"],
        staged["peak_rss_bytes"],
    )
    before_command = os.environ.get("LUMEN_WAVE2_4K_BEFORE_COMMAND")
    after_command = os.environ.get("LUMEN_WAVE2_4K_AFTER_COMMAND")
    if before_command and after_command:
        real_before = _sample_process_rss(before_command)
        real_after = _sample_process_rss(after_command)
        real_comparison: dict[str, Any] = {
            "status": (
                "measured"
                if real_before["status"] == real_after["status"] == "measured"
                else "failed"
            ),
            "before": real_before,
            "after": real_after,
            "peak_rss_reduction_percent": _reduction_percent(
                real_before["peak_rss_bytes"],
                real_after["peak_rss_bytes"],
            ),
        }
    else:
        real_comparison = {
            "status": "gated",
            "reason": (
                "Set both LUMEN_WAVE2_4K_BEFORE_COMMAND and "
                "LUMEN_WAVE2_4K_AFTER_COMMAND on the same host and workload."
            ),
        }
    invariants = {
        "base64_expansion_is_at_least_four_thirds": (
            legacy["base64_expansion_ratio"] >= 4 / 3
        ),
        "staged_source_matches_input_bytes": (
            staged["staged_file_bytes"] == payload_bytes
        ),
        "staged_temporary_path_removed": staged["temporary_path_removed"],
        "fixed_logical_reduction_target_met": (
            logical_reduction >= STAGED_PAYLOAD_REDUCTION_TARGET_PERCENT
        ),
    }
    return {
        "status": "measured",
        "input": {
            "width": FOUR_K_WIDTH,
            "height": FOUR_K_HEIGHT,
            "compressed_payload_bytes": payload_bytes,
        },
        "synthetic_shapes": {
            "legacy_url_bytes_base64": legacy,
            "staged_payload_target": staged,
            "logical_peak_reduction_percent": logical_reduction,
            "peak_rss_reduction_percent": rss_reduction,
        },
        "real_workload_comparison": real_comparison,
        "invariants": invariants,
        "acceptance": {
            "fixed_reduction_target_percent": (STAGED_PAYLOAD_REDUCTION_TARGET_PERCENT),
            "status": "met" if all(invariants.values()) else "not_met",
            "note": (
                "Synthetic acceptance validates the harness oracle only. "
                "Production acceptance requires the gated real command comparison."
            ),
        },
    }


def _contract_detection() -> dict[str, Any]:
    paths = {
        "queue": ROOT / "apps/worker/app/tasks/generation_parts/queue.py",
        "direct_images": ROOT / "apps/worker/app/upstream_parts/direct_images.py",
        "generation_parts": ROOT / "apps/worker/app/tasks/generation_parts",
        "upstream_parts": ROOT / "apps/worker/app/upstream_parts",
        "worker_app": ROOT / "apps/worker/app",
        "core": ROOT / "packages/core/lumen_core",
    }
    queue_text = paths["queue"].read_text(encoding="utf-8")
    direct_text = paths["direct_images"].read_text(encoding="utf-8")
    contract_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (
            paths["generation_parts"],
            paths["upstream_parts"],
            paths["worker_app"],
            paths["core"],
        )
        for path in sorted(root.glob("*.py"))
    )
    return {
        "status": "observed",
        "dispatch_revision_contract_present": "dispatch_revision" in contract_text,
        "resource_demand_contract_present": "ResourceDemand" in contract_text,
        "generated_payload_contract_present": "GeneratedPayload" in contract_text,
        "legacy_enqueue_dedupe_present": "IMAGE_QUEUE_ENQUEUE_DEDUPE_TTL_S"
        in queue_text,
        "url_download_base64_return_present": (
            "base64.b64encode(raw).decode" in direct_text
        ),
    }


def _suite(
    candidate_counts: list[int],
    capacity: int,
    payload_mib: int,
    generated_at: str | None = None,
) -> dict[str, Any]:
    branch = _git_output("branch", "--show-current") or "(detached)"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "work_base_sha": _git_output("rev-parse", "HEAD"),
        "branch": branch,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "fixed_thresholds": {
            "scheduler_candidate_scan_rtt_limit_100_candidates": (
                SCHEDULER_SCAN_RTT_LIMIT_100
            ),
            "scheduler_candidate_scan_rtt_growth_limit_per_candidate": (
                SCHEDULER_SCAN_RTT_GROWTH_LIMIT
            ),
            "staged_payload_reduction_target_percent": (
                STAGED_PAYLOAD_REDUCTION_TARGET_PERCENT
            ),
        },
        "scenarios": {
            "contract_detection": _contract_detection(),
            "scheduler_tick": asyncio.run(
                _scheduler_characterization(candidate_counts, capacity)
            ),
            "enqueue_result_unknown": {
                "current": asyncio.run(_enqueue_unknown_characterization()),
                "target_oracle": _enqueue_unknown_target_model(),
            },
            "mixed_resource_demand": _resource_budget_simulation(),
            "four_k_payload": _payload_characterization(payload_mib),
        },
        "invariants": [
            "Current production characterization is reported even when acceptance is not met.",
            "Fixed thresholds are source constants and are never derived from the measured baseline.",
            "Synthetic resource and payload models are labeled and are not production SLO proof.",
            "External before/after commands must run on the same host and representative workload.",
            "No production code, test manifest, execution ledger, or existing baseline is mutated.",
        ],
    }


def _measurement_100(payload: dict[str, Any]) -> dict[str, Any] | None:
    measurements = payload["scenarios"]["scheduler_tick"]["measurements"]
    return next(
        (item for item in measurements if item["candidate_count"] == 100),
        None,
    )


def _compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_scheduler = _measurement_100(before)
    after_scheduler = _measurement_100(after)
    before_enqueue = before["scenarios"]["enqueue_result_unknown"]["current"]
    after_enqueue = after["scenarios"]["enqueue_result_unknown"]["current"]
    before_payload = before["scenarios"]["four_k_payload"]["synthetic_shapes"]
    after_payload = after["scenarios"]["four_k_payload"]["synthetic_shapes"]
    return {
        "status": "compared",
        "before_sha": before.get("work_base_sha"),
        "after_sha": after.get("work_base_sha"),
        "scheduler_100": {
            "before_redis_commands": (
                before_scheduler["redis_commands"] if before_scheduler else None
            ),
            "after_redis_commands": (
                after_scheduler["redis_commands"] if after_scheduler else None
            ),
            "before_redis_round_trips": (
                before_scheduler["redis_round_trips"] if before_scheduler else None
            ),
            "after_redis_round_trips": (
                after_scheduler["redis_round_trips"] if after_scheduler else None
            ),
        },
        "enqueue_result_unknown": {
            "before_active_revisions": before_enqueue[
                "accepted_active_dispatch_revisions"
            ],
            "after_active_revisions": after_enqueue[
                "accepted_active_dispatch_revisions"
            ],
        },
        "synthetic_payload": {
            "before_legacy_peak_rss_bytes": before_payload["legacy_url_bytes_base64"][
                "peak_rss_bytes"
            ],
            "after_staged_peak_rss_bytes": after_payload["staged_payload_target"][
                "peak_rss_bytes"
            ],
            "cross_run_peak_rss_reduction_percent": _reduction_percent(
                before_payload["legacy_url_bytes_base64"]["peak_rss_bytes"],
                after_payload["staged_payload_target"]["peak_rss_bytes"],
            ),
        },
        "after_acceptance": {
            "scheduler": after["scenarios"]["scheduler_tick"]["acceptance"],
            "enqueue_result_unknown": after_enqueue["acceptance"],
            "resource_demand": after["scenarios"]["mixed_resource_demand"][
                "acceptance"
            ],
            "payload_oracle": after["scenarios"]["four_k_payload"]["acceptance"],
        },
    }


def _parse_counts(raw: str) -> list[int]:
    counts = sorted({int(value.strip()) for value in raw.split(",") if value.strip()})
    if not counts or any(value <= 0 for value in counts):
        raise argparse.ArgumentTypeError("candidate counts must be positive integers")
    if 100 not in counts:
        counts.append(100)
        counts.sort()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    suite_parser = subparsers.add_parser("suite")
    suite_parser.add_argument("--candidate-counts", default="10,100")
    suite_parser.add_argument("--capacity", type=int, default=4)
    suite_parser.add_argument("--payload-mib", type=int, default=12)
    suite_parser.add_argument("--generated-at")
    suite_parser.add_argument("--output")

    scheduler_parser = subparsers.add_parser("scheduler")
    scheduler_parser.add_argument("--candidate-counts", default="10,100")
    scheduler_parser.add_argument("--capacity", type=int, default=4)

    subparsers.add_parser("enqueue-unknown")
    subparsers.add_parser("resources")

    payload_parser = subparsers.add_parser("payload")
    payload_parser.add_argument("--payload-mib", type=int, default=12)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--before", required=True)
    compare_parser.add_argument("--after", required=True)
    compare_parser.add_argument("--output")

    child_parser = subparsers.add_parser("_payload_child")
    child_parser.add_argument("mode", choices=("legacy", "staged"))
    child_parser.add_argument("payload_bytes", type=int)

    args = parser.parse_args()
    if args.command == "suite":
        counts = _parse_counts(args.candidate_counts)
        if args.capacity <= 0 or args.payload_mib <= 0:
            parser.error("capacity and payload-mib must be positive")
        _json_dump(
            _suite(
                counts,
                args.capacity,
                args.payload_mib,
                generated_at=args.generated_at,
            ),
            args.output,
        )
    elif args.command == "scheduler":
        _json_dump(
            asyncio.run(
                _scheduler_characterization(
                    _parse_counts(args.candidate_counts),
                    args.capacity,
                )
            )
        )
    elif args.command == "enqueue-unknown":
        _json_dump(
            {
                "current": asyncio.run(_enqueue_unknown_characterization()),
                "target_oracle": _enqueue_unknown_target_model(),
            }
        )
    elif args.command == "resources":
        _json_dump(_resource_budget_simulation())
    elif args.command == "payload":
        _json_dump(_payload_characterization(args.payload_mib))
    elif args.command == "compare":
        before = json.loads(Path(args.before).read_text(encoding="utf-8"))
        after = json.loads(Path(args.after).read_text(encoding="utf-8"))
        _json_dump(_compare(before, after), args.output)
    elif args.command == "_payload_child":
        child = (
            _payload_legacy_child(args.payload_bytes)
            if args.mode == "legacy"
            else _payload_staged_child(args.payload_bytes)
        )
        _json_dump(child)


if __name__ == "__main__":
    main()
