from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request

from lumen_core.schemas import VideoProvidersOut, VideoProvidersUpdateIn
from lumen_core.video_providers import parse_video_provider_item


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/admin/providers/video",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def _provider_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": "volcano-main",
        "kind": "volcano",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "generation-key",
        "access_key_id": "AKLToldasset",
        "secret_access_key": "old-secret-access-key",
        "project_name": "project-a",
        "region": "cn-shanghai",
        "enabled": True,
        "models": {"seedance:reference": "doubao-seedance-ref"},
    }
    item.update(overrides)
    return item


class _Db:
    committed = False

    async def commit(self) -> None:
        self.committed = True


async def _persist_update(
    monkeypatch: pytest.MonkeyPatch,
    *,
    old_items: list[dict[str, Any]],
    body: VideoProvidersUpdateIn,
) -> dict[str, Any]:
    from app.routes import providers

    written: dict[str, str] = {}

    async def read_video(_db: Any) -> tuple[str, str]:
        return json.dumps({"providers": old_items}), "db"

    async def read_shared(_db: Any) -> tuple[None, str]:
        return None, "none"

    async def upsert(_db: Any, key: str, value: str) -> None:
        if key == "video.providers":
            written["raw"] = value

    async def delete(_db: Any, _key: str) -> None:
        return None

    async def audit(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def list_after(*_args: Any, **_kwargs: Any) -> VideoProvidersOut:
        return VideoProvidersOut(enabled=body.enabled, items=[], source="db")

    monkeypatch.setattr(providers, "_read_video_providers_raw", read_video)
    monkeypatch.setattr(providers, "_read_providers", read_shared)
    monkeypatch.setattr(providers, "_upsert_setting_value", upsert)
    monkeypatch.setattr(providers, "_delete_setting_value", delete)
    monkeypatch.setattr(providers, "write_audit", audit)
    monkeypatch.setattr(providers, "list_video_providers", list_after)

    await providers.update_video_providers(
        body,
        _request(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        _Db(),  # type: ignore[arg-type]
    )
    return json.loads(written["raw"])


def test_video_provider_output_masks_asset_credentials() -> None:
    from app.routes import providers

    provider = parse_video_provider_item(_provider_item(), index=0)
    output = providers._to_video_provider_out(provider)
    serialized = output.model_dump_json()

    assert output.access_key_id_hint == "AKLT...sset"
    assert output.secret_access_key_hint == "****-key"
    assert output.asset_management_ready is True
    assert output.project_name == "project-a"
    assert output.region == "cn-shanghai"
    assert "AKLToldasset" not in serialized
    assert "old-secret-access-key" not in serialized


@pytest.mark.asyncio
async def test_video_provider_put_preserves_blank_asset_credentials_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoProvidersUpdateIn(
        enabled=True,
        items=[
            {
                "name": "volcano-main",
                "kind": "volcano",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "",
                "access_key_id": "",
                "secret_access_key": "",
                "models": {"seedance:reference": "doubao-seedance-ref"},
            }
        ],
    )

    persisted = await _persist_update(
        monkeypatch,
        old_items=[_provider_item()],
        body=body,
    )

    item = persisted["providers"][0]
    assert item["api_key"] == "generation-key"
    assert item["access_key_id"] == "AKLToldasset"
    assert item["secret_access_key"] == "old-secret-access-key"
    assert item["project_name"] == "project-a"
    assert item["region"] == "cn-shanghai"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("access_key_id", "secret_access_key"),
    [
        ("AKLTnewasset", ""),
        ("", "new-secret-access-key"),
    ],
)
async def test_video_provider_put_rejects_partial_asset_credential_rotation(
    monkeypatch: pytest.MonkeyPatch,
    access_key_id: str,
    secret_access_key: str,
) -> None:
    body = VideoProvidersUpdateIn(
        enabled=True,
        items=[
            {
                "name": "volcano-main",
                "kind": "volcano",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "",
                "access_key_id": access_key_id,
                "secret_access_key": secret_access_key,
                "models": {"seedance:reference": "doubao-seedance-ref"},
            }
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        await _persist_update(
            monkeypatch,
            old_items=[_provider_item()],
            body=body,
        )

    assert exc_info.value.status_code == 422
    assert "必须同时填写" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_video_provider_put_drops_legacy_partial_asset_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoProvidersUpdateIn(
        enabled=True,
        items=[
            {
                "name": "volcano-main",
                "kind": "volcano",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "",
                "models": {"seedance:reference": "doubao-seedance-ref"},
            }
        ],
    )

    persisted = await _persist_update(
        monkeypatch,
        old_items=[_provider_item(secret_access_key="")],
        body=body,
    )

    item = persisted["providers"][0]
    assert "access_key_id" not in item
    assert "secret_access_key" not in item


@pytest.mark.asyncio
async def test_video_provider_rename_does_not_inherit_asset_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoProvidersUpdateIn(
        enabled=True,
        items=[
            {
                "name": "volcano-renamed",
                "kind": "volcano",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "new-generation-key",
                "models": {"seedance:reference": "doubao-seedance-ref"},
            }
        ],
    )

    persisted = await _persist_update(
        monkeypatch,
        old_items=[_provider_item()],
        body=body,
    )

    item = persisted["providers"][0]
    assert "access_key_id" not in item
    assert "secret_access_key" not in item
    assert item["project_name"] == "default"
    assert item["region"] == "cn-beijing"


@pytest.mark.asyncio
async def test_non_volcano_provider_does_not_persist_asset_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoProvidersUpdateIn(
        enabled=True,
        items=[
            {
                "name": "dashscope",
                "kind": "dashscope",
                "base_url": "https://dashscope-intl.aliyuncs.com",
                "api_key": "dashscope-key",
                "access_key_id": "AKLTignored",
                "secret_access_key": "ignored-secret",
                "project_name": "ignored-project",
                "region": "cn-shanghai",
                "models": {"seedance:reference": "provider-model"},
            }
        ],
    )

    persisted = await _persist_update(
        monkeypatch,
        old_items=[],
        body=body,
    )

    item = persisted["providers"][0]
    assert "access_key_id" not in item
    assert "secret_access_key" not in item
    assert "project_name" not in item
    assert "region" not in item
