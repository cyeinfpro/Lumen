from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

from lumen_core.schemas import (
    VideoAssetCreateIn,
    VideoAssetGroupCreateIn,
    VideoAssetGroupUpdateIn,
    VideoAssetUpdateIn,
)
from lumen_core.video_providers import (
    VideoProviderDefinition,
    video_provider_binding_fingerprint,
)


def _provider(**overrides: Any) -> VideoProviderDefinition:
    values: dict[str, Any] = {
        "name": "volcano-main",
        "kind": "volcano",
        "base_url": "https://generation.example/v1",
        "api_key": "generation-key",
        "access_key_id": "AKLTEXAMPLE",
        "secret_access_key": "secret-example",
        "project_name": "project-a",
        "region": "cn-shanghai",
        "enabled": True,
        "priority": 100,
        "models": {"seedance:reference": "doubao-seedance-ref"},
    }
    values.update(overrides)
    return VideoProviderDefinition(**values)


def _request(method: str = "POST") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/video-assets/assets",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Db:
    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.statements: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(self.result)

    async def commit(self) -> None:
        self.commits += 1


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.zsets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **_kwargs: Any) -> bool:
        self.values[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def zrem(self, key: str, member: str) -> None:
        self.zsets.setdefault(key, set()).discard(member)

    async def eval(
        self,
        script: str,
        _numkeys: int,
        key: str,
        *args: Any,
    ) -> list[Any]:
        if "cjson.decode" not in script:
            raise AssertionError("unexpected Redis script")
        (
            owner,
            expected_status,
            expected_attempt,
            expected_progress,
            replacement,
            _ttl,
        ) = args
        raw = self.values.get(key)
        if raw is None:
            return [0, ""]
        current = json.loads(raw)
        if str(current.get("user_id") or "") != owner:
            return [-2, ""]
        if (
            str(current.get("status") or "") != expected_status
            or int(current.get("attempt") or 1) != int(expected_attempt)
            or (
                expected_progress
                and str(current.get("progress_stage") or "") != expected_progress
            )
        ):
            return [2, raw]
        self.values[key] = replacement
        return [1, replacement]


def test_signed_request_is_deterministic_and_uses_region_host() -> None:
    from app.volcano_assets import build_signed_asset_request

    kwargs = {
        "action": "ListAssetGroups",
        "body": {
            "ProjectName": "project-a",
            "Filter": {"GroupType": "AIGC"},
        },
        "access_key_id": "AKLTEXAMPLE",
        "secret_access_key": "secret-example",
        "region": "cn-shanghai",
        "now": datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    }

    first = build_signed_asset_request(**kwargs)
    second = build_signed_asset_request(**kwargs)

    assert first == second
    assert first.url == (
        "https://ark.cn-shanghai.volcengineapi.com/"
        "?Action=ListAssetGroups&Version=2024-01-01"
    )
    assert first.headers["host"] == "ark.cn-shanghai.volcengineapi.com"
    assert first.headers["x-date"] == "20240102T030405Z"
    assert first.body == (b'{"Filter":{"GroupType":"AIGC"},"ProjectName":"project-a"}')
    assert first.headers["x-content-sha256"] == (
        "85205cd7cae892828c9101e5a1a88e8c59ef8e12ae415b5c6635db11c512949d"
    )
    assert first.headers["authorization"] == (
        "HMAC-SHA256 Credential=AKLTEXAMPLE/"
        "20240102/cn-shanghai/ark/request, "
        "SignedHeaders=content-type;host;x-content-sha256;x-date, "
        "Signature=5b6506a1017a32353bf95f04d13ab34d9ae684e917069f274aa4edcc3e9ab421"
    )
    assert "host:ark.cn-shanghai.volcengineapi.com" in first.canonical_request


def test_signed_request_rejects_unsafe_region() -> None:
    from app.volcano_assets import build_signed_asset_request

    with pytest.raises(ValueError, match="invalid Volcano region"):
        build_signed_asset_request(
            action="GetAsset",
            body={"Id": "asset-1"},
            access_key_id="AKLTEXAMPLE",
            secret_access_key="secret-example",
            region="cn-shanghai.example.com",
        )


def test_volcano_reference_url_uses_safe_lumen_filename() -> None:
    from lumen_core.volcano_assets import (
        volcano_asset_reference_url,
        volcano_asset_safe_filename,
    )

    filename = volcano_asset_safe_filename(
        "019f64df-0d1a-7871-bb13-efebd18f3b6e",
        asset_type="Image",
    )
    url = volcano_asset_reference_url(
        "https://lumen.example",
        resource_id="019f64df-0d1a-7871-bb13-efebd18f3b6e",
        asset_type="Image",
        token="token-value",
    )

    assert filename.endswith(".jpg")
    assert re.fullmatch(r"[a-z0-9.-]+", filename)
    assert f"/binary/{filename}?" in url
    assert "variant=volcano_asset_img_v1" in url


def test_result_and_asset_error_normalization() -> None:
    from app.volcano_assets import (
        normalize_asset_list,
        normalize_volcano_result,
    )

    nested = normalize_volcano_result(
        {
            "ResponseMetadata": {"RequestId": "request-1"},
            "Result": {
                "Assets": [
                    {
                        "Id": "asset-1",
                        "GroupId": "group-1",
                        "Name": "portrait",
                        "AssetType": "Image",
                        "Status": "Failed",
                        "ProjectName": "project-a",
                        "Error": {
                            "Code": "ContentRestricted",
                            "Message": "content restricted",
                        },
                    }
                ],
                "TotalCount": 1,
                "PageNumber": 2,
                "PageSize": 20,
            },
        }
    )
    top_level = normalize_volcano_result(
        {
            "ResponseMetadata": {"RequestId": "request-2"},
            "Assets": [],
            "TotalCount": 0,
        }
    )
    normalized = normalize_asset_list(
        nested,
        project_name="project-a",
        page_number=1,
        page_size=20,
    )

    assert top_level == {"Assets": [], "TotalCount": 0}
    assert normalized["total_count"] == 1
    assert normalized["page_number"] == 2
    assert normalized["items"][0]["error_code"] == "ContentRestricted"
    assert normalized["items"][0]["error_message"] == "content restricted"


@pytest.mark.parametrize(
    ("provider", "expected_reason"),
    [
        (
            _provider(
                kind="dashscope",
                access_key_id="",
                secret_access_key="",
            ),
            "reference_provider_not_official_volcano",
        ),
        (
            _provider(access_key_id="", secret_access_key=""),
            "volcano_asset_credentials_missing",
        ),
        (_provider(), None),
    ],
)
def test_capability_guards(
    provider: VideoProviderDefinition,
    expected_reason: str | None,
) -> None:
    from app.routes import volcano_assets

    selected, reason = volcano_assets._capability(
        [provider],
        model="seedance",
    )

    assert selected is provider
    assert reason == expected_reason


@pytest.mark.asyncio
async def test_capabilities_returns_public_https_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()

    async def provider_state(*_args: Any, **_kwargs: Any) -> tuple[Any, None]:
        return provider, None

    async def public_base(*_args: Any, **_kwargs: Any) -> str:
        return "https://lumen.example"

    monkeypatch.setattr(volcano_assets, "_provider_state", provider_state)
    monkeypatch.setattr(volcano_assets, "_public_base_url", public_base)

    output = await volcano_assets.get_capabilities(
        "seedance",
        _request("GET"),
        SimpleNamespace(id="user-1"),
        _Db(),  # type: ignore[arg-type]
    )

    assert output.enabled is True
    assert output.reason is None
    assert output.public_base_url == "https://lumen.example"
    assert output.quotas.max_assets == 50
    assert output.quotas.max_asset_groups == 50
    assert output.quotas.create_asset_qpm == 3
    assert output.quotas.create_asset_window_seconds == 60


@pytest.mark.asyncio
async def test_capabilities_disables_asset_management_without_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()

    async def provider_state(*_args: Any, **_kwargs: Any) -> tuple[Any, None]:
        return provider, None

    async def public_base(*_args: Any, **_kwargs: Any) -> str:
        raise volcano_assets._http(
            "video_asset_public_url_missing",
            "public URL missing",
            503,
        )

    monkeypatch.setattr(volcano_assets, "_provider_state", provider_state)
    monkeypatch.setattr(volcano_assets, "_public_base_url", public_base)

    output = await volcano_assets.get_capabilities(
        "seedance",
        _request("GET"),
        SimpleNamespace(id="user-1"),
        _Db(),  # type: ignore[arg-type]
    )

    assert output.enabled is False
    assert output.reason == "video_asset_public_url_missing"
    assert output.public_base_url is None


@pytest.mark.asyncio
async def test_usage_returns_current_provider_project_totals_without_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    calls: list[tuple[str, dict[str, Any]]] = []
    selected_providers: list[VideoProviderDefinition] = []

    async def require_provider(
        _db: Any,
        *,
        model: str,
    ) -> VideoProviderDefinition:
        assert model == "seedance"
        return provider

    async def unexpected_receipts(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("quota usage must not apply ownership filtering")

    class Client:
        def __init__(self, selected_provider: VideoProviderDefinition) -> None:
            selected_providers.append(selected_provider)

        async def request(self, action: str, payload: dict[str, Any]) -> Any:
            calls.append((action, payload))
            if action == "ListAssets":
                return {
                    "Assets": [{"Id": "asset-must-not-leak"}],
                    "TotalCount": 2875,
                }
            if action == "ListAssetGroups":
                return {
                    "AssetGroups": [{"Id": "group-must-not-leak"}],
                    "TotalCount": 91,
                }
            raise AssertionError(f"unexpected action: {action}")

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(
        volcano_assets,
        "_owned_resource_receipts",
        unexpected_receipts,
    )
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", Client)

    output = await volcano_assets.get_usage(
        "seedance",
        SimpleNamespace(id="user-1", role="member"),
        object(),  # type: ignore[arg-type]
    )

    assert output.model_dump() == {
        "assets_used": 2875,
        "asset_groups_used": 91,
    }
    assert selected_providers == [provider]
    payloads = {action: payload for action, payload in calls}
    assert set(payloads) == {"ListAssets", "ListAssetGroups"}
    for payload in payloads.values():
        assert payload == {
            "ProjectName": provider.project_name,
            "Filter": {"GroupType": "AIGC"},
            "PageNumber": 1,
            "PageSize": 1,
        }


@pytest.mark.asyncio
async def test_usage_propagates_provider_and_upstream_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    async def unavailable_provider(*_args: Any, **_kwargs: Any) -> Any:
        raise volcano_assets._http(
            "volcano_asset_provider_unavailable",
            "provider unavailable",
            503,
        )

    monkeypatch.setattr(volcano_assets, "_require_provider", unavailable_provider)
    monkeypatch.setattr(
        volcano_assets,
        "VolcanoAssetClient",
        lambda _provider: (_ for _ in ()).throw(
            AssertionError("client must not be created without a provider")
        ),
    )

    with pytest.raises(HTTPException) as provider_error:
        await volcano_assets.get_usage(
            "seedance",
            SimpleNamespace(id="user-1", role="member"),
            object(),  # type: ignore[arg-type]
        )

    assert provider_error.value.status_code == 503
    assert (
        provider_error.value.detail["error"]["code"]
        == "volcano_asset_provider_unavailable"
    )

    async def require_provider(*_args: Any, **_kwargs: Any) -> Any:
        return _provider()

    class FailingClient:
        def __init__(self, _provider: VideoProviderDefinition) -> None:
            pass

        async def request(self, action: str, _payload: dict[str, Any]) -> Any:
            if action == "ListAssets":
                return {"Assets": [], "TotalCount": 3}
            raise volcano_assets._http(
                "volcano_asset_unavailable",
                "upstream unavailable",
                502,
            )

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", FailingClient)

    with pytest.raises(HTTPException) as upstream_error:
        await volcano_assets.get_usage(
            "seedance",
            SimpleNamespace(id="user-1", role="member"),
            object(),  # type: ignore[arg-type]
        )

    assert upstream_error.value.status_code == 502
    assert upstream_error.value.detail["error"]["code"] == "volcano_asset_unavailable"


def test_list_payloads_force_project_and_aigc_scope() -> None:
    from app.routes import volcano_assets

    provider = _provider()
    groups = volcano_assets._group_list_payload(
        provider,
        name="portraits",
        group_ids=["g-1,g-2"],
        page_number=2,
        page_size=100,
        sort_by="CreatedAt",
        sort_order="desc",
    )
    assets = volcano_assets._asset_list_payload(
        provider,
        name="speaker",
        group_ids=["g-1"],
        statuses=["Active,Failed"],
        page_number=1,
        page_size=20,
        sort_by="CreatedAt",
        sort_order="Asc",
    )

    assert groups == {
        "ProjectName": "project-a",
        "Filter": {
            "GroupType": "AIGC",
            "Name": "portraits",
            "GroupIds": ["g-1", "g-2"],
        },
        "PageNumber": 2,
        "PageSize": 100,
        "SortBy": "CreatedAt",
        "SortOrder": "Desc",
    }
    assert assets["ProjectName"] == "project-a"
    assert assets["Filter"]["GroupType"] == "AIGC"
    assert assets["Filter"]["Statuses"] == ["Active", "Failed"]


@pytest.mark.asyncio
async def test_ownership_receipt_query_binds_current_provider_snapshot() -> None:
    from app.routes import volcano_assets

    provider = _provider()
    db = _Db(result="user-1")

    owner = await volcano_assets._resource_owner_user_id(  # noqa: SLF001
        db,  # type: ignore[arg-type]
        provider=provider,
        resource_type="asset",
        resource_id="asset-1",
    )

    assert owner == "user-1"
    compiled = db.statements[0].compile()
    params = set(compiled.params.values())
    assert provider.name in params
    assert provider.project_name in params
    assert provider.region in params
    assert video_provider_binding_fingerprint(provider) in params


@pytest.mark.asyncio
async def test_member_lists_filter_other_users_resources_and_hide_global_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def owned_receipts(
        _db: Any,
        *,
        resource_type: str,
        **_kwargs: Any,
    ) -> Any:
        if resource_type == "group":
            return volcano_assets.OwnedResourceReceipts(
                resource_ids=frozenset({"group-owned"}),
                group_ids=frozenset(),
            )
        return volcano_assets.OwnedResourceReceipts(
            resource_ids=frozenset({"asset-owned"}),
            group_ids=frozenset({"group-owned"}),
        )

    class Client:
        def __init__(self, _provider: VideoProviderDefinition) -> None:
            pass

        async def request(self, action: str, payload: dict[str, Any]) -> Any:
            calls.append((action, payload))
            if action == "ListAssetGroups":
                return {
                    "AssetGroups": [
                        {
                            "Id": "group-owned",
                            "Name": "Owned",
                            "GroupType": "AIGC",
                            "ProjectName": "project-a",
                        },
                        {
                            "Id": "group-other",
                            "Name": "Other",
                            "GroupType": "AIGC",
                            "ProjectName": "project-a",
                        },
                    ],
                    "TotalCount": 2,
                }
            return {
                "Assets": [
                    {
                        "Id": "asset-owned",
                        "GroupId": "group-owned",
                        "Name": "Owned",
                        "AssetType": "Image",
                        "Status": "Active",
                        "ProjectName": "project-a",
                    },
                    {
                        "Id": "asset-other",
                        "GroupId": "group-owned",
                        "Name": "Other",
                        "AssetType": "Image",
                        "Status": "Active",
                        "ProjectName": "project-a",
                    },
                ],
                "TotalCount": 2,
            }

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(
        volcano_assets,
        "_owned_resource_receipts",
        owned_receipts,
    )
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", Client)
    user = SimpleNamespace(id="user-1", role="member")

    groups = await volcano_assets.list_groups(
        "seedance",
        user,
        object(),  # type: ignore[arg-type]
        None,
        None,
        1,
        20,
        None,
        None,
    )
    assets = await volcano_assets.list_assets(
        "seedance",
        user,
        object(),  # type: ignore[arg-type]
        None,
        None,
        None,
        1,
        20,
        None,
        None,
    )

    assert [item.id for item in groups.items] == ["group-owned"]
    assert groups.total_count == 1
    assert [item.id for item in assets.items] == ["asset-owned"]
    assert assets.total_count == 1
    group_call = next(
        payload for action, payload in calls if action == "ListAssetGroups"
    )
    asset_call = next(payload for action, payload in calls if action == "ListAssets")
    assert group_call["Filter"]["GroupIds"] == ["group-owned"]
    assert asset_call["Filter"]["GroupIds"] == ["group-owned"]
    assert group_call["PageNumber"] == asset_call["PageNumber"] == 1
    assert group_call["PageSize"] == asset_call["PageSize"] == 100


@pytest.mark.asyncio
async def test_member_asset_list_scans_past_100_before_local_page_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    owned_ids = [f"asset-owned-{index:03d}" for index in range(150)]
    remote_pages = {
        1: owned_ids[:100],
        2: owned_ids[100:],
    }
    calls: list[dict[str, Any]] = []

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def owned_receipts(*_args: Any, **_kwargs: Any) -> Any:
        return volcano_assets.OwnedResourceReceipts(
            resource_ids=frozenset(owned_ids),
            group_ids=frozenset({"group-owned"}),
        )

    class Client:
        def __init__(self, _provider: VideoProviderDefinition) -> None:
            pass

        async def request(self, action: str, payload: dict[str, Any]) -> Any:
            assert action == "ListAssets"
            calls.append(payload)
            ids = remote_pages.get(payload["PageNumber"], [])
            return {
                "Assets": [
                    {
                        "Id": asset_id,
                        "GroupId": "group-owned",
                        "Name": f"Portrait {asset_id}",
                        "AssetType": "Image",
                        "Status": "Active",
                        "ProjectName": provider.project_name,
                    }
                    for asset_id in ids
                ],
                "TotalCount": len(owned_ids),
                "PageNumber": payload["PageNumber"],
                "PageSize": payload["PageSize"],
            }

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(
        volcano_assets,
        "_owned_resource_receipts",
        owned_receipts,
    )
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", Client)

    output = await volcano_assets.list_assets(
        "seedance",
        SimpleNamespace(id="user-1", role="member"),
        object(),  # type: ignore[arg-type]
        "Portrait",
        ["group-owned"],
        ["Active"],
        2,
        100,
        "CreatedAt",
        "desc",
    )

    assert [item.id for item in output.items] == owned_ids[100:]
    assert output.total_count == 150
    assert output.page_number == 2
    assert output.page_size == 100
    assert [payload["PageNumber"] for payload in calls] == [1, 2, 3]
    assert all(payload["PageSize"] == 100 for payload in calls)
    assert all(payload["Filter"]["Name"] == "Portrait" for payload in calls)
    assert all(payload["Filter"]["GroupIds"] == ["group-owned"] for payload in calls)
    assert all(payload["Filter"]["Statuses"] == ["Active"] for payload in calls)
    assert all(payload["SortBy"] == "CreatedAt" for payload in calls)
    assert all(payload["SortOrder"] == "Desc" for payload in calls)


@pytest.mark.asyncio
async def test_member_asset_list_finds_owned_item_after_unowned_remote_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    owned_id = "asset-owned-late"
    first_page = [f"asset-other-{index:03d}" for index in range(100)]
    calls: list[int] = []

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def owned_receipts(*_args: Any, **_kwargs: Any) -> Any:
        return volcano_assets.OwnedResourceReceipts(
            resource_ids=frozenset({owned_id}),
            group_ids=frozenset({"group-owned"}),
        )

    class Client:
        def __init__(self, _provider: VideoProviderDefinition) -> None:
            pass

        async def request(self, action: str, payload: dict[str, Any]) -> Any:
            assert action == "ListAssets"
            page_number = payload["PageNumber"]
            calls.append(page_number)
            ids = (
                first_page
                if page_number == 1
                else [owned_id]
                if page_number == 2
                else []
            )
            return {
                "Assets": [
                    {
                        "Id": asset_id,
                        "GroupId": "group-owned",
                        "Name": asset_id,
                        "AssetType": "Image",
                        "Status": "Active",
                        "ProjectName": provider.project_name,
                    }
                    for asset_id in ids
                ],
                "TotalCount": 101,
            }

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(
        volcano_assets,
        "_owned_resource_receipts",
        owned_receipts,
    )
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", Client)

    output = await volcano_assets.list_assets(
        "seedance",
        SimpleNamespace(id="user-1", role="member"),
        object(),  # type: ignore[arg-type]
        None,
        None,
        None,
        1,
        20,
        None,
        None,
    )

    assert [item.id for item in output.items] == [owned_id]
    assert output.total_count == 1
    assert calls == [1, 2, 3]


@pytest.mark.asyncio
async def test_member_asset_scan_stops_when_remote_page_makes_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    repeated_ids = [f"asset-other-{index:03d}" for index in range(100)]
    calls: list[int] = []

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def owned_receipts(*_args: Any, **_kwargs: Any) -> Any:
        return volcano_assets.OwnedResourceReceipts(
            resource_ids=frozenset({"asset-owned-never-returned"}),
            group_ids=frozenset({"group-owned"}),
        )

    class Client:
        def __init__(self, _provider: VideoProviderDefinition) -> None:
            pass

        async def request(self, action: str, payload: dict[str, Any]) -> Any:
            assert action == "ListAssets"
            calls.append(payload["PageNumber"])
            return {
                "Assets": [
                    {
                        "Id": asset_id,
                        "GroupId": "group-owned",
                        "Name": asset_id,
                        "AssetType": "Image",
                        "Status": "Active",
                        "ProjectName": provider.project_name,
                    }
                    for asset_id in repeated_ids
                ],
                "TotalCount": 3000,
            }

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(
        volcano_assets,
        "_owned_resource_receipts",
        owned_receipts,
    )
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", Client)

    output = await volcano_assets.list_assets(
        "seedance",
        SimpleNamespace(id="user-1", role="member"),
        object(),  # type: ignore[arg-type]
        None,
        None,
        None,
        1,
        20,
        None,
        None,
    )

    assert output.items == []
    assert output.total_count == 0
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_member_asset_type_filter_applies_after_ownership_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    remote_assets = [
        {
            "Id": f"asset-{index:03d}",
            "GroupId": "group-owned",
            "Name": f"Asset {index}",
            "AssetType": "Video" if index % 2 else "Image",
            "Status": "Active",
            "ProjectName": provider.project_name,
        }
        for index in range(120)
    ]
    owned_ids = frozenset(
        item["Id"] for index, item in enumerate(remote_assets) if index % 3
    )
    expected_ids = [
        item["Id"]
        for item in remote_assets
        if item["Id"] in owned_ids and item["AssetType"] == "Video"
    ]
    calls: list[dict[str, Any]] = []

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def owned_receipts(*_args: Any, **_kwargs: Any) -> Any:
        return volcano_assets.OwnedResourceReceipts(
            resource_ids=owned_ids,
            group_ids=frozenset({"group-owned"}),
        )

    class Client:
        def __init__(self, _provider: VideoProviderDefinition) -> None:
            pass

        async def request(self, action: str, payload: dict[str, Any]) -> Any:
            assert action == "ListAssets"
            calls.append(payload)
            start = (payload["PageNumber"] - 1) * payload["PageSize"]
            end = start + payload["PageSize"]
            return {
                "Assets": remote_assets[start:end],
                "TotalCount": len(remote_assets),
            }

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(
        volcano_assets,
        "_owned_resource_receipts",
        owned_receipts,
    )
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", Client)

    output = await volcano_assets.list_assets(
        "seedance",
        SimpleNamespace(id="user-1", role="member"),
        object(),  # type: ignore[arg-type]
        None,
        None,
        None,
        2,
        15,
        None,
        None,
        asset_types=["Video", "Video"],
    )

    assert [item.id for item in output.items] == expected_ids[15:30]
    assert output.total_count == len(expected_ids)
    assert all(item.asset_type == "Video" for item in output.items)
    assert [payload["PageNumber"] for payload in calls] == [1, 2, 3]
    assert all("AssetTypes" not in payload["Filter"] for payload in calls)


@pytest.mark.asyncio
async def test_admin_asset_type_filter_scans_but_unfiltered_stays_single_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    remote_assets = [
        {
            "Id": f"asset-{index:03d}",
            "GroupId": "group-1",
            "Name": f"Asset {index}",
            "AssetType": "Image" if index % 2 == 0 else "Video",
            "Status": "Active",
            "ProjectName": provider.project_name,
        }
        for index in range(130)
    ]
    expected_images = [
        item["Id"] for item in remote_assets if item["AssetType"] == "Image"
    ]
    calls: list[dict[str, Any]] = []

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def unexpected_receipts(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("admin asset list must not query ownership receipts")

    class Client:
        def __init__(self, _provider: VideoProviderDefinition) -> None:
            pass

        async def request(self, action: str, payload: dict[str, Any]) -> Any:
            assert action == "ListAssets"
            calls.append(payload)
            start = (payload["PageNumber"] - 1) * payload["PageSize"]
            end = start + payload["PageSize"]
            return {
                "Assets": remote_assets[start:end],
                "TotalCount": len(remote_assets),
                "PageNumber": payload["PageNumber"],
                "PageSize": payload["PageSize"],
            }

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(
        volcano_assets,
        "_owned_resource_receipts",
        unexpected_receipts,
    )
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", Client)
    admin = SimpleNamespace(id="admin-1", role="admin")

    unfiltered = await volcano_assets.list_assets(
        "seedance",
        admin,
        object(),  # type: ignore[arg-type]
        None,
        None,
        None,
        2,
        10,
        None,
        None,
    )

    assert [item.id for item in unfiltered.items] == [
        item["Id"] for item in remote_assets[10:20]
    ]
    assert unfiltered.total_count == len(remote_assets)
    assert len(calls) == 1
    assert calls[0]["PageNumber"] == 2
    assert calls[0]["PageSize"] == 10

    calls.clear()
    filtered = await volcano_assets.list_assets(
        "seedance",
        admin,
        object(),  # type: ignore[arg-type]
        None,
        None,
        None,
        2,
        25,
        None,
        None,
        asset_types=["Image"],
    )

    assert [item.id for item in filtered.items] == expected_images[25:50]
    assert filtered.total_count == len(expected_images)
    assert all(item.asset_type == "Image" for item in filtered.items)
    assert [payload["PageNumber"] for payload in calls] == [1, 2, 3]
    assert all(payload["PageSize"] == 100 for payload in calls)
    assert all("AssetTypes" not in payload["Filter"] for payload in calls)


@pytest.mark.asyncio
async def test_admin_lists_keep_global_project_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    captured: dict[str, Any] = {}

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def unexpected_receipts(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("admin list must not query ownership receipts")

    class Client:
        def __init__(self, _provider: VideoProviderDefinition) -> None:
            pass

        async def request(self, action: str, payload: dict[str, Any]) -> Any:
            captured["action"] = action
            captured["payload"] = payload
            return {
                "AssetGroups": [
                    {
                        "Id": "group-1",
                        "Name": "One",
                        "GroupType": "AIGC",
                        "ProjectName": "project-a",
                    },
                    {
                        "Id": "group-2",
                        "Name": "Two",
                        "GroupType": "AIGC",
                        "ProjectName": "project-a",
                    },
                ],
                "TotalCount": 2,
                "PageNumber": 2,
                "PageSize": 1,
            }

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(
        volcano_assets,
        "_owned_resource_receipts",
        unexpected_receipts,
    )
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", Client)

    groups = await volcano_assets.list_groups(
        "seedance",
        SimpleNamespace(id="admin-1", role="admin"),
        object(),  # type: ignore[arg-type]
        None,
        None,
        2,
        1,
        None,
        None,
    )

    assert [item.id for item in groups.items] == ["group-1", "group-2"]
    assert groups.total_count == 2
    assert captured["payload"]["PageNumber"] == 2
    assert captured["payload"]["PageSize"] == 1


@pytest.mark.asyncio
async def test_group_and_asset_mutations_only_queue_scoped_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    redis = _Redis()
    queued: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def enqueue(operation: dict[str, Any]) -> None:
        queued.append(dict(operation))

    async def audit_write(**kwargs: Any) -> None:
        audits.append(kwargs["details"])

    async def unexpected_admission(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("only create_asset may reserve CreateAsset QPM")

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(
        volcano_assets,
        "VolcanoAssetClient",
        lambda _p: (_ for _ in ()).throw(
            AssertionError("write request must not instantiate an upstream client")
        ),
    )
    monkeypatch.setattr(volcano_assets, "_audit_write", audit_write)
    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "_enqueue_operation", enqueue)
    monkeypatch.setattr(
        volcano_assets,
        "acquire_volcano_create_rate_limit",
        unexpected_admission,
    )

    user = SimpleNamespace(id="user-1", email="user@example.com", role="admin")
    db = _Db()
    outputs = [
        await volcano_assets.create_group(
            VideoAssetGroupCreateIn(name="Portraits", description="AIGC people"),
            _request(),
            user,
            db,  # type: ignore[arg-type]
            "seedance",
        ),
        await volcano_assets.update_group(
            "group-1",
            VideoAssetGroupUpdateIn(name="Renamed", description="Updated"),
            _request("PATCH"),
            user,
            db,  # type: ignore[arg-type]
            "seedance",
        ),
        await volcano_assets.delete_group(
            "group-1",
            _request("DELETE"),
            user,
            db,  # type: ignore[arg-type]
            "seedance",
        ),
        await volcano_assets.update_asset(
            "asset-1",
            VideoAssetUpdateIn(name="Asset renamed"),
            _request("PATCH"),
            user,
            db,  # type: ignore[arg-type]
            "seedance",
        ),
        await volcano_assets.delete_asset(
            "asset-1",
            _request("DELETE"),
            user,
            db,  # type: ignore[arg-type]
            "seedance",
        ),
    ]

    assert [output.action for output in outputs] == [
        "create_group",
        "update_group",
        "delete_group",
        "update_asset",
        "delete_asset",
    ]
    assert all(output.status == "queued" for output in outputs)
    assert all(output.delivery_generation == 0 for output in outputs)
    assert [operation["action"] for operation in queued] == [
        "create_group",
        "update_group",
        "delete_group",
        "update_asset",
        "delete_asset",
    ]
    assert queued[0]["fields"] == {
        "name": "Portraits",
        "description": "AIGC people",
        "group_type": "AIGC",
    }
    assert queued[1]["target_id"] == "group-1"
    assert queued[1]["fields"] == {
        "name": "Renamed",
        "description": "Updated",
    }
    assert queued[2]["fields"] == {"cascade_assets": True}
    assert queued[3]["target_id"] == "asset-1"
    assert queued[3]["fields"] == {"name": "Asset renamed"}
    assert queued[4]["target_id"] == "asset-1"
    assert queued[4]["fields"] == {}
    assert all(operation["user_id"] == "user-1" for operation in queued)
    assert all(operation["model"] == "seedance" for operation in queued)
    assert all(operation["provider_name"] == "volcano-main" for operation in queued)
    assert all(len(operation["provider_binding"]) == 64 for operation in queued)
    assert all(operation["project_name"] == "project-a" for operation in queued)
    assert all(operation["attempt"] == 1 for operation in queued)
    assert all(operation["delivery_generation"] == 0 for operation in queued)
    serialized = f"{queued!r}{audits!r}"
    assert "AKLTEXAMPLE" not in serialized
    assert "secret-example" not in serialized


@pytest.mark.asyncio
async def test_member_mutations_require_resource_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()

    async def owner(*_args: Any, **_kwargs: Any) -> str:
        return "user-1"

    monkeypatch.setattr(volcano_assets, "_resource_owner_user_id", owner)

    await volcano_assets._require_resource_owner(  # noqa: SLF001
        _Db(),  # type: ignore[arg-type]
        user=SimpleNamespace(id="user-1", role="member"),
        provider=provider,
        resource_type="asset",
        resource_id="asset-1",
    )

    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets._require_resource_owner(  # noqa: SLF001
            _Db(),  # type: ignore[arg-type]
            user=SimpleNamespace(id="user-2", role="member"),
            provider=provider,
            resource_type="asset",
            resource_id="asset-1",
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"]["code"] == "video_asset_forbidden"


@pytest.mark.asyncio
async def test_queue_operation_recovers_lost_redis_set_response_without_duplication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import volcano_asset_operation_key

    provider = _provider()
    redis = _Redis()
    enqueued: list[str] = []

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def store_then_lose(
        _redis: Any,
        operation: dict[str, Any],
    ) -> None:
        redis.values[volcano_asset_operation_key(operation["id"])] = json.dumps(
            operation
        )
        raise ConnectionError("response lost after Redis committed")

    async def enqueue(operation: dict[str, Any]) -> None:
        enqueued.append(str(operation["id"]))

    async def audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "_redis_set_operation", store_then_lose)
    monkeypatch.setattr(volcano_assets, "_enqueue_operation", enqueue)
    monkeypatch.setattr(volcano_assets, "_audit_write", audit)

    output = await volcano_assets.update_asset(
        "asset-1",
        VideoAssetUpdateIn(name="Portrait"),
        _request("PATCH"),
        SimpleNamespace(id="user-1", email="user@example.com", role="admin"),
        _Db(),  # type: ignore[arg-type]
        "seedance",
    )

    assert output.status == "queued"
    assert enqueued == [output.id]
    stored = json.loads(redis.values[volcano_asset_operation_key(output.id)])
    assert stored["action"] == "update_asset"
    assert stored["target_id"] == "asset-1"
    assert stored["fields"] == {"name": "Portrait"}


@pytest.mark.asyncio
async def test_enqueue_operation_uses_attempt_and_delivery_generation_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    calls: list[tuple[Any, ...]] = []

    class _Pool:
        async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
            calls.append((*args, kwargs))

    async def get_pool() -> _Pool:
        return _Pool()

    monkeypatch.setattr(volcano_assets, "get_arq_pool", get_pool)

    operation = {
        "id": "operation-1",
        "attempt": 3,
        "delivery_generation": 2,
    }
    await volcano_assets._enqueue_operation(operation)
    await volcano_assets._enqueue_operation(operation)

    assert len(calls) == 2
    for job_name, operation_id, attempt, generation, kwargs in calls:
        assert job_name == "process_volcano_asset_operation"
        assert operation_id == "operation-1"
        assert attempt == 3
        assert generation == 2
        assert kwargs["_job_id"] == "volcano-asset:operation-1:3:2"


@pytest.mark.asyncio
async def test_create_asset_uses_local_source_and_processing_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    audits: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    db = _Db()

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def resolve_source(**_kwargs: Any) -> Any:
        return volcano_assets._LocalAssetSource(
            asset_type="Image",
            local_id="image-1",
        )

    async def public_base(*_args: Any, **_kwargs: Any) -> str:
        return "https://lumen.example"

    async def acquire(*_args: Any, **kwargs: Any) -> None:
        assert kwargs["bucket"] == "admission"

    async def store(_redis: Any, operation: dict[str, Any]) -> None:
        queued.append(dict(operation))

    async def enqueue(operation: dict[str, Any]) -> None:
        assert operation["id"] == queued[0]["id"]

    async def audit_write(**kwargs: Any) -> None:
        audits.append(kwargs["details"])

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(volcano_assets, "_resolve_local_asset_source", resolve_source)
    monkeypatch.setattr(volcano_assets, "_public_base_url", public_base)
    monkeypatch.setattr(volcano_assets, "get_redis", lambda: object())
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", acquire)
    monkeypatch.setattr(volcano_assets, "_redis_set_operation", store)
    monkeypatch.setattr(volcano_assets, "_enqueue_operation", enqueue)
    monkeypatch.setattr(volcano_assets, "_audit_write", audit_write)
    monkeypatch.setattr(
        volcano_assets,
        "VolcanoAssetClient",
        lambda _p: (_ for _ in ()).throw(
            AssertionError("create_asset must not call the upstream in the API")
        ),
    )

    output = await volcano_assets.create_asset(
        VideoAssetCreateIn(
            group_id="group-1",
            name=" \n",
            image_id="image-1",
        ),
        _request(),
        SimpleNamespace(id="user-1", email="user@example.com", role="admin"),
        db,  # type: ignore[arg-type]
        "seedance",
    )

    assert output.id == queued[0]["id"]
    assert output.action == "create_asset"
    assert output.status == "queued"
    assert output.progress_stage == "queued"
    assert output.result is None
    assert db.commits == 0
    assert queued[0]["public_base_url"] == "https://lumen.example"
    assert queued[0]["local_source_id"] == "image-1"
    assert queued[0]["name"].startswith("lumen-asset-")
    assert queued[0]["fields"]["name"] == queued[0]["name"]
    assert queued[0]["fields"]["group_id"] == "group-1"
    assert "url" not in queued[0]
    assert "access_key_id" not in queued[0]
    assert "secret_access_key" not in queued[0]
    assert "https://" not in str(audits)


@pytest.mark.asyncio
async def test_create_asset_returns_structured_429_before_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import VolcanoAssetCreateRateLimited

    provider = _provider()

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def resolve_source(**_kwargs: Any) -> Any:
        return volcano_assets._LocalAssetSource(
            asset_type="Video",
            local_id="video-1",
        )

    async def public_base(*_args: Any, **_kwargs: Any) -> str:
        return "https://lumen.example"

    async def reject(*_args: Any, **_kwargs: Any) -> None:
        raise VolcanoAssetCreateRateLimited(retry_after_ms=12_500)

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(volcano_assets, "_resolve_local_asset_source", resolve_source)
    monkeypatch.setattr(volcano_assets, "_public_base_url", public_base)
    monkeypatch.setattr(volcano_assets, "get_redis", lambda: object())
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", reject)

    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets.create_asset(
            VideoAssetCreateIn(
                group_id="group-1",
                video_id="video-1",
            ),
            _request(),
            SimpleNamespace(id="user-1", email="user@example.com", role="admin"),
            _Db(),  # type: ignore[arg-type]
            "seedance",
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"] == "13"
    error = exc_info.value.detail["error"]
    assert error["code"] == "volcano_asset_create_rate_limited"
    assert error["details"]["limit"] == 3
    assert error["details"]["window_seconds"] == 60


@pytest.mark.asyncio
async def test_operation_status_is_user_scoped_and_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import volcano_asset_operation_key

    redis = _Redis()
    operation = {
        "id": "operation-1",
        "action": "create_asset",
        "status": "running",
        "progress_stage": "normalizing_video",
        "attempt": 1,
        "retryable": False,
        "user_id": "user-1",
        "group_id": "group-1",
        "name": "Portrait",
        "asset_type": "Video",
        "project_name": "project-a",
        "created_at": "2026-07-15T00:00:00+00:00",
        "updated_at": "2026-07-15T00:00:01+00:00",
    }
    redis.values[volcano_asset_operation_key("operation-1")] = json.dumps(operation)
    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)

    output = await volcano_assets.get_operation(
        "operation-1",
        SimpleNamespace(id="user-1"),
    )

    assert output.id == "operation-1"
    assert output.status == "running"
    assert output.progress_stage == "normalizing_video"
    assert output.result is None

    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets.get_operation(
            "operation-1",
            SimpleNamespace(id="user-2"),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_asset_compat_poll_follows_completed_operation_to_real_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import volcano_asset_operation_key

    provider = _provider()
    redis = _Redis()
    operation = {
        "id": "operation-1",
        "action": "create_asset",
        "status": "succeeded",
        "progress_stage": "completed",
        "user_id": "user-1",
        "model": "seedance",
        "provider_name": provider.name,
        "provider_binding": video_provider_binding_fingerprint(provider),
        "region": provider.region,
        "group_id": "group-1",
        "name": "Portrait",
        "asset_type": "Image",
        "project_name": "project-a",
        "created_at": "2026-07-15T00:00:00+00:00",
        "updated_at": "2026-07-15T00:00:01+00:00",
        "result": {
            "id": "asset-1",
            "group_id": "group-1",
            "name": "Portrait",
            "asset_type": "Image",
            "status": "Processing",
            "project_name": "project-a",
        },
    }
    redis.values[volcano_asset_operation_key("operation-1")] = json.dumps(operation)

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def current_asset(
        _client: Any,
        _provider: VideoProviderDefinition,
        asset_id: str,
    ) -> dict[str, Any]:
        assert asset_id == "asset-1"
        return {
            **operation["result"],
            "status": "Active",
        }

    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: object())
    monkeypatch.setattr(volcano_assets, "_get_asset", current_asset)

    output = await volcano_assets.get_asset(
        "operation-1",
        "seedance",
        SimpleNamespace(id="user-1"),
        _Db(),  # type: ignore[arg-type]
    )

    assert output.id == "asset-1"
    assert output.status == "Active"


@pytest.mark.asyncio
async def test_old_operation_without_provider_binding_cannot_follow_current_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import volcano_asset_operation_key

    provider = _provider()
    redis = _Redis()
    operation = {
        "id": "operation-legacy",
        "action": "create_asset",
        "status": "succeeded",
        "progress_stage": "completed",
        "user_id": "user-1",
        "model": "seedance",
        "group_id": "group-1",
        "name": "Portrait",
        "asset_type": "Image",
        "project_name": provider.project_name,
        "created_at": "2026-07-15T00:00:00+00:00",
        "updated_at": "2026-07-15T00:00:01+00:00",
        "result": {
            "id": "asset-legacy",
            "group_id": "group-1",
            "name": "Portrait",
            "asset_type": "Image",
            "status": "Processing",
            "project_name": provider.project_name,
        },
    }
    redis.values[volcano_asset_operation_key(operation["id"])] = json.dumps(operation)

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def unexpected_get(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("legacy operation must not authorize the current binding")

    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(volcano_assets, "_get_asset", unexpected_get)

    output = await volcano_assets.get_asset(
        operation["id"],
        "seedance",
        SimpleNamespace(id="user-1", role="member"),
        _Db(),  # type: ignore[arg-type]
    )

    assert output.id == "asset-legacy"
    assert output.status == "Processing"


@pytest.mark.asyncio
async def test_direct_asset_get_requires_owner_but_admin_keeps_global_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    upstream_calls = 0

    async def no_operation(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def owner(*_args: Any, **_kwargs: Any) -> str:
        return "user-owner"

    async def get_current(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal upstream_calls
        upstream_calls += 1
        return {
            "id": "asset-1",
            "group_id": "group-1",
            "name": "Portrait",
            "asset_type": "Image",
            "status": "Active",
            "project_name": provider.project_name,
        }

    monkeypatch.setattr(volcano_assets, "_redis_get_operation", no_operation)
    monkeypatch.setattr(volcano_assets, "get_redis", lambda: object())
    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(volcano_assets, "_resource_owner_user_id", owner)
    monkeypatch.setattr(volcano_assets, "VolcanoAssetClient", lambda _p: object())
    monkeypatch.setattr(volcano_assets, "_get_asset", get_current)

    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets.get_asset(
            "asset-1",
            "seedance",
            SimpleNamespace(id="user-other", role="member"),
            _Db(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403
    assert upstream_calls == 0

    output = await volcano_assets.get_asset(
        "asset-1",
        "seedance",
        SimpleNamespace(id="admin-1", role="admin"),
        _Db(),  # type: ignore[arg-type]
    )
    assert output.id == "asset-1"
    assert upstream_calls == 1


@pytest.mark.asyncio
async def test_failed_operation_can_be_retried_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import volcano_asset_operation_key

    redis = _Redis()
    operation = {
        **{
            "id": "operation-1",
            "action": "create_asset",
            "status": "failed",
            "progress_stage": "failed",
            "attempt": 1,
            "retryable": True,
            "user_id": "user-1",
            "model": "seedance",
            "provider_name": "volcano-main",
            "project_name": "project-a",
            "region": "cn-beijing",
            "group_id": "group-1",
            "name": "Portrait",
            "asset_type": "Image",
            "local_source_id": "image-1",
            "public_base_url": "https://lumen.example",
            "created_at": "2026-07-15T00:00:00+00:00",
            "updated_at": "2026-07-15T00:00:01+00:00",
        },
        "error": {
            "code": "volcano_asset_unavailable",
            "message": "temporary failure",
            "retryable": True,
        },
    }
    redis.values[volcano_asset_operation_key("operation-1")] = json.dumps(operation)
    enqueued: list[dict[str, Any]] = []

    async def acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def enqueue(current: dict[str, Any]) -> None:
        enqueued.append(dict(current))

    async def audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", acquire)
    monkeypatch.setattr(volcano_assets, "_enqueue_operation", enqueue)
    monkeypatch.setattr(volcano_assets, "_audit_write", audit)

    output = await volcano_assets.retry_operation(
        "operation-1",
        _request(),
        SimpleNamespace(id="user-1", email="user@example.com", role="admin"),
        _Db(),  # type: ignore[arg-type]
    )

    assert output.status == "queued"
    assert output.progress_stage == "queued"
    assert output.attempt == 2
    assert output.error is None
    assert enqueued[0]["attempt"] == 2
    assert (
        "access_key_id" not in redis.values[volcano_asset_operation_key("operation-1")]
    )
    assert (
        "secret_access_key"
        not in redis.values[volcano_asset_operation_key("operation-1")]
    )


@pytest.mark.parametrize(
    "action",
    [
        "create_group",
        "update_group",
        "delete_group",
        "create_asset",
        "update_asset",
        "delete_asset",
    ],
)
@pytest.mark.asyncio
async def test_retry_supports_every_action_and_only_rates_create_asset(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import volcano_asset_operation_key

    redis = _Redis()
    operation = {
        "id": f"operation-{action}",
        "action": action,
        "status": "failed",
        "progress_stage": "failed",
        "attempt": 2,
        "delivery_generation": 1,
        "retryable": True,
        "user_id": "user-1",
        "model": "seedance",
        "provider_name": "volcano-main",
        "project_name": "project-a",
        "region": "cn-shanghai",
        "target_id": "target-1",
        "fields": {"name": "Portrait"},
        "created_at": "2026-07-15T00:00:00+00:00",
        "updated_at": "2026-07-15T00:00:01+00:00",
        "error": {
            "code": "volcano_asset_unavailable",
            "message": "temporary failure",
            "retryable": True,
        },
    }
    redis.values[volcano_asset_operation_key(operation["id"])] = json.dumps(operation)
    rate_calls: list[str] = []
    enqueued: list[dict[str, Any]] = []

    async def acquire(*_args: Any, **kwargs: Any) -> None:
        rate_calls.append(str(kwargs["operation_id"]))

    async def enqueue(current: dict[str, Any]) -> None:
        enqueued.append(dict(current))

    async def audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", acquire)
    monkeypatch.setattr(volcano_assets, "_enqueue_operation", enqueue)
    monkeypatch.setattr(volcano_assets, "_audit_write", audit)

    output = await volcano_assets.retry_operation(
        operation["id"],
        _request(),
        SimpleNamespace(id="user-1", email="user@example.com", role="admin"),
        _Db(),  # type: ignore[arg-type]
    )

    assert output.action == action
    assert output.status == "queued"
    assert output.attempt == 3
    assert output.delivery_generation == 0
    assert enqueued[0]["delivery_generation"] == 0
    assert enqueued[0]["model"] == "seedance"
    assert enqueued[0]["target_id"] == "target-1"
    assert len(rate_calls) == (1 if action == "create_asset" else 0)


@pytest.mark.asyncio
async def test_retry_operation_rejects_other_owner_before_rate_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import volcano_asset_operation_key

    redis = _Redis()
    redis.values[volcano_asset_operation_key("operation-1")] = json.dumps(
        {
            "id": "operation-1",
            "status": "failed",
            "progress_stage": "failed",
            "attempt": 1,
            "retryable": True,
            "user_id": "user-1",
        }
    )
    rate_calls = 0

    async def acquire(*_args: Any, **_kwargs: Any) -> None:
        nonlocal rate_calls
        rate_calls += 1

    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", acquire)

    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets.retry_operation(
            "operation-1",
            _request(),
            SimpleNamespace(id="user-2", email="other@example.com"),
            _Db(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 404
    assert rate_calls == 0


@pytest.mark.asyncio
async def test_retry_operation_enqueues_after_lost_cas_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import volcano_asset_operation_key

    redis = _Redis()
    operation = {
        "id": "operation-1",
        "action": "create_asset",
        "status": "failed",
        "progress_stage": "failed",
        "attempt": 1,
        "retryable": True,
        "user_id": "user-1",
        "model": "seedance",
        "provider_name": "volcano-main",
        "project_name": "project-a",
        "region": "cn-beijing",
    }
    redis.values[volcano_asset_operation_key("operation-1")] = json.dumps(operation)
    enqueued: list[dict[str, Any]] = []

    async def acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def compare(
        _redis: Any,
        operation_id: str,
        **kwargs: Any,
    ) -> tuple[bool, dict[str, Any]]:
        replacement = dict(kwargs["replacement"])
        redis.values[volcano_asset_operation_key(operation_id)] = json.dumps(
            replacement
        )
        return False, replacement

    async def enqueue(current: dict[str, Any]) -> None:
        enqueued.append(dict(current))

    async def audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", acquire)
    monkeypatch.setattr(
        volcano_assets,
        "compare_and_set_volcano_asset_operation",
        compare,
    )
    monkeypatch.setattr(volcano_assets, "_enqueue_operation", enqueue)
    monkeypatch.setattr(volcano_assets, "_audit_write", audit)

    output = await volcano_assets.retry_operation(
        "operation-1",
        _request(),
        SimpleNamespace(id="user-1", email="user@example.com", role="admin"),
        _Db(),  # type: ignore[arg-type]
    )

    assert output.status == "queued"
    assert output.attempt == 2
    assert [item["attempt"] for item in enqueued] == [2]


@pytest.mark.asyncio
async def test_retry_rejects_model_scope_change_after_cas_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets
    from app.volcano_assets import volcano_asset_operation_key

    redis = _Redis()
    operation = {
        "id": "operation-1",
        "action": "update_asset",
        "status": "failed",
        "progress_stage": "failed",
        "attempt": 1,
        "retryable": True,
        "user_id": "user-1",
        "model": "seedance",
        "provider_name": "volcano-main",
        "project_name": "project-a",
        "region": "cn-beijing",
        "target_id": "asset-1",
    }
    redis.values[volcano_asset_operation_key("operation-1")] = json.dumps(operation)
    enqueue_calls = 0

    async def compare(
        _redis: Any,
        _operation_id: str,
        **kwargs: Any,
    ) -> tuple[bool, dict[str, Any]]:
        current = {
            **kwargs["replacement"],
            "model": "other-model",
        }
        return False, current

    async def enqueue(_operation: dict[str, Any]) -> None:
        nonlocal enqueue_calls
        enqueue_calls += 1

    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(
        volcano_assets,
        "compare_and_set_volcano_asset_operation",
        compare,
    )
    monkeypatch.setattr(volcano_assets, "_enqueue_operation", enqueue)

    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets.retry_operation(
            "operation-1",
            _request(),
            SimpleNamespace(id="user-1", email="user@example.com"),
            _Db(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert enqueue_calls == 0


@pytest.mark.asyncio
async def test_create_asset_enqueue_failure_is_pollable_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    redis = _Redis()
    released: list[str] = []

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def resolve_source(**_kwargs: Any) -> Any:
        return volcano_assets._LocalAssetSource(
            asset_type="Image",
            local_id="image-1",
        )

    async def public_base(*_args: Any, **_kwargs: Any) -> str:
        return "https://lumen.example"

    async def acquire(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def enqueue(_operation: dict[str, Any]) -> None:
        raise ConnectionError("queue response lost")

    async def release(
        _redis: Any,
        _key: Any,
        *,
        bucket: str,
        operation_id: str,
    ) -> None:
        assert bucket == "admission"
        released.append(operation_id)

    async def audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(volcano_assets, "_resolve_local_asset_source", resolve_source)
    monkeypatch.setattr(volcano_assets, "_public_base_url", public_base)
    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", acquire)
    monkeypatch.setattr(volcano_assets, "_enqueue_operation", enqueue)
    monkeypatch.setattr(volcano_assets, "release_volcano_create_rate_limit", release)
    monkeypatch.setattr(volcano_assets, "_audit_write", audit)

    output = await volcano_assets.create_asset(
        VideoAssetCreateIn(group_id="group-1", image_id="image-1"),
        _request(),
        SimpleNamespace(id="user-1", email="user@example.com", role="admin"),
        _Db(),  # type: ignore[arg-type]
        "seedance",
    )

    operation = json.loads(
        next(
            value
            for key, value in redis.values.items()
            if key.startswith("video-assets:operation:")
        )
    )
    assert output.status == "failed"
    assert output.action == "create_asset"
    assert output.progress_stage == "enqueue_failed"
    assert output.retryable is True
    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "video_asset_queue_unavailable"
    assert released == [operation["id"]]


@pytest.mark.asyncio
async def test_local_image_lookup_is_scoped_to_current_user() -> None:
    from app.routes import volcano_assets

    db = _Db(result=None)
    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets._resolve_local_asset_source(
            body=VideoAssetCreateIn(
                group_id="group-1",
                image_id="image-other-user",
            ),
            request=_request(),
            user_id="user-1",
            db=db,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"]["code"] == "video_asset_image_not_found"
    statement = str(db.statements[0])
    assert "images.user_id" in statement
    assert "images.deleted_at IS NULL" in statement


@pytest.mark.asyncio
async def test_local_image_source_lookup_does_not_transcode_in_request() -> None:
    from app.routes import volcano_assets

    image = SimpleNamespace(
        id="image-1",
        metadata_jsonb={},
    )

    source = await volcano_assets._resolve_local_asset_source(
        body=VideoAssetCreateIn(
            group_id="group-1",
            image_id="image-1",
        ),
        request=_request(),
        user_id="user-1",
        db=_Db(result=image),  # type: ignore[arg-type]
    )

    assert source.asset_type == "Image"
    assert source.local_id == "image-1"
    assert not hasattr(source, "url")


@pytest.mark.asyncio
async def test_local_video_source_lookup_does_not_transcode_in_request() -> None:
    from app.routes import volcano_assets

    video = SimpleNamespace(
        id="video-1",
        metadata_jsonb={},
    )
    source = await volcano_assets._resolve_local_asset_source(
        body=VideoAssetCreateIn(
            group_id="group-1",
            video_id="video-1",
        ),
        request=_request(),
        user_id="user-1",
        db=_Db(result=video),  # type: ignore[arg-type]
    )

    assert source.asset_type == "Video"
    assert source.local_id == "video-1"
    assert not hasattr(source, "url")


@pytest.mark.asyncio
async def test_non_create_enqueue_failure_is_pollable_without_qpm_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes import volcano_assets

    provider = _provider()
    redis = _Redis()
    admission_calls = 0

    async def require_provider(*_args: Any, **_kwargs: Any) -> VideoProviderDefinition:
        return provider

    async def enqueue(_operation: dict[str, Any]) -> None:
        raise ConnectionError("queue unavailable")

    async def admission(*_args: Any, **_kwargs: Any) -> None:
        nonlocal admission_calls
        admission_calls += 1

    async def audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(volcano_assets, "_require_provider", require_provider)
    monkeypatch.setattr(volcano_assets, "get_redis", lambda: redis)
    monkeypatch.setattr(volcano_assets, "_enqueue_operation", enqueue)
    monkeypatch.setattr(volcano_assets, "_audit_write", audit)
    monkeypatch.setattr(volcano_assets, "acquire_volcano_create_rate_limit", admission)

    output = await volcano_assets.delete_group(
        "group-1",
        _request("DELETE"),
        SimpleNamespace(id="user-1", email="user@example.com", role="admin"),
        _Db(),  # type: ignore[arg-type]
        "seedance",
    )

    operation = json.loads(
        next(
            value
            for key, value in redis.values.items()
            if key.startswith("video-assets:operation:")
        )
    )
    assert output.action == "delete_group"
    assert output.status == "failed"
    assert output.progress_stage == "enqueue_failed"
    assert output.retryable is True
    assert operation["target_id"] == "group-1"
    assert operation["error"]["code"] == "video_asset_queue_unavailable"
    assert admission_calls == 0


@pytest.mark.asyncio
async def test_client_maps_rate_limit_and_uses_configured_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import volcano_assets

    captured: dict[str, Any] = {}
    response = httpx.Response(
        429,
        json={
            "ResponseMetadata": {
                "RequestId": "request-1",
                "Error": {"Code": "Throttling"},
            }
        },
        headers={"Retry-After": "2"},
        request=httpx.Request("POST", "https://example.test"),
    )

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            captured["url"] = url
            captured["request"] = kwargs
            return response

    async def proxy_url(_proxy: Any) -> str:
        return "socks5h://127.0.0.1:1080"

    monkeypatch.setattr(volcano_assets.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(volcano_assets, "resolve_provider_proxy_url", proxy_url)

    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets.VolcanoAssetClient(_provider()).request(
            "GetAsset",
            {"Id": "asset-1", "ProjectName": "project-a"},
        )

    error = exc_info.value.detail["error"]
    assert exc_info.value.status_code == 429
    assert error["code"] == "volcano_asset_rate_limited"
    assert error["retry_after_ms"] == 2_000
    assert captured["proxy"] == "socks5h://127.0.0.1:1080"
    assert captured["url"].startswith("https://ark.cn-shanghai.volcengineapi.com/")
    assert captured["request"]["headers"]["host"] == (
        "ark.cn-shanghai.volcengineapi.com"
    )


@pytest.mark.asyncio
async def test_client_maps_already_deleted_to_idempotent_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import volcano_assets

    response = httpx.Response(
        409,
        json={
            "ResponseMetadata": {
                "RequestId": "request-delete",
                "Error": {"Code": "AssetAlreadyDeleted"},
            }
        },
        request=httpx.Request("POST", "https://example.test"),
    )

    class _Client:
        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            return response

    monkeypatch.setattr(
        volcano_assets.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets.VolcanoAssetClient(_provider()).request(
            "DeleteAsset",
            {"Id": "asset-1", "ProjectName": "project-a"},
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"]["code"] == "volcano_asset_not_found"
    assert "secret-example" not in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_client_maps_upstream_5xx_without_leaking_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import volcano_assets

    response = httpx.Response(
        503,
        json={"ResponseMetadata": {"Error": {"Code": "InternalError"}}},
        request=httpx.Request("POST", "https://example.test"),
    )

    class _Client:
        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            return response

    monkeypatch.setattr(
        volcano_assets.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await volcano_assets.VolcanoAssetClient(_provider()).request(
            "GetAsset",
            {"Id": "asset-1", "ProjectName": "project-a"},
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"]["code"] == "volcano_asset_unavailable"
    assert "secret-example" not in str(exc_info.value.detail)


def test_all_video_asset_writes_require_csrf() -> None:
    from app.deps import verify_csrf
    from app.routes import volcano_assets
    from lumen_core.schemas import VideoAssetOperationOut

    write_routes = [
        route
        for route in volcano_assets.router.routes
        if isinstance(route, APIRoute)
        and route.methods.intersection({"POST", "PATCH", "PUT", "DELETE"})
    ]

    assert write_routes
    for route in write_routes:
        assert any(
            dependency.call is verify_csrf
            for dependency in route.dependant.dependencies
        ), route.path
        assert route.status_code == 202, route.path
        assert route.response_model is VideoAssetOperationOut, route.path

    route_contracts = {
        (route.path, method)
        for route in write_routes
        for method in route.methods
        if method in {"POST", "PATCH", "DELETE"}
    }
    assert route_contracts == {
        ("/video-assets/groups", "POST"),
        ("/video-assets/groups/{group_id}", "PATCH"),
        ("/video-assets/groups/{group_id}", "DELETE"),
        ("/video-assets/assets", "POST"),
        ("/video-assets/assets/{asset_id}", "PATCH"),
        ("/video-assets/assets/{asset_id}", "DELETE"),
        ("/video-assets/operations/{operation_id}/retry", "POST"),
    }


def test_main_registers_video_asset_routes() -> None:
    from app.main import app

    paths = {route.path for route in app.routes}

    assert "/video-assets/capabilities" in paths
    assert "/video-assets/usage" in paths
    assert "/video-assets/groups" in paths
    assert "/video-assets/assets" in paths
    assert "/video-assets/operations/{operation_id}" in paths
    assert "/video-assets/operations/{operation_id}/retry" in paths


def test_usage_route_is_authenticated_and_has_stable_openapi_contract() -> None:
    from fastapi import FastAPI

    from app.deps import get_current_user
    from app.routes import volcano_assets
    from lumen_core.schemas import VideoAssetQuotaUsageOut

    route = next(
        route
        for route in volcano_assets.router.routes
        if isinstance(route, APIRoute) and route.path == "/video-assets/usage"
    )

    assert route.methods == {"GET"}
    assert route.response_model is VideoAssetQuotaUsageOut
    assert any(
        dependency.call is get_current_user
        for dependency in route.dependant.dependencies
    )

    app = FastAPI()
    app.include_router(volcano_assets.router)
    schema = app.openapi()
    operation = schema["paths"]["/video-assets/usage"]["get"]
    parameters = {item["name"]: item for item in operation["parameters"]}
    response_schema = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    usage_schema = schema["components"]["schemas"]["VideoAssetQuotaUsageOut"]

    assert parameters["model"]["in"] == "query"
    assert parameters["model"]["required"] is True
    assert response_schema == {"$ref": "#/components/schemas/VideoAssetQuotaUsageOut"}
    assert usage_schema["required"] == ["assets_used", "asset_groups_used"]
    assert usage_schema["properties"]["assets_used"]["minimum"] == 0
    assert usage_schema["properties"]["asset_groups_used"]["minimum"] == 0
