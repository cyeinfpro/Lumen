from __future__ import annotations

import json
from typing import Any

import pytest

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


def _asset(asset_id: str, *, asset_type: str = "Video") -> dict[str, Any]:
    return {
        "Id": asset_id,
        "GroupId": "group-1",
        "Name": "User Display Name",
        "AssetType": asset_type,
        "Status": "Active",
        "ProjectName": "project-a",
    }


def _install_create_asset_runtime(
    monkeypatch: pytest.MonkeyPatch,
    volcano_assets: Any,
    provider: VideoProviderDefinition,
    client: Any,
) -> None:
    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def normalized(_operation: dict[str, Any]) -> tuple[str, str]:
        return ("https://lumen.example/safe.mp4", "volcano_asset_video_v1")

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def reject_receipt(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ambiguous operation must not write ownership receipt")

    async def no_receipt(_operation: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: client)
    monkeypatch.setattr(volcano_assets, "_normalized_source_url", normalized)
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", noop)
    monkeypatch.setattr(volcano_assets, "_write_audit", noop)
    monkeypatch.setattr(volcano_assets, "_read_success_receipt", no_receipt)
    monkeypatch.setattr(volcano_assets, "_write_success_receipt", reject_receipt)


@pytest.mark.asyncio
async def test_later_page_existing_asset_is_in_submit_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_operation())
    create_calls = 0
    filtered_pages: list[int] = []
    existing = [_asset(f"asset-old-{index}") for index in range(101)]

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
                asset_filter = body.get("Filter") or {}
                if "Name" not in asset_filter:
                    return {"Assets": [], "TotalCount": 0}
                page = int(body["PageNumber"])
                filtered_pages.append(page)
                start = (page - 1) * 100
                return {
                    "Assets": existing[start : start + 100],
                    "TotalCount": len(existing),
                }
            create_calls += 1
            raise VolcanoAssetServiceError(
                "volcano_asset_timeout",
                "Volcano asset service timed out",
                504,
            )

    client = Client()
    _install_create_asset_runtime(monkeypatch, volcano_assets, provider, client)

    first = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
        1,
        0,
    )

    assert first["status"] == "failed"
    assert first["error"]["code"] == "volcano_asset_create_reconcile_ambiguous"
    assert "asset-old-100" in redis.operation()["baseline_asset_ids"]
    assert create_calls == 1
    assert 2 in filtered_pages

    failed = redis.operation()
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

    assert second["status"] == "failed"
    assert second["error"]["code"] == "volcano_asset_create_reconcile_ambiguous"
    assert create_calls == 1


@pytest.mark.asyncio
async def test_later_page_second_new_candidate_keeps_reconcile_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    old_assets = [_asset(f"asset-old-{index}") for index in range(100)]
    operation = {
        **_operation(),
        "submit_started_at": "2026-07-15T00:00:10+00:00",
        "submit_outcome_uncertain": True,
        "source_url": "https://lumen.example/safe.mp4",
        "baseline_asset_ids": [asset["Id"] for asset in old_assets],
    }
    redis = _Redis(operation)
    create_calls = 0
    filtered_pages: list[int] = []
    pages = {
        1: [*old_assets[:99], _asset("asset-new-first")],
        2: [
            old_assets[99],
            _asset("asset-new-second"),
            _asset("asset-wrong-type", asset_type="Image"),
        ],
    }

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
                page = int(body["PageNumber"])
                filtered_pages.append(page)
                return {
                    "Assets": pages.get(page, []),
                    "TotalCount": 103,
                }
            create_calls += 1
            return {"Id": "must-not-submit"}

    client = Client()
    _install_create_asset_runtime(monkeypatch, volcano_assets, provider, client)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
        1,
        0,
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "volcano_asset_create_reconcile_ambiguous"
    assert create_calls == 0
    assert filtered_pages.count(2) == 3


@pytest.mark.asyncio
async def test_operation_asset_scan_stops_at_safe_item_limit() -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _operation()
    requested_pages: list[int] = []

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            assert action == "ListAssets"
            page = int(body["PageNumber"])
            requested_pages.append(page)
            start = (page - 1) * 100
            return {
                "Assets": [
                    _asset(f"asset-{index}") for index in range(start, start + 100)
                ],
                "TotalCount": 3001,
            }

    assets, complete = await volcano_assets._scan_operation_assets(
        Client(),
        provider,
        operation,
    )

    assert complete is False
    assert len(assets) == 3000
    assert requested_pages == list(range(1, 31))


@pytest.mark.asyncio
async def test_operation_asset_scan_stops_when_page_makes_no_progress() -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _operation()
    page = [_asset(f"asset-{index}") for index in range(100)]
    requested_pages: list[int] = []

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            assert action == "ListAssets"
            requested_pages.append(int(body["PageNumber"]))
            return {"Assets": page, "TotalCount": 200}

    assets, complete = await volcano_assets._scan_operation_assets(
        Client(),
        provider,
        operation,
    )

    assert complete is False
    assert len(assets) == 100
    assert requested_pages == [1, 2]
