#!/usr/bin/env python3
"""Run an impact-test JSON plan with bounded, resource-aware concurrency."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import Any, Awaitable, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXCLUSIVE_RESOURCES = {
    "postgres",
    "redis",
    "filesystem",
    "web-build",
}
PYTEST_FAILURE_RE = re.compile(r"^FAILED\s+(\S+(?:::\S+)*)", re.MULTILINE)
NODE_FAILURE_RE = re.compile(r"^not ok\s+\d+\s+-\s+(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class CommandResult:
    id: str
    command: str
    duration_seconds: float
    exit_code: int
    failures: list[str]
    output: str


class ProcessRegistry:
    def __init__(self) -> None:
        self._processes: set[asyncio.subprocess.Process] = set()

    def add(self, process: asyncio.subprocess.Process) -> None:
        self._processes.add(process)

    def discard(self, process: asyncio.subprocess.Process) -> None:
        self._processes.discard(process)

    async def terminate_all(self) -> None:
        processes = list(self._processes)
        await asyncio.gather(
            *(_terminate_process(process) for process in processes),
            return_exceptions=True,
        )


def extract_failures(output: str) -> list[str]:
    failures: list[str] = []
    seen: set[str] = set()
    for regex in (PYTEST_FAILURE_RE, NODE_FAILURE_RE):
        for match in regex.finditer(output):
            failure = match.group(1).strip()
            if failure not in seen:
                seen.add(failure)
                failures.append(failure)
    return failures


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError):
        process.kill()
    await process.wait()


async def run_command(
    command: dict[str, Any],
    *,
    registry: ProcessRegistry,
) -> CommandResult:
    started = time.monotonic()
    process = await asyncio.create_subprocess_shell(
        str(command["command"]),
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    registry.add(process)
    try:
        output_bytes, _ = await process.communicate()
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    finally:
        registry.discard(process)
    output = output_bytes.decode("utf-8", errors="replace")
    return CommandResult(
        id=str(command["id"]),
        command=str(command["command"]),
        duration_seconds=round(time.monotonic() - started, 3),
        exit_code=process.returncode if process.returncode is not None else 1,
        failures=extract_failures(output),
        output=output,
    )


async def execute_commands(
    commands: Sequence[dict[str, Any]],
    *,
    max_jobs: int,
    exclusive_resources: set[str],
    runner: Callable[[dict[str, Any]], Awaitable[CommandResult]],
) -> list[CommandResult]:
    if max_jobs < 1:
        raise ValueError("max_jobs must be at least 1")
    semaphore = asyncio.Semaphore(max_jobs)
    resource_locks = {
        resource: asyncio.Lock() for resource in sorted(exclusive_resources)
    }

    async def execute_one(command: dict[str, Any]) -> CommandResult:
        resources = sorted(
            set(command.get("resource_tags", [])) & exclusive_resources
        )
        acquired: list[asyncio.Lock] = []
        try:
            for resource in resources:
                lock = resource_locks[resource]
                await lock.acquire()
                acquired.append(lock)
            async with semaphore:
                return await runner(command)
        finally:
            for lock in reversed(acquired):
                lock.release()

    return list(await asyncio.gather(*(execute_one(command) for command in commands)))


def select_commands(
    commands: Sequence[dict[str, Any]],
    *,
    rerun_failed: bool,
    previous_results: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not rerun_failed:
        return list(commands)
    if previous_results is None:
        raise ValueError("--rerun-failed requires an existing results file")
    failed_ids = {
        str(result["id"])
        for result in previous_results.get("results", [])
        if int(result.get("exit_code", 1)) != 0
    }
    return [command for command in commands if str(command["id"]) in failed_ids]


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _validate_commands(plan: dict[str, Any]) -> list[dict[str, Any]]:
    commands = plan.get("commands")
    if not isinstance(commands, list):
        raise ValueError("plan.commands must be a list")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        if not isinstance(command, dict):
            raise ValueError(f"plan.commands[{index}] must be an object")
        command_id = command.get("id")
        shell_command = command.get("command")
        resources = command.get("resource_tags", [])
        if not isinstance(command_id, str) or not command_id:
            raise ValueError(f"plan.commands[{index}].id must be a string")
        if command_id in seen:
            raise ValueError(f"duplicate command id: {command_id}")
        seen.add(command_id)
        if not isinstance(shell_command, str) or not shell_command:
            raise ValueError(f"plan.commands[{index}].command must be a string")
        if not isinstance(resources, list) or not all(
            isinstance(resource, str) for resource in resources
        ):
            raise ValueError(
                f"plan.commands[{index}].resource_tags must be strings"
            )
        validated.append(command)
    return validated


def build_plan_identity(
    plan: dict[str, Any],
    commands: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    command_set = [
        {
            "command": str(command["command"]),
            "id": str(command["id"]),
            "resource_tags": sorted(
                str(resource) for resource in command.get("resource_tags", [])
            ),
        }
        for command in commands
    ]
    payload = {
        "base": str(plan.get("base", "")),
        "commands": command_set,
        "head": str(plan.get("head", "")),
        "plan_schema_version": plan.get("schema_version"),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        **payload,
        "digest": digest,
    }


def _validate_previous_results(
    previous_results: dict[str, Any],
    *,
    plan_identity: dict[str, Any],
    commands: Sequence[dict[str, Any]],
) -> None:
    if previous_results.get("plan_identity") != plan_identity:
        raise ValueError(
            "--rerun-failed results do not match the current plan "
            "digest/base/head/command set"
        )
    results = previous_results.get("results")
    if not isinstance(results, list):
        raise ValueError("--rerun-failed results are missing the results list")
    result_ids = {
        str(result.get("id"))
        for result in results
        if isinstance(result, dict) and result.get("id")
    }
    missing = [
        str(command["id"])
        for command in commands
        if str(command["id"]) not in result_ids
    ]
    if missing:
        raise ValueError(
            "--rerun-failed cannot skip commands that were never executed: "
            + ", ".join(missing)
        )


def _merge_results(
    commands: Sequence[dict[str, Any]],
    current: Sequence[CommandResult],
    previous: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    merged = {
        str(result["id"]): result
        for result in (previous or {}).get("results", [])
        if isinstance(result, dict) and "id" in result
    }
    merged.update({result.id: asdict(result) for result in current})
    return [
        merged[str(command["id"])]
        for command in commands
        if str(command["id"]) in merged
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--max-jobs", "--jobs", type=int, default=4)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--rerun-failed", action="store_true")
    return parser.parse_args(argv)


async def _run_selected(
    selected: Sequence[dict[str, Any]],
    *,
    max_jobs: int,
    exclusive_resources: set[str],
    registry: ProcessRegistry,
) -> list[CommandResult]:
    return await execute_commands(
        selected,
        max_jobs=max_jobs,
        exclusive_resources=exclusive_resources,
        runner=lambda command: run_command(command, registry=registry),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_jobs < 1:
        raise SystemExit("--max-jobs must be at least 1")
    plan = _load_json(args.plan)
    commands = _validate_commands(plan)
    plan_identity = build_plan_identity(plan, commands)
    results_path = args.results or args.plan.with_suffix(".results.json")
    previous = _load_json(results_path) if results_path.is_file() else None
    try:
        if args.rerun_failed and previous is not None:
            _validate_previous_results(
                previous,
                plan_identity=plan_identity,
                commands=commands,
            )
        selected = select_commands(
            commands,
            rerun_failed=args.rerun_failed,
            previous_results=previous,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not selected:
        print("No failed command groups to rerun.")
        return 0

    exclusive_resources = set(
        plan.get("exclusive_resources", DEFAULT_EXCLUSIVE_RESOURCES)
    )
    registry = ProcessRegistry()
    started_at = datetime.now(UTC)
    started = time.monotonic()
    try:
        results = asyncio.run(
            _run_selected(
                selected,
                max_jobs=args.max_jobs,
                exclusive_resources=exclusive_resources,
                registry=registry,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted; terminating child process groups.", file=sys.stderr)
        asyncio.run(registry.terminate_all())
        return 130

    for result in results:
        status = "PASS" if result.exit_code == 0 else "FAIL"
        print(
            f"[{status}] {result.id} duration={result.duration_seconds:.3f}s "
            f"exit={result.exit_code}"
        )
        if result.output:
            print(result.output, end="" if result.output.endswith("\n") else "\n")
        if result.failures:
            print(f"failures: {', '.join(result.failures)}")

    payload = {
        "schema_version": 2,
        "plan": str(args.plan),
        "plan_identity": plan_identity,
        "started_at": started_at.isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "selected_command_ids": [str(command["id"]) for command in selected],
        "results": _merge_results(commands, results, previous),
    }
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote results to {results_path}")
    return 1 if any(result.exit_code != 0 for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
