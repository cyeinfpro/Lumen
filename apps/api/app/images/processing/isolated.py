from __future__ import annotations

import asyncio
import ctypes
import multiprocessing
import os
import signal
import sys
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Literal

from pillow_heif import register_heif_opener

from ..ports.image_processing import (
    ImageProcessingRequest,
    ImageVariantProcessingRequest,
    PreparedImageVariant,
)
from .model_metadata import (
    MODEL_LIBRARY_METADATA_PROFILE,
    model_metadata_json_from_upload,
)
from .service import (
    ImageInspection,
    ImageProcessor,
    PreparedUpload,
    ProcessingError,
)
from .variants import render_image_variant


_PROCESS_JOIN_TIMEOUT_SECONDS = 2.0
# A hung child would otherwise pin the request and its renewed capacity
# leases forever: bound the result read so the child is killed and the
# upload/variant capacity slot is released.
_PROCESS_RESULT_TIMEOUT_SECONDS = 60.0

register_heif_opener()


@dataclass(frozen=True)
class _InspectRequest:
    operation: Literal["inspect"]
    source_path: str
    upload_bytes: int
    allowed_mime: tuple[str, ...]
    normalizable_mime: tuple[str, ...]
    max_pixels: int
    max_long_side: int


@dataclass(frozen=True)
class _ProcessRequest:
    operation: Literal["process"]
    request: ImageProcessingRequest


@dataclass(frozen=True)
class _RenderVariantRequest:
    operation: Literal["render_variant"]
    request: ImageVariantProcessingRequest


@dataclass(frozen=True)
class _Success:
    value: Any


@dataclass(frozen=True)
class _Failure:
    kind: str
    message: str
    code: str | None = None
    status_code: int | None = None


class _FixedOutputStage:
    def __init__(
        self,
        *,
        source_path: str,
        source_size_bytes: int,
        source_sha256: str,
        output_paths: tuple[str, ...],
    ) -> None:
        self.path = Path(source_path)
        self.size_bytes = source_size_bytes
        self.sha256 = source_sha256
        self.lease = None
        self._output_paths = iter(Path(value) for value in output_paths)

    def new_temp_path(self, *, suffix: str) -> Path:
        try:
            path = next(self._output_paths)
        except StopIteration as exc:
            raise RuntimeError(
                f"isolated image process did not receive an output path for {suffix}"
            ) from exc
        return path


def _metadata_reader(profile: str | None) -> Any:
    if profile is None:
        return None
    if profile == MODEL_LIBRARY_METADATA_PROFILE:
        return model_metadata_json_from_upload
    raise ProcessingError(
        "image_processing_unavailable",
        f"unsupported image metadata profile: {profile}",
        503,
    )


def _install_parent_death_signal(expected_parent_pid: int) -> None:
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def _execute_request(
    request: _InspectRequest | _ProcessRequest | _RenderVariantRequest,
) -> Any:
    processor = ImageProcessor()
    if request.operation == "inspect":
        return processor.inspect(
            Path(request.source_path),
            upload_bytes=request.upload_bytes,
            allowed_mime=set(request.allowed_mime),
            normalizable_mime=set(request.normalizable_mime),
            max_pixels=request.max_pixels,
            max_long_side=request.max_long_side,
        )
    if request.operation == "render_variant":
        return render_image_variant(request.request)
    process_request = request.request
    stage = _FixedOutputStage(
        source_path=str(process_request.source_path),
        source_size_bytes=process_request.source_size_bytes,
        source_sha256=process_request.source_sha256,
        output_paths=tuple(str(path) for path in process_request.output_paths),
    )
    return processor.process(
        stage,
        process_request.filename,
        allowed_mime=set(process_request.allowed_mime),
        normalizable_mime=set(process_request.normalizable_mime),
        max_bytes=process_request.max_bytes,
        max_pixels=process_request.max_pixels,
        max_long_side=process_request.max_long_side,
        mask_requested=process_request.mask_requested,
        reference_size=process_request.reference_size,
        metadata_reader=_metadata_reader(process_request.metadata_profile),
    )


def _child_main(
    connection: Connection,
    request: _InspectRequest | _ProcessRequest | _RenderVariantRequest,
    parent_pid: int,
) -> None:
    try:
        _install_parent_death_signal(parent_pid)
        connection.send(_Success(_execute_request(request)))
    except ProcessingError as exc:
        connection.send(
            _Failure(
                kind="processing",
                code=exc.code,
                message=exc.message,
                status_code=exc.status_code,
            )
        )
    except BaseException as exc:
        connection.send(
            _Failure(
                kind=exc.__class__.__name__,
                message=str(exc)[:2000],
            )
        )
    finally:
        connection.close()


def _stop_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
        process.join(_PROCESS_JOIN_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
    process.join()


class IsolatedImageProcessingExecutor:
    def __init__(self, result_timeout_seconds: float | None = None) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._active: set[multiprocessing.Process] = set()
        self._closed = False
        # 默认读模块常量而非函数默认值,保证测试 monkeypatch
        # _PROCESS_RESULT_TIMEOUT_SECONDS 仍然生效;生产由 composition 注入
        # settings.image_processing_result_timeout_s(环境变量可配)。
        self._result_timeout_seconds = (
            result_timeout_seconds
            if result_timeout_seconds is not None
            else _PROCESS_RESULT_TIMEOUT_SECONDS
        )

    async def _run(
        self,
        request: _InspectRequest | _ProcessRequest | _RenderVariantRequest,
    ) -> Any:
        if self._closed:
            raise ProcessingError(
                "image_processing_unavailable",
                "image processing executor is closed",
                503,
            )
        receive, send = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_child_main,
            args=(send, request, os.getpid()),
            daemon=True,
            name=f"lumen-image-{request.operation}",
        )
        try:
            process.start()
        except Exception as exc:
            receive.close()
            send.close()
            raise ProcessingError(
                "image_processing_unavailable",
                "failed to start isolated image process",
                503,
            ) from exc
        send.close()
        self._active.add(process)
        receive_task = asyncio.create_task(
            asyncio.to_thread(receive.recv),
            name=f"lumen-image-{request.operation}-result",
        )
        try:
            result = await asyncio.wait_for(
                receive_task,
                timeout=self._result_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ProcessingError(
                "image_processing_timeout",
                "isolated image process timed out",
                503,
            ) from exc
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(_stop_process, process))
            await asyncio.gather(receive_task, return_exceptions=True)
            raise
        except (EOFError, OSError) as exc:
            raise ProcessingError(
                "image_processing_failed",
                "isolated image process exited without a result",
                503,
            ) from exc
        finally:
            receive.close()
            if process.is_alive():
                await asyncio.to_thread(process.join, _PROCESS_JOIN_TIMEOUT_SECONDS)
            if process.is_alive():
                await asyncio.to_thread(_stop_process, process)
            else:
                process.join()
            self._active.discard(process)
        if isinstance(result, _Failure):
            if result.kind == "processing":
                raise ProcessingError(
                    result.code or "image_processing_failed",
                    result.message,
                    result.status_code or 503,
                )
            raise ProcessingError(
                "image_processing_failed",
                f"isolated image process failed: {result.kind}: {result.message}",
                503,
            )
        if not isinstance(result, _Success):
            raise ProcessingError(
                "image_processing_failed",
                "isolated image process returned an invalid result",
                503,
            )
        return result.value

    async def inspect(
        self,
        source_path: Path,
        *,
        upload_bytes: int,
        allowed_mime: set[str],
        normalizable_mime: set[str],
        max_pixels: int,
        max_long_side: int,
    ) -> ImageInspection:
        result = await self._run(
            _InspectRequest(
                operation="inspect",
                source_path=str(source_path),
                upload_bytes=upload_bytes,
                allowed_mime=tuple(sorted(allowed_mime)),
                normalizable_mime=tuple(sorted(normalizable_mime)),
                max_pixels=max_pixels,
                max_long_side=max_long_side,
            )
        )
        if not isinstance(result, ImageInspection):
            raise ProcessingError(
                "image_processing_failed",
                "isolated image inspection returned an invalid result",
                503,
            )
        return result

    async def process(
        self,
        request: ImageProcessingRequest,
    ) -> PreparedUpload:
        result = await self._run(
            _ProcessRequest(
                operation="process",
                request=request,
            )
        )
        if not isinstance(result, PreparedUpload):
            raise ProcessingError(
                "image_processing_failed",
                "isolated image processing returned an invalid result",
                503,
            )
        return result

    async def render_variant(
        self,
        request: ImageVariantProcessingRequest,
    ) -> PreparedImageVariant:
        result = await self._run(
            _RenderVariantRequest(
                operation="render_variant",
                request=request,
            )
        )
        if not isinstance(result, PreparedImageVariant):
            raise ProcessingError(
                "image_processing_failed",
                "isolated image variant rendering returned an invalid result",
                503,
            )
        return result

    async def aclose(self) -> None:
        self._closed = True
        processes = list(self._active)
        if processes:
            await asyncio.gather(
                *(asyncio.to_thread(_stop_process, process) for process in processes),
                return_exceptions=True,
            )
        self._active.clear()
