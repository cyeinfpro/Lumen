"""签名图片代理 `/api/images/_/sig/{image_id}/{variant}` 路由测试。

覆盖：
- secret 未配置 → 503
- 非法 variant → 400
- 篡改 / 过期签名 → 403
- 不存在的 image_id → 404
- 合法签名 + variant=orig → 200 + binary
- 合法签名 + variant=display2048 → 200 + binary
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request

from app.config import settings
from app.routes import images
from lumen_core.image_signing import (
    DEFAULT_TTL_SEC,
    build_signed_path,
    sign_image_url_query,
)


_SECRET = "x" * 48  # >=32, dev-friendly
_IMG_ID = "01900000-0000-7000-8000-000000000001"


# --- Mock DB ----------------------------------------------------------------

class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._v = value

    def scalar_one_or_none(self) -> Any:
        return self._v


class _SequencedDb:
    """按顺序返回多次 execute() 的结果（Image -> ImageVariant 链）。"""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)

    async def execute(self, _stmt: Any) -> _ScalarResult:
        if not self._results:
            return _ScalarResult(None)
        return _ScalarResult(self._results.pop(0))


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """避免依赖真实 redis，统一关掉公共预览限流。"""

    async def _noop(_req: Request) -> None:
        return None

    monkeypatch.setattr(images, "_check_public_image_lookup_rate_limit", _noop)


@pytest.fixture
def _signed_proxy_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "image_proxy_secret", _SECRET)
    return _SECRET


# --- 503 secret missing -----------------------------------------------------

@pytest.mark.asyncio
async def test_returns_503_when_secret_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "image_proxy_secret", "")
    with pytest.raises(HTTPException) as exc:
        await images.get_image_signed(
            _IMG_ID, "orig", 1, "a" * 24, _request(), _SequencedDb([])  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 503


# --- 400 invalid variant ----------------------------------------------------

@pytest.mark.asyncio
async def test_returns_400_for_unknown_variant(_signed_proxy_secret: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await images.get_image_signed(
            _IMG_ID, "huge8k", 1, "a" * 24, _request(), _SequencedDb([])  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 400


# --- 403 invalid / expired sig ----------------------------------------------

@pytest.mark.asyncio
async def test_returns_403_for_bad_signature(_signed_proxy_secret: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await images.get_image_signed(
            _IMG_ID,
            "orig",
            10**13,  # far future
            "deadbeef" * 3,
            _request(),
            _SequencedDb([]),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_returns_403_for_expired_signature(_signed_proxy_secret: str) -> None:
    # exp_ms 已经过去
    exp_ms, sig = sign_image_url_query(
        _IMG_ID,
        "orig",
        _SECRET.encode("utf-8"),
        ttl_sec=3600,
        now_ms=1_500_000_000_000,
    )
    with pytest.raises(HTTPException) as exc:
        await images.get_image_signed(
            _IMG_ID, "orig", exp_ms, sig, _request(), _SequencedDb([])  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


# --- 404 unknown image ------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_404_when_image_not_found(_signed_proxy_secret: str) -> None:
    exp_ms, sig = sign_image_url_query(
        _IMG_ID, "orig", _SECRET.encode("utf-8"), ttl_sec=3600
    )
    db = _SequencedDb([None])
    with pytest.raises(HTTPException) as exc:
        await images.get_image_signed(
            _IMG_ID, "orig", exp_ms, sig, _request(), db  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404


# --- 200 happy path: orig ---------------------------------------------------

@pytest.mark.asyncio
async def test_returns_200_for_valid_orig(
    _signed_proxy_secret: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    fp = tmp_path / "u" / "user-1" / "img.png"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"PNG-PAYLOAD")

    img = SimpleNamespace(
        id=_IMG_ID,
        storage_key="u/user-1/img.png",
        mime="image/png",
        sha256="abc123",
        deleted_at=None,
    )
    exp_ms, sig = sign_image_url_query(
        _IMG_ID, "orig", _SECRET.encode("utf-8"), ttl_sec=3600
    )
    # 路由查询顺序：Image → Share（defense-in-depth，必须有公开 share 才服务）
    resp = await images.get_image_signed(
        _IMG_ID, "orig", exp_ms, sig, _request(), _SequencedDb([img, "share-1"])  # type: ignore[arg-type]
    )
    assert resp.media_type == "image/png"
    assert resp.headers["etag"] == '"abc123"'
    assert resp.headers["cache-control"].startswith("public")
    assert resp.headers["content-length"] == str(len(b"PNG-PAYLOAD"))


# --- 200 happy path: display2048 -------------------------------------------

@pytest.mark.asyncio
async def test_returns_200_for_valid_variant(
    _signed_proxy_secret: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    fp = tmp_path / "u" / "user-1" / "img.display2048.webp"
    fp.parent.mkdir(parents=True)
    fp.write_bytes(b"WEBP-PAYLOAD")

    img = SimpleNamespace(
        id=_IMG_ID,
        storage_key="u/user-1/img.png",
        mime="image/png",
        sha256="abc123",
        deleted_at=None,
    )
    variant = SimpleNamespace(
        image_id=_IMG_ID,
        kind="display2048",
        storage_key="u/user-1/img.display2048.webp",
    )
    exp_ms, sig = sign_image_url_query(
        _IMG_ID, "display2048", _SECRET.encode("utf-8"), ttl_sec=3600
    )
    resp = await images.get_image_signed(
        _IMG_ID,
        "display2048",
        exp_ms,
        sig,
        _request(),
        _SequencedDb([img, "share-1", variant]),  # type: ignore[arg-type]
    )
    assert resp.media_type == "image/webp"
    assert resp.headers["etag"] == f'"{_IMG_ID}-display2048"'


# --- build_signed_path 与路由对接 ------------------------------------------

def test_build_signed_path_matches_route_template() -> None:
    path = build_signed_path(
        _IMG_ID, "thumb256", _SECRET.encode("utf-8"), ttl_sec=DEFAULT_TTL_SEC
    )
    assert path.startswith(f"/api/images/_/sig/{_IMG_ID}/thumb256?")
