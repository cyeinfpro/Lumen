from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
from fastapi import Request

from app.routes import me, me_export


def _image(
    image_id: str = "image-1",
    storage_key: str | None = "u/user-1/image.png",
) -> me_export.ExportImageDescriptor:
    return me_export.ExportImageDescriptor(
        id=image_id,
        storage_key=storage_key,
        mime="image/png",
    )


def test_export_required_open_rejects_invalid_and_missing_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(me_export.settings, "storage_root", str(tmp_path))

    with pytest.raises(me_export.ExportIntegrityError) as invalid:
        me_export.open_export_image_required(_image(storage_key="../escape.png"))
    assert invalid.value.reason == "invalid_storage_key"

    with pytest.raises(me_export.ExportIntegrityError) as missing:
        me_export.open_export_image_required(_image(storage_key="missing.png"))
    assert missing.value.reason == "missing_file"


def test_export_required_open_rejects_non_regular_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    monkeypatch.setattr(me_export.settings, "storage_root", str(tmp_path))

    with pytest.raises(me_export.ExportIntegrityError) as exc_info:
        me_export.open_export_image_required(_image(storage_key="pipe"))

    assert exc_info.value.reason == "not_regular_file"


@pytest.mark.asyncio
async def test_complete_export_manifest_matches_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def message_batches(
        _db: object,
        _user_id: str,
    ) -> AsyncIterator[tuple[me_export.ExportMessageDescriptor, ...]]:
        yield (
            me_export.ExportMessageDescriptor(
                conversation_id="conv-1",
                id="message-1",
                role="assistant",
                content={"text": "ok"},
                intent="chat",
                status="succeeded",
                created_at=None,
            ),
        )

    async def image_batches(
        _db: object,
        _user_id: str,
    ) -> AsyncIterator[tuple[me_export.ExportImageDescriptor, ...]]:
        yield (_image("image-1"), _image("image-2"))

    async def empty_agent_batches(
        _db: object,
        _user_id: str,
    ) -> AsyncIterator[tuple[object, ...]]:
        if False:
            yield ()

    def open_required(image: me_export.ExportImageDescriptor) -> io.BytesIO:
        return io.BytesIO(f"payload:{image.id}".encode())

    monkeypatch.setattr(me_export, "iter_export_message_batches", message_batches)
    monkeypatch.setattr(me_export, "iter_export_image_batches", image_batches)
    monkeypatch.setattr(
        me_export,
        "iter_export_agent_session_batches",
        empty_agent_batches,
    )
    monkeypatch.setattr(
        me_export,
        "iter_export_agent_run_batches",
        empty_agent_batches,
    )
    monkeypatch.setattr(
        me_export,
        "iter_export_agent_tool_call_batches",
        empty_agent_batches,
    )
    monkeypatch.setattr(me_export, "open_export_image_required", open_required)
    output = io.BytesIO()

    stats = await me_export.build_export_archive(
        SimpleNamespace(),  # type: ignore[arg-type]
        output,
        "user-1",
    )

    output.seek(0)
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("export-manifest.json"))
        image_names = [
            name for name in archive.namelist() if name.startswith("images/")
        ]
        assert manifest == {
            "schema": 2,
            "complete": True,
            "messages": 1,
            "images": 2,
            "agent_sessions": 0,
            "agent_runs": 0,
            "agent_tool_calls": 0,
        }
        assert archive.read("agent-sessions.ndjson") == b""
        assert archive.read("agent-runs.ndjson") == b""
        assert archive.read("agent-tool-calls.ndjson") == b""
        assert len(image_names) == manifest["images"]
        assert archive.read("images/image-1.png") == b"payload:image-1"
        assert archive.read("images/image-2.png") == b"payload:image-2"
    assert stats.messages == 1
    assert stats.images == 2
    assert stats.images_skipped == 0
    assert stats.zip_bytes == len(output.getvalue())


@pytest.mark.asyncio
async def test_export_read_failure_is_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenReader(io.BytesIO):
        def read(self, *_args: object, **_kwargs: object) -> bytes:
            raise OSError("read failed")

    monkeypatch.setattr(
        me_export,
        "open_export_image_required",
        lambda _image: BrokenReader(),
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        with pytest.raises(me_export.ExportIntegrityError) as exc_info:
            await me_export._write_export_image(archive, _image())  # noqa: SLF001

    assert exc_info.value.image_id == "image-1"
    assert exc_info.value.reason == "read_failed"


@pytest.mark.asyncio
async def test_export_route_returns_structured_failure_and_closes_tempfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ActiveUserResult:
        def scalar_one_or_none(self) -> str:
            return "user-1"

    class Db:
        async def execute(self, _statement: object) -> ActiveUserResult:
            return ActiveUserResult()

        async def rollback(self) -> None:
            return None

    class TrackedBuffer(io.BytesIO):
        pass

    tmp = TrackedBuffer()
    audit_calls: list[dict[str, object]] = []

    async def no_limit(*_args: object, **_kwargs: object) -> None:
        return None

    async def fail_build(*_args: object, **_kwargs: object) -> object:
        raise me_export.ExportIntegrityError("image-missing", "missing_file")

    async def record_audit(**kwargs: object) -> bool:
        audit_calls.append(dict(kwargs))
        return True

    monkeypatch.setattr(me._EXPORT_LIMITER, "check", no_limit)
    monkeypatch.setattr(me, "get_redis", lambda: object())
    monkeypatch.setattr(me, "_build_export_archive", fail_build)
    monkeypatch.setattr(me, "write_audit_isolated", record_audit)
    monkeypatch.setattr(me.tempfile, "TemporaryFile", lambda: tmp)

    with pytest.raises(Exception) as exc_info:
        await me.export_my_data(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/me/export",
                    "headers": [],
                    "client": ("127.0.0.1", 12345),
                }
            ),
            SimpleNamespace(id="user-1", email="user@example.test"),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 500
    assert exc_info.value.detail["error"] == {
        "code": "export_incomplete",
        "message": "data export could not be completed",
        "details": {
            "image_id": "image-missing",
            "reason": "missing_file",
        },
    }
    assert tmp.closed is True
    assert len(audit_calls) == 1
    audit_call = audit_calls[0]
    assert isinstance(audit_call.pop("actor_ip_hash"), str)
    assert audit_call == {
        "event_type": "me.data.export.fail",
        "user_id": "user-1",
        "actor_email": "user@example.test",
        "target_user_id": "user-1",
        "details": {
            "image_id": "image-missing",
            "reason": "missing_file",
        },
    }
