"""Actual Canvas HTTP routes and durable lifecycle assertions for the local stub."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

from lumen_canvas_stub_harness import ROOT, child_env

_original_connect = socket.socket.connect
NETWORK_ATTEMPTS = 0


def _unix_only_connect(instance, address):
    if instance.family != socket.AF_UNIX:
        global NETWORK_ATTEMPTS
        NETWORK_ATTEMPTS += 1
        raise RuntimeError("stub acceptance prohibits all network connections")
    return _original_connect(instance, address)


socket.socket.connect = _unix_only_connect

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import DefaultClause, MetaData, func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from lumen_core.models import (  # noqa: E402
    Base,
    Generation,
    Image,
    OutboxEvent,
    PricingRule,
    SystemSetting,
    User,
    UserWallet,
    WalletTransaction,
)
from lumen_core.canvas_models import CanvasExecutionTask, CanvasRun  # noqa: E402
from app.routes.canvases import router  # noqa: E402
from app.db import get_db  # noqa: E402
from app.deps import get_current_user, verify_csrf  # noqa: E402
from app.canvas_services.plan_dispatch import dispatch_plan_once  # noqa: E402
from app.canvas_services import event_commit, execution_service  # noqa: E402


def graph():
    return {
        "nodes": [
            {
                "id": "prompt",
                "type": "prompt",
                "position": {"x": 0, "y": 0},
                "config": {"text": "local stub only"},
            },
            {
                "id": "a",
                "type": "image_generate",
                "position": {"x": 300, "y": 0},
                "config": {"quality": "1k"},
            },
            {
                "id": "b",
                "type": "image_edit",
                "position": {"x": 600, "y": 0},
                "config": {"quality": "1k"},
            },
        ],
        "edges": [
            {
                "id": "pa",
                "source_node_id": "prompt",
                "target_node_id": "a",
                "source_handle": "text",
                "target_handle": "prompt",
                "data_type": "text",
            },
            {
                "id": "pb",
                "source_node_id": "prompt",
                "target_node_id": "b",
                "source_handle": "text",
                "target_handle": "prompt",
                "data_type": "text",
            },
            {
                "id": "ab",
                "source_node_id": "a",
                "target_node_id": "b",
                "source_handle": "image",
                "target_handle": "source",
                "data_type": "image",
            },
        ],
    }


async def bootstrap(engine, factory):
    schema = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(schema)
        for column in copied.columns:
            if column.server_default and "ARRAY[" in str(column.server_default.arg):
                column.server_default = DefaultClause("[]")
    async with engine.begin() as connection:
        await connection.run_sync(schema.create_all)
    async with factory() as db:
        db.add_all(
            [
                User(
                    id="stub-owner",
                    email="stub-owner@example.invalid",
                    account_mode="wallet",
                ),
                User(
                    id="stub-other",
                    email="stub-other@example.invalid",
                    account_mode="wallet",
                ),
                UserWallet(user_id="stub-owner", balance_micro=100_000, hold_micro=0),
                SystemSetting(key="billing.enabled", value="1"),
                *[
                    PricingRule(
                        scope="image_size", key=tier, unit="per_image", price_micro=100
                    )
                    for tier in (
                        "1k",
                        "2k",
                        "4k",
                        "standard",
                        "low",
                        "medium",
                        "high",
                        "auto",
                    )
                ],
            ]
        )
        await db.commit()


async def noop(*_args, **_kwargs):
    return None


class Harness:
    def __init__(self, directory, factory):
        self.directory = directory
        self.factory = factory
        self.owner_id = "stub-owner"
        self.http = []
        self.client = None

    async def request(self, method, path, body=None, expected=200):
        headers = (
            {"Idempotency-Key": body["idempotency_key"]}
            if body and "idempotency_key" in body
            else {}
        )
        response = await self.client.request(
            method, "/api/canvases" + path, json=body, headers=headers
        )
        assert response.status_code == expected, (
            path,
            response.status_code,
            response.text,
        )
        self.http.append(
            {"method": method, "path": path, "status": response.status_code}
        )
        return response.json()

    async def dispatch(self, run_id):
        async with self.factory() as db:
            return await dispatch_plan_once(db, run_id=run_id)

    async def worker(self, entrypoint="lumen_canvas_stub_worker.py"):
        assert entrypoint in {
            "lumen_canvas_stub_worker.py",
            "lumen_canvas_stub_video_worker.py",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "scripts" / entrypoint),
            str(self.directory),
            cwd=self.directory,
            env=child_env(self.directory, "worker"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=45)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        with (self.directory / "worker.log").open("ab") as stream:
            stream.write(output)
        assert process.returncode == 0, output.decode()[-6000:]

    async def start(self, key, prompt="local stub only", single=False):
        source = copy.deepcopy(graph())
        source["nodes"][0]["config"]["text"] = prompt
        source["nodes"][1]["config"]["count"] = 1 if single else 2
        if single:
            source["nodes"] = source["nodes"][:2]
            source["edges"] = source["edges"][:1]
        document = await self.request("POST", "", {"title": key, "graph": source})
        prefix = "/" + document["id"]
        body = {
            "document_revision": document["revision"],
            "kind": "all",
            "budget_micro": 100 if single else 500,
            "output_indices": {} if single else {"a": 1},
            "failure_policy": "continue_independent",
            "auto_select_on_success": True,
        }
        preview = await self.request("POST", prefix + "/plans/preview", body)
        start = {
            **body,
            "plan_hash": preview["plan"]["plan_hash"],
            "idempotency_key": key,
        }
        run = await self.request("POST", prefix + "/plans/run", start)
        return prefix, body, start, run

    async def snapshot(self):
        async with self.factory() as db:
            wallet = await db.get(UserWallet, "stub-owner")
            tasks = (
                await db.execute(select(func.count()).select_from(Generation))
            ).scalar_one()
            images = (
                await db.execute(select(func.count()).select_from(Image))
            ).scalar_one()
            transactions = (
                await db.execute(select(func.count()).select_from(WalletTransaction))
            ).scalar_one()
            return tasks, images, wallet.balance_micro, wallet.hold_micro, transactions

    async def scenario(self, prompt, key):
        prefix, body, start, run = await self.start(key, prompt, single=True)
        assert await self.dispatch(run["id"])
        await self.worker()
        await self.dispatch(run["id"])
        detail = await self.request("GET", prefix + "/runs/" + run["id"])
        return prefix, body, start, detail


async def successful_chain(harness):
    h = harness
    prefix, _, start, run = await h.start("local-harness-original")
    run_id = run["id"]
    replay = await h.request("POST", prefix + "/plans/run", start)
    assert replay["id"] == run_id
    receipt = await h.request("GET", prefix + "/plans/intents/local-harness-original")
    assert receipt["admitted"] and receipt["run"]["id"] == run_id
    assert await h.dispatch(run_id)
    assert not await h.dispatch(run_id), "b must wait for a's actual artifact"
    async with h.factory() as db:
        assert len(list((await db.execute(select(Generation))).scalars())) == 2
        assert len(list((await db.execute(select(CanvasExecutionTask))).scalars())) == 2
        assert len(list((await db.execute(select(OutboxEvent))).scalars())) == 2
        assert (await db.get(UserWallet, "stub-owner")).hold_micro == 200
    await h.worker()
    assert await h.dispatch(run_id)
    await h.worker()
    await h.dispatch(run_id)
    detail = await h.request("GET", prefix + "/runs/" + run_id)
    assert detail["status"] == "succeeded" and len(detail["executions"]) == 2
    assert all(
        item["status"] == "succeeded" and item["outputs"]
        for item in detail["executions"]
    )
    document = await h.request("GET", prefix)
    assert len(document["selections"]) == 2
    await verify_artifacts_and_candidate(h, detail)
    before = await h.snapshot()
    assert before[:4] == (3, 3, 99_700, 0), before
    async with h.factory() as db:
        transactions = list((await db.execute(select(WalletTransaction))).scalars())
        assert len([row for row in transactions if row.kind == "settle"]) == 3
    await h.request("POST", prefix + "/plans/run", start)
    await h.worker()
    await h.request("GET", prefix)
    assert await h.snapshot() == before
    async with h.factory() as db:
        assert (
            await db.execute(select(func.count()).select_from(CanvasRun))
        ).scalar_one() == 1
    h.owner_id = "stub-other"
    await h.request("GET", prefix, expected=404)
    await h.request(
        "GET", prefix + "/plans/intents/local-harness-original", expected=404
    )
    h.owner_id = "stub-owner"
    return {"run": detail, "document": document, "snapshot": before}


async def verify_artifacts_and_candidate(h, detail):
    by_node = {execution["node_id"]: execution for execution in detail["executions"]}
    selected = by_node["a"]["outputs"][1]
    assert selected["ordinal"] == 1
    async with h.factory() as db:
        downstream = await db.get(Generation, by_node["b"]["tasks"][0]["generation_id"])
        assert downstream.primary_input_image_id == selected["image_id"]
        for execution in detail["executions"]:
            for output in execution["outputs"]:
                image = await db.get(Image, output["image_id"])
                raw = (h.directory / "artifacts" / image.storage_key).read_bytes()
                assert (
                    hashlib.sha256(raw).hexdigest() == output["sha256"] == image.sha256
                )


async def confirmed_failure_repair(h):
    prefix, _, _, failed = await h.scenario("stub-fail-once", "confirmed-failure")
    execution = failed["executions"][0]
    assert execution["status"] == "failed" and not execution["outputs"]
    repair = {
        "execution_ids": [execution["id"]],
        "additional_budget_micro": 0,
        "idempotency_key": "confirmed-repair",
    }
    path = prefix + "/runs/" + failed["id"] + "/retry-failed"
    await h.request("POST", path, repair, expected=422)
    repair["additional_budget_micro"] = 100
    await h.request("POST", path, repair)
    await h.request("POST", path, repair)
    receipt = await h.request(
        "GET", prefix + "/plans/intents/confirmed-repair?run_id=" + failed["id"]
    )
    assert receipt["admitted"]
    assert await h.dispatch(failed["id"])
    await h.worker()
    await h.dispatch(failed["id"])
    repaired = await h.request("GET", prefix + "/runs/" + failed["id"])
    assert repaired["status"] == "succeeded" and len(repaired["executions"]) == 2
    assert sorted(item["status"] for item in repaired["executions"]) == [
        "failed",
        "succeeded",
    ]
    return repaired


async def fail_fast_exact_closure(h):
    source = graph()
    source["nodes"][0]["config"]["text"] = "stub-fail-fast-once"
    source["nodes"].append(
        {
            "id": "c",
            "type": "image_generate",
            "position": {"x": 300, "y": 300},
            "config": {"quality": "1k"},
        }
    )
    source["edges"].append(
        {
            "id": "pc",
            "source_node_id": "prompt",
            "target_node_id": "c",
            "source_handle": "text",
            "target_handle": "prompt",
            "data_type": "text",
        }
    )
    document = await h.request(
        "POST", "", {"title": "exact fail-fast closure", "graph": source}
    )
    prefix = "/" + document["id"]
    body = {
        "document_revision": document["revision"],
        "kind": "all",
        "budget_micro": 300,
        "failure_policy": "fail_fast",
        "auto_select_on_success": True,
    }
    preview = await h.request("POST", prefix + "/plans/preview", body)
    run = await h.request(
        "POST",
        prefix + "/plans/run",
        {
            **body,
            "plan_hash": preview["plan"]["plan_hash"],
            "idempotency_key": "failfast-original",
        },
    )
    run_id = run["id"]
    assert await h.dispatch(run_id)
    await h.worker()
    await h.dispatch(run_id)
    original = await h.request("GET", prefix + "/runs/" + run_id)
    by_node = {row["node_id"]: row for row in original["executions"]}
    assert {node: row["status"] for node, row in by_node.items()} == {
        "a": "failed",
        "b": "blocked",
        "c": "blocked",
    }
    repair = {
        "execution_ids": [by_node["a"]["id"]],
        "additional_budget_micro": 0,
        "idempotency_key": "failfast-repair",
    }
    path = prefix + "/runs/" + run_id + "/retry-failed"
    await h.request("POST", path, repair)
    await h.request("POST", path, repair)
    assert await h.dispatch(run_id)
    await h.worker()
    assert await h.dispatch(run_id)
    await h.worker()
    await h.dispatch(run_id)
    repaired = await h.request("GET", prefix + "/runs/" + run_id)
    latest = {}
    for row in repaired["executions"]:
        if (
            row["node_id"] not in latest
            or row["attempt"] > latest[row["node_id"]]["attempt"]
        ):
            latest[row["node_id"]] = row
    assert latest["a"]["status"] == latest["b"]["status"] == "succeeded"
    assert latest["a"]["attempt"] == latest["b"]["attempt"] == 1
    assert (
        latest["c"]["id"] == by_node["c"]["id"] and latest["c"]["status"] == "blocked"
    )
    assert not latest["c"]["tasks"]
    return repaired


async def unknown_stays_blocked(h):
    prefix, body, start, unknown = await h.scenario("stub-unknown", "unknown-original")
    execution = unknown["executions"][0]
    assert execution["status"] == "failed"
    before = await h.snapshot()
    blocked = await h.request("POST", prefix + "/plans/preview", body, expected=409)
    assert blocked["detail"]["error"]["code"] == "canvas_execution_unknown", blocked
    await h.request(
        "POST",
        prefix + "/runs/" + unknown["id"] + "/retry-failed",
        {
            "execution_ids": [execution["id"]],
            "additional_budget_micro": 100,
            "idempotency_key": "unsafe-unknown-repair",
        },
        expected=409,
    )
    await h.request("POST", prefix + "/plans/run", start)
    await h.worker()
    await h.request("GET", prefix)
    assert await h.snapshot() == before
    return {"run": unknown, "snapshot": before, "no_additional_task_or_charge": True}


def application(h):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def database():
        async with h.factory() as db:
            yield db

    async def owner():
        async with h.factory() as db:
            return await db.get(User, h.owner_id)

    async def csrf():
        return None

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = owner
    app.dependency_overrides[verify_csrf] = csrf
    event_commit.publish_canvas_notices = noop
    execution_service.publish_canvas_image_task = noop
    return app


async def main(directory):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await bootstrap(engine, factory)
        harness = Harness(directory, factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application(harness)),
            base_url="http://stub.local",
        ) as client:
            harness.client = client
            success = await successful_chain(harness)
            repaired = await confirmed_failure_repair(harness)
            closure = await fail_fast_exact_closure(harness)
            unknown = await unknown_stays_blocked(harness)
        calls = [
            json.loads(line)
            for line in (directory / "provider-calls.jsonl").read_text().splitlines()
        ]
        assert len(calls) == 9, calls
        assert NETWORK_ATTEMPTS == 0
        evidence = {
            "http": harness.http,
            "success": success,
            "confirmed_failure_repair": repaired,
            "fail_fast_exact_closure": closure,
            "unknown": unknown,
            "provider_calls": calls,
            "provider_network_calls": 0,
            "boundaries": [
                "synthetic HTTP authentication",
                "locally driven outbox/ARQ dispatch",
                "in-process provider and reservation stub",
                "SQLite ARRAY defaults adapted only",
            ],
        }
        output = directory / "evidence.json"
        output.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
        print(
            json.dumps(
                {
                    "result": "passed",
                    "evidence": str(output),
                    "provider_calls": len(calls),
                    "success_wallet_debit_micro": 300,
                    "unknown_replay_unchanged": True,
                }
            )
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
