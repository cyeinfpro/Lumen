from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from lumen_core.video_providers import VideoProviderDefinition
from lumen_core.volcano_assets import (
    VolcanoAssetClient,
    VolcanoAssetCreateRateLimited,
    VolcanoAssetOperationOwnershipError,
    VolcanoAssetQuotaExceeded,
    VolcanoAssetQuotaKey,
    VolcanoAssetServiceError,
    acquire_volcano_create_rate_limit,
    compare_and_set_volcano_asset_operation,
    normalize_asset,
    normalize_volcano_asset_name,
    release_volcano_create_rate_limit,
    reserve_volcano_asset_quota,
    volcano_asset_safe_filename,
)


class _Redis:
    def __init__(self, result: list[int]) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: Any) -> list[int]:
        self.calls.append(args)
        return self.result


class _FlakyRedis:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: Any) -> Any:
        self.calls.append(args)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def zrem(self, *args: Any) -> Any:
        self.calls.append(args)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _LostCasResponseRedis:
    def __init__(self, current: dict[str, Any]) -> None:
        self.raw = json.dumps(current)
        self.calls = 0

    async def eval(
        self,
        _script: str,
        _numkeys: int,
        _key: str,
        owner: str,
        expected_status: str,
        expected_attempt: int,
        _expected_progress: str,
        replacement: str,
        _ttl: int,
    ) -> list[Any]:
        self.calls += 1
        current = json.loads(self.raw)
        if self.calls == 1:
            assert current["user_id"] == owner
            assert current["status"] == expected_status
            assert current["attempt"] == expected_attempt
            self.raw = replacement
            raise ConnectionError("response lost after commit")
        return [2, self.raw]


def _key() -> VolcanoAssetQuotaKey:
    return VolcanoAssetQuotaKey(
        provider_name="volcano-main",
        project_name="project-a",
        region="cn-beijing",
    )


def _provider() -> VideoProviderDefinition:
    return VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="ark-key",
        access_key_id="AKLTasset",
        secret_access_key="secret-asset-key",
        project_name="project-a",
        region="cn-beijing",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0"},
    )


@pytest.mark.asyncio
async def test_redis_quota_reservation_is_atomic_and_counts_other_jobs() -> None:
    redis = _Redis([1, 2])

    await reserve_volcano_asset_quota(
        redis,
        _key(),
        resource="assets",
        operation_id="operation-1",
        upstream_total=47,
        limit=50,
        now_ms=1_000_000,
    )

    script = str(redis.calls[0][0])
    assert "ZREMRANGEBYSCORE" in script
    assert "ZSCORE" in script
    assert "ZADD" in script
    assert redis.calls[0][1] == 1


@pytest.mark.asyncio
async def test_redis_quota_rejection_reports_upstream_and_reservations() -> None:
    redis = _Redis([0, 1])

    with pytest.raises(VolcanoAssetQuotaExceeded) as exc_info:
        await reserve_volcano_asset_quota(
            redis,
            _key(),
            resource="assets",
            operation_id="operation-2",
            upstream_total=49,
            limit=50,
            now_ms=1_000_000,
        )

    assert exc_info.value.upstream_total == 49
    assert exc_info.value.local_reservations == 1


@pytest.mark.asyncio
async def test_redis_create_rate_limit_returns_precise_retry_after() -> None:
    redis = _Redis([0, 12_500])

    with pytest.raises(VolcanoAssetCreateRateLimited) as exc_info:
        await acquire_volcano_create_rate_limit(
            redis,
            _key(),
            bucket="submit",
            operation_id="operation-3",
            now_ms=1_000_000,
        )

    assert exc_info.value.retry_after_ms == 12_500
    script = str(redis.calls[0][0])
    assert "ZREMRANGEBYSCORE" in script
    assert "ZRANGE" in script
    assert "ZADD" in script
    assert script.index("ZSCORE") < script.index("ZCARD")


@pytest.mark.asyncio
async def test_redis_quota_reservation_retries_transient_response_loss() -> None:
    redis = _FlakyRedis([TimeoutError("lost response"), [1, 0]])

    await reserve_volcano_asset_quota(
        redis,
        _key(),
        resource="assets",
        operation_id="operation-retry",
        upstream_total=1,
        limit=50,
        now_ms=1_000_000,
    )

    assert len(redis.calls) == 2
    assert redis.calls[0][7] == redis.calls[1][7] == "operation-retry"


@pytest.mark.asyncio
async def test_rate_limit_release_retries_and_is_idempotent() -> None:
    redis = _FlakyRedis([ConnectionError("temporary"), 1])

    await release_volcano_create_rate_limit(
        redis,
        _key(),
        bucket="admission",
        operation_id="operation-1",
    )

    assert len(redis.calls) == 2
    assert redis.calls[0] == redis.calls[1]


@pytest.mark.asyncio
async def test_operation_compare_and_set_rejects_owner_mismatch() -> None:
    redis = _Redis([-2, 0])

    with pytest.raises(VolcanoAssetOperationOwnershipError):
        await compare_and_set_volcano_asset_operation(
            redis,
            "operation-1",
            owner_user_id="user-2",
            expected_status="failed",
            expected_attempt=1,
            replacement={"id": "operation-1"},
        )

    script = str(redis.calls[0][0])
    assert "user_id" in script
    assert "expected_progress" in script


@pytest.mark.asyncio
async def test_operation_compare_and_set_recovers_lost_success_response() -> None:
    redis = _LostCasResponseRedis(
        {
            "id": "operation-1",
            "user_id": "user-1",
            "status": "failed",
            "attempt": 1,
        }
    )
    replacement = {
        "id": "operation-1",
        "user_id": "user-1",
        "status": "queued",
        "attempt": 2,
    }

    swapped, current = await compare_and_set_volcano_asset_operation(
        redis,
        "operation-1",
        owner_user_id="user-1",
        expected_status="failed",
        expected_attempt=1,
        replacement=replacement,
    )

    assert swapped is False
    assert current == replacement
    assert redis.calls == 2


def test_safe_filename_never_uses_unsafe_source_text() -> None:
    filename = volcano_asset_safe_filename(
        "../User Original 名称.PNG",
        asset_type="Image",
    )

    assert filename.startswith("lumen-asset-")
    assert filename.endswith(".jpg")
    assert "/" not in filename
    assert "\\" not in filename
    assert "User" not in filename


def test_empty_asset_name_gets_deterministic_safe_fallback() -> None:
    first = normalize_volcano_asset_name(" \x00\n", fallback_id="operation-1")
    second = normalize_volcano_asset_name(None, fallback_id="operation-1")

    assert first == second
    assert first.startswith("lumen-asset-")
    assert len(first) < 64


def test_normalize_asset_removes_internal_and_sensitive_urls() -> None:
    internal = normalize_asset(
        {
            "Id": "asset-1",
            "GroupId": "group-1",
            "AssetType": "Image",
            "URL": (
                "https://lumen.example/api/images/reference/image-1/binary"
                "?token=internal-secret"
            ),
        },
        project_name="project-a",
    )
    external = normalize_asset(
        {
            "Id": "asset-2",
            "GroupId": "group-1",
            "AssetType": "Image",
            "URL": (
                "https://cdn.example.com/asset.jpg?width=300"
                "&X-Tos-Signature=secret-signature"
                "&X-Tos-Security-Token=secret-token#preview"
            ),
        },
        project_name="project-a",
    )
    credentialed = normalize_asset(
        {
            "Id": "asset-3",
            "GroupId": "group-1",
            "AssetType": "Image",
            "URL": "https://user:password@cdn.example.com/asset.jpg",
        },
        project_name="project-a",
    )

    assert internal["url"] is None
    assert external["url"] == "https://cdn.example.com/asset.jpg?width=300"
    assert credentialed["url"] is None


@pytest.mark.asyncio
async def test_volcano_asset_client_rejects_redirect_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lumen_core import volcano_assets

    response = httpx.Response(
        302,
        json={"Result": {"Id": "asset-1"}},
        request=httpx.Request("POST", "https://example.test"),
    )

    class _Client:
        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            return response

    async def no_proxy(_proxy: Any) -> None:
        return None

    monkeypatch.setattr(
        volcano_assets.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(),
    )

    with pytest.raises(VolcanoAssetServiceError) as exc_info:
        await VolcanoAssetClient(
            _provider(),
            proxy_resolver=no_proxy,
        ).request(
            "GetAsset",
            {"Id": "asset-1", "ProjectName": "project-a"},
        )

    assert exc_info.value.code == "volcano_asset_upstream_error"
    assert exc_info.value.status_code == 502
    assert exc_info.value.details["upstream_status"] == 302
