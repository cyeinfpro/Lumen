from __future__ import annotations

import json
from typing import Any

import pytest
from arq import Retry
from lumen_core.video_providers import (
    VideoProviderDefinition,
    video_provider_binding_fingerprint,
)
from lumen_core.volcano_assets import (
    VolcanoAssetCreateRateLimited,
    VolcanoAssetQuotaExceeded,
    VolcanoAssetServiceError,
    volcano_asset_operation_key,
)

from apps.worker.tests.volcano_asset_test_support import (
    Redis as _Redis,
)
from apps.worker.tests.volcano_asset_test_support import (
    management_operation as _management_operation,
)
from apps.worker.tests.volcano_asset_test_support import (
    operation as _operation,
)
from apps.worker.tests.volcano_asset_test_support import (
    provider as _provider,
)


@pytest.mark.asyncio
async def test_provider_operation_binding_uses_exact_named_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    expected = _provider()
    higher_priority = VideoProviderDefinition(
        name="volcano-other",
        kind="volcano",
        base_url="https://other.example/v1",
        api_key="other-generation-key",
        access_key_id="AKLTOTHER",
        secret_access_key="other-secret",
        project_name="project-other",
        region="cn-beijing",
        priority=999,
        models={"seedance:reference": "doubao-seedance-ref"},
    )
    operation = {
        **_operation(),
        "provider_binding": video_provider_binding_fingerprint(expected),
    }

    async def resolve(_key: str) -> str:
        return "{}"

    def parse(*_args: Any, **_kwargs: Any) -> tuple[Any, Any, Any]:
        return [higher_priority, expected], [], []

    monkeypatch.setattr(volcano_assets.runtime_settings, "resolve", resolve)
    monkeypatch.setattr(volcano_assets, "parse_video_provider_config_json", parse)

    selected = await volcano_assets._provider_for_operation(operation)  # noqa: SLF001
    assert selected.name == "volcano-main"

    changed = VideoProviderDefinition(
        **{
            **expected.__dict__,
            "secret_access_key": "rotated-secret",
        }
    )

    def parse_changed(*_args: Any, **_kwargs: Any) -> tuple[Any, Any, Any]:
        return [changed], [], []

    monkeypatch.setattr(
        volcano_assets,
        "parse_video_provider_config_json",
        parse_changed,
    )
    with pytest.raises(
        volcano_assets._OperationFailure,  # noqa: SLF001
        match="credentials or route have changed",
    ):
        await volcano_assets._provider_for_operation(operation)  # noqa: SLF001


@pytest.fixture(autouse=True)
def _stub_success_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import volcano_assets

    async def read(_operation: dict[str, Any]) -> dict[str, Any] | None:
        return None

    async def write(
        _operation: dict[str, Any],
        _asset: dict[str, Any],
    ) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_read_success_receipt", read)
    monkeypatch.setattr(volcano_assets, "_write_success_receipt", write)


@pytest.mark.asyncio
async def test_worker_create_asset_forces_scope_and_safe_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _operation()
    redis = _Redis(operation)
    calls: list[tuple[str, dict[str, Any]]] = []
    audits: list[tuple[str, dict[str, Any]]] = []
    released: list[str] = []

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            calls.append((action, body))
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            if action == "ListAssets":
                return {"Assets": [], "TotalCount": 49}
            return {"Id": "asset-1"}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def normalized(_operation: dict[str, Any]) -> tuple[str, str]:
        return (
            "https://lumen.example/api/videos/reference/video-1/"
            "binary/lumen-asset-video-1.mp4"
            "?token=secret-token&variant=volcano_asset_video_v1",
            "volcano_asset_video_v1",
        )

    async def reserve(*_args: Any, **kwargs: Any) -> None:
        assert kwargs["resource"] == "assets"
        assert kwargs["upstream_total"] == 49
        assert kwargs["limit"] == 50

    async def release(*_args: Any, **kwargs: Any) -> None:
        released.append(kwargs["operation_id"])

    async def acquire(*_args: Any, **kwargs: Any) -> None:
        assert kwargs["bucket"] == "submit"

    async def audit(
        _operation: dict[str, Any],
        *,
        event_type: str,
        details: dict[str, Any],
    ) -> None:
        audits.append((event_type, details))

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_normalized_source_url", normalized)
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", reserve)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", release)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", acquire)
    monkeypatch.setattr(volcano_assets, "_write_audit", audit)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "succeeded"
    assert calls[0] == (
        "GetAssetGroup",
        {"Id": "group-1", "ProjectName": "project-a"},
    )
    assert calls[1] == (
        "ListAssets",
        {
            "ProjectName": "project-a",
            "Filter": {"GroupType": "AIGC"},
            "PageNumber": 1,
            "PageSize": 1,
        },
    )
    assert calls[2] == (
        "ListAssets",
        {
            "ProjectName": "project-a",
            "Filter": {
                "GroupType": "AIGC",
                "GroupIds": ["group-1"],
                "Name": "User Display Name",
            },
            "PageNumber": 1,
            "PageSize": 100,
        },
    )
    action, body = calls[3]
    assert action == "CreateAsset"
    assert body["ProjectName"] == "project-a"
    assert body["AssetType"] == "Video"
    assert body["Name"] == "User Display Name"
    assert "/binary/lumen-asset-video-1.mp4?" in body["URL"]
    assert "User Display Name" not in body["URL"]
    stored = redis.operation()
    assert stored["status"] == "succeeded"
    assert stored["result"]["status"] == "Processing"
    assert released == ["operation-1"]
    assert audits[0][0] == "video_asset.create"
    assert "URL" not in str(audits)
    assert "secret-token" not in str(audits)
    assert "secret-example" not in str(stored)


@pytest.mark.asyncio
async def test_worker_asset_quota_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_operation())

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            return {"Assets": [], "TotalCount": 50}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def reject(*_args: Any, **_kwargs: Any) -> None:
        raise VolcanoAssetQuotaExceeded(
            resource="assets",
            limit=50,
            upstream_total=50,
            local_reservations=0,
        )

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", reject)
    monkeypatch.setattr(volcano_assets, "_write_audit", audit)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "failed"
    stored = redis.operation()
    assert stored["status"] == "failed"
    assert stored["retryable"] is True
    assert stored["error"]["code"] == "volcano_asset_quota_exceeded"


@pytest.mark.asyncio
async def test_worker_defers_when_actual_create_qpm_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_operation())
    released: list[str] = []

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            return {"Assets": [], "TotalCount": 1}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def reserve(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def release(*_args: Any, **kwargs: Any) -> None:
        released.append(kwargs["operation_id"])

    async def normalized(_operation: dict[str, Any]) -> tuple[str, str]:
        return ("https://lumen.example/safe.mp4", "volcano_asset_video_v1")

    async def reject(*_args: Any, **_kwargs: Any) -> None:
        raise VolcanoAssetCreateRateLimited(retry_after_ms=4_500)

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", reserve)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", release)
    monkeypatch.setattr(volcano_assets, "_normalized_source_url", normalized)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", reject)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "deferred"
    stored = redis.operation()
    assert stored["status"] == "queued"
    assert stored["progress_stage"] == "waiting_rate_limit"
    assert stored["retry_after_seconds"] == 5
    assert redis.enqueued
    assert released == []


@pytest.mark.asyncio
async def test_worker_operation_lock_defers_duplicate_delivery() -> None:
    from app.tasks import volcano_assets

    redis = _Redis(_operation())
    redis.values["video-assets:operation-lock:operation-1"] = "other-worker"

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )
    duplicate = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "locked"
    assert result["retry_after_seconds"] == 605
    assert result["recovery_scheduled"] is True
    assert duplicate["recovery_scheduled"] is False
    assert len(redis.enqueued) == 1
    assert redis.enqueued[0][0] == "process_volcano_asset_operation"


@pytest.mark.asyncio
async def test_worker_skips_failed_and_stale_deliveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    failed = _operation()
    failed.update(
        {
            "status": "failed",
            "progress_stage": "failed",
            "retryable": True,
            "error": {"code": "temporary"},
        }
    )
    failed_redis = _Redis(failed)

    async def unexpected_provider(
        _operation: dict[str, Any],
    ) -> VideoProviderDefinition:
        raise AssertionError("terminal delivery must not reach upstream")

    monkeypatch.setattr(
        volcano_assets,
        "_provider_for_operation",
        unexpected_provider,
    )

    failed_result = await volcano_assets.process_volcano_asset_operation(
        {"redis": failed_redis},
        "operation-1",
        1,
        0,
    )
    assert failed_result["status"] == "failed"

    stale = _operation()
    stale["delivery_generation"] = 2
    stale_redis = _Redis(stale)
    stale_result = await volcano_assets.process_volcano_asset_operation(
        {"redis": stale_redis},
        "operation-1",
        1,
        1,
    )
    assert stale_result["status"] == "stale"


@pytest.mark.asyncio
async def test_worker_normalizes_legacy_empty_name_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _operation()
    operation["name"] = " \x00"
    redis = _Redis(operation)
    submitted_names: list[str] = []

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            if action == "ListAssets":
                return {"Assets": [], "TotalCount": 1}
            submitted_names.append(body["Name"])
            return {"Id": "asset-1"}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def normalized(_operation: dict[str, Any]) -> tuple[str, str]:
        return ("https://lumen.example/safe.mp4", "volcano_asset_video_v1")

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_normalized_source_url", normalized)
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", noop)
    monkeypatch.setattr(volcano_assets, "_write_audit", noop)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
        1,
        0,
    )

    assert result["status"] == "succeeded"
    assert submitted_names == [redis.operation()["name"]]
    assert submitted_names[0].startswith("lumen-asset-")


@pytest.mark.asyncio
async def test_worker_recovers_success_receipt_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_operation())
    redis.fail_success_sets = 3
    create_calls = 0
    released: list[str] = []
    receipt: dict[str, Any] | None = None

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            nonlocal create_calls
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            if action == "ListAssets":
                return {"Assets": [], "TotalCount": 1}
            create_calls += 1
            return {"Id": "asset-1"}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def normalized(_operation: dict[str, Any]) -> tuple[str, str]:
        return ("https://lumen.example/safe.mp4", "volcano_asset_video_v1")

    async def reserve(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def release(*_args: Any, **kwargs: Any) -> None:
        released.append(kwargs["operation_id"])

    async def acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def read(_operation: dict[str, Any]) -> dict[str, Any] | None:
        return receipt

    async def write(
        _operation: dict[str, Any],
        asset: dict[str, Any],
    ) -> None:
        nonlocal receipt
        receipt = dict(asset)

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_normalized_source_url", normalized)
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", reserve)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", release)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", acquire)
    monkeypatch.setattr(volcano_assets, "_read_success_receipt", read)
    monkeypatch.setattr(volcano_assets, "_write_success_receipt", write)
    monkeypatch.setattr(volcano_assets, "_write_audit", audit)

    with pytest.raises(Retry):
        await volcano_assets.process_volcano_asset_operation(
            {"redis": redis, "job_try": 1},
            "operation-1",
            1,
            0,
        )

    assert create_calls == 1
    assert receipt is not None
    assert released == ["operation-1"]

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis, "job_try": 2},
        "operation-1",
        1,
        0,
    )

    assert result["status"] == "succeeded"
    assert create_calls == 1
    assert redis.operation()["result"]["id"] == "asset-1"


@pytest.mark.asyncio
async def test_worker_reconciles_ambiguous_create_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_operation())
    create_calls = 0
    list_calls = 0
    source_url = "https://lumen.example/safe.mp4"

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            nonlocal create_calls, list_calls
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            if action == "ListAssets":
                list_calls += 1
                if list_calls <= 2:
                    return {"Assets": [], "TotalCount": 1}
                return {
                    "Assets": [
                        {
                            "Id": "asset-recovered",
                            "GroupId": "group-1",
                            "Name": "User Display Name",
                            "AssetType": "Video",
                            "Status": "Processing",
                            "URL": source_url,
                            "ProjectName": "project-a",
                        }
                    ],
                    "TotalCount": 1,
                }
            create_calls += 1
            raise VolcanoAssetServiceError(
                "volcano_asset_timeout",
                "Volcano asset service timed out",
                504,
            )

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def normalized(_operation: dict[str, Any]) -> tuple[str, str]:
        return (source_url, "volcano_asset_video_v1")

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_normalized_source_url", normalized)
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", noop)
    monkeypatch.setattr(volcano_assets, "_write_audit", noop)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
        1,
        0,
    )

    assert result["status"] == "succeeded"
    assert result["result"]["id"] == "asset-recovered"
    assert create_calls == 1


@pytest.mark.asyncio
async def test_worker_never_resubmits_when_create_asset_reconcile_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = {
        **_operation(),
        "submit_started_at": "2026-07-15T00:00:10+00:00",
        "submit_outcome_uncertain": True,
        "source_url": "https://lumen.example/safe.mp4",
        "baseline_asset_ids": ["asset-old"],
    }
    redis = _Redis(operation)
    create_calls = 0
    reserve_calls = 0

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            nonlocal create_calls
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            if action == "ListAssets":
                return {
                    "Assets": [
                        {
                            "Id": "asset-old",
                            "GroupId": "group-1",
                            "Name": "User Display Name",
                            "AssetType": "Video",
                            "Status": "Active",
                            "ProjectName": "project-a",
                        }
                    ],
                    "TotalCount": 1,
                }
            create_calls += 1
            return {"Id": "asset-new"}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def reserve(*_args: Any, **_kwargs: Any) -> None:
        nonlocal reserve_calls
        reserve_calls += 1

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", reserve)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "_write_audit", noop)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
        1,
        0,
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "volcano_asset_create_reconcile_ambiguous"
    assert create_calls == 0
    assert reserve_calls == 0


@pytest.mark.asyncio
async def test_worker_recovers_unconfirmed_rate_limit_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_operation())
    redis.enqueue_error = ConnectionError("queue unavailable")
    released: list[str] = []

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            return {"Assets": [], "TotalCount": 1}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def normalized(_operation: dict[str, Any]) -> tuple[str, str]:
        return ("https://lumen.example/safe.mp4", "volcano_asset_video_v1")

    async def reserve(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def release(*_args: Any, **kwargs: Any) -> None:
        released.append(kwargs["operation_id"])

    async def reject(*_args: Any, **_kwargs: Any) -> None:
        raise VolcanoAssetCreateRateLimited(retry_after_ms=4_500)

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_normalized_source_url", normalized)
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", reserve)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", release)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", reject)

    with pytest.raises(Retry):
        await volcano_assets.process_volcano_asset_operation(
            {"redis": redis},
            "operation-1",
            1,
            0,
        )

    assert released == ["operation-1"]
    assert redis.operation()["delivery_enqueued"] is False

    redis.enqueue_error = None
    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
        1,
        0,
    )

    assert result["status"] == "delivery_recovered"
    assert len(redis.enqueued) == 1
    assert redis.operation()["delivery_enqueued"] is True


@pytest.mark.asyncio
async def test_worker_does_not_submit_after_lease_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_operation())
    redis.renew_result = 0
    create_calls = 0
    released: list[str] = []

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            nonlocal create_calls
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            if action == "ListAssets":
                return {"Assets": [], "TotalCount": 1}
            create_calls += 1
            return {"Id": "asset-1"}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def normalized(_operation: dict[str, Any]) -> tuple[str, str]:
        return ("https://lumen.example/safe.mp4", "volcano_asset_video_v1")

    async def reserve(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def release(*_args: Any, **kwargs: Any) -> None:
        released.append(kwargs["operation_id"])

    async def acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_normalized_source_url", normalized)
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", reserve)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", release)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", acquire)

    with pytest.raises(Retry):
        await volcano_assets.process_volcano_asset_operation(
            {"redis": redis},
            "operation-1",
            1,
            0,
        )

    assert create_calls == 0
    assert released == ["operation-1"]


@pytest.mark.asyncio
async def test_worker_create_group_uses_group_quota_without_create_asset_qpm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _management_operation(
        "create_group",
        name="Portrait Group",
    )
    redis = _Redis(operation)
    calls: list[tuple[str, dict[str, Any]]] = []
    released: list[tuple[str, str]] = []

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            calls.append((action, body))
            if action == "ListAssetGroups":
                return {"AssetGroups": [], "TotalCount": 49}
            assert action == "CreateAssetGroup"
            return {
                "Id": "group-created",
                "Name": body["Name"],
                "Description": body["Description"],
                "GroupType": "AIGC",
                "ProjectName": "project-a",
            }

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def reserve(*_args: Any, **kwargs: Any) -> None:
        assert kwargs["resource"] == "asset_groups"
        assert kwargs["upstream_total"] == 49
        assert kwargs["limit"] == 50

    async def release(*_args: Any, **kwargs: Any) -> None:
        released.append((kwargs["resource"], kwargs["operation_id"]))

    async def unexpected_qpm(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("create_group must not consume CreateAsset QPM")

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", reserve)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", release)
    monkeypatch.setattr(
        volcano_assets,
        "acquire_volcano_create_rate_limit",
        unexpected_qpm,
    )
    monkeypatch.setattr(volcano_assets, "_write_audit", audit)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "succeeded"
    assert result["result"]["id"] == "group-created"
    assert [action for action, _body in calls] == [
        "ListAssetGroups",
        "CreateAssetGroup",
    ]
    assert calls[1][1] == {
        "Name": "Portrait Group",
        "Description": "Private portrait references",
        "GroupType": "AIGC",
        "ProjectName": "project-a",
    }
    assert released == [("asset_groups", "operation-1")]


@pytest.mark.asyncio
async def test_worker_create_group_does_not_claim_unique_heuristic_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _management_operation(
        "create_group",
        name="Portrait Group",
    )
    redis = _Redis(operation)
    create_calls = 0
    list_calls = 0

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            nonlocal create_calls, list_calls
            if action == "ListAssetGroups":
                list_calls += 1
                if list_calls == 1:
                    return {"AssetGroups": [], "TotalCount": 1}
                submitted_at = redis.operation()["submit_started_at"]
                return {
                    "AssetGroups": [
                        {
                            "Id": "group-recovered",
                            "Name": "Portrait Group",
                            "Description": "Private portrait references",
                            "GroupType": "AIGC",
                            "ProjectName": "project-a",
                            "CreateTime": submitted_at,
                        },
                        {
                            "Id": "wrong-description",
                            "Name": "Portrait Group",
                            "Description": "different",
                            "GroupType": "AIGC",
                            "ProjectName": "project-a",
                            "CreateTime": submitted_at,
                        },
                        {
                            "Id": "outside-window",
                            "Name": "Portrait Group",
                            "Description": "Private portrait references",
                            "GroupType": "AIGC",
                            "ProjectName": "project-a",
                            "CreateTime": "2020-01-01T00:00:00+00:00",
                        },
                    ],
                    "TotalCount": 3,
                }
            create_calls += 1
            raise VolcanoAssetServiceError(
                "volcano_asset_timeout",
                "Volcano asset service timed out",
                504,
            )

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def reject_receipt(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ambiguous create_group must not write ownership receipt")

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "_write_audit", noop)
    monkeypatch.setattr(volcano_assets, "_write_success_receipt", reject_receipt)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "volcano_asset_create_group_reconcile_ambiguous"
    assert redis.operation()["submit_outcome_uncertain"] is True
    assert create_calls == 1
    assert list_calls == 1


@pytest.mark.asyncio
async def test_worker_create_group_never_resubmits_when_reconcile_is_not_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _management_operation(
        "create_group",
        name="Portrait Group",
    )
    redis = _Redis(operation)
    create_calls = 0
    list_calls = 0
    reserve_calls = 0

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            nonlocal create_calls, list_calls
            if action == "ListAssetGroups":
                list_calls += 1
                if list_calls == 1:
                    return {"AssetGroups": [], "TotalCount": 1}
                submitted_at = redis.operation()["submit_started_at"]
                return {
                    "AssetGroups": [
                        {
                            "Id": f"group-{index}",
                            "Name": "Portrait Group",
                            "Description": "Private portrait references",
                            "GroupType": "AIGC",
                            "ProjectName": "project-a",
                            "CreateTime": submitted_at,
                        }
                        for index in range(2)
                    ],
                    "TotalCount": 2,
                }
            create_calls += 1
            raise VolcanoAssetServiceError(
                "volcano_asset_timeout",
                "Volcano asset service timed out",
                504,
            )

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def reserve(*_args: Any, **_kwargs: Any) -> None:
        nonlocal reserve_calls
        reserve_calls += 1

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "reserve_volcano_asset_quota", reserve)
    monkeypatch.setattr(volcano_assets, "release_volcano_asset_quota", noop)
    monkeypatch.setattr(volcano_assets, "_write_audit", noop)

    first = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert first["status"] == "failed"
    assert first["error"]["code"] == ("volcano_asset_create_group_reconcile_ambiguous")
    failed = redis.operation()
    assert failed["retryable"] is True
    assert failed["submit_outcome_uncertain"] is True

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
    assert second["error"]["code"] == ("volcano_asset_create_group_reconcile_ambiguous")
    assert create_calls == 1
    assert reserve_calls == 1
