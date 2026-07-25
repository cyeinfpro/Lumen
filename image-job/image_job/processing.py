"""Instance-scoped image decoding, artifact, and upstream processing."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from image_artifacts import ImageArtifactFacade
from image_candidates import ImageCandidate, ImageCandidateFacade
from image_url_security import (
    ImageDownloadResolutionError,
    PublicImageDownloadTarget,
    pinned_async_http_transport,
    resolve_public_image_download_target,
)
from request_bodies import (
    SseLineDecoder,
    parse_content_length,
    parse_json_bytes,
    read_download_body_bounded,
    read_response_body_bounded,
)
from upstream_runtime import UpstreamFacade

from .config import ImageJobSettings
from .contracts import (
    ERROR_CLASS_IMAGE_SAVE,
    ERROR_CLASS_INTERNAL,
    ERROR_CLASS_NETWORK,
    ERROR_CLASS_NO_IMAGE,
    ERROR_CLASS_UPSTREAM_4XX,
    ERROR_CLASS_UPSTREAM_5XX,
    ERROR_CLASS_VALIDATION,
    ImageCandidateBudget,
    JobFailure,
)
from .payloads import (
    body_preview,
    json_dump,
    normalize_image_edit_input_transport,
    upstream_idempotency_key,
)


class ImageProcessing:
    """Composable operations used by the upstream gateway and focused tests."""

    def __init__(
        self,
        settings: ImageJobSettings,
        *,
        http_client: Callable[[], Any | None],
        touch_running: Callable[[str], Awaitable[None]] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.http_client = http_client
        self.touch_running = touch_running or self._noop_touch
        self.log = logger or logging.getLogger("image-job.processing")
        self.candidate_facade = ImageCandidateFacade(
            max_image_bytes=lambda: self.settings.max_image_bytes,
            max_total_image_bytes=lambda: self.settings.max_total_image_bytes,
            max_image_url_redirects=lambda: self.settings.max_image_url_redirects,
            responses_stream_idle_timeout_s=(
                lambda: self.settings.responses_stream_idle_timeout_s
            ),
            responses_stream_max_bytes=(
                lambda: self.settings.responses_stream_max_bytes
            ),
            job_heartbeat_interval_s=(lambda: self.settings.job_heartbeat_interval_s),
            error_class_network=lambda: ERROR_CLASS_NETWORK,
            error_class_upstream_4xx=lambda: ERROR_CLASS_UPSTREAM_4XX,
            error_class_upstream_5xx=lambda: ERROR_CLASS_UPSTREAM_5XX,
            error_class_image_save=lambda: ERROR_CLASS_IMAGE_SAVE,
            error_class_validation=lambda: ERROR_CLASS_VALIDATION,
            job_failure=lambda error, **kwargs: JobFailure(error, **kwargs),
            job_failure_type=JobFailure,
            image_candidate=lambda data, mime_type=None: ImageCandidate(
                data,
                mime_type,
            ),
            budget_factory=lambda: ImageCandidateBudget(
                max_count=self.settings.max_image_candidates,
                max_image_bytes=self.settings.max_image_bytes,
                max_total_bytes=self.settings.max_total_image_bytes,
            ),
            parse_json_bytes=parse_json_bytes,
            body_preview=body_preview,
            download_content_length=parse_content_length,
            read_download_body_bounded=read_download_body_bounded,
            new_pinned_image_download_client=(
                lambda target: self.new_pinned_download_client(target)
            ),
            resolve_public_image_download_target=resolve_public_image_download_target,
            image_download_resolution_error=ImageDownloadResolutionError,
            touch_running=lambda job_id: self.touch_running(job_id),
            download_image_url_fn=(
                lambda client, url, **kwargs: self.download_image_url(
                    client,
                    url,
                    **kwargs,
                )
            ),
            extract_candidates_fn=(
                lambda value, client, **kwargs: self.extract_candidates(
                    value,
                    client,
                    **kwargs,
                )
            ),
            sse_line_decoder_factory=SseLineDecoder,
        )
        self.artifact_facade = ImageArtifactFacade(
            data_dir=lambda: self.settings.data_dir,
            public_base_url=lambda: self.settings.public_base_url,
            max_image_bytes=lambda: self.settings.max_image_bytes,
            max_image_candidates=lambda: self.settings.max_image_candidates,
            max_total_image_bytes=lambda: self.settings.max_total_image_bytes,
            max_image_pixels=lambda: self.settings.max_image_pixels,
            error_class_image_save=lambda: ERROR_CLASS_IMAGE_SAVE,
            error_class_validation=lambda: ERROR_CLASS_VALIDATION,
            job_failure=lambda error, **kwargs: JobFailure(error, **kwargs),
            image_candidate=lambda data, mime_type=None: ImageCandidate(
                data,
                mime_type,
            ),
            decode_data_url=lambda value: self.decode_data_url(value),
            decode_base64=lambda value: self.decode_base64(value),
            download_image_url=(
                lambda client, url, **kwargs: self.download_image_url(
                    client,
                    url,
                    **kwargs,
                )
            ),
            json_dump=json_dump,
            job_image_dir_fn=lambda job_id, created_at: self.job_image_dir(
                job_id,
                created_at,
            ),
            image_metadata_fn=lambda data, mime_type: self.image_metadata(
                data,
                mime_type,
            ),
            atomic_write_fn=lambda path, data: self.atomic_write(path, data),
            save_one_image_sync_fn=(
                lambda image_dir, filename, data: self.save_one_image_sync(
                    image_dir,
                    filename,
                    data,
                )
            ),
            save_input_image_fn=(
                lambda *args, **kwargs: self.save_input_image(*args, **kwargs)
            ),
            image_candidate_from_ref_fn=(
                lambda ref: self.image_candidate_from_ref(ref)
            ),
            candidate_filename_fn=lambda stem, candidate: self.candidate_filename(
                stem,
                candidate,
            ),
            token_hex=secrets.token_hex,
        )
        self.upstream_facade = UpstreamFacade(
            http_client=self.http_client,
            upstream_base_url=lambda: self.settings.upstream_base_url,
            upstream_idempotency_guaranteed=(
                lambda: self.settings.upstream_idempotency_guaranteed
            ),
            retry_network_max=lambda: self.settings.retry_network_max,
            retry_responses_stream_max=(
                lambda: self.settings.retry_responses_stream_max
            ),
            retry_upstream_5xx_max=lambda: self.settings.retry_upstream_5xx_max,
            retry_backoff_s=lambda: self.settings.retry_backoff_s,
            max_upstream_error_body_bytes=(
                lambda: self.settings.max_upstream_error_body_bytes
            ),
            max_upstream_response_bytes=(
                lambda: self.settings.max_upstream_response_bytes
            ),
            max_image_bytes=lambda: self.settings.max_image_bytes,
            error_class_network=lambda: ERROR_CLASS_NETWORK,
            error_class_upstream_4xx=lambda: ERROR_CLASS_UPSTREAM_4XX,
            error_class_upstream_5xx=lambda: ERROR_CLASS_UPSTREAM_5XX,
            error_class_no_image=lambda: ERROR_CLASS_NO_IMAGE,
            error_class_image_save=lambda: ERROR_CLASS_IMAGE_SAVE,
            error_class_internal=lambda: ERROR_CLASS_INTERNAL,
            job_failure=lambda error, **kwargs: JobFailure(error, **kwargs),
            job_failure_type=JobFailure,
            parse_json_bytes=parse_json_bytes,
            body_preview=body_preview,
            read_response_body_bounded=read_response_body_bounded,
            extract_response_images=(
                lambda response, client, **kwargs: self.extract_response_images(
                    response,
                    client,
                    **kwargs,
                )
            ),
            extract_responses_stream_images=(
                lambda response, client, **kwargs: self.extract_responses_stream_images(
                    response,
                    client,
                    **kwargs,
                )
            ),
            materialize_edit_input_files=(
                lambda client, body: self.materialize_edit_input_files(client, body)
            ),
            materialize_edit_input_urls=(
                lambda row, body: self.materialize_edit_input_urls(row, body)
            ),
            save_images=lambda *args, **kwargs: self.save_images(*args, **kwargs),
            normalize_image_edit_input_transport=normalize_image_edit_input_transport,
            upstream_idempotency_key=upstream_idempotency_key,
            call_upstream_once_fn=(
                lambda row, **kwargs: self.call_upstream_once(row, **kwargs)
            ),
            log=self.log,
        )

    async def _noop_touch(self, _job_id: str) -> None:
        return None

    def new_pinned_download_client(
        self,
        target: PublicImageDownloadTarget,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=pinned_async_http_transport(target),
            timeout=httpx.Timeout(
                60.0,
                connect=self.settings.timeouts.connect_s,
            ),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept-Encoding": "identity",
                "User-Agent": "lumen-image",
            },
        )

    def __getattr__(self, name: str) -> Any:
        if hasattr(self.candidate_facade, name):
            return getattr(self.candidate_facade, name)
        if hasattr(self.artifact_facade, name):
            return getattr(self.artifact_facade, name)
        if hasattr(self.upstream_facade, name):
            return getattr(self.upstream_facade, name)
        raise AttributeError(name)

    async def download_image_url(self, *args: Any, **kwargs: Any) -> Any:
        return await self.candidate_facade.download_image_url(*args, **kwargs)

    async def extract_candidates(self, *args: Any, **kwargs: Any) -> list[Any]:
        return await self.candidate_facade.extract_candidates(*args, **kwargs)

    async def extract_response_images(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[Any]:
        return await self.candidate_facade.extract_response_images(*args, **kwargs)

    async def extract_responses_stream_images(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[Any]:
        return await self.candidate_facade.extract_responses_stream_images(
            *args,
            **kwargs,
        )

    def decode_data_url(self, *args: Any, **kwargs: Any) -> Any:
        return self.candidate_facade.decode_data_url(*args, **kwargs)

    def decode_base64(self, *args: Any, **kwargs: Any) -> Any:
        return self.candidate_facade.decode_base64(*args, **kwargs)

    def image_metadata(self, *args: Any, **kwargs: Any) -> Any:
        return self.artifact_facade.image_metadata(*args, **kwargs)

    def job_image_dir(self, *args: Any, **kwargs: Any) -> Any:
        return self.artifact_facade.job_image_dir(*args, **kwargs)

    def atomic_write(self, *args: Any, **kwargs: Any) -> Any:
        return self.artifact_facade.atomic_write(*args, **kwargs)

    def save_one_image_sync(self, *args: Any, **kwargs: Any) -> Any:
        return self.artifact_facade.save_one_image_sync(*args, **kwargs)

    async def save_images(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return await self.artifact_facade.save_images(*args, **kwargs)

    async def save_input_image(self, *args: Any, **kwargs: Any) -> str:
        return await self.artifact_facade.save_input_image(*args, **kwargs)

    def image_candidate_from_ref(self, *args: Any, **kwargs: Any) -> Any:
        return self.artifact_facade.image_candidate_from_ref(*args, **kwargs)

    def candidate_filename(self, *args: Any, **kwargs: Any) -> Any:
        return self.artifact_facade.candidate_filename(*args, **kwargs)

    async def materialize_edit_input_urls(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.artifact_facade.materialize_edit_input_urls(*args, **kwargs)

    async def materialize_edit_input_files(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return await self.artifact_facade.materialize_edit_input_files(*args, **kwargs)

    async def call_upstream_once(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[int, list[dict[str, Any]]]:
        return await self.upstream_facade.call_upstream_once(*args, **kwargs)

    async def call_upstream(self, row: Any) -> tuple[int, list[dict[str, Any]]]:
        return await self.upstream_facade.call_upstream(row)
