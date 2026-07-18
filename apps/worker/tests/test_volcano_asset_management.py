from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from arq import Retry
from lumen_core.video_providers import VideoProviderDefinition
from lumen_core.volcano_assets import (
    VolcanoAssetServiceError,
)
from sqlalchemy.exc import IntegrityError

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


@pytest.fixture(autouse=True)
def _stub_success_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import volcano_assets

    async def read(
        _operation: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        return None

    async def write(
        _operation: dict[str, Any],
        _asset: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_read_success_receipt", read)
    monkeypatch.setattr(volcano_assets, "_write_success_receipt", write)


@pytest.mark.asyncio
async def test_worker_update_group_recovers_success_receipt_after_redis_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _management_operation(
        "update_group",
        name="Renamed Group",
        description=None,
    )
    redis = _Redis(operation)
    redis.fail_success_sets = 3
    remote_name = "Old Group"
    update_calls = 0
    receipt: dict[str, Any] | None = None

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            nonlocal remote_name, update_calls
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "Name": remote_name,
                    "Description": "Existing",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            assert action == "UpdateAssetGroup"
            update_calls += 1
            remote_name = body["Name"]
            return {
                "Id": "group-1",
                "Name": remote_name,
                "Description": "Existing",
                "GroupType": "AIGC",
                "ProjectName": "project-a",
            }

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def read(
        _operation: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        return receipt

    async def write(
        _operation: dict[str, Any],
        result: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        nonlocal receipt
        receipt = volcano_assets._receipt_result(_operation, result)

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
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

    assert update_calls == 1
    assert receipt is not None

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis, "job_try": 2},
        "operation-1",
        1,
        0,
    )

    assert result["status"] == "succeeded"
    assert result["result"]["name"] == "Renamed Group"
    assert update_calls == 1


@pytest.mark.asyncio
async def test_worker_update_group_retry_reads_target_before_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _management_operation(
        "update_group",
        name="Renamed Group",
        description=None,
        attempt=2,
        submit_started_at="2026-07-15T00:00:10+00:00",
    )
    redis = _Redis(operation)
    calls: list[str] = []

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            calls.append(action)
            if action != "GetAssetGroup":
                raise AssertionError("reached update target must not be resubmitted")
            return {
                "Id": "group-1",
                "Name": "Renamed Group",
                "Description": "Existing",
                "GroupType": "AIGC",
                "ProjectName": "project-a",
            }

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_write_audit", audit)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
        2,
        0,
    )

    assert result["status"] == "succeeded"
    assert calls == ["GetAssetGroup"]


@pytest.mark.asyncio
async def test_worker_update_asset_reconciles_timeout_by_reading_target_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    operation = _management_operation(
        "update_asset",
        name="Renamed Asset",
    )
    redis = _Redis(operation)
    remote_name = "Old Asset"
    update_calls = 0

    class Client:
        async def request(self, action: str, body: dict[str, Any]) -> Any:
            nonlocal remote_name, update_calls
            if action == "GetAsset":
                return {
                    "Id": "asset-1",
                    "GroupId": "group-1",
                    "Name": remote_name,
                    "AssetType": "Video",
                    "Status": "Active",
                    "ProjectName": "project-a",
                }
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            assert action == "UpdateAsset"
            update_calls += 1
            remote_name = body["Name"]
            raise VolcanoAssetServiceError(
                "volcano_asset_timeout",
                "Volcano asset service timed out",
                504,
            )

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def unexpected_quota(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("update_asset must not reserve create quota")

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(
        volcano_assets,
        "reserve_volcano_asset_quota",
        unexpected_quota,
    )
    monkeypatch.setattr(
        volcano_assets,
        "acquire_volcano_create_rate_limit",
        unexpected_quota,
    )
    monkeypatch.setattr(volcano_assets, "_write_audit", audit)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "succeeded"
    assert result["result"]["name"] == "Renamed Asset"
    assert update_calls == 1


@pytest.mark.asyncio
async def test_worker_delete_group_succeeds_when_target_is_already_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(
        _management_operation(
            "delete_group",
            deleted_asset_ids=["asset-known"],
        )
    )
    calls: list[str] = []

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            calls.append(action)
            raise VolcanoAssetServiceError(
                "volcano_asset_not_found",
                "not found",
                404,
            )

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_write_audit", audit)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "succeeded"
    assert result["result"] == {
        "id": "group-1",
        "deleted": True,
        "resource_type": "group",
        "group_id": "group-1",
        "deleted_asset_ids": ["asset-known"],
        "already_deleted": True,
        "cascade_assets": True,
    }
    assert calls == ["GetAssetGroup"]


@pytest.mark.asyncio
async def test_worker_delete_group_records_cascaded_asset_ids_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_management_operation("delete_group"))
    calls: list[str] = []

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            calls.append(action)
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
                            "Id": "asset-1",
                            "GroupId": "group-1",
                            "ProjectName": "project-a",
                        },
                        {
                            "Id": "asset-2",
                            "GroupId": "group-1",
                            "ProjectName": "project-a",
                        },
                        {
                            "Id": "other-group-asset",
                            "GroupId": "group-2",
                            "ProjectName": "project-a",
                        },
                    ],
                    "TotalCount": 3,
                }
            assert action == "DeleteAssetGroup"
            return {}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_write_audit", audit)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "succeeded"
    assert result["result"] == {
        "id": "group-1",
        "deleted": True,
        "resource_type": "group",
        "group_id": "group-1",
        "deleted_asset_ids": ["asset-1", "asset-2"],
        "already_deleted": False,
        "cascade_assets": True,
    }
    assert calls == ["GetAssetGroup", "ListAssets", "DeleteAssetGroup"]
    assert redis.operation()["deleted_asset_ids"] == ["asset-1", "asset-2"]


@pytest.mark.asyncio
async def test_worker_delete_asset_treats_delete_not_found_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(_management_operation("delete_asset"))
    calls: list[str] = []

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            calls.append(action)
            if action == "GetAsset":
                return {
                    "Id": "asset-1",
                    "GroupId": "group-1",
                    "Name": "Portrait",
                    "AssetType": "Image",
                    "Status": "Active",
                    "ProjectName": "project-a",
                }
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            raise VolcanoAssetServiceError(
                "volcano_asset_not_found",
                "already deleted",
                404,
            )

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    async def audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())
    monkeypatch.setattr(volcano_assets, "_write_audit", audit)

    result = await volcano_assets.process_volcano_asset_operation(
        {"redis": redis},
        "operation-1",
    )

    assert result["status"] == "succeeded"
    assert result["result"] == {
        "id": "asset-1",
        "deleted": True,
        "resource_type": "asset",
        "group_id": "group-1",
        "asset_id": "asset-1",
        "deleted_asset_ids": ["asset-1"],
        "already_deleted": True,
        "cascade_assets": False,
    }
    assert calls == ["GetAsset", "GetAssetGroup", "DeleteAsset"]


@pytest.mark.asyncio
async def test_worker_management_action_does_not_mutate_after_lease_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    provider = _provider()
    redis = _Redis(
        _management_operation(
            "update_group",
            name="Renamed Group",
            description=None,
        )
    )
    redis.renew_result = 0
    update_calls = 0

    class Client:
        async def request(self, action: str, _body: dict[str, Any]) -> Any:
            nonlocal update_calls
            if action == "GetAssetGroup":
                return {
                    "Id": "group-1",
                    "Name": "Old Group",
                    "GroupType": "AIGC",
                    "ProjectName": "project-a",
                }
            update_calls += 1
            return {}

    async def provider_for(_operation: dict[str, Any]) -> VideoProviderDefinition:
        return provider

    monkeypatch.setattr(volcano_assets, "_provider_for_operation", provider_for)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: Client())

    with pytest.raises(Retry):
        await volcano_assets.process_volcano_asset_operation(
            {"redis": redis},
            "operation-1",
            1,
            0,
        )

    assert update_calls == 0


@pytest.mark.parametrize(
    ("operation", "result"),
    [
        (
            _management_operation("create_group", name="Portrait Group"),
            {
                "id": "group-created",
                "name": "Portrait Group",
                "description": "Private portrait references",
                "group_type": "AIGC",
                "project_name": "project-a",
            },
        ),
        (
            _management_operation(
                "update_group",
                name="Renamed Group",
                description=None,
            ),
            {
                "id": "group-1",
                "name": "Renamed Group",
                "description": "Existing",
                "group_type": "AIGC",
                "project_name": "project-a",
            },
        ),
        (
            _management_operation("delete_group"),
            {
                "id": "group-1",
                "deleted": True,
                "resource_type": "group",
                "group_id": "group-1",
                "deleted_asset_ids": ["asset-1", "asset-2"],
                "already_deleted": False,
                "cascade_assets": True,
            },
        ),
        (
            _management_operation("create_asset"),
            {
                "id": "asset-created",
                "group_id": "group-1",
                "name": "User Display Name",
                "asset_type": "Video",
                "status": "Processing",
                "project_name": "project-a",
            },
        ),
        (
            _management_operation("update_asset", name="Renamed Asset"),
            {
                "id": "asset-1",
                "group_id": "group-1",
                "name": "Renamed Asset",
                "asset_type": "Video",
                "status": "Active",
                "project_name": "project-a",
            },
        ),
        (
            _management_operation("delete_asset"),
            {
                "id": "asset-1",
                "deleted": True,
                "resource_type": "asset",
                "group_id": "group-1",
                "asset_id": "asset-1",
                "deleted_asset_ids": ["asset-1"],
                "already_deleted": True,
                "cascade_assets": False,
            },
        ),
    ],
)
def test_success_receipts_are_action_scoped_and_redacted(
    operation: dict[str, Any],
    result: dict[str, Any],
) -> None:
    from app.tasks import volcano_assets

    malicious_result = {
        **result,
        "source_url": "https://lumen.example/file?token=secret-token",
        "token": "secret-token",
        "access_key_id": "AKLTEXAMPLE",
        "secret_access_key": "secret-example",
    }

    receipt = volcano_assets._receipt_result(operation, malicious_result)

    assert volcano_assets._validated_receipt_result(operation, receipt) == receipt
    serialized = json.dumps(receipt)
    assert "source_url" not in serialized
    assert "secret-token" not in serialized
    assert "AKLTEXAMPLE" not in serialized
    assert "secret-example" not in serialized
    if str(operation.get("action") or "").startswith("delete_"):
        assert receipt["resource_type"] == result["resource_type"]
        assert receipt["deleted_asset_ids"] == result["deleted_asset_ids"]


def test_success_receipt_rejects_result_for_another_action_target() -> None:
    from app.tasks import volcano_assets

    operation = _management_operation(
        "update_asset",
        name="Renamed Asset",
    )

    assert (
        volcano_assets._validated_receipt_result(
            operation,
            {
                "id": "another-asset",
                "group_id": "group-1",
                "name": "Renamed Asset",
                "project_name": "project-a",
            },
        )
        is None
    )


def test_success_receipt_redacts_source_url_and_token() -> None:
    from app.tasks import volcano_assets

    receipt = volcano_assets._receipt_asset(
        {
            "id": "asset-1",
            "group_id": "group-1",
            "name": "Portrait",
            "asset_type": "Image",
            "status": "Processing",
            "project_name": "project-a",
            "url": "https://lumen.example/file?token=secret-token",
        }
    )

    assert "url" not in receipt
    assert "secret-token" not in str(receipt)


def test_success_receipt_is_bound_to_exact_provider_route() -> None:
    from app.tasks import volcano_assets

    operation = _operation()
    result = {
        "id": "asset-1",
        "group_id": "group-1",
        "name": "User Display Name",
        "asset_type": "Video",
        "status": "Processing",
        "project_name": "project-a",
    }

    fence = SimpleNamespace(
        details=lambda: {
            "lock_token": "worker-1",
            "attempt": 1,
            "fencing": 7,
        }
    )
    details = volcano_assets._success_receipt_details(
        operation,
        result,
        fence=fence,
    )

    assert details["provider_name"] == operation["provider_name"]
    assert details["region"] == operation["region"]
    assert details["provider_binding"] == operation["provider_binding"]
    assert details["lock_token"] == "worker-1"
    assert details["attempt"] == 1
    assert details["fencing"] == 7
    assert volcano_assets._receipt_binding_matches(operation, details) is True
    for field in ("provider_name", "region", "provider_binding"):
        legacy_or_mismatched = dict(details)
        legacy_or_mismatched.pop(field)
        assert (
            volcano_assets._receipt_binding_matches(
                operation,
                legacy_or_mismatched,
            )
            is False
        )


@pytest.mark.asyncio
async def test_legacy_receipt_cannot_satisfy_new_provider_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import volcano_assets

    monkeypatch.undo()
    operation = _operation()
    result = {
        "id": "asset-1",
        "group_id": "group-1",
        "name": "User Display Name",
        "asset_type": "Video",
        "status": "Processing",
        "project_name": "project-a",
    }
    existing = SimpleNamespace(
        event_type=volcano_assets._LEGACY_SUCCESS_RECEIPT_EVENT,
        user_id=operation["user_id"],
        details={
            "operation_id": operation["id"],
            "asset": volcano_assets._receipt_asset(result),
        },
    )

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def add(self, _row: Any) -> None:
            return None

        async def commit(self) -> None:
            raise IntegrityError("insert receipt", {}, RuntimeError("duplicate"))

        async def rollback(self) -> None:
            return None

        async def get(self, _model: Any, _operation_id: str) -> Any:
            return existing

    monkeypatch.setattr(volcano_assets, "SessionLocal", Session)
    fence = SimpleNamespace(
        fencing=1,
        lock_token="worker-1",
        details=lambda: {
            "lock_token": "worker-1",
            "attempt": 1,
            "fencing": 1,
        },
    )

    with pytest.raises(RuntimeError, match="success receipt conflicts"):
        await volcano_assets._write_success_receipt(
            operation,
            result,
            fence=fence,
        )


def test_reference_token_reuses_unexpired_value() -> None:
    from app.tasks import volcano_assets

    metadata = {
        "token": "existing-token",
        "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }

    token = volcano_assets._ensure_reference_token(
        metadata,
        token_key="token",
        expires_key="expires",
    )

    assert token == "existing-token"
    assert metadata["token"] == "existing-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asset_type", "token_key", "ensure_name"),
    [
        (
            "Image",
            "video_reference_access_token",
            "ensure_volcano_asset_image_variant",
        ),
        (
            "Video",
            "reference_access_token",
            "ensure_volcano_asset_video_variant",
        ),
    ],
)
async def test_concurrent_source_urls_share_one_locked_token(
    monkeypatch: pytest.MonkeyPatch,
    asset_type: str,
    token_key: str,
    ensure_name: str,
) -> None:
    from app.tasks import volcano_assets

    source = SimpleNamespace(id="source-1", metadata_jsonb={})
    row_lock = asyncio.Lock()
    locked_statements: list[str] = []

    class _Result:
        def scalar_one_or_none(self) -> Any:
            return source

    class _Session:
        def __init__(self) -> None:
            self.owns_lock = False

        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            if self.owns_lock:
                row_lock.release()
                self.owns_lock = False

        async def execute(self, statement: Any) -> _Result:
            rendered = str(statement)
            if "FOR UPDATE" in rendered:
                await row_lock.acquire()
                self.owns_lock = True
                locked_statements.append(rendered)
            return _Result()

        async def commit(self) -> None:
            if self.owns_lock:
                row_lock.release()
                self.owns_lock = False

    async def ensure_variant(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "SessionLocal", _Session)
    monkeypatch.setattr(
        volcano_assets,
        ensure_name,
        ensure_variant,
    )
    operation = {
        "asset_type": asset_type,
        "local_source_id": "source-1",
        "user_id": "user-1",
        "public_base_url": "https://lumen.example",
    }

    first, second = await asyncio.gather(
        volcano_assets._normalized_source_url(dict(operation)),
        volcano_assets._normalized_source_url(dict(operation)),
    )

    assert first[0] == second[0]
    assert source.metadata_jsonb[token_key]
    assert len(locked_statements) == 2
