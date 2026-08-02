from __future__ import annotations

from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import VideoGenerationStatus
from lumen_core.model_entities import User
from lumen_core.model_entities.media_workflows import (
    Image,
    ImageVariant,
    Video,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.model_entities.tasks import VideoGeneration


_VIDEO_VARIANT_METADATA_KEYS = (
    "upstream_reference_video_variant",
    "volcano_asset_video_variant",
)
_VIDEO_TERMINAL_STATUSES = (
    VideoGenerationStatus.SUCCEEDED.value,
    VideoGenerationStatus.FAILED.value,
    VideoGenerationStatus.CANCELED.value,
    VideoGenerationStatus.EXPIRED.value,
)
_STORYBOARD_ASSEMBLY_FILENAMES = frozenset(
    {"output.mp4", "poster.jpg", "commit-recovery.json"}
)


def _is_safe_storage_segment(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "\x00" not in value
        and "\\" not in value
    )


def _metadata_storage_keys(metadata: Any) -> Iterator[str]:
    if not isinstance(metadata, dict):
        return
    normalized_ref = metadata.get("normalized_ref")
    if not isinstance(normalized_ref, dict):
        return
    storage_key = normalized_ref.get("storage_key")
    if isinstance(storage_key, str) and storage_key:
        yield storage_key


def _manifest_storage_keys(manifest: Any) -> Iterator[str]:
    if not isinstance(manifest, dict):
        return
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return
    for artifact in artifacts.values():
        if not isinstance(artifact, dict):
            continue
        storage_key = artifact.get("storage_key")
        if isinstance(storage_key, str) and storage_key:
            yield storage_key


def _video_metadata_storage_keys(metadata: Any) -> Iterator[str]:
    if not isinstance(metadata, dict):
        return
    for key in _VIDEO_VARIANT_METADATA_KEYS:
        value = metadata.get(key)
        if not isinstance(value, dict):
            continue
        storage_key = value.get("storage_key")
        if isinstance(storage_key, str) and storage_key:
            yield storage_key


def _reference_media_items(upstream_request: Any) -> Iterator[dict[str, Any]]:
    if not isinstance(upstream_request, dict):
        return
    raw_items = upstream_request.get("reference_media")
    if not isinstance(raw_items, list):
        return
    for item in raw_items:
        if isinstance(item, dict):
            yield item


def _active_generation_storage_keys(
    input_image_storage_key: Any,
    upstream_request: Any,
) -> Iterator[str]:
    if isinstance(input_image_storage_key, str) and input_image_storage_key:
        yield input_image_storage_key
    for item in _reference_media_items(upstream_request):
        for field in ("storage_key", "upstream_reference_storage_key"):
            storage_key = item.get(field)
            if isinstance(storage_key, str) and storage_key:
                yield storage_key


def _video_storage_keys(video: Any) -> set[str]:
    keys = {
        key
        for key in (
            getattr(video, "storage_key", None),
            getattr(video, "poster_storage_key", None),
        )
        if isinstance(key, str) and key
    }
    keys.update(_video_metadata_storage_keys(getattr(video, "metadata_jsonb", None)))
    return keys


def _active_generation_output_candidates(
    candidates: set[str],
) -> dict[tuple[str, str], set[str]]:
    outputs: dict[tuple[str, str], set[str]] = {}
    for key in candidates:
        parts = PurePosixPath(key).parts
        if len(parts) < 5 or parts[0] != "u" or parts[2] != "v":
            continue
        outputs.setdefault((parts[1], parts[3]), set()).add(key)
    return outputs


def _storyboard_unknown_storage_keys(
    *,
    run_id: str,
    user_id: str,
    output_json: Any,
) -> Iterator[str]:
    if not isinstance(output_json, dict):
        return
    if output_json.get("assembly_commit_state") != "unknown":
        return
    candidate = output_json.get("assembly_commit_candidate")
    if not isinstance(candidate, dict) or candidate.get("user_id") != user_id:
        return
    metadata = candidate.get("metadata_jsonb")
    attempt_token = output_json.get("assembly_attempt_token")
    fingerprint = output_json.get("assembly_fingerprint")
    if (
        not isinstance(attempt_token, str)
        or not attempt_token
        or not isinstance(fingerprint, str)
        or not fingerprint
        or not isinstance(metadata, dict)
        or metadata.get("workflow_type") != "storyboard"
        or metadata.get("workflow_run_id") != run_id
        or metadata.get("assembly_attempt_token") != attempt_token
        or metadata.get("assembly_fingerprint") != fingerprint
    ):
        return
    expected_prefix = ("u", user_id, "storyboards", run_id, "assembly")
    for field, filename in (
        ("storage_key", "output.mp4"),
        ("poster_storage_key", "poster.jpg"),
    ):
        storage_key = candidate.get(field)
        if not isinstance(storage_key, str) or not storage_key:
            continue
        parts = tuple(storage_key.split("/"))
        if (
            len(parts) == 7
            and parts[:5] == expected_prefix
            and _is_safe_storage_segment(parts[5])
            and parts[6] == filename
        ):
            yield storage_key


def _active_storyboard_assembly_storage_keys(
    candidates: set[str],
    *,
    run_id: str,
    user_id: str,
) -> Iterator[str]:
    expected_prefix = ("u", user_id, "storyboards", run_id, "assembly")
    for storage_key in candidates:
        parts = tuple(storage_key.split("/"))
        if (
            len(parts) == 7
            and parts[:5] == expected_prefix
            and _is_safe_storage_segment(parts[5])
            and parts[6] in _STORYBOARD_ASSEMBLY_FILENAMES
        ):
            yield storage_key


def _json_storage_key(column: Any, *path: str) -> Any:
    expression = column
    for part in path:
        expression = expression[part]
    return expression.as_string()


def _candidate_user_ids(candidates: set[str]) -> set[str]:
    user_ids: set[str] = set()
    for key in candidates:
        parts = PurePosixPath(key).parts
        if len(parts) >= 2 and parts[0] == "u" and parts[1]:
            user_ids.add(parts[1])
    return user_ids


async def known_live_image_storage_keys(
    db: AsyncSession,
    candidates: set[str],
) -> set[str]:
    """Return candidate keys that still have at least one live image reference.

    Soft-deleted image rows deliberately do not protect their storage objects.
    The durable ``deleted_at`` commit therefore makes those objects eligible for
    the normal orphan sweep, while any cross-row live reference still wins.
    """

    if not candidates:
        return set()

    ordered_candidates = sorted(candidates)
    candidate_user_ids = sorted(_candidate_user_ids(candidates))
    image_reference_conditions = [Image.storage_key.in_(ordered_candidates)]
    if candidate_user_ids:
        # Image references are valid only inside their owner's ``u/{user_id}``
        # namespace. Scope JSON checks by owner so per-file safety rechecks use
        # the existing user/deleted index instead of scanning every image row.
        image_reference_conditions.append(
            and_(
                Image.user_id.in_(candidate_user_ids),
                or_(
                    _json_storage_key(
                        Image.metadata_jsonb,
                        "normalized_ref",
                        "storage_key",
                    ).in_(ordered_candidates),
                    _json_storage_key(
                        Image.artifact_manifest_jsonb,
                        "artifacts",
                        "original",
                        "storage_key",
                    ).in_(ordered_candidates),
                    _json_storage_key(
                        Image.artifact_manifest_jsonb,
                        "artifacts",
                        "normalized_ref",
                        "storage_key",
                    ).in_(ordered_candidates),
                ),
            )
        )
    live_image_rows = (
        await db.execute(
            select(
                Image.storage_key,
                Image.metadata_jsonb,
                Image.artifact_manifest_jsonb,
            ).where(
                Image.deleted_at.is_(None),
                or_(*image_reference_conditions),
            )
        )
    ).all()

    known: set[str] = set()
    for row in live_image_rows:
        storage_key = row[0] if len(row) >= 1 else None
        metadata = row[1] if len(row) >= 2 else None
        manifest = row[2] if len(row) >= 3 else None
        if isinstance(storage_key, str) and storage_key in candidates:
            known.add(storage_key)
        known.update(
            key for key in _metadata_storage_keys(metadata) if key in candidates
        )
        known.update(
            key for key in _manifest_storage_keys(manifest) if key in candidates
        )

    live_variant_keys = (
        (
            await db.execute(
                select(ImageVariant.storage_key)
                .join(Image, Image.id == ImageVariant.image_id)
                .where(
                    ImageVariant.storage_key.in_(ordered_candidates),
                    Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    known.update(
        key for key in live_variant_keys if isinstance(key, str) and key in candidates
    )
    return known


async def known_live_video_storage_keys(
    db: AsyncSession,
    candidates: set[str],
) -> set[str]:
    if not candidates:
        return set()

    ordered_candidates = sorted(candidates)
    candidate_user_ids = sorted(_candidate_user_ids(candidates))
    video_conditions = [
        Video.storage_key.in_(ordered_candidates),
        Video.poster_storage_key.in_(ordered_candidates),
    ]
    if candidate_user_ids:
        video_conditions.append(
            and_(
                Video.user_id.in_(candidate_user_ids),
                or_(
                    *(
                        _json_storage_key(
                            Video.metadata_jsonb,
                            metadata_key,
                            "storage_key",
                        ).in_(ordered_candidates)
                        for metadata_key in _VIDEO_VARIANT_METADATA_KEYS
                    )
                ),
            )
        )
    rows = (
        await db.execute(
            select(
                Video.storage_key,
                Video.poster_storage_key,
                Video.metadata_jsonb,
            ).where(
                Video.deleted_at.is_(None),
                or_(*video_conditions),
            )
        )
    ).all()

    known: set[str] = set()
    for storage_key, poster_storage_key, metadata in rows:
        if isinstance(storage_key, str) and storage_key in candidates:
            known.add(storage_key)
        if isinstance(poster_storage_key, str) and poster_storage_key in candidates:
            known.add(poster_storage_key)
        known.update(
            key for key in _video_metadata_storage_keys(metadata) if key in candidates
        )
    return known


async def known_active_video_generation_storage_keys(
    db: AsyncSession,
    candidates: set[str],
) -> set[str]:
    if not candidates:
        return set()
    candidate_user_ids = sorted(_candidate_user_ids(candidates))
    if not candidate_user_ids:
        return set()
    output_candidates = _active_generation_output_candidates(candidates)
    # Account deletion persists cancellation intent before the video worker
    # settles active jobs. Those fenced jobs must not retain deleted-account
    # storage, while active jobs for a live account still protect their inputs,
    # references, and deterministic finalization paths.
    rows = (
        await db.execute(
            select(
                VideoGeneration.id,
                VideoGeneration.user_id,
                VideoGeneration.input_image_storage_key,
                VideoGeneration.upstream_request,
            )
            .join(User, User.id == VideoGeneration.user_id)
            .where(
                VideoGeneration.user_id.in_(candidate_user_ids),
                User.deleted_at.is_(None),
                ~VideoGeneration.status.in_(_VIDEO_TERMINAL_STATUSES),
            )
        )
    ).all()
    known: set[str] = set()
    for generation_id, user_id, input_image_storage_key, upstream_request in rows:
        known.update(
            key
            for key in _active_generation_storage_keys(
                input_image_storage_key,
                upstream_request,
            )
            if key in candidates
        )
        known.update(output_candidates.get((str(user_id), str(generation_id)), set()))
    return known


async def known_storyboard_commit_storage_keys(
    db: AsyncSession,
    candidates: set[str],
) -> set[str]:
    if not candidates:
        return set()
    candidate_user_ids = sorted(_candidate_user_ids(candidates))
    if not candidate_user_ids:
        return set()
    rows = (
        await db.execute(
            select(
                WorkflowRun.id,
                WorkflowRun.user_id,
                WorkflowStep.output_json,
            )
            .join(
                WorkflowStep,
                WorkflowStep.workflow_run_id == WorkflowRun.id,
            )
            .where(
                WorkflowRun.user_id.in_(candidate_user_ids),
                WorkflowRun.type == "storyboard",
                WorkflowRun.deleted_at.is_(None),
                WorkflowStep.step_key == "assembly",
                WorkflowStep.status == "compositing",
            )
        )
    ).all()
    known: set[str] = set()
    for run_id, user_id, output_json in rows:
        known.update(
            _active_storyboard_assembly_storage_keys(
                candidates,
                run_id=str(run_id),
                user_id=str(user_id),
            )
        )
        known.update(
            key
            for key in _storyboard_unknown_storage_keys(
                run_id=str(run_id),
                user_id=str(user_id),
                output_json=output_json,
            )
            if key in candidates
        )
    return known


async def active_video_generation_reference_id(
    db: AsyncSession,
    *,
    video: Any,
) -> str | None:
    user_id = getattr(video, "user_id", None)
    video_id = getattr(video, "id", None)
    if not isinstance(user_id, str) or not user_id:
        return None
    storage_keys = _video_storage_keys(video)
    rows = (
        await db.execute(
            select(
                VideoGeneration.id,
                VideoGeneration.upstream_request,
            ).where(
                VideoGeneration.user_id == user_id,
                ~VideoGeneration.status.in_(_VIDEO_TERMINAL_STATUSES),
            )
        )
    ).all()
    for generation_id, upstream_request in rows:
        for item in _reference_media_items(upstream_request):
            if (
                isinstance(video_id, str)
                and video_id
                and item.get("video_id") == video_id
            ):
                return str(generation_id)
            if any(
                isinstance(item.get(field), str) and item.get(field) in storage_keys
                for field in ("storage_key", "upstream_reference_storage_key")
            ):
                return str(generation_id)
    return None


async def known_live_media_storage_keys(
    db: AsyncSession,
    candidates: set[str],
) -> set[str]:
    storyboard_candidates = {
        key
        for key in candidates
        if len(PurePosixPath(key).parts) > 2
        and PurePosixPath(key).parts[2] == "storyboards"
    }
    video_candidates = {
        key
        for key in candidates
        if len(PurePosixPath(key).parts) > 2
        and PurePosixPath(key).parts[2] in {"v", "vref", "storyboards"}
    }
    image_candidates = candidates - video_candidates
    image_keys = await known_live_image_storage_keys(db, image_candidates)
    video_keys = await known_live_video_storage_keys(db, video_candidates)
    active_generation_keys = await known_active_video_generation_storage_keys(
        db,
        candidates,
    )
    storyboard_commit_keys = await known_storyboard_commit_storage_keys(
        db,
        storyboard_candidates,
    )
    return image_keys | video_keys | active_generation_keys | storyboard_commit_keys


__all__ = [
    "active_video_generation_reference_id",
    "known_active_video_generation_storage_keys",
    "known_live_image_storage_keys",
    "known_live_media_storage_keys",
    "known_live_video_storage_keys",
    "known_storyboard_commit_storage_keys",
]
