"""Artifact schema validation and durable corruption transitions."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..contracts import ArtifactCorrupt
from .common import DbExec, terminal_retention_expiry


ARTIFACT_SCHEMA_CURRENT = 2
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def validated_images(
    *,
    job_id: str,
    raw_json: str | None,
    expected_count: int,
    require_checksum: bool,
    strict_json_loads: Callable[[str], Any],
) -> list[dict[str, Any]]:
    if not raw_json:
        raise ArtifactCorrupt(job_id, "images_json_missing")
    try:
        value = strict_json_loads(raw_json)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise ArtifactCorrupt(job_id, "images_json_invalid") from exc
    if not isinstance(value, list):
        raise ArtifactCorrupt(job_id, "images_json_not_list")
    if len(value) != expected_count:
        raise ArtifactCorrupt(job_id, "image_count_mismatch")
    if not value:
        raise ArtifactCorrupt(job_id, "succeeded_without_images")
    result: list[dict[str, Any]] = []
    for index, raw_image in enumerate(value):
        if not isinstance(raw_image, dict):
            raise ArtifactCorrupt(job_id, f"image_{index}_not_object")
        image = dict(raw_image)
        url = image.get("url")
        if not isinstance(url, str) or not url:
            raise ArtifactCorrupt(job_id, f"image_{index}_url_missing")
        for field in ("bytes", "width", "height"):
            field_value = image.get(field)
            if (
                not isinstance(field_value, int)
                or isinstance(field_value, bool)
                or field_value <= 0
            ):
                raise ArtifactCorrupt(job_id, f"image_{index}_{field}_invalid")
        for field in ("format", "expires_at"):
            field_value = image.get(field)
            if not isinstance(field_value, str) or not field_value:
                raise ArtifactCorrupt(job_id, f"image_{index}_{field}_invalid")
        digest = image.get("sha256")
        if require_checksum and (
            not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
        ):
            raise ArtifactCorrupt(job_id, f"image_{index}_checksum_invalid")
        if digest is not None and (
            not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
        ):
            raise ArtifactCorrupt(job_id, f"image_{index}_checksum_invalid")
        result.append(image)
    return result


@dataclass(frozen=True)
class ArtifactIntegrityFacade:
    db_exec: DbExec
    now_iso: Callable[[], str]
    job_ttl_days: Callable[[], int]
    json_dump: Callable[[Any], str]
    strict_json_loads: Callable[[str], Any]
    verify_artifacts: Callable[
        [str, list[dict[str, Any]]],
        Awaitable[list[dict[str, Any]]],
    ]
    row_get: Callable[[sqlite3.Row, str], Any]
    response_builder: Callable[..., dict[str, Any]]

    async def mark_corrupt(
        self,
        job_id: str,
        *,
        reason: str,
        execution_token: str | None = None,
        elapsed_ms: int | None = None,
        endpoint_used: str | None = None,
    ) -> bool:
        now = self.now_iso()
        retention_expires_at = terminal_retention_expiry(
            now,
            job_ttl_days=self.job_ttl_days(),
        )
        state_predicate = (
            "status = 'running' AND execution_token = ?"
            if execution_token is not None
            else "status = 'succeeded'"
        )
        params: tuple[Any, ...] = (
            now,
            now,
            elapsed_ms,
            reason,
            retention_expires_at,
            retention_expires_at,
            endpoint_used,
            job_id,
        )
        if execution_token is not None:
            params = (*params, execution_token)
        changed = await self.db_exec(
            f"""
            UPDATE jobs
            SET status = 'artifact_corrupt',
                auth_header = NULL,
                auth_ciphertext = NULL,
                auth_nonce = NULL,
                auth_key_id = NULL,
                execution_token = NULL,
                finished_at = COALESCE(finished_at, ?),
                updated_at = ?,
                elapsed_ms = COALESCE(?, elapsed_ms),
                error = ?,
                error_class = 'artifact_corrupt',
                retryable = 0,
                retry_suppressed = 1,
                outcome_uncertain = 1,
                retention_expires_at = CASE
                    WHEN retention_expires_at IS NULL
                         OR retention_expires_at > ?
                    THEN ?
                    ELSE retention_expires_at
                END,
                endpoint_used = COALESCE(?, endpoint_used)
            WHERE job_id = ? AND {state_predicate}
            """,  # nosec B608 - predicate is selected from fixed literals.
            params,
        )
        return changed == 1

    async def validated_response(self, row: sqlite3.Row) -> dict[str, Any]:
        if row["status"] != "succeeded":
            return self.response_builder(row)
        job_id = str(row["job_id"])
        try:
            schema = int(self.row_get(row, "artifact_schema") or 1)
        except (TypeError, ValueError) as exc:
            raise ArtifactCorrupt(job_id, "artifact_schema_invalid") from exc
        images = validated_images(
            job_id=job_id,
            raw_json=row["images_json"],
            expected_count=int(row["image_count"]),
            require_checksum=schema >= ARTIFACT_SCHEMA_CURRENT,
            strict_json_loads=self.strict_json_loads,
        )
        verified = await self.verify_artifacts(job_id, images)
        verified = validated_images(
            job_id=job_id,
            raw_json=self.json_dump(verified),
            expected_count=len(images),
            require_checksum=True,
            strict_json_loads=self.strict_json_loads,
        )
        if schema < ARTIFACT_SCHEMA_CURRENT:
            changed = await self.db_exec(
                """
                UPDATE jobs
                SET images_json = ?, artifact_schema = ?, updated_at = ?
                WHERE job_id = ? AND status = 'succeeded'
                """,
                (
                    self.json_dump(verified),
                    ARTIFACT_SCHEMA_CURRENT,
                    self.now_iso(),
                    job_id,
                ),
            )
            if changed != 1:
                raise ArtifactCorrupt(job_id, "artifact_schema_upgrade_fence_lost")
        return self.response_builder(row, succeeded_images=verified)


def artifact_integrity_facade(
    owner: Any,
    strict_json_loads: Callable[[str], Any],
) -> ArtifactIntegrityFacade:
    return ArtifactIntegrityFacade(
        owner.db_exec,
        owner.now_iso,
        owner.job_ttl_days,
        owner.json_dump,
        strict_json_loads,
        owner.verify_artifacts,
        owner.row_get,
        owner.row_to_response,
    )
