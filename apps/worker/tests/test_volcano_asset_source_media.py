from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.tasks import volcano_asset_source_media
from lumen_core.volcano_asset_media import VolcanoAssetInstallReceipt


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Session:
    def __init__(
        self,
        source: Any,
        *,
        commit_error: BaseException | None = None,
    ) -> None:
        self.source = source
        self.commit_error = commit_error

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self.source)

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error


def _operation() -> dict[str, Any]:
    return {
        "asset_type": "Image",
        "local_source_id": "image-1",
        "user_id": "user-1",
        "public_base_url": "https://lumen.example",
    }


@pytest.mark.asyncio
async def test_commit_failure_cleans_only_returned_install_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(id="image-1", metadata_jsonb={})
    receipt = VolcanoAssetInstallReceipt(
        storage_key="images/image-1.volcano.jpg",
        size_bytes=7,
        sha256="a" * 64,
    )
    cleaned: list[VolcanoAssetInstallReceipt] = []

    async def ensure(*_args: Any, **_kwargs: Any) -> tuple[object, Any]:
        return object(), receipt

    async def delete(_root: str, value: Any) -> bool:
        cleaned.append(value)
        return True

    monkeypatch.setattr(
        volcano_asset_source_media,
        "SessionLocal",
        lambda: _Session(source, commit_error=RuntimeError("commit failed")),
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "ensure_volcano_asset_image_variant",
        ensure,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "delete_volcano_asset_install",
        delete,
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await volcano_asset_source_media.normalized_source_url(
            _operation(),
            storage_writes=SimpleNamespace(
                capacity=object(),
                lease_ttl_seconds=30,
            ),
        )

    assert cleaned == [receipt]


@pytest.mark.asyncio
async def test_successful_commit_adopts_install_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(id="image-1", metadata_jsonb={})
    receipt = VolcanoAssetInstallReceipt(
        storage_key="images/image-1.volcano.jpg",
        size_bytes=7,
        sha256="a" * 64,
    )
    cleaned: list[VolcanoAssetInstallReceipt] = []

    async def ensure(*_args: Any, **_kwargs: Any) -> tuple[object, Any]:
        return object(), receipt

    async def delete(_root: str, value: Any) -> bool:
        cleaned.append(value)
        return True

    monkeypatch.setattr(
        volcano_asset_source_media,
        "SessionLocal",
        lambda: _Session(source),
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "ensure_volcano_asset_image_variant",
        ensure,
    )
    monkeypatch.setattr(
        volcano_asset_source_media,
        "delete_volcano_asset_install",
        delete,
    )

    url, kind = await volcano_asset_source_media.normalized_source_url(
        _operation(),
        storage_writes=SimpleNamespace(
            capacity=object(),
            lease_ttl_seconds=30,
        ),
    )

    assert url.startswith("https://lumen.example/")
    assert kind == "volcano_asset_img_v1"
    assert cleaned == []


@pytest.mark.asyncio
async def test_source_normalization_requires_storage_coordinator() -> None:
    with pytest.raises(RuntimeError, match="storage_write_coordinator"):
        await volcano_asset_source_media.normalized_source_url(_operation())
