"""FastAPI rendering for workflow binary delivery."""

from __future__ import annotations

from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from ...application.delivery import WorkflowBinaryFile


def _stream_file(binary: WorkflowBinaryFile):
    with binary.path.open("rb") as file_obj:
        while chunk := file_obj.read(64 * 1024):
            yield chunk


def binary_file_response(
    binary: WorkflowBinaryFile,
    request: Request,
) -> Response:
    etag = f'"{binary.sha256}"'
    headers = {
        "Cache-Control": "private, max-age=86400",
        "ETag": etag,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return StreamingResponse(
        _stream_file(binary),
        media_type=binary.media_type,
        headers={**headers, "Content-Length": str(binary.size)},
    )


__all__ = ["binary_file_response"]
