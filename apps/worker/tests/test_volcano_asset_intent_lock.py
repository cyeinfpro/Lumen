from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from arq import Retry
from lumen_core.video_providers import VideoProviderDefinition
from lumen_core.volcano_assets import (
    VolcanoAssetServiceError,
    volcano_asset_operation_key,
)

from apps.worker.tests.volcano_asset_test_support import (
    Redis as _Redis,
    operation as _operation,
    provider as _provider,
)


_INTENT_LOCK_PREFIX = "video-assets:create-intent:"


def _listed_asset(asset_id: str, name: str) -> dict[str, Any]:
    return {
        "Id": asset_id,
        "GroupId": "group-1",
        "Name": name,
        "AssetType": "Video",
        "Status": "Active",
        "ProjectName": "project-a",
    }


def _install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    volcano_assets: Any,
    provider: VideoProviderDefinition,
    client: Any,
    receipts: list[tuple[str, str]],
) -> None:
    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def normalized(operation: dict[str, Any]) -> tuple[str, str]:
        return (
            f"https://lumen.example/{operation['id']}.mp4",
            "volcano_asset_video_v1",
        )

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_receipt(_operation: dict[str, Any]) -> None:
        return None

    async def write_receipt(
        operation: dict[str, Any],
        asset: dict[str, Any],
    ) -> None:
        receipts.append((str(operation["id"]), str(asset["id"])))

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: client)
    monkeypatch.setattr(volcano_assets, "_normalized_source_url", normalized)
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", noop)
    monkeypatch.setattr(volcano_assets, "_write_audit", noop)
    monkeypatch.setattr(volcano_assets, "_read_success_receipt", no_receipt)
    monkeypatch.setattr(volcano_assets, "_write_success_receipt", write_receipt)


@pytest.mark.asyncio
async def test_http_429_clears_uncertain_state_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_operation())
    receipts: list[tuple[str, str]] = []
    create_calls = 0
    filtered_list_calls = 0

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            nonlocal create_calls, filtered_list_calls
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            if action == "ListAssets":
                if "Name" in (body.get("Filter") or {}):
                    filtered_list_calls += 1
                return {"Assets": [], "TotalCount": 0}
            create_calls += 1
            if create_calls == 1:
                raise VolcanoAssetServiceError(
                    "volcano_asset_rate_limited",
                    "Volcano asset service rate limited the request",
                    429,
                    retry_after_ms=4_500,
                )
            return {"Id": "asset-after-retry"}

    _install_runtime(
        monkeypatch,
        volcano_assets,
        provider,
        Client(),
        receipts,
    )

    first = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
        1,
        0,
    )

    assert first["status"] == "failed"
    assert first["error"]["code"] == "volcano_asset_rate_limited"
    failed = redis.operation()
    assert failed["retryable"] is True
    assert failed["retry_after_seconds"] == 5
    assert failed["submit_started_at"] is None
    assert failed["submit_outcome_uncertain"] is False
    assert failed["baseline_asset_ids"] == []
    assert filtered_list_calls == 1
    assert receipts == []
    assert not any(key.startswith(_INTENT_LOCK_PREFIX) for key in redis.values)

    failed.update(
        {
            "status": "queued",
            "progress_stage": "queued",
            "attempt": 2,
            "completed_at": None,
            "result": None,
            "error": None,
        }
    )
    redis.values[volcano_asset_operation_key("operation-1")] = json.dumps(failed)

    second = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
        2,
        0,
    )

    assert second["status"] == "succeeded"
    assert second["result"]["id"] == "asset-after-retry"
    assert create_calls == 2
    assert receipts == [("operation-1", "asset-after-retry")]


@pytest.mark.asyncio
async def test_same_intent_operations_do_not_share_reconcile_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation_one = _operation()
    operation_two = {
        **_operation(),
        "id": "operation-2",
        "user_id": "user-2",
        "local_source_id": "video-2",
    }
    redis = _Redis([operation_one, operation_two])
    receipts: list[tuple[str, str]] = []
    upstream_assets: list[dict[str, Any]] = []
    first_submit_started = asyncio.Event()
    allow_first_success = asyncio.Event()
    create_calls = 0

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            nonlocal create_calls
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            if action == "ListAssets":
                name = str((body.get("Filter") or {}).get("Name") or "")
                assets = [
                    asset
                    for asset in upstream_assets
                    if not name or asset["Name"] == name
                ]
                return {"Assets": assets, "TotalCount": len(assets)}
            create_calls += 1
            if create_calls == 1:
                first_submit_started.set()
                await allow_first_success.wait()
                asset = _listed_asset(
                    "asset-operation-1",
                    str(body.get("Name") or ""),
                )
                upstream_assets.append(asset)
                return {"Id": asset["Id"]}
            raise VolcanoAssetServiceError(
                "volcano_asset_timeout",
                "Volcano asset service timed out",
                504,
            )

    _install_runtime(
        monkeypatch,
        volcano_assets,
        provider,
        Client(),
        receipts,
    )

    first_task = asyncio.create_task(
        volcano_assets.process_volcano_asset_operation(
            {"redis": redis},
            "operation-1",
            1,
            0,
        )
    )
    await asyncio.wait_for(first_submit_started.wait(), timeout=2)
    try:
        with pytest.raises(Retry):
            await volcano_assets.process_volcano_asset_operation(
                {"redis": redis},
                "operation-2",
                1,
                0,
            )
    finally:
        allow_first_success.set()
    first = await first_task

    assert first["status"] == "succeeded"
    assert receipts == [("operation-1", "asset-operation-1")]
    assert redis.operation("operation-2")["progress_stage"] == "waiting_intent_lock"

    second = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-2",
        1,
        0,
    )

    assert second["status"] == "failed"
    assert second["error"]["code"] == "volcano_asset_create_reconcile_ambiguous"
    assert create_calls == 2
    assert receipts == [("operation-1", "asset-operation-1")]
    intent_owners = [
        value
        for key, value in redis.values.items()
        if key.startswith(_INTENT_LOCK_PREFIX)
    ]
    assert intent_owners == ["operation-2"]


@pytest.mark.asyncio
async def test_different_create_asset_intents_can_submit_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation_one = {**_operation(), "name": "Alpha Upload"}
    operation_two = {
        **_operation(),
        "id": "operation-2",
        "user_id": "user-2",
        "local_source_id": "video-2",
        "name": "Beta Upload",
    }
    redis = _Redis([operation_one, operation_two])
    receipts: list[tuple[str, str]] = []
    upstream_assets: list[dict[str, Any]] = []
    both_submitting = asyncio.Event()
    active_submits = 0
    max_active_submits = 0

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            nonlocal active_submits, max_active_submits
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            if action == "ListAssets":
                name = str((body.get("Filter") or {}).get("Name") or "")
                assets = [
                    asset
                    for asset in upstream_assets
                    if not name or asset["Name"] == name
                ]
                return {"Assets": assets, "TotalCount": len(assets)}
            active_submits += 1
            max_active_submits = max(max_active_submits, active_submits)
            if active_submits == 2:
                both_submitting.set()
            try:
                await asyncio.wait_for(both_submitting.wait(), timeout=2)
                name = str(body.get("Name") or "")
                asset = _listed_asset(f"asset-{name.lower().replace(' ', '-')}", name)
                upstream_assets.append(asset)
                return {"Id": asset["Id"]}
            finally:
                active_submits -= 1

    _install_runtime(
        monkeypatch,
        volcano_assets,
        provider,
        Client(),
        receipts,
    )

    first, second = await asyncio.gather(
        volcano_assets.process_volcano_asset_operation(
            {"redis": redis},
            "operation-1",
            1,
            0,
        ),
        volcano_assets.process_volcano_asset_operation(
            {"redis": redis},
            "operation-2",
            1,
            0,
        ),
    )

    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert max_active_submits == 2
    assert sorted(receipts) == [
        ("operation-1", "asset-alpha-upload"),
        ("operation-2", "asset-beta-upload"),
    ]
    assert not any(key.startswith(_INTENT_LOCK_PREFIX) for key in redis.values)
