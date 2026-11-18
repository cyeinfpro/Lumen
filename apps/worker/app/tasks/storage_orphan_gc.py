"""Grace-period two-stage mark-and-sweep GC for orphan media under /u/*."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    CompletionStatus,
    GenerationStatus,
    VideoGenerationStatus,
)
from lumen_core.model_entities import (
    Completion,
    Generation,
    Image,
    ImageVariant,
    ImageVariantClaim,
    ModelLibraryItem,
    PosterStyleItem,
    User,
)
from lumen_core.model_entities.media_workflows import Video
from lumen_core.model_entities.tasks import VideoGeneration

from ..config import settings
from ..db import SessionLocal

logger = logging.getLogger(__name__)

DEFAULT_GRACE_PERIOD_SECONDS = 48 * 3600.0
DEFAULT_QUARANTINE_RETENTION_SECONDS = 7 * 86400.0
QUARANTINE_DIRNAME = ".quarantine"
QUARANTINE_MANIFEST_NAME = ".manifest.json"
REFERENCE_TABLES = (
    "images",
    "image_variants",
    "videos",
    "model_library_items",
    "poster_style_items",
)
_VIDEO_TERMINAL_STATUSES = (
    VideoGenerationStatus.SUCCEEDED.value,
    VideoGenerationStatus.FAILED.value,
    VideoGenerationStatus.CANCELED.value,
    VideoGenerationStatus.EXPIRED.value,
)
_GENERATION_ACTIVE_STATUSES = (
    GenerationStatus.QUEUED.value,
    GenerationStatus.RUNNING.value,
)
_COMPLETION_ACTIVE_STATUSES = (
    CompletionStatus.QUEUED.value,
    CompletionStatus.STREAMING.value,
)


@dataclass(frozen=True, slots=True)
class OrphanGcPolicy:
    grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS
    quarantine_retention_seconds: float = DEFAULT_QUARANTINE_RETENTION_SECONDS
    max_files: int = 500
    max_entries: int = 10_000
    dry_run: bool = True

    def __post_init__(self) -> None:
        if self.grace_period_seconds < 0:
            raise ValueError("grace_period_seconds must be non-negative")
        if self.quarantine_retention_seconds < 0:
            raise ValueError("quarantine_retention_seconds must be non-negative")
        if self.max_files < 1:
            raise ValueError("max_files must be positive")
        if self.max_entries < self.max_files:
            raise ValueError("max_entries must be >= max_files")


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    storage_key: str
    path: Path
    size_bytes: int
    dev: int
    ino: int
    mtime_ns: int
    mtime_sec: float


def _is_safe_segment(segment: str) -> bool:
    return (
        bool(segment)
        and segment not in {".", "..", QUARANTINE_DIRNAME}
        and not segment.startswith(".tmp")
        and "\x00" not in segment
        and "/" not in segment
        and "\\" not in segment
    )


def _extract_nested_storage_keys(payload: Any, *, depth: int = 0) -> set[str]:
    if depth > 6:
        return set()
    found: set[str] = set()
    if isinstance(payload, str):
        if payload.startswith("u/") and len(PurePosixPath(payload).parts) >= 3:
            found.add(payload)
        return found
    if isinstance(payload, dict):
        for value in payload.values():
            found.update(_extract_nested_storage_keys(value, depth=depth + 1))
        return found
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found.update(_extract_nested_storage_keys(item, depth=depth + 1))
    return found


def _iter_regular_files_under(
    base_dir: Path,
    *,
    rel_prefix: str,
    max_entries: int,
) -> Iterator[tuple[str, Path, os.stat_result, bool]]:
    entries_seen = 0
    try:
        base_info = base_dir.lstat()
    except OSError:
        return
    if stat.S_ISLNK(base_info.st_mode) or not stat.S_ISDIR(base_info.st_mode):
        return
    stack: list[tuple[Path, str]] = [(base_dir, rel_prefix)]
    while stack:
        current_dir, prefix = stack.pop()
        try:
            with os.scandir(current_dir) as raw_entries:
                entries = sorted(raw_entries, key=lambda item: item.name, reverse=True)
        except OSError:
            continue
        for entry in entries:
            entries_seen += 1
            if entries_seen > max_entries:
                yield "", current_dir, base_info, True
                return
            if not _is_safe_segment(entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            rel_key = f"{prefix}/{entry.name}"
            if stat.S_ISDIR(info.st_mode):
                stack.append((Path(entry.path), rel_key))
            elif stat.S_ISREG(info.st_mode):
                yield rel_key, Path(entry.path), info, False


def _scan_mature_user_files(
    storage_root: Path,
    *,
    cutoff_ts: float,
    policy: OrphanGcPolicy,
) -> tuple[list[_ScannedFile], dict[str, int | bool]]:
    candidates: list[_ScannedFile] = []
    scanned_files = 0
    scanned_bytes = 0
    young_skipped = 0
    budget_exhausted = False
    for rel_key, file_path, info, exhausted in _iter_regular_files_under(
        storage_root / "u",
        rel_prefix="u",
        max_entries=policy.max_entries,
    ):
        if exhausted:
            budget_exhausted = True
            break
        scanned_files += 1
        size = int(info.st_size)
        scanned_bytes += size
        if info.st_mtime >= cutoff_ts:
            young_skipped += 1
            continue
        candidates.append(
            _ScannedFile(
                storage_key=rel_key,
                path=file_path,
                size_bytes=size,
                dev=int(info.st_dev),
                ino=int(info.st_ino),
                mtime_ns=int(info.st_mtime_ns),
                mtime_sec=float(info.st_mtime),
            )
        )
        if len(candidates) >= policy.max_files:
            budget_exhausted = True
            break
    stats: dict[str, int | bool] = {
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "young_skipped": young_skipped,
        "budget_exhausted": budget_exhausted,
    }
    return candidates, stats


def _candidate_user_ids(candidates: set[str]) -> list[str]:
    users = {
        PurePosixPath(key).parts[1]
        for key in candidates
        if len(PurePosixPath(key).parts) >= 3 and PurePosixPath(key).parts[0] == "u"
    }
    return sorted(user_id for user_id in users if user_id)


async def _collect_image_and_variant_refs(
    db: AsyncSession,
    candidates: set[str],
    user_ids: list[str],
) -> set[str]:
    ordered = sorted(candidates)
    conditions = [Image.storage_key.in_(ordered)]
    if user_ids:
        conditions.append(Image.user_id.in_(user_ids))
    rows = (
        await db.execute(
            select(
                Image.storage_key,
                Image.metadata_jsonb,
                Image.artifact_manifest_jsonb,
            ).where(Image.deleted_at.is_(None), or_(*conditions))
        )
    ).all()
    referenced: set[str] = set()
    for storage_key, metadata, manifest in rows:
        if isinstance(storage_key, str) and storage_key in candidates:
            referenced.add(storage_key)
        referenced.update(_extract_nested_storage_keys(metadata) & candidates)
        referenced.update(_extract_nested_storage_keys(manifest) & candidates)
    variant_keys = (
        (
            await db.execute(
                select(ImageVariant.storage_key)
                .join(Image, Image.id == ImageVariant.image_id)
                .where(
                    ImageVariant.storage_key.in_(ordered),
                    Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    referenced.update(k for k in variant_keys if isinstance(k, str) and k in candidates)
    return referenced


async def _collect_video_refs(
    db: AsyncSession,
    candidates: set[str],
    user_ids: list[str],
) -> set[str]:
    ordered = sorted(candidates)
    conditions = [
        Video.storage_key.in_(ordered),
        Video.poster_storage_key.in_(ordered),
    ]
    if user_ids:
        conditions.append(Video.user_id.in_(user_ids))
    rows = (
        await db.execute(
            select(
                Video.storage_key,
                Video.poster_storage_key,
                Video.metadata_jsonb,
            ).where(Video.deleted_at.is_(None), or_(*conditions))
        )
    ).all()
    referenced: set[str] = set()
    for storage_key, poster_key, metadata in rows:
        if isinstance(storage_key, str) and storage_key in candidates:
            referenced.add(storage_key)
        if isinstance(poster_key, str) and poster_key in candidates:
            referenced.add(poster_key)
        referenced.update(_extract_nested_storage_keys(metadata) & candidates)
    return referenced


async def _collect_library_refs(
    db: AsyncSession,
    candidates: set[str],
    user_ids: list[str],
) -> set[str]:
    if not user_ids:
        return set()
    referenced: set[str] = set()
    protected_image_ids: set[str] = set()
    model_rows = (
        await db.execute(
            select(ModelLibraryItem.image_id, ModelLibraryItem.metadata_jsonb).where(
                ModelLibraryItem.user_id.in_(user_ids)
            )
        )
    ).all()
    for image_id, metadata in model_rows:
        if isinstance(image_id, str) and image_id:
            protected_image_ids.add(image_id)
        referenced.update(_extract_nested_storage_keys(metadata) & candidates)
    poster_rows = (
        await db.execute(
            select(
                PosterStyleItem.cover_image_id,
                PosterStyleItem.sample_image_ids,
                PosterStyleItem.metadata_jsonb,
            ).where(PosterStyleItem.user_id.in_(user_ids))
        )
    ).all()
    for cover_id, sample_ids, metadata in poster_rows:
        if isinstance(cover_id, str) and cover_id:
            protected_image_ids.add(cover_id)
        if isinstance(sample_ids, list):
            protected_image_ids.update(s for s in sample_ids if isinstance(s, str) and s)
        referenced.update(_extract_nested_storage_keys(metadata) & candidates)
    if protected_image_ids:
        id_list = sorted(protected_image_ids)
        img_rows = (
            await db.execute(
                select(
                    Image.storage_key,
                    Image.metadata_jsonb,
                    Image.artifact_manifest_jsonb,
                ).where(Image.id.in_(id_list))
            )
        ).all()
        for storage_key, metadata, manifest in img_rows:
            if isinstance(storage_key, str) and storage_key in candidates:
                referenced.add(storage_key)
            referenced.update(_extract_nested_storage_keys(metadata) & candidates)
            referenced.update(_extract_nested_storage_keys(manifest) & candidates)
        var_keys = (
            (
                await db.execute(
                    select(ImageVariant.storage_key).where(
                        ImageVariant.image_id.in_(id_list)
                    )
                )
            )
            .scalars()
            .all()
        )
        referenced.update(k for k in var_keys if isinstance(k, str) and k in candidates)
    return referenced


async def _collect_inflight_task_refs(
    db: AsyncSession,
    candidates: set[str],
    user_ids: list[str],
) -> set[str]:
    if not user_ids:
        return set()
    referenced: set[str] = set()
    vgen_rows = (
        await db.execute(
            select(
                VideoGeneration.id,
                VideoGeneration.user_id,
                VideoGeneration.input_image_storage_key,
                VideoGeneration.upstream_request,
            )
            .join(User, User.id == VideoGeneration.user_id)
            .where(
                VideoGeneration.user_id.in_(user_ids),
                User.deleted_at.is_(None),
                ~VideoGeneration.status.in_(_VIDEO_TERMINAL_STATUSES),
            )
        )
    ).all()
    for gen_id, user_id, input_key, upstream_req in vgen_rows:
        if isinstance(input_key, str) and input_key in candidates:
            referenced.add(input_key)
        referenced.update(_extract_nested_storage_keys(upstream_req) & candidates)
        prefix = f"u/{user_id}/v/{gen_id}/"
        referenced.update(k for k in candidates if k.startswith(prefix))
    gen_ids = (
        (
            await db.execute(
                select(Generation.id).where(
                    Generation.user_id.in_(user_ids),
                    Generation.status.in_(_GENERATION_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    for gid in gen_ids:
        referenced.update(k for k in candidates if f"/g/{gid}/" in k)
    comp_ids = (
        (
            await db.execute(
                select(Completion.id).where(
                    Completion.user_id.in_(user_ids),
                    Completion.status.in_(_COMPLETION_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    for cid in comp_ids:
        referenced.update(k for k in candidates if f"/completion-tools/{cid}/" in k)
    claim_sources = (
        (
            await db.execute(
                select(ImageVariantClaim.source_key).where(
                    ImageVariantClaim.lease_until > datetime.now(timezone.utc)
                )
            )
        )
        .scalars()
        .all()
    )
    referenced.update(k for k in claim_sources if isinstance(k, str) and k in candidates)
    return referenced


async def collect_referenced_storage_keys(
    db: AsyncSession,
    candidates: set[str],
) -> set[str]:
    if not candidates:
        return set()
    user_ids = _candidate_user_ids(candidates)
    refs = await _collect_image_and_variant_refs(db, candidates, user_ids)
    refs.update(await _collect_video_refs(db, candidates, user_ids))
    refs.update(await _collect_library_refs(db, candidates, user_ids))
    refs.update(await _collect_inflight_task_refs(db, candidates, user_ids))
    return refs


def _load_quarantine_manifest(quarantine_root: Path) -> dict[str, dict[str, Any]]:
    manifest_path = quarantine_root / QUARANTINE_MANIFEST_NAME
    try:
        info = manifest_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            return {}
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_quarantine_manifest(
    quarantine_root: Path,
    manifest: dict[str, dict[str, Any]],
) -> None:
    quarantine_root.mkdir(parents=True, exist_ok=True)
    manifest_path = quarantine_root / QUARANTINE_MANIFEST_NAME
    tmp = manifest_path.with_name(f".{manifest_path.name}.tmp.{os.getpid()}")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o600)
    except OSError as exc:
        if exc.errno != errno.EPERM:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, manifest_path)


def _quarantine_file(
    item: _ScannedFile,
    *,
    quarantine_root: Path,
    now_ts: float,
    manifest: dict[str, dict[str, Any]],
) -> bool:
    try:
        live_info = item.path.lstat()
    except OSError:
        return False
    if (
        stat.S_ISLNK(live_info.st_mode)
        or not stat.S_ISREG(live_info.st_mode)
        or (int(live_info.st_dev), int(live_info.st_ino), int(live_info.st_mtime_ns))
        != (item.dev, item.ino, item.mtime_ns)
    ):
        return False
    dest = quarantine_root / item.storage_key
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(item.path, dest)
    except OSError:
        return False
    try:
        os.utime(dest, (now_ts, now_ts))
    except OSError as exc:
        if exc.errno != errno.EPERM:
            logger.debug("quarantine utime skipped for %s: %s", dest, exc)
    manifest[item.storage_key] = {
        "quarantined_at": now_ts,
        "original_mtime": item.mtime_sec,
        "size_bytes": item.size_bytes,
    }
    return True


def _prune_empty_parents(path: Path, *, stop_at: Path) -> None:
    current = path.parent
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


async def _sweep_quarantine(
    db: AsyncSession,
    *,
    quarantine_root: Path,
    now_ts: float,
    policy: OrphanGcPolicy,
    manifest: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    q_items: list[tuple[str, Path, int, float]] = []
    for rel_key, q_path, info, _exhausted in _iter_regular_files_under(
        quarantine_root / "u",
        rel_prefix="u",
        max_entries=policy.max_entries,
    ):
        if not rel_key:
            continue
        meta = manifest.get(rel_key)
        q_at_raw = meta.get("quarantined_at") if isinstance(meta, dict) else None
        q_at = (
            float(q_at_raw)
            if isinstance(q_at_raw, (int, float))
            else float(info.st_mtime)
        )
        q_items.append((rel_key, q_path, int(info.st_size), q_at))
    due = [
        entry
        for entry in q_items
        if (now_ts - entry[3]) >= policy.quarantine_retention_seconds
    ]
    due_refs = await collect_referenced_storage_keys(
        db,
        {entry[0] for entry in due},
    )
    purged_keys: list[str] = []
    purged_bytes = 0
    eligible_keys: list[str] = []
    eligible_bytes = 0
    protected_in_quarantine = 0
    manifest_dirty = False
    stop_dir = quarantine_root / "u"
    for rel_key, q_path, size_bytes, _q_at in due:
        if rel_key in due_refs:
            protected_in_quarantine += 1
            continue
        eligible_keys.append(rel_key)
        eligible_bytes += size_bytes
        if policy.dry_run:
            continue
        try:
            q_path.unlink()
            purged_keys.append(rel_key)
            purged_bytes += size_bytes
            if manifest.pop(rel_key, None) is not None:
                manifest_dirty = True
            _prune_empty_parents(q_path, stop_at=stop_dir)
        except OSError:
            continue
    return (
        {
            "quarantined_total_files": len(q_items),
            "quarantined_total_bytes": sum(item[2] for item in q_items),
            "purge_eligible_count": len(eligible_keys),
            "purge_eligible_bytes": eligible_bytes,
            "purged_count": len(purged_keys),
            "purged_bytes": purged_bytes,
            "purged_keys": purged_keys,
            "protected_in_quarantine": protected_in_quarantine,
        },
        manifest_dirty,
    )


async def reconcile_storage_orphans(
    db: AsyncSession,
    *,
    storage_root: str | Path,
    policy: OrphanGcPolicy | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    effective_policy = policy or OrphanGcPolicy()
    now_ts = float(time.time() if now is None else now)
    cutoff_ts = now_ts - effective_policy.grace_period_seconds
    root = Path(storage_root)
    candidates, scan_stats = _scan_mature_user_files(
        root,
        cutoff_ts=cutoff_ts,
        policy=effective_policy,
    )
    candidate_keys = {item.storage_key for item in candidates}
    referenced = await collect_referenced_storage_keys(db, candidate_keys)
    orphans = [item for item in candidates if item.storage_key not in referenced]

    quarantine_root = root / QUARANTINE_DIRNAME
    manifest = _load_quarantine_manifest(quarantine_root)
    manifest_dirty = False
    quarantined_keys: list[str] = []
    quarantined_bytes = 0
    if not effective_policy.dry_run and orphans:
        rechecked_refs = await collect_referenced_storage_keys(
            db,
            {item.storage_key for item in orphans},
        )
        for item in orphans:
            if item.storage_key in rechecked_refs:
                continue
            if _quarantine_file(
                item,
                quarantine_root=quarantine_root,
                now_ts=now_ts,
                manifest=manifest,
            ):
                quarantined_keys.append(item.storage_key)
                quarantined_bytes += item.size_bytes
                manifest_dirty = True

    sweep_summary, sweep_dirty = await _sweep_quarantine(
        db,
        quarantine_root=quarantine_root,
        now_ts=now_ts,
        policy=effective_policy,
        manifest=manifest,
    )
    if (manifest_dirty or sweep_dirty) and not effective_policy.dry_run:
        _save_quarantine_manifest(quarantine_root, manifest)

    return {
        "dry_run": effective_policy.dry_run,
        "reference_tables": list(REFERENCE_TABLES),
        "grace_period_seconds": effective_policy.grace_period_seconds,
        "quarantine_retention_seconds": effective_policy.quarantine_retention_seconds,
        **scan_stats,
        "mature_candidates": len(candidates),
        "referenced_count": len(referenced),
        "orphan_count": len(orphans),
        "orphan_bytes": sum(item.size_bytes for item in orphans),
        "orphan_keys": [item.storage_key for item in orphans],
        "quarantined_count": len(quarantined_keys),
        "quarantined_bytes": quarantined_bytes,
        "quarantined_keys": quarantined_keys,
        **sweep_summary,
    }


async def run_storage_orphan_gc(
    ctx: dict[str, Any] | None = None,
    *,
    policy: OrphanGcPolicy | None = None,
    dry_run: bool = False,
    storage_root: str | Path | None = None,
    session_factory: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    _ = ctx
    effective_policy = policy or OrphanGcPolicy(dry_run=dry_run)
    effective_root = storage_root or settings.storage_root
    factory = session_factory or SessionLocal
    async with factory() as session:
        summary = await reconcile_storage_orphans(
            session,
            storage_root=effective_root,
            policy=effective_policy,
            now=now,
        )
    logger.info(
        "storage orphan gc completed dry_run=%s orphans=%d quarantined=%d purged=%d",
        summary["dry_run"],
        summary["orphan_count"],
        summary["quarantined_count"],
        summary["purged_count"],
    )
    return summary


__all__ = [
    "DEFAULT_GRACE_PERIOD_SECONDS",
    "DEFAULT_QUARANTINE_RETENTION_SECONDS",
    "OrphanGcPolicy",
    "QUARANTINE_DIRNAME",
    "REFERENCE_TABLES",
    "collect_referenced_storage_keys",
    "reconcile_storage_orphans",
    "run_storage_orphan_gc",
]
