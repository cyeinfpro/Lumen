from __future__ import annotations

import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app.routes import workflows
from lumen_core.schemas import ApparelModelLibrarySyncOut


def _claim_sync_lease_worker(
    storage_root: str,
    start: Any,
    results: Any,
) -> None:
    workflows.settings.storage_root = storage_root
    if not start.wait(timeout=5):
        raise RuntimeError("sync lease worker start timed out")
    token, _state = workflows._claim_library_sync_lease_sync()  # noqa: SLF001
    results.put(token)


class _ChunkedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"123"
        yield b"45"

    async def aclose(self) -> None:
        return None


def test_model_library_json_read_is_size_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(workflows, "MODEL_LIBRARY_MAX_INDEX_BYTES", 4)
    index_path = tmp_path / "index.json"
    index_path.write_bytes(b'{"large":true}')

    with pytest.raises(HTTPException) as excinfo:
        workflows._read_json_file(index_path, {})  # noqa: SLF001

    assert excinfo.value.status_code == 500
    assert excinfo.value.detail["error"]["code"] == "invalid_index"


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process flock test requires fork",
)
def test_cross_process_sync_lease_allows_only_one_claim(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_claim_sync_lease_worker,
            args=(str(tmp_path), start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()

    tokens = [results.get(timeout=5) for _ in processes]
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    results.close()

    assert sum(isinstance(token, str) and bool(token) for token in tokens) == 1
    assert tokens.count(None) == 1


@pytest.mark.asyncio
async def test_sync_releases_process_lock_before_external_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = workflows._default_sync_state()  # noqa: SLF001

    async def fake_claim() -> tuple[str, dict[str, Any]]:
        return "lease-token", state

    async def fake_do_sync(
        contents_url: str,
        claimed_state: dict[str, Any],
        *,
        proxy_url: str | None,
        lease_token: str | None,
    ) -> ApparelModelLibrarySyncOut:
        assert contents_url.endswith("apparel-model-presets?ref=main")
        assert claimed_state is state
        assert proxy_url is None
        assert lease_token == "lease-token"
        assert workflows._SYNC_LOCK.locked() is False  # noqa: SLF001
        return ApparelModelLibrarySyncOut(status="ok")

    monkeypatch.setattr(workflows, "_claim_library_sync_lease", fake_claim)
    monkeypatch.setattr(workflows, "_do_sync_library_presets", fake_do_sync)

    out = await workflows._sync_library_presets_from_github_folder(  # noqa: SLF001
        "https://api.github.com/repos/cyeinfpro/Lumen/contents/"
        "assets/apparel-model-presets?ref=main"
    )

    assert out.status == "ok"


@pytest.mark.asyncio
async def test_github_walk_rejects_unbounded_file_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflows, "MODEL_LIBRARY_MAX_GITHUB_FILES", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=[
                {"type": "file", "name": "one.webp", "path": "one.webp"},
                {"type": "file", "name": "two.webp", "path": "two.webp"},
                {"type": "file", "name": "three.webp", "path": "three.webp"},
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            workflows._ModelLibrarySyncLimitExceeded,  # noqa: SLF001
            match="file limit",
        ):
            await workflows._walk_github_contents(  # noqa: SLF001
                client,
                "https://api.github.com/repos/cyeinfpro/Lumen/contents/"
                "assets/apparel-model-presets?ref=main",
            )


@pytest.mark.asyncio
async def test_github_walk_rejects_excessive_directory_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflows, "MODEL_LIBRARY_MAX_GITHUB_DEPTH", 1)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        name = "level-one" if len(calls) == 1 else "level-two"
        return httpx.Response(
            200,
            request=request,
            json=[{"type": "dir", "name": name}],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            workflows._ModelLibrarySyncLimitExceeded,  # noqa: SLF001
            match="depth limit",
        ):
            await workflows._walk_github_contents(  # noqa: SLF001
                client,
                "https://api.github.com/repos/cyeinfpro/Lumen/contents/"
                "assets/apparel-model-presets?ref=main",
            )

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_github_walk_rejects_excessive_directory_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflows, "MODEL_LIBRARY_MAX_GITHUB_DIRECTORIES", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=[
                {"type": "dir", "name": "one"},
                {"type": "dir", "name": "two"},
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            workflows._ModelLibrarySyncLimitExceeded,  # noqa: SLF001
            match="directory limit",
        ):
            await workflows._walk_github_contents(  # noqa: SLF001
                client,
                "https://api.github.com/repos/cyeinfpro/Lumen/contents/"
                "assets/apparel-model-presets?ref=main",
            )


@pytest.mark.asyncio
async def test_fetch_bytes_stops_chunked_response_at_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            stream=_ChunkedStream(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            workflows._ModelLibrarySyncLimitExceeded,  # noqa: SLF001
            match="exceeds 4 bytes",
        ):
            await workflows._fetch_bytes(  # noqa: SLF001
                client,
                "https://raw.githubusercontent.com/cyeinfpro/Lumen/main/model.webp",
                max_bytes=4,
            )


@pytest.mark.asyncio
async def test_sync_end_to_end_publishes_index_and_clears_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(workflows.settings, "storage_root", str(tmp_path))
    image_bytes = b"bounded-image"
    thumb_bytes = b"bounded-thumb"
    raw_requests: list[str] = []
    contents_url = (
        "https://api.github.com/repos/cyeinfpro/Lumen/contents/"
        "assets/apparel-model-presets?ref=main"
    )
    image_path = (
        "assets/apparel-model-presets/05_adult/female/"
        "adult-female-asian-001.webp"
    )
    thumb_path = (
        "assets/apparel-model-presets/05_adult/female/"
        "adult-female-asian-001.thumb.webp"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert workflows._SYNC_LOCK.locked() is False  # noqa: SLF001
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                request=request,
                json=[
                    {
                        "type": "file",
                        "name": Path(image_path).name,
                        "path": image_path,
                        "download_url": (
                            "https://raw.githubusercontent.com/cyeinfpro/Lumen/"
                            f"main/{image_path}"
                        ),
                        "sha": "github-image-sha",
                        "size": len(image_bytes),
                    },
                    {
                        "type": "file",
                        "name": Path(thumb_path).name,
                        "path": thumb_path,
                        "download_url": (
                            "https://raw.githubusercontent.com/cyeinfpro/Lumen/"
                            f"main/{thumb_path}"
                        ),
                        "sha": "github-thumb-sha",
                        "size": len(thumb_bytes),
                    },
                ],
            )
        raw_requests.append(request.url.path)
        body = thumb_bytes if ".thumb." in request.url.path else image_bytes
        return httpx.Response(200, request=request, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(workflows.httpx, "AsyncClient", lambda **_kwargs: client)

    out = await workflows._sync_library_presets_from_github_folder(  # noqa: SLF001
        contents_url
    )

    assert out.status == "ok"
    assert out.added == 1
    assert len(raw_requests) == 2
    index = workflows._load_global_library_index()  # noqa: SLF001
    assert len(index["preset_items"]) == 1
    item = index["preset_items"][0]
    assert item["github_sha"] == "github-image-sha"
    assert item["github_thumb_sha"] == "github-thumb-sha"
    assert workflows._storage_path(item["image_storage_key"]).read_bytes() == image_bytes  # noqa: SLF001
    assert workflows._storage_path(item["thumb_storage_key"]).read_bytes() == thumb_bytes  # noqa: SLF001
    state = workflows._read_json_file(  # noqa: SLF001
        workflows._library_sync_state_path(),  # noqa: SLF001
        workflows._default_sync_state(),  # noqa: SLF001
    )
    assert state["sync_lease"] is None
    assert state["last_success_at"] is not None


@pytest.mark.asyncio
async def test_sync_route_closes_db_transaction_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Db:
        rolled_back = False

        async def rollback(self) -> None:
            self.rolled_back = True

    db = _Db()

    async def fake_proxy(_db: Any) -> tuple[None, None]:
        assert db.rolled_back is False
        return None, None

    async def fake_sync(
        contents_url: str,
        *,
        proxy_url: str | None,
    ) -> ApparelModelLibrarySyncOut:
        assert db.rolled_back is True
        assert contents_url
        assert proxy_url is None
        return ApparelModelLibrarySyncOut(status="skipped")

    monkeypatch.setattr(workflows, "_resolve_model_library_sync_proxy", fake_proxy)
    monkeypatch.setattr(
        workflows,
        "_sync_library_presets_from_github_folder",
        fake_sync,
    )

    out = await workflows.sync_apparel_model_library_presets(
        SimpleNamespace(role="admin"),
        db,  # type: ignore[arg-type]
    )

    assert out.status == "skipped"
    assert db.rolled_back is True
