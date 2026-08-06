"""Generated-image metadata, persistence, and edit input materialization."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import secrets
import stat
import threading
import warnings
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from PIL import Image, UnidentifiedImageError

from .contracts import ArtifactCorrupt
from .durable_files import atomic_write_bytes, durable_mkdir


# 允许作为交付物写盘 / 挂 public URL 的图片格式。image_metadata() 会把任何
# Pillow 不认识的格式归一成 "bin"，那种东西不能当成生成结果交给用户。
DELIVERABLE_IMAGE_FORMATS: frozenset[str] = frozenset({"png", "jpeg", "webp", "gif"})


@dataclass(frozen=True)
class ImageArtifactFacade:
    data_dir: Callable[[], Path]
    public_base_url: Callable[[], str]
    max_image_bytes: Callable[[], int]
    max_image_candidates: Callable[[], int]
    max_total_image_bytes: Callable[[], int]
    max_image_pixels: Callable[[], int]
    error_class_image_save: Callable[[], str]
    error_class_validation: Callable[[], str]
    job_failure: Callable[..., Exception]
    image_candidate: Callable[[bytes, str | None], Any]
    decode_data_url: Callable[[str], Any | None]
    decode_base64: Callable[[str], bytes | None]
    download_image_url: Callable[..., Awaitable[Any | None]]
    json_dump: Callable[[Any], str]
    job_image_dir_fn: Callable[[str, str], tuple[Path, str]]
    image_metadata_fn: Callable[
        [bytes, str | None],
        tuple[int | None, int | None, str],
    ]
    atomic_write_fn: Callable[[Path, bytes], None]
    save_one_image_sync_fn: Callable[[Path, str, bytes], None]
    save_input_image_fn: Callable[..., Awaitable[str]]
    image_candidate_from_ref_fn: Callable[[dict[str, Any]], Any | None]
    candidate_filename_fn: Callable[[str, Any], tuple[str, str]]
    token_hex: Callable[[int], str] = secrets.token_hex
    image_verify_lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )

    def image_metadata(
        self,
        data: bytes,
        mime_type: str | None,
    ) -> tuple[int | None, int | None, str]:
        width: int | None = None
        height: int | None = None
        fmt = ""
        max_pixels = self.max_image_pixels()
        try:
            with self.image_verify_lock:
                previous_max_pixels = Image.MAX_IMAGE_PIXELS
                Image.MAX_IMAGE_PIXELS = max_pixels
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter(
                            "error",
                            Image.DecompressionBombWarning,
                        )
                        with Image.open(BytesIO(data)) as image:
                            width, height = image.size
                            if width <= 0 or height <= 0 or width * height > max_pixels:
                                raise Image.DecompressionBombError(
                                    f"{width}x{height} exceeds {max_pixels}"
                                )
                            fmt = (image.format or "").lower()
                            image.verify()
                finally:
                    Image.MAX_IMAGE_PIXELS = previous_max_pixels
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise self.job_failure(
                f"图片像素超过限制（max {max_pixels} pixels）",
                upstream_status=413,
                error_class=self.error_class_image_save(),
            ) from exc
        except (
            EOFError,
            OSError,
            SyntaxError,
            UnidentifiedImageError,
            ValueError,
        ) as exc:
            raise self.job_failure(
                "图片无法通过 Pillow 完整性校验",
                upstream_status=400,
                error_class=self.error_class_image_save(),
            ) from exc

        if fmt in {"jpg", "jpeg"}:
            return width, height, "jpeg"
        if fmt in {"png", "webp", "gif"}:
            return width, height, fmt
        return width, height, "bin"

    def job_image_dir(
        self,
        job_id: str,
        created_at: str,
    ) -> tuple[Path, str]:
        created = datetime.fromisoformat(created_at)
        rel = (
            Path("images")
            / "temp"
            / created.strftime("%Y")
            / created.strftime("%m")
            / created.strftime("%d")
            / job_id
        )
        return self.data_dir() / rel, rel.as_posix()

    def atomic_write(self, path: Path, data: bytes) -> None:
        tmp = path.with_suffix(path.suffix + f".tmp-{self.token_hex(4)}")
        atomic_write_bytes(path, data, temporary_path=tmp)

    def save_one_image_sync(
        self,
        image_dir: Path,
        filename: str,
        data: bytes,
    ) -> None:
        durable_mkdir(image_dir)
        self.atomic_write_fn(image_dir / filename, data)

    async def save_images(
        self,
        job_id: str,
        created_at: str,
        retention_days: int,
        candidates: Iterable[Any],
    ) -> list[dict[str, Any]]:
        image_dir, rel_dir = self.job_image_dir_fn(job_id, created_at)
        expires_at = (
            datetime.fromisoformat(created_at) + timedelta(days=retention_days)
        ).isoformat()

        seen: set[str] = set()
        plan: list[
            tuple[
                str,
                Any,
                int,
                int | None,
                int | None,
                str,
            ]
        ] = []
        planned_bytes = 0
        for candidate in candidates:
            if len(candidate.data) > self.max_image_bytes():
                raise self.job_failure(
                    f"上游单图超过大小限制（max {self.max_image_bytes()}）",
                    error_class=self.error_class_image_save(),
                )
            digest = hashlib.sha256(candidate.data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            if len(plan) >= self.max_image_candidates():
                raise self.job_failure(
                    f"上游图片候选数超过限制（max {self.max_image_candidates()}）",
                    error_class=self.error_class_image_save(),
                )
            planned_bytes += len(candidate.data)
            if planned_bytes > self.max_total_image_bytes():
                raise self.job_failure(
                    f"上游图片总字节超过限制（max {self.max_total_image_bytes()}）",
                    error_class=self.error_class_image_save(),
                )
            width, height, fmt = await asyncio.to_thread(
                self.image_metadata_fn,
                candidate.data,
                candidate.mime_type,
            )
            if fmt not in DELIVERABLE_IMAGE_FORMATS:
                # H-13：image_metadata 把一切 Pillow 不认识的格式折叠成 "bin"，
                # 于是过去会落成 `image-1.bin` 并挂上 public URL —— 浏览器无法
                # 渲染，客户端只能下载到一坨二进制。save_input_image /
                # candidate_filename 早就拒绝 "bin" 了，交付路径必须对齐。
                #
                # 偏离审计建议：审计写的是「统一重编码为 PNG」。这里不做重编码，
                # 因为那是有损（丢 EXIF/动图帧/色彩配置）且内存放大的操作
                # （MAX_IMAGE_PIXELS 级别的图解码成 RGB 要几百 MB，还乘上并发），
                # 用白名单直接拒收更便宜也更安全。
                raise self.job_failure(
                    f"上游返回了无法交付的图片格式 {fmt or 'unknown'}"
                    f"（仅 {'/'.join(sorted(DELIVERABLE_IMAGE_FORMATS))}）",
                    error_class=self.error_class_image_save(),
                )
            index = len(plan) + 1
            filename = f"image-{index}.{fmt}"
            plan.append(
                (
                    filename,
                    candidate,
                    len(candidate.data),
                    width,
                    height,
                    fmt,
                )
            )

        await asyncio.gather(
            *(
                asyncio.to_thread(
                    self.save_one_image_sync_fn,
                    image_dir,
                    filename,
                    candidate.data,
                )
                for filename, candidate, _, _, _, _ in plan
            )
        )

        return [
            {
                "url": (f"{self.public_base_url()}/{rel_dir}/{filename}"),
                "width": width,
                "height": height,
                "bytes": size,
                "format": fmt,
                "expires_at": expires_at,
                "sha256": hashlib.sha256(candidate.data).hexdigest(),
            }
            for filename, candidate, size, width, height, fmt in plan
        ]

    def _artifact_path(self, job_id: str, url: str) -> Path:
        base = urlsplit(self.public_base_url())
        parsed = urlsplit(url)
        if (
            parsed.scheme != base.scheme
            or parsed.netloc != base.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ArtifactCorrupt(job_id, "artifact_url_outside_public_base")
        prefix = base.path.rstrip("/") + "/"
        if not parsed.path.startswith(prefix):
            raise ArtifactCorrupt(job_id, "artifact_url_outside_public_base")
        relative = PurePosixPath(unquote(parsed.path[len(prefix) :]))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ArtifactCorrupt(job_id, "artifact_url_path_invalid")
        return self.data_dir().joinpath(*relative.parts)

    def _verify_artifact_sync(
        self,
        job_id: str,
        image: dict[str, Any],
    ) -> dict[str, Any]:
        target = self._artifact_path(job_id, str(image["url"]))
        try:
            root = self.data_dir().resolve(strict=True)
        except OSError as exc:
            raise ArtifactCorrupt(job_id, "artifact_root_missing") from exc
        try:
            parent = target.parent.resolve(strict=True)
        except OSError as exc:
            raise ArtifactCorrupt(job_id, "artifact_file_missing") from exc
        if parent != root and root not in parent.parents:
            raise ArtifactCorrupt(job_id, "artifact_path_escape")
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise ArtifactCorrupt(job_id, "artifact_nofollow_unavailable")
        try:
            fd = os.open(
                parent / target.name,
                os.O_RDONLY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
        except OSError as exc:
            raise ArtifactCorrupt(job_id, "artifact_file_missing") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactCorrupt(job_id, "artifact_not_regular_file")
            if before.st_size != int(image["bytes"]):
                raise ArtifactCorrupt(job_id, "artifact_size_mismatch")
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(fd)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
            ):
                raise ArtifactCorrupt(job_id, "artifact_changed_during_verify")
            actual = digest.hexdigest()
            expected = image.get("sha256")
            if expected is not None and not secrets.compare_digest(
                str(expected),
                actual,
            ):
                raise ArtifactCorrupt(job_id, "artifact_checksum_mismatch")
            return {**image, "sha256": actual}
        except OSError as exc:
            raise ArtifactCorrupt(job_id, "artifact_read_failed") from exc
        finally:
            os.close(fd)

    async def verify_saved_artifacts(
        self,
        job_id: str,
        images: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            lambda: [
                self._verify_artifact_sync(job_id, image) for image in images
            ]
        )

    async def save_input_image(
        self,
        job_id: str,
        created_at: str,
        retention_days: int,
        candidate: Any,
        *,
        stem: str,
    ) -> str:
        _ = retention_days
        image_dir, rel_dir = self.job_image_dir_fn(job_id, created_at)
        width, height, fmt = await asyncio.to_thread(
            self.image_metadata_fn,
            candidate.data,
            candidate.mime_type,
        )
        if width is None or height is None or fmt == "bin":
            raise self.job_failure(
                "图生图输入不是可识别的图片",
                upstream_status=400,
            )
        filename = f"{stem}.{fmt}"
        await asyncio.to_thread(
            self.save_one_image_sync_fn,
            image_dir,
            filename,
            candidate.data,
        )
        return f"{self.public_base_url()}/{rel_dir}/{filename}"

    def image_candidate_from_ref(
        self,
        ref: dict[str, Any],
    ) -> Any | None:
        url = ref.get("image_url")
        if isinstance(url, str) and url.startswith("data:image/"):
            return self.decode_data_url(url)
        for key in (
            "b64_json",
            "image_b64",
            "image_base64",
            "base64_image",
            "data",
        ):
            value = ref.get(key)
            if isinstance(value, str):
                data = self.decode_base64(value)
                if data is not None:
                    return self.image_candidate(
                        data,
                        ref.get("mimeType") or ref.get("mime_type"),
                    )
        return None

    async def materialize_edit_input_urls(
        self,
        row: Any,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        rewritten = copy.deepcopy(body)
        images = rewritten.get("images")
        if isinstance(images, list):
            for index, item in enumerate(images, start=1):
                if not isinstance(item, dict):
                    continue
                url = item.get("image_url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    continue
                candidate = self.image_candidate_from_ref_fn(item)
                if candidate is None:
                    continue
                new_url = await self.save_input_image_fn(
                    row["job_id"],
                    row["created_at"],
                    row["retention_days"],
                    candidate,
                    stem=f"input-{index}",
                )
                item.clear()
                item["image_url"] = new_url

        mask = rewritten.get("mask")
        if isinstance(mask, dict):
            url = mask.get("image_url")
            if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
                candidate = self.image_candidate_from_ref_fn(mask)
                if candidate is not None:
                    new_url = await self.save_input_image_fn(
                        row["job_id"],
                        row["created_at"],
                        row["retention_days"],
                        candidate,
                        stem="mask",
                    )
                    mask.clear()
                    mask["image_url"] = new_url
        return rewritten

    def candidate_filename(
        self,
        stem: str,
        candidate: Any,
    ) -> tuple[str, str]:
        width, height, fmt = self.image_metadata_fn(
            candidate.data,
            candidate.mime_type,
        )
        if width is None or height is None or fmt == "bin":
            raise self.job_failure(
                "图生图输入不是可识别的图片",
                upstream_status=400,
            )
        if fmt in {"jpg", "jpeg"}:
            return f"{stem}.jpg", "image/jpeg"
        if fmt == "webp":
            return f"{stem}.webp", "image/webp"
        if fmt == "png":
            return f"{stem}.png", "image/png"
        raise self.job_failure(
            f"file 模式不支持图片格式 {fmt}（仅 png/jpeg/webp）",
            upstream_status=400,
            error_class=self.error_class_validation(),
        )

    async def materialize_edit_input_files(
        self,
        client: Any,
        body: dict[str, Any],
    ) -> tuple[
        dict[str, str],
        list[tuple[str, tuple[str, bytes, str]]],
    ]:
        data: dict[str, str] = {}
        for key, value in body.items():
            if key in {"images", "mask"}:
                continue
            if value is None:
                continue
            if isinstance(value, bool):
                data[key] = "true" if value else "false"
            elif isinstance(value, (dict, list)):
                data[key] = self.json_dump(value)
            else:
                data[key] = str(value)

        files: list[tuple[str, tuple[str, bytes, str]]] = []
        cache: dict[str, Any] = {}
        images = body.get("images")
        if not isinstance(images, list) or not images:
            raise self.job_failure(
                "图生图 file 模式缺少 images",
                upstream_status=400,
                error_class=self.error_class_validation(),
            )
        for index, item in enumerate(images):
            if not isinstance(item, dict):
                continue
            candidate = self.image_candidate_from_ref_fn(item)
            url = item.get("image_url")
            if candidate is None and isinstance(url, str):
                candidate = await self.download_image_url(
                    client,
                    url,
                    cache=cache,
                    retry_requires_idempotency=False,
                )
            if candidate is None:
                continue
            filename, mime = await asyncio.to_thread(
                self.candidate_filename_fn,
                f"ref-{index}",
                candidate,
            )
            files.append(("image[]", (filename, candidate.data, mime)))
        if not files:
            raise self.job_failure(
                "图生图 file 模式没有可上传的参考图",
                upstream_status=400,
                error_class=self.error_class_validation(),
            )

        mask = body.get("mask")
        if isinstance(mask, dict):
            candidate = self.image_candidate_from_ref_fn(mask)
            url = mask.get("image_url")
            if candidate is None and isinstance(url, str):
                candidate = await self.download_image_url(
                    client,
                    url,
                    cache=cache,
                    retry_requires_idempotency=False,
                )
            if candidate is not None:
                filename, mime = await asyncio.to_thread(
                    self.candidate_filename_fn,
                    "mask",
                    candidate,
                )
                files.append(("mask", (filename, candidate.data, mime)))

        return data, files
