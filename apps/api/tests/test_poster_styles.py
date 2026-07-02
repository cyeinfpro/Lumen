"""Unit tests for the poster style library route module.

Mirrors apps/api/tests/test_model_library_db.py's style: stub session,
exercise the route helpers directly, then assert side effects.

Routes are not exercised through TestClient — the apparel-model-library
tests in test_model_library_db.py use the same direct-helper approach so
we follow that pattern for compatibility with the rest of the suite.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.routes import _poster_library as plib
from app.routes import poster_styles
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

    called: list[str] = []

    async def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        called.append("walked")
        raise AssertionError("must not hit GitHub during cooldown")

    monkeypatch.setattr(poster_styles, "_do_sync_library_presets", _should_not_run)
    out = await poster_styles._sync_library_presets_from_github_folder(
        "https://api.example.test/contents", proxy_url=None
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

    async def fake_walk(_client: Any, _url: str) -> list[dict[str, str]]:
        return [
            {"path": "dir_a/meta.json"},
            {"path": "dir_b/meta.json"},
        ]

    async def fake_meta(_client: Any, entry: dict[str, str]) -> dict[str, Any]:
        return {
            "preset_id": "duplicate_style",
            "version": 1,
            "title": f"Style {entry['path']}",
            "category": "minimal",
        }

    monkeypatch.setattr(poster_styles, "_walk_github_contents", fake_walk)
    monkeypatch.setattr(poster_styles, "_fetch_meta_json", fake_meta)

    out = await poster_styles._do_sync_library_presets(  # noqa: SLF001
        "https://api.example.test/contents",
        poster_styles._default_sync_state(),  # noqa: SLF001
        proxy_url=None,
    )

    assert out.status == "ok"
    assert out.added == 1
    assert out.skipped == 1
    assert any("duplicate preset_id/version duplicate_style@1" in e for e in out.errors)


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
