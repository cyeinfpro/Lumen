"""Unit tests for the poster style library route module.

Mirrors apps/api/tests/test_model_library_db.py's style: stub session,
exercise the route helpers directly, then assert side effects.

Routes are not exercised through TestClient — the apparel-model-library
tests in test_model_library_db.py use the same direct-helper approach so
we follow that pattern for compatibility with the rest of the suite.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routes import _poster_library as plib
from app.routes import poster_styles
from app.workflow_services import library_sync_operation
from lumen_core.constants import (
    POSTER_STYLE_CATEGORIES,
    POSTER_STYLE_DEFAULT_ASPECTS,
)
from lumen_core.models import PosterStyleHiddenPreset, PosterStyleItem
from lumen_core.schemas import (
    PosterStyleBatchDeleteIn,
    PosterStyleCreateIn,
    PosterStyleGenerateIn,
)

_GITHUB_CONTENTS_URL = (
    "https://api.github.com/repos/example/repo/contents/"
    "assets/poster-style-presets?ref=main"
)
_GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/example/repo/main/"
    "assets/poster-style-presets/sample.webp"
)


# --------------------------- Stub session ----------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _StubDb:
    """A minimal AsyncSession stub good enough for route helpers."""

    def __init__(self, response_batches: list[list[Any]] | None = None) -> None:
        self.response_batches: list[list[Any]] = list(response_batches or [])
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flushes = 0
        self.commits = 0
        self.refreshes = 0

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        rows = self.response_batches.pop(0) if self.response_batches else []
        return _Result(rows)

    def add(self, row: Any) -> None:
        self.added.append(row)
        if getattr(row, "id", None) is None:
            try:
                row.id = f"row-{len(self.added)}"
            except Exception:  # noqa: BLE001
                pass

    async def delete(self, row: Any) -> None:
        self.deleted.append(row)

    async def flush(self) -> None:
        self.flushes += 1
        now = datetime(2026, 5, 12, tzinfo=timezone.utc)
        for row in self.added:
            if getattr(row, "created_at", None) is None:
                try:
                    row.created_at = now
                except Exception:  # noqa: BLE001
                    pass
            if getattr(row, "updated_at", None) is None:
                try:
                    row.updated_at = now
                except Exception:  # noqa: BLE001
                    pass

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _row: Any) -> None:
        self.refreshes += 1

    async def get(self, _model: Any, _key: Any) -> Any | None:
        return None


def _admin_user() -> SimpleNamespace:
    return SimpleNamespace(id="user-admin", role="admin", email="admin@example.com")


def _member_user() -> SimpleNamespace:
    return SimpleNamespace(id="user-1", role="member", email="m@example.com")


def _user_item(item_id: str = "user:pstyle-1") -> PosterStyleItem:
    return PosterStyleItem(
        id=item_id,
        user_id="user-1",
        source="user_upload",
        cover_image_id="img-cover",
        sample_image_ids=[],
        title="My Style",
        category="user_favorites",
        mood=None,
        prompt_template=None,
        palette=[],
        recommended_aspects=[],
        style_tags=[],
        library_folder=None,
        auto_tagged_at=None,
        auto_tag_notes=None,
        metadata_jsonb={},
    )


# --------------------------- Helper sanity ---------------------------------


def test_normalize_category_handles_aliases_and_folders() -> None:
    assert plib._normalize_category("illustration") == "illustration"
    assert plib._normalize_category("01_flat_illustration") == "illustration"
    assert plib._normalize_category("flat_illustration") == "illustration"
    # Chinese alias
    assert plib._normalize_category("扁平") == "illustration"
    # Unknown → "other"
    assert plib._normalize_category("steampunk") == "other"
    # Empty → user_favorites
    assert plib._normalize_category("") == "user_favorites"
    assert plib._normalize_category(None) == "user_favorites"


def test_poster_style_categories_constant_covers_normalizer_outputs() -> None:
    # Every output from _normalize_category must be a valid POSTER_STYLE_CATEGORIES member.
    sample_inputs = [
        "illustration",
        "01_flat_illustration",
        "扁平",
        "3d",
        "minimal",
        "retro",
        "traditional",
        "photo",
        "unknown",
        "",
    ]
    for value in sample_inputs:
        assert plib._normalize_category(value) in POSTER_STYLE_CATEGORIES


def test_sha256_file_reads_incrementally(tmp_path: Path) -> None:
    path = tmp_path / "style.bin"
    path.write_bytes((b"abc123" * 20_000) + b"tail")

    assert (
        poster_styles._sha256_file(path)
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )


def test_stream_file_never_reads_past_advertised_size(tmp_path: Path) -> None:
    path = tmp_path / "style.bin"
    path.write_bytes(b"123456")

    assert b"".join(poster_styles._stream_file(path, 3)) == b"123"  # noqa: SLF001


@pytest.mark.asyncio
async def test_binary_response_rejects_oversize_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "oversize.webp"
    path.write_bytes(b"1234")
    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_BINARY_BYTES", 3)
    monkeypatch.setattr(poster_styles, "_storage_path", lambda _key: path)

    hashed: list[Path] = []

    def fail_hash(candidate: Path) -> str:
        hashed.append(candidate)
        raise AssertionError("oversize files must be rejected before hashing")

    monkeypatch.setattr(poster_styles, "_sha256_file", fail_hash)

    with pytest.raises(Exception) as excinfo:
        await poster_styles._binary_response(  # noqa: SLF001
            "poster-style-library/oversize.webp",
            SimpleNamespace(headers={}),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 413
    assert hashed == []


def test_metadata_from_meta_json_returns_none_without_preset_id() -> None:
    meta_no_id = {"title": "Untitled", "category": "illustration"}
    out = plib._metadata_from_meta_json(meta_no_id, directory=None)
    assert out is None


def test_metadata_from_meta_json_normalizes_fields() -> None:
    meta = {
        "preset_id": "flat_illustration",
        "title": "扁平插画",
        "category": "illustration",
        "prompt_template": "flat vector",
        "palette": ["#FF6B6B", "#4ECDC4"],
        "recommended_aspects": ["1:1", "9:16"],
        "tags": ["扁平", "矢量", "扁平"],  # duplicate
    }
    out = plib._metadata_from_meta_json(meta)
    assert out is not None
    assert out["preset_id"] == "flat_illustration"
    assert out["category"] == "illustration"
    assert out["palette"] == ["#FF6B6B", "#4ECDC4"]
    assert out["recommended_aspects"] == ["1:1", "9:16"]
    assert out["style_tags"] == ["扁平", "矢量"]  # dedupe applied
    assert out["library_folder"] == "01_flat_illustration"


def test_metadata_recommended_aspects_falls_back_to_defaults() -> None:
    meta = {
        "preset_id": "x",
        "title": "X",
        "category": "minimal",
    }
    out = plib._metadata_from_meta_json(meta)
    assert out is not None
    assert out["recommended_aspects"] == list(POSTER_STYLE_DEFAULT_ASPECTS)


def test_scan_local_presets_reads_repo_assets() -> None:
    # The repo ships 6 presets under assets/poster-style-presets/.
    root = Path(__file__).resolve().parents[3] / "assets" / "poster-style-presets"
    out = plib._scan_local_presets(root)
    preset_ids = {item["preset_id"] for item in out}
    assert "flat_illustration" in preset_ids
    assert "editorial_photo" in preset_ids
    assert len(out) >= 6
    # Every entry has a category that is a valid POSTER_STYLE_CATEGORIES member.
    for entry in out:
        assert entry["category"] in POSTER_STYLE_CATEGORIES


def test_github_contents_url_validator_rejects_non_github_and_traversal() -> None:
    for url in (
        "http://127.0.0.1/repos/example/repo/contents/assets",
        "https://api.github.com.evil.test/repos/example/repo/contents/assets",
        "https://user@api.github.com/repos/example/repo/contents/assets",
        "https://api.github.com/repos/example/repo/contents/%2e%2e",
    ):
        with pytest.raises(Exception) as excinfo:
            poster_styles._validate_github_contents_url(url)  # noqa: SLF001
        assert getattr(excinfo.value, "status_code", None) == 503
        assert excinfo.value.detail["error"]["code"] == "invalid_preset_sync_url"


def test_github_contents_url_validator_accepts_expected_folder_url() -> None:
    assert (
        poster_styles._validate_github_contents_url(_GITHUB_CONTENTS_URL)  # noqa: SLF001
        == _GITHUB_CONTENTS_URL
    )
    trailing = _GITHUB_CONTENTS_URL.replace("?ref=main", "/?ref=main")
    assert poster_styles._validate_github_contents_url(trailing) == trailing  # noqa: SLF001


def test_poster_style_http_client_disables_automatic_redirects() -> None:
    kwargs = poster_styles._http_client_kwargs(None)  # noqa: SLF001
    assert kwargs["follow_redirects"] is False


@pytest.mark.asyncio
async def test_github_download_redirect_is_revalidated_before_following() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/internal"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="GitHub raw URL"):
            await poster_styles._fetch_github_download_bytes(  # noqa: SLF001
                client,
                _GITHUB_RAW_URL,
            )

    assert requested == [_GITHUB_RAW_URL]


@pytest.mark.asyncio
async def test_github_download_allows_bounded_safe_redirect() -> None:
    target = (
        "https://media.githubusercontent.com/media/example/repo/main/"
        "assets/poster-style-presets/sample.webp"
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == _GITHUB_RAW_URL:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=b"image")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await poster_styles._fetch_github_download_bytes(  # noqa: SLF001
            client,
            _GITHUB_RAW_URL,
            max_bytes=5,
        )

    assert payload == b"image"
    assert requested == [_GITHUB_RAW_URL, target]


@pytest.mark.asyncio
async def test_github_download_enforces_streamed_byte_limit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"1234")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            poster_styles._PosterStyleSyncLimitExceeded,  # noqa: SLF001
            match="exceeds 3 bytes",
        ):
            await poster_styles._fetch_github_download_bytes(  # noqa: SLF001
                client,
                _GITHUB_RAW_URL,
                max_bytes=3,
            )


@pytest.mark.asyncio
async def test_github_contents_walk_ignores_untrusted_entry_url() -> None:
    child_url = poster_styles._github_api_child_url(  # noqa: SLF001
        _GITHUB_CONTENTS_URL,
        "nested",
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == _GITHUB_CONTENTS_URL:
            return httpx.Response(
                200,
                json=[
                    {
                        "type": "dir",
                        "name": "nested",
                        "url": "http://127.0.0.1/internal",
                    }
                ],
            )
        assert str(request.url) == child_url
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        files = await poster_styles._walk_github_contents(  # noqa: SLF001
            client,
            _GITHUB_CONTENTS_URL,
        )

    assert files == []
    assert requested == [_GITHUB_CONTENTS_URL, child_url]


@pytest.mark.asyncio
async def test_github_contents_walk_enforces_directory_and_depth_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"type": "dir", "name": "nested"}])

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_GITHUB_DIRECTORIES", 1)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(
            poster_styles._PosterStyleSyncLimitExceeded,  # noqa: SLF001
            match="directory limit",
        ):
            await poster_styles._walk_github_contents(  # noqa: SLF001
                client,
                _GITHUB_CONTENTS_URL,
            )

    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_GITHUB_DIRECTORIES", 8)
    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_GITHUB_DEPTH", 0)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(
            poster_styles._PosterStyleSyncLimitExceeded,  # noqa: SLF001
            match="depth limit",
        ):
            await poster_styles._walk_github_contents(  # noqa: SLF001
                client,
                _GITHUB_CONTENTS_URL,
            )


@pytest.mark.asyncio
async def test_github_contents_walk_enforces_file_and_response_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = [
        {"type": "file", "name": "a.webp"},
        {"type": "file", "name": "b.webp"},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=files)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_GITHUB_FILES", 1)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(
            poster_styles._PosterStyleSyncLimitExceeded,  # noqa: SLF001
            match="file limit",
        ):
            await poster_styles._walk_github_contents(  # noqa: SLF001
                client,
                _GITHUB_CONTENTS_URL,
            )

    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_GITHUB_FILES", 8)
    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_GITHUB_RESPONSE_BYTES", 8)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(
            poster_styles._PosterStyleSyncLimitExceeded,  # noqa: SLF001
            match="exceeds 8 bytes",
        ):
            await poster_styles._walk_github_contents(  # noqa: SLF001
                client,
                _GITHUB_CONTENTS_URL,
            )


def test_sync_download_budget_enforces_per_file_and_total_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_BINARY_BYTES", 10)
    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_SYNC_DOWNLOAD_BYTES", 5)

    assert (
        poster_styles._sync_download_limit(  # noqa: SLF001
            downloaded_bytes=2,
            expected_size=3,
        )
        == 3
    )
    with pytest.raises(
        poster_styles._PosterStyleSyncLimitExceeded,  # noqa: SLF001
        match="download budget",
    ):
        poster_styles._sync_download_limit(  # noqa: SLF001
            downloaded_bytes=2,
            expected_size=4,
        )
    with pytest.raises(
        poster_styles._PosterStyleSyncLimitExceeded,  # noqa: SLF001
        match="per-file",
    ):
        poster_styles._sync_download_limit(  # noqa: SLF001
            downloaded_bytes=0,
            expected_size=11,
        )


def test_json_index_read_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "index.json"
    path.write_bytes(b'{"value":"too-large"}')
    monkeypatch.setattr(poster_styles, "POSTER_STYLE_MAX_INDEX_BYTES", 8)

    with pytest.raises(Exception) as excinfo:
        poster_styles._read_json_file(path, {})  # noqa: SLF001

    assert getattr(excinfo.value, "status_code", None) == 500
    assert excinfo.value.detail["error"]["code"] == "invalid_index"


# --------------------------- Schema validation -----------------------------


def test_poster_style_create_in_requires_cover_image_id() -> None:
    # Missing cover_image_id must raise
    with pytest.raises(ValidationError):
        PosterStyleCreateIn(title="X")  # type: ignore[call-arg]


def test_poster_style_create_in_accepts_minimal_payload() -> None:
    body = PosterStyleCreateIn(
        cover_image_id="img-1",
        title="Custom Style",
        category="illustration",
    )
    assert body.cover_image_id == "img-1"
    assert body.title == "Custom Style"
    assert body.category == "illustration"
    # auto_tag default = True
    assert body.auto_tag is True


def test_poster_style_generate_in_validates_count_whitelist() -> None:
    body = PosterStyleGenerateIn(title="A", prompt="p", count=2)
    assert body.count == 2

    with pytest.raises(ValidationError):
        # count=5 is outside the 1-4 whitelist
        PosterStyleGenerateIn(title="A", prompt="p", count=5)  # type: ignore[call-arg]


def test_poster_style_batch_delete_requires_at_least_one_id() -> None:
    body = PosterStyleBatchDeleteIn(item_ids=["preset:a:v1", "user:xyz"])
    assert len(body.item_ids) == 2

    with pytest.raises(ValidationError):
        PosterStyleBatchDeleteIn(item_ids=[])


# --------------------------- Item out shape --------------------------------


def test_item_out_from_row_user_routes_use_image_api() -> None:
    row = _user_item()
    row.created_at = datetime(2026, 5, 12, tzinfo=timezone.utc)
    row.updated_at = datetime(2026, 5, 12, tzinfo=timezone.utc)
    out = poster_styles._item_out_from_row(row)
    assert out.cover_image_url == "/api/images/img-cover/binary"
    assert out.cover_image_id == "img-cover"
    assert out.display_url == "/api/images/img-cover/variants/display2048"
    # The cover is exposed as the first sample, even if sample_image_ids was empty.
    assert out.sample_image_ids == ["img-cover"]
    assert len(out.samples) == 1
    assert out.samples[0].image_id == "img-cover"


def test_item_out_from_preset_routes_use_library_endpoints() -> None:
    raw = {
        "id": "preset:flat_illustration:v1",
        "source": "preset",
        "preset_id": "flat_illustration",
        "version": 1,
        "title": "扁平插画",
        "category": "illustration",
        "library_folder": "01_flat_illustration",
        "mood": "现代",
        "prompt_template": "flat vector",
        "palette": ["#FF6B6B"],
        "recommended_aspects": ["1:1"],
        "style_tags": ["扁平"],
        "samples": [
            {
                "name": "sample-01.webp",
                "image_storage_key": "poster-style-library/presets/flat_illustration/v1/sample-01.webp",
                "thumb_storage_key": "poster-style-library/presets/flat_illustration/v1/sample-01.thumb.webp",
                "sha256": "abc",
                "thumb_sha256": "def",
            }
        ],
    }
    out = poster_styles._item_out_from_preset(raw)
    assert out.id == "preset:flat_illustration:v1"
    assert out.source == "preset"
    assert (
        out.cover_image_url
        == "/api/poster-styles/items/preset:flat_illustration:v1/binary"
    )
    assert out.thumb_url == "/api/poster-styles/items/preset:flat_illustration:v1/thumb"
    assert len(out.samples) == 1
    assert (
        out.samples[0].image_url
        == "/api/poster-styles/items/preset:flat_illustration:v1/samples/0"
    )


def test_item_out_from_preset_without_samples_has_no_broken_cover_url() -> None:
    raw = {
        "id": "preset:minimal:v1",
        "source": "preset",
        "preset_id": "minimal",
        "version": 1,
        "title": "极简排版",
        "category": "minimal",
        "samples": [],
    }

    out = poster_styles._item_out_from_preset(raw)

    assert out.cover_image_url == ""
    assert out.display_url is None
    assert out.thumb_url is None
    assert out.samples == []


def test_item_out_from_preset_caps_untrusted_sample_list() -> None:
    raw = {
        "id": "preset:minimal:v1",
        "source": "preset",
        "preset_id": "minimal",
        "version": 1,
        "title": "极简排版",
        "category": "minimal",
        "samples": [
            {
                "name": f"sample-{index}.webp",
                "image_storage_key": f"poster-style-library/{index}.webp",
            }
            for index in range(plib.POSTER_STYLE_MAX_SAMPLES + 5)
        ],
    }

    out = poster_styles._item_out_from_preset(raw)

    assert len(out.samples) == plib.POSTER_STYLE_MAX_SAMPLES
    assert out.samples[-1].index == plib.POSTER_STYLE_MAX_SAMPLES - 1


# --------------------------- List filter / sync state ----------------------


def test_filter_preset_items_by_category_and_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        {
            "id": "preset:a:v1",
            "preset_id": "a",
            "version": 1,
            "title": "扁平插画",
            "category": "illustration",
            "style_tags": ["扁平", "矢量"],
            "prompt_template": "flat",
        },
        {
            "id": "preset:b:v1",
            "preset_id": "b",
            "version": 1,
            "title": "3D 渲染",
            "category": "3d",
            "style_tags": ["立体"],
            "prompt_template": "3d",
        },
    ]
    out = poster_styles._filter_preset_items(items, category="3d", q="", tags=[])
    assert [item["id"] for item in out] == ["preset:b:v1"]

    out_q = poster_styles._filter_preset_items(items, category="all", q="扁平", tags=[])
    assert [item["id"] for item in out_q] == ["preset:a:v1"]

    out_tags = poster_styles._filter_preset_items(
        items, category="all", q="", tags=["立体"]
    )
    assert [item["id"] for item in out_tags] == ["preset:b:v1"]


@pytest.mark.asyncio
async def test_sync_state_out_reports_can_sync_for_admin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        poster_styles, "_library_sync_state_path", lambda: tmp_path / "missing.json"
    )

    async def _admin_only_mode(_db: Any) -> str:
        return "admin_only"

    monkeypatch.setattr(poster_styles, "_sync_mode", _admin_only_mode)
    state = await poster_styles._sync_state_out(_StubDb(), _admin_user())
    assert state.can_sync is True
    state_member = await poster_styles._sync_state_out(_StubDb(), _member_user())
    assert state_member.can_sync is False


@pytest.mark.asyncio
async def test_sync_state_out_anyone_can_sync_when_mode_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        poster_styles, "_library_sync_state_path", lambda: tmp_path / "missing.json"
    )

    async def _open_mode(_db: Any) -> str:
        return "any_authenticated"

    monkeypatch.setattr(poster_styles, "_sync_mode", _open_mode)
    member = await poster_styles._sync_state_out(_StubDb(), _member_user())
    assert member.can_sync is True


@pytest.mark.asyncio
async def test_sync_state_out_disabled_blocks_everyone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        poster_styles, "_library_sync_state_path", lambda: tmp_path / "missing.json"
    )

    async def _disabled_mode(_db: Any) -> str:
        return "disabled"

    monkeypatch.setattr(poster_styles, "_sync_mode", _disabled_mode)
    admin = await poster_styles._sync_state_out(_StubDb(), _admin_user())
    assert admin.can_sync is False


# --------------------------- Sync cooldown ---------------------------------


@pytest.mark.asyncio
async def test_sync_lease_allows_only_one_active_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "sync-state.json"
    monkeypatch.setattr(poster_styles, "_library_sync_state_path", lambda: state_path)
    monkeypatch.setattr(
        poster_styles, "_library_sync_lock_path", lambda: tmp_path / "sync.lock"
    )

    first_token, first_state = await poster_styles._claim_library_sync_lease()  # noqa: SLF001
    second_token, second_state = await poster_styles._claim_library_sync_lease()  # noqa: SLF001

    assert first_token
    assert first_state["sync_lease"]["token"] == first_token
    assert second_token is None
    assert second_state["sync_lease"]["token"] == first_token


def test_sync_lease_is_atomic_across_processes(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from app.routes import poster_styles as target\n"
        "root = Path(sys.argv[1])\n"
        "target._library_sync_state_path = lambda: root / 'state.json'\n"
        "target._library_sync_lock_path = lambda: root / 'sync.lock'\n"
        "token, _state = target._claim_library_sync_lease_sync()\n"
        "print('won' if token else 'lost')\n"
    )
    processes = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", script, str(tmp_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results: list[str] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        results.append(stdout.strip())

    assert results.count("won") == 1
    assert results.count("lost") == 3


@pytest.mark.asyncio
async def test_lost_sync_lease_cannot_publish_stale_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    index_path = tmp_path / "index.json"
    monkeypatch.setattr(poster_styles, "_library_sync_state_path", lambda: state_path)
    monkeypatch.setattr(poster_styles, "_global_preset_index_path", lambda: index_path)
    monkeypatch.setattr(
        poster_styles, "_library_sync_lock_path", lambda: tmp_path / "sync.lock"
    )

    token, state = await poster_styles._claim_library_sync_lease()  # noqa: SLF001
    assert token
    state["sync_lease"]["token"] = "newer-owner"
    poster_styles._save_sync_state(state)  # noqa: SLF001

    with pytest.raises(
        poster_styles._PosterStyleSyncLeaseLost,  # noqa: SLF001
        match="lease was lost",
    ):
        await poster_styles._complete_library_sync_lease(  # noqa: SLF001
            token,
            {
                "schema_version": 1,
                "updated_at": None,
                "preset_items": [{"id": "preset:stale:v1"}],
            },
            {"added": 1, "updated": 0, "skipped": 0, "errors": []},
            datetime.now(timezone.utc),
        )

    assert index_path.exists() is False


@pytest.mark.asyncio
async def test_sync_releases_process_lock_before_external_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        poster_styles, "_library_sync_state_path", lambda: tmp_path / "state.json"
    )
    monkeypatch.setattr(
        poster_styles, "_library_sync_lock_path", lambda: tmp_path / "sync.lock"
    )

    async def fake_sync(
        _url: str,
        _state: dict[str, Any],
        *,
        proxy_url: str | None,
        lease_token: str,
    ) -> Any:
        assert proxy_url is None
        assert lease_token
        assert plib._SYNC_LOCK.locked() is False
        return poster_styles.PosterStyleSyncOut(status="ok")

    monkeypatch.setattr(poster_styles, "_do_sync_library_presets", fake_sync)

    out = await poster_styles._sync_library_presets_from_github_folder(  # noqa: SLF001
        _GITHUB_CONTENTS_URL,
    )

    assert out.status == "ok"


@pytest.mark.asyncio
async def test_do_sync_library_presets_delegates_to_workflow_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = poster_styles._default_sync_state()  # noqa: SLF001
    captured: dict[str, Any] = {}

    async def fake_sync(
        runtime: Any,
        contents_url: str,
        sync_state: dict[str, Any],
        *,
        proxy_url: str | None,
        lease_token: str | None,
    ) -> Any:
        captured.update(
            runtime=runtime,
            contents_url=contents_url,
            state=sync_state,
            proxy_url=proxy_url,
            lease_token=lease_token,
        )
        return poster_styles.PosterStyleSyncOut(status="ok", added=2)

    monkeypatch.setattr(poster_styles, "_do_poster_style_sync", fake_sync)

    out = await poster_styles._do_sync_library_presets(  # noqa: SLF001
        "delegated://poster-styles",
        state,
        proxy_url="http://proxy.test:8080",
        lease_token="lease-token",
    )

    assert out.added == 2
    assert captured == {
        "runtime": poster_styles,
        "contents_url": "delegated://poster-styles",
        "state": state,
        "proxy_url": "http://proxy.test:8080",
        "lease_token": "lease-token",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected_status", "expected_code", "same_exception"),
    [
        (
            poster_styles._http("upstream_failed", "upstream rejected", 418),  # noqa: SLF001
            418,
            "upstream_failed",
            True,
        ),
        (
            poster_styles._PosterStyleSyncLeaseLost(  # noqa: SLF001
                "poster style sync lease was lost"
            ),
            409,
            "preset_sync_conflict",
            False,
        ),
        (
            ValueError("poster sync exploded"),
            502,
            "preset_sync_failed",
            False,
        ),
    ],
)
async def test_poster_sync_service_preserves_error_semantics(
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    expected_status: int,
    expected_code: str,
    same_exception: bool,
) -> None:
    captured_failure: dict[str, Any] = {}

    async def fail_build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise raised

    async def fake_fail(
        token: str,
        *,
        message: str,
        result: dict[str, Any],
    ) -> bool:
        captured_failure.update(token=token, message=message, result=result)
        return True

    monkeypatch.setattr(
        library_sync_operation,
        "_build_poster_style_sync_index",
        fail_build,
    )
    monkeypatch.setattr(poster_styles, "_fail_library_sync_lease", fake_fail)

    with pytest.raises(HTTPException) as excinfo:
        await poster_styles._do_sync_library_presets(  # noqa: SLF001
            _GITHUB_CONTENTS_URL,
            poster_styles._default_sync_state(),  # noqa: SLF001
            lease_token="lease-token",
        )

    error = excinfo.value
    assert error.status_code == expected_status
    assert error.detail["error"]["code"] == expected_code
    if same_exception:
        assert error is raised
    expected_message = str(raised) or raised.__class__.__name__
    assert captured_failure == {
        "token": "lease-token",
        "message": expected_message,
        "result": {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [expected_message[:300]],
        },
    }


@pytest.mark.asyncio
async def test_sync_short_circuits_on_recent_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """5min 内已成功一次 → 同步 endpoint 必须返回 skipped，不再打 GitHub。"""
    state_path = tmp_path / "sync-state.json"
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "last_success_at": fresh,
                "last_error": None,
                "last_attempt_at": fresh,
                "last_result": {"added": 0, "updated": 0, "skipped": 0, "errors": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(poster_styles, "_library_sync_state_path", lambda: state_path)
    monkeypatch.setattr(
        poster_styles, "_library_sync_lock_path", lambda: tmp_path / "sync.lock"
    )

    called: list[str] = []

    async def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        called.append("walked")
        raise AssertionError("must not hit GitHub during cooldown")

    monkeypatch.setattr(poster_styles, "_do_sync_library_presets", _should_not_run)
    out = await poster_styles._sync_library_presets_from_github_folder(
        _GITHUB_CONTENTS_URL, proxy_url=None
    )
    assert out.status == "skipped"
    assert called == []


@pytest.mark.asyncio
async def test_sync_skips_duplicate_preset_id_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        poster_styles, "_global_preset_index_path", lambda: tmp_path / "index.json"
    )
    monkeypatch.setattr(
        poster_styles, "_library_sync_state_path", lambda: tmp_path / "state.json"
    )
    monkeypatch.setattr(
        poster_styles, "_library_sync_lock_path", lambda: tmp_path / "sync.lock"
    )

    async def fake_walk(
        _client: Any,
        _url: str,
        *,
        progress: Any = None,
    ) -> list[dict[str, str]]:
        if progress is not None:
            await progress()
        return [
            {"path": "dir_a/meta.json"},
            {"path": "dir_b/meta.json"},
        ]

    async def fake_meta(
        _client: Any,
        entry: dict[str, str],
        *,
        progress: Any = None,
    ) -> dict[str, Any]:
        if progress is not None:
            await progress()
        return {
            "preset_id": "duplicate_style",
            "version": 1,
            "title": f"Style {entry['path']}",
            "category": "minimal",
        }

    monkeypatch.setattr(poster_styles, "_walk_github_contents", fake_walk)
    monkeypatch.setattr(poster_styles, "_fetch_meta_json", fake_meta)

    out = await poster_styles._do_sync_library_presets(  # noqa: SLF001
        _GITHUB_CONTENTS_URL,
        poster_styles._default_sync_state(),  # noqa: SLF001
        proxy_url=None,
    )

    assert out.status == "ok"
    assert out.added == 1
    assert out.skipped == 1
    assert any("duplicate preset_id/version duplicate_style@1" in e for e in out.errors)


@pytest.mark.asyncio
async def test_poster_sync_service_materializes_sample_and_thumb(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.json"
    state_path = tmp_path / "state.json"
    thumb_url = _GITHUB_RAW_URL.replace("sample.webp", "sample.thumb.webp")
    monkeypatch.setattr(
        poster_styles,
        "_global_preset_index_path",
        lambda: index_path,
    )
    monkeypatch.setattr(
        poster_styles,
        "_library_sync_state_path",
        lambda: state_path,
    )
    monkeypatch.setattr(
        poster_styles,
        "_library_sync_lock_path",
        lambda: tmp_path / "sync.lock",
    )
    monkeypatch.setattr(
        poster_styles,
        "_storage_path",
        lambda storage_key: tmp_path / storage_key,
    )

    async def fake_walk(
        _client: Any,
        _url: str,
        *,
        progress: Any = None,
    ) -> list[dict[str, Any]]:
        if progress is not None:
            await progress()
        base = "assets/poster-style-presets/01_minimal/example"
        return [
            {"path": f"{base}/meta.json"},
            {
                "path": f"{base}/sample.webp",
                "download_url": _GITHUB_RAW_URL,
                "sha": "github-image-sha",
                "size": 12,
            },
            {
                "path": f"{base}/sample.thumb.webp",
                "download_url": thumb_url,
                "sha": "github-thumb-sha",
                "size": 10,
            },
        ]

    async def fake_meta(
        _client: Any,
        _entry: dict[str, Any],
        *,
        progress: Any = None,
    ) -> dict[str, Any]:
        if progress is not None:
            await progress()
        return {
            "preset_id": "materialized",
            "version": 1,
            "title": "Materialized",
            "category": "minimal",
        }

    payloads = {
        _GITHUB_RAW_URL: b"sample-bytes",
        thumb_url: b"thumb-data",
    }

    async def fake_download(
        _client: Any,
        url: str,
        *,
        max_bytes: int,
        progress: Any = None,
    ) -> bytes:
        if progress is not None:
            await progress()
        payload = payloads[url]
        assert len(payload) <= max_bytes
        return payload

    monkeypatch.setattr(poster_styles, "_walk_github_contents", fake_walk)
    monkeypatch.setattr(poster_styles, "_fetch_meta_json", fake_meta)
    monkeypatch.setattr(
        poster_styles,
        "_fetch_github_download_bytes",
        fake_download,
    )

    out = await poster_styles._do_sync_library_presets(  # noqa: SLF001
        _GITHUB_CONTENTS_URL,
        poster_styles._default_sync_state(),  # noqa: SLF001
    )

    assert out.status == "ok"
    assert out.added == 1
    saved = json.loads(index_path.read_text("utf-8"))
    sample = saved["preset_items"][0]["samples"][0]
    assert sample["sha256"] == hashlib.sha256(b"sample-bytes").hexdigest()
    assert sample["thumb_sha256"] == hashlib.sha256(b"thumb-data").hexdigest()
    assert sample["github_sha"] == "github-image-sha"
    assert sample["github_thumb_sha"] == "github-thumb-sha"
    assert (tmp_path / sample["image_storage_key"]).read_bytes() == b"sample-bytes"
    assert (tmp_path / sample["thumb_storage_key"]).read_bytes() == b"thumb-data"


# --------------------------- Delete semantics ------------------------------


@pytest.mark.asyncio
async def test_delete_user_item_calls_db_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _user_item("user:to-delete")
    db = _StubDb(
        response_batches=[
            [row],  # _find_user_item SELECT result
        ]
    )

    ok = await poster_styles._delete_poster_style_item_for_user(
        db, user_id="user-1", item_id="user:to-delete"
    )
    assert ok is True
    assert db.deleted == [row]


@pytest.mark.asyncio
async def test_delete_preset_item_inserts_hidden_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_preset = {
        "id": "preset:flat:v1",
        "source": "preset",
        "preset_id": "flat",
        "version": 1,
    }

    async def _fake_find_preset(
        _db: Any, *, user_id: str, item_id: str
    ) -> dict[str, Any] | None:
        return dict(fake_preset)

    monkeypatch.setattr(poster_styles, "_find_preset_item", _fake_find_preset)
    db = _StubDb(
        response_batches=[
            [],  # existing hidden preset row → none → must INSERT
        ]
    )
    ok = await poster_styles._delete_poster_style_item_for_user(
        db, user_id="user-1", item_id="preset:flat:v1"
    )
    assert ok is True
    assert any(isinstance(row, PosterStyleHiddenPreset) for row in db.added), (
        "preset deletion must insert a PosterStyleHiddenPreset row"
    )


@pytest.mark.asyncio
async def test_delete_preset_item_does_not_duplicate_existing_hide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-hidden preset: skip insert, still return True (idempotent)."""
    fake_preset = {
        "id": "preset:flat:v1",
        "source": "preset",
        "preset_id": "flat",
        "version": 1,
    }
    existing_hide = PosterStyleHiddenPreset(
        user_id="user-1", preset_id="preset:flat:v1"
    )

    async def _fake_find_preset(
        _db: Any, *, user_id: str, item_id: str
    ) -> dict[str, Any] | None:
        return dict(fake_preset)

    monkeypatch.setattr(poster_styles, "_find_preset_item", _fake_find_preset)
    db = _StubDb(response_batches=[[existing_hide]])
    ok = await poster_styles._delete_poster_style_item_for_user(
        db, user_id="user-1", item_id="preset:flat:v1"
    )
    assert ok is True
    assert not any(isinstance(row, PosterStyleHiddenPreset) for row in db.added), (
        "already-hidden preset must not insert a duplicate row"
    )


# --------------------------- Auto-tag persistence ---------------------------


@pytest.mark.asyncio
async def test_auto_tag_skips_persistence_when_provider_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _user_item()
    db = _StubDb(response_batches=[[row]])

    async def _empty_upstream(
        _db: Any, *, image_id: str, user_id: str
    ) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(
        poster_styles, "_api_call_poster_style_tagging_upstream", _empty_upstream
    )
    out = await poster_styles._auto_tag_poster_style_item(
        db=db, user_id="user-1", item_id="user:pstyle-1"
    )
    assert out.style_tags == []
    assert row.auto_tagged_at is None
    assert db.commits == 0


@pytest.mark.asyncio
async def test_auto_tag_persists_when_provider_returns_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _user_item()
    db = _StubDb(response_batches=[[row]])

    async def _vision(_db: Any, *, image_id: str, user_id: str) -> dict[str, Any]:
        return {
            "category": "illustration",
            "style_tags": ["扁平", "矢量"],
            "mood": "现代",
            "palette": ["#FF6B6B"],
            "notes": "清新现代",
        }

    monkeypatch.setattr(
        poster_styles, "_api_call_poster_style_tagging_upstream", _vision
    )
    out = await poster_styles._auto_tag_poster_style_item(
        db=db, user_id="user-1", item_id="user:pstyle-1"
    )
    assert out.style_tags == ["扁平", "矢量"]
    assert out.category == "illustration"
    assert row.category == "illustration"
    assert row.mood == "现代"
    assert row.palette == ["#FF6B6B"]
    assert row.auto_tagged_at is not None
    assert db.commits == 1


def test_poster_style_auto_tag_concurrency_env_is_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTER_STYLE_AUTO_TAG_CONCURRENCY", "99")
    assert poster_styles._poster_style_auto_tag_concurrency() == 4  # noqa: SLF001

    monkeypatch.setenv("POSTER_STYLE_AUTO_TAG_CONCURRENCY", "bad")
    assert poster_styles._poster_style_auto_tag_concurrency() == 2  # noqa: SLF001


@pytest.mark.asyncio
async def test_auto_tag_runs_provider_call_inside_rate_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _user_item()
    db = _StubDb(response_batches=[[row]])

    class Gate:
        def __init__(self) -> None:
            self.entered = 0

        async def __aenter__(self) -> None:
            self.entered += 1

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    gate = Gate()
    monkeypatch.setattr(poster_styles, "_poster_style_auto_tag_semaphore", lambda: gate)

    async def _vision(_db: Any, *, image_id: str, user_id: str) -> dict[str, Any]:
        assert gate.entered == 1
        return {"style_tags": ["扁平"], "notes": "ok"}

    monkeypatch.setattr(
        poster_styles, "_api_call_poster_style_tagging_upstream", _vision
    )

    out = await poster_styles._auto_tag_poster_style_item(  # noqa: SLF001
        db=db, user_id="user-1", item_id="user:pstyle-1"
    )

    assert gate.entered == 1
    assert out.style_tags == ["扁平"]


# --------------------------- Generate prompt --------------------------------


def test_generate_prompt_contains_user_intent_at_tail() -> None:
    body = PosterStyleGenerateIn(
        title="复古",
        prompt="给一张暑期促销复古海报",
        category="retro",
        style_tags=["复古", "波普"],
        palette=["#FFC857", "#E9724C"],
        mood="怀旧",
    )
    prompt = poster_styles._poster_style_generate_prompt(body=body, candidate_index=1)
    # User intent must appear (placed near the tail to keep cache prefix stable).
    assert "User intent: 给一张暑期促销复古海报" in prompt
    # Stable cache-prefix prelude present:
    assert "Create one stylish poster sample" in prompt
    # Style metadata gets surfaced
    assert "Style tags: 复古, 波普" in prompt
    assert "Mood: 怀旧" in prompt


@pytest.mark.asyncio
async def test_generate_endpoint_publishes_created_generation_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /poster-styles/generate must enqueue immediately after commit."""

    async def fake_enqueue(**kwargs: Any) -> tuple[list[str], list[dict[str, Any]]]:
        step = kwargs["step"]
        step.task_ids = ["gen-1"]
        return ["gen-1"], [
            {
                "assistant_msg_id": "msg-a",
                "outbox_payloads": [
                    {"kind": "generation", "task_id": "gen-1", "user_id": "user-1"}
                ],
                "outbox_rows": [SimpleNamespace(published_at=None)],
            }
        ]

    published: list[dict[str, Any]] = []

    async def fake_publish(**kwargs: Any) -> None:
        published.append(kwargs)

    monkeypatch.setattr(
        poster_styles, "_enqueue_poster_style_generate_tasks", fake_enqueue
    )
    import app.redis_client as redis_client
    import app.routes.messages as messages

    monkeypatch.setattr(redis_client, "get_redis", lambda: SimpleNamespace())
    monkeypatch.setattr(messages, "_publish_assistant_task", fake_publish)

    db = _StubDb()
    user = _member_user()
    body = PosterStyleGenerateIn(
        title="复古风格", prompt="生成一张复古促销海报", count=1
    )

    out = await poster_styles.generate_poster_style_samples(body, user, db)  # type: ignore[arg-type]

    assert out.task_ids == ["gen-1"]
    assert db.commits == 1
    assert len(published) == 1
    assert published[0]["user_id"] == "user-1"
    assert published[0]["assistant_msg_id"] == "msg-a"
    assert published[0]["outbox_payloads"][0]["task_id"] == "gen-1"
