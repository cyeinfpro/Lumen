#!/usr/bin/env python3
"""Backfill legacy per-user model-library JSON indexes into PostgreSQL.

The route layer (apps/api/app/routes/workflows.py) was switched from
"read whole user index file → mutate → write whole file" to direct
``model_library_items`` row INSERT/UPDATE so concurrent favorites and
concurrent vision auto-tag writes stop trampling each other.

This one-off script copies any existing user JSON entries into the new
table so V1.0 users don't lose their saved models on the cutover.

Idempotent: re-running is safe (rows already present in the DB are
left alone — we INSERT only missing ids).

Run inside the lumen-api container so it inherits the same DB URL and
storage root:

    docker exec -it lumen-pg psql -U lumen -d lumen -c '\\dt model_library_items'   # confirm migration applied
    docker exec lumen-api python /app/scripts/migrate_model_library_to_pg.py --dry-run
    docker exec lumen-api python /app/scripts/migrate_model_library_to_pg.py

Or pass an explicit storage root if you're running outside the container:

    uv run python scripts/migrate_model_library_to_pg.py --storage-root /opt/lumendata/storage
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent
for candidate in (
    REPO_ROOT / "apps" / "api",
    Path.cwd(),
    Path("/app/apps/api"),
):
    candidate_str = str(candidate)
    if candidate.is_dir() and candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

# Imports below assume the lumen-api package is on sys.path. Inside the
# container this script is normally executed from /app/scripts with
# /app/apps/api inserted above; locally uv/poetry sets it up via pyproject.
# If those aren't available the
# script aborts loudly so users don't end up with a half-completed run.
try:
    from app.db import SessionLocal  # type: ignore[import-not-found]
    from app.config import settings  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - environment guard
    sys.stderr.write(
        "Cannot import app.db / app.config — run this script inside the "
        "lumen-api container or set PYTHONPATH=apps/api before invoking.\n"
        f"Underlying ImportError: {exc}\n"
    )
    sys.exit(2)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from lumen_core.models import Image, ModelLibraryHiddenPreset, ModelLibraryItem, User  # noqa: E402


def _safe_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, (str, int, float)) and str(item).strip()]


async def _migrate_user(
    db: AsyncSession,
    *,
    user_id: str,
    index: dict[str, Any],
    dry_run: bool,
) -> dict[str, int]:
    """Insert any missing items / hidden presets for one user.

    Returns counts so the caller can log totals across users.
    """
    raw_items = index.get("items") or []
    raw_hidden = index.get("hidden_preset_ids") or []
    counts = {"items_inserted": 0, "items_skipped": 0, "hidden_inserted": 0, "hidden_skipped": 0}
    hidden_ids = [str(pid).strip() for pid in raw_hidden if isinstance(pid, str) and pid.strip()]

    user_exists = (
        await db.execute(select(User.id).where(User.id == user_id))
    ).scalar_one_or_none()
    if user_exists is None:
        counts["items_skipped"] += sum(1 for item in raw_items if isinstance(item, dict))
        counts["hidden_skipped"] += len(hidden_ids)
        return counts

    # Items
    item_ids: list[str] = [
        str(item.get("id") or "").strip()
        for item in raw_items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    image_ids: list[str] = [
        str(item.get("image_id") or "").strip()
        for item in raw_items
        if isinstance(item, dict) and str(item.get("image_id") or "").strip()
    ]
    existing_ids: set[str] = set()
    if item_ids:
        rows = await db.execute(
            select(ModelLibraryItem.id).where(ModelLibraryItem.id.in_(item_ids))
        )
        existing_ids = {row[0] for row in rows}
    valid_image_ids: set[str] = set()
    if image_ids:
        rows = await db.execute(
            select(Image.id).where(
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
                Image.id.in_(image_ids),
            )
        )
        valid_image_ids = {row[0] for row in rows}

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or "").strip()
        if not item_id:
            continue
        if item_id in existing_ids:
            counts["items_skipped"] += 1
            continue
        image_id = str(raw.get("image_id") or "").strip()
        if not image_id or image_id not in valid_image_ids:
            # Legacy preset-only entries shouldn't live in user index.
            # Skip rather than insert a NULL/missing image_id (NOT NULL + FK).
            counts["items_skipped"] += 1
            continue
        created_at = _safe_dt(raw.get("created_at"))
        updated_at = _safe_dt(raw.get("updated_at"))
        row = ModelLibraryItem(
            id=item_id,
            user_id=user_id,
            source=str(raw.get("source") or "user_upload"),
            image_id=image_id,
            title=str(raw.get("title") or "").strip()[:120],
            age_segment=str(raw.get("age_segment") or "user_favorites"),
            gender=(str(raw.get("gender")).strip()[:40] if raw.get("gender") else None),
            appearance_direction=(
                str(raw.get("appearance_direction")).strip()[:80]
                if raw.get("appearance_direction")
                else None
            ),
            style_tags=_coerce_str_list(raw.get("style_tags") or raw.get("tags") or []),
            library_folder=(
                str(raw.get("library_folder")).strip()[:64]
                if raw.get("library_folder")
                else None
            ),
            prompt_hint=(
                str(raw.get("prompt_hint"))
                if raw.get("prompt_hint")
                else None
            ),
            auto_tagged_at=_safe_dt(raw.get("auto_tagged_at")),
            auto_tag_notes=(
                str(raw.get("auto_tag_notes"))
                if raw.get("auto_tag_notes")
                else None
            ),
            metadata_jsonb={
                k: v
                for k, v in raw.items()
                if k
                not in {
                    "id",
                    "user_id",
                    "owner_user_id",
                    "source",
                    "image_id",
                    "title",
                    "age_segment",
                    "gender",
                    "appearance_direction",
                    "style_tags",
                    "tags",
                    "library_folder",
                    "prompt_hint",
                    "auto_tagged_at",
                    "auto_tag_notes",
                    "created_at",
                    "updated_at",
                }
            },
        )
        if created_at is not None:
            row.created_at = created_at
        if updated_at is not None:
            row.updated_at = updated_at
        if not dry_run:
            db.add(row)
        counts["items_inserted"] += 1

    # Hidden presets
    if hidden_ids:
        existing_hidden_rows = await db.execute(
            select(ModelLibraryHiddenPreset.preset_id).where(
                ModelLibraryHiddenPreset.user_id == user_id,
                ModelLibraryHiddenPreset.preset_id.in_(hidden_ids),
            )
        )
        existing_hidden = {row[0] for row in existing_hidden_rows}
        for pid in hidden_ids:
            if pid in existing_hidden:
                counts["hidden_skipped"] += 1
                continue
            if not dry_run:
                db.add(ModelLibraryHiddenPreset(user_id=user_id, preset_id=pid))
            counts["hidden_inserted"] += 1

    if not dry_run and (counts["items_inserted"] or counts["hidden_inserted"]):
        await db.commit()
    elif dry_run:
        await db.rollback()
    return counts


async def main_async(args: argparse.Namespace) -> int:
    storage_root = Path(args.storage_root) if args.storage_root else Path(settings.storage_root)
    library_root = storage_root / "apparel-model-library"
    users_root = library_root / "users"
    if not users_root.is_dir():
        print(f"no legacy user library directory at {users_root}; nothing to migrate")
        return 0

    user_dirs = sorted(p for p in users_root.iterdir() if p.is_dir())
    if not user_dirs:
        print(f"{users_root} has no per-user subdirectories; nothing to migrate")
        return 0

    totals = {"users": 0, "items_inserted": 0, "items_skipped": 0, "hidden_inserted": 0, "hidden_skipped": 0}
    async with SessionLocal() as session:
        for user_dir in user_dirs:
            user_id = user_dir.name
            index_path = user_dir / "index.json"
            if not index_path.is_file():
                continue
            try:
                index = json.loads(index_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  user={user_id} skipped (cannot parse {index_path.name}): {exc}", file=sys.stderr)
                continue
            if not isinstance(index, dict):
                continue
            counts = await _migrate_user(
                session,
                user_id=user_id,
                index=index,
                dry_run=args.dry_run,
            )
            totals["users"] += 1
            totals["items_inserted"] += counts["items_inserted"]
            totals["items_skipped"] += counts["items_skipped"]
            totals["hidden_inserted"] += counts["hidden_inserted"]
            totals["hidden_skipped"] += counts["hidden_skipped"]
            print(
                f"  user={user_id} items: +{counts['items_inserted']} "
                f"(skip {counts['items_skipped']})  "
                f"hidden: +{counts['hidden_inserted']} (skip {counts['hidden_skipped']})"
            )

    label = "DRY RUN" if args.dry_run else "DONE"
    print(
        f"{label}: scanned {totals['users']} users; "
        f"items inserted {totals['items_inserted']} skipped {totals['items_skipped']}; "
        f"hidden_presets inserted {totals['hidden_inserted']} skipped {totals['hidden_skipped']}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage-root",
        help="storage root path (defaults to settings.storage_root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be inserted without committing.",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
