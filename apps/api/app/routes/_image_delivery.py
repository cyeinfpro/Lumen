"""Secure image file delivery helpers."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
from typing import BinaryIO, Callable, Iterator

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..services import storage_files


_INTERNAL_REDIRECT_PREFIX = "/_internal_storage/"


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def open_regular_file_no_symlink(path: Path) -> tuple[BinaryIO, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _http("not_found", "binary missing", 404) from exc
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            raise _http("not_found", "binary missing", 404) from exc
        if exc.errno == errno.ELOOP:
            raise _http(
                "invalid_path",
                "symlink storage paths are not allowed",
                400,
            ) from exc
        raise
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise _http("not_found", "binary missing", 404)
        return os.fdopen(fd, "rb"), int(file_stat.st_size)
    except Exception:
        os.close(fd)
        raise


def iter_open_file_and_close(file: BinaryIO) -> Iterator[bytes]:
    yield from storage_files.iter_open_file_and_close(file)


def internal_redirect_enabled() -> bool:
    return os.environ.get("LUMEN_INTERNAL_REDIRECT_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def etag_matches_if_none_match(etag: str, header_value: str) -> bool:
    header_value = header_value.strip()
    if header_value == "*":
        return True
    canonical = etag.removeprefix("W/").strip()
    for raw in header_value.split(","):
        candidate = raw.strip().removeprefix("W/").strip()
        if candidate and candidate == canonical:
            return True
    return False


def storage_streaming_response(
    path: Path,
    *,
    media_type: str,
    etag: str,
    cache_control: str,
    storage_key: str | None,
    request: Request | None,
    inline_filename: str | None,
    etag_matches: Callable[[str, str], bool],
    validate_storage_key: Callable[[str], Path],
    open_file: Callable[[Path], tuple[BinaryIO, int]],
    iter_file: Callable[[BinaryIO], Iterator[bytes]],
    redirect_enabled: Callable[[], bool],
) -> Response:
    headers = {
        "Cache-Control": cache_control,
        "ETag": etag,
    }
    if inline_filename:
        headers["Content-Disposition"] = f'inline; filename="{inline_filename}"'

    if request is not None:
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and etag_matches(etag, if_none_match):
            return Response(status_code=304, headers=headers)

    if redirect_enabled() and storage_key:
        try:
            validate_storage_key(storage_key)
        except HTTPException:
            pass
        else:
            return Response(
                status_code=200,
                media_type=media_type,
                headers={
                    **headers,
                    "X-Accel-Redirect": (
                        _INTERNAL_REDIRECT_PREFIX + storage_key.lstrip("/")
                    ),
                },
            )

    file, size = open_file(path)
    return StreamingResponse(
        iter_file(file),
        media_type=media_type,
        headers={**headers, "Content-Length": str(size)},
    )
