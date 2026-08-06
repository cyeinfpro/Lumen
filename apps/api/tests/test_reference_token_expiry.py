from __future__ import annotations

import importlib.util
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.images.application.http_route_parts.reference_access import (
    video_reference_token_is_valid,
)
from app.services.video.reference_media import (
    REFERENCE_ACCESS_TOKEN_TTL,
    ensure_reference_access_token,
    reference_token_is_valid,
)

ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    ROOT
    / "apps/api/alembic/versions/0059_reference_token_expiry.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "reference_token_expiry_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "expiry",
    [None, "", 123, "not-a-date", "2025-01-01T00:00:00+00:00"],
)
def test_reference_token_missing_bad_or_expired_expiry_is_invalid(
    expiry: object,
) -> None:
    metadata = {
        "reference_access_token": "old-token",
        "reference_access_token_expires_at": expiry,
    }

    assert (
        reference_token_is_valid(
            metadata,
            token_key="reference_access_token",
            expires_key="reference_access_token_expires_at",
            token="old-token",
        )
        is False
    )


@pytest.mark.parametrize(
    "expiry",
    [None, "", 123, "not-a-date", "2025-01-01T00:00:00+00:00"],
)
def test_image_reference_token_missing_bad_or_expired_expiry_is_invalid(
    expiry: object,
) -> None:
    metadata = {
        "video_reference_access_token": "old-token",
        "video_reference_access_token_expires_at": expiry,
    }

    assert video_reference_token_is_valid(metadata, token="old-token") is False


def test_reference_validators_have_no_updated_at_fallback() -> None:
    assert "updated_at" not in inspect.signature(reference_token_is_valid).parameters
    assert (
        "updated_at"
        not in inspect.signature(video_reference_token_is_valid).parameters
    )


def test_valid_token_does_not_slide_expiry() -> None:
    fixed_expiry = "2099-01-01T00:00:00+00:00"
    metadata = {
        "reference_access_token": "same-token",
        "reference_access_token_expires_at": fixed_expiry,
    }

    token = ensure_reference_access_token(
        metadata,
        token_key="reference_access_token",
        expires_key="reference_access_token_expires_at",
    )

    assert token == "same-token"
    assert metadata["reference_access_token_expires_at"] == fixed_expiry


def test_expired_token_rotates_token_and_expiry() -> None:
    before = datetime.now(timezone.utc)
    metadata = {
        "reference_access_token": "old-token",
        "reference_access_token_expires_at": (
            before - timedelta(seconds=1)
        ).isoformat(),
    }

    token = ensure_reference_access_token(
        metadata,
        token_key="reference_access_token",
        expires_key="reference_access_token_expires_at",
    )
    expiry = datetime.fromisoformat(metadata["reference_access_token_expires_at"])
    after = datetime.now(timezone.utc)

    assert token != "old-token"
    assert metadata["reference_access_token"] == token
    assert before + REFERENCE_ACCESS_TOKEN_TTL <= expiry
    assert expiry <= after + REFERENCE_ACCESS_TOKEN_TTL


def test_reference_token_migration_revokes_only_unbounded_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    images = sa.Table(
        "images",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("metadata_jsonb", sa.JSON(), nullable=True),
    )
    videos = sa.Table(
        "videos",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("metadata_jsonb", sa.JSON(), nullable=True),
    )
    metadata.create_all(engine)
    valid_expiry = "2099-01-01T00:00:00+00:00"

    with engine.begin() as connection:
        connection.execute(
            images.insert(),
            [
                {
                    "id": "image-missing-expiry",
                    "metadata_jsonb": {
                        "video_reference_access_token": "legacy",
                        "unrelated": "kept",
                    },
                },
                {
                    "id": "image-valid",
                    "metadata_jsonb": {
                        "video_reference_access_token": "bounded",
                        "video_reference_access_token_expires_at": valid_expiry,
                    },
                },
            ],
        )
        connection.execute(
            videos.insert(),
            [
                {
                    "id": "video-bad-expiry",
                    "metadata_jsonb": {
                        "reference_access_token": "legacy-video",
                        "reference_access_token_expires_at": "not-a-date",
                        "unrelated": {"kept": True},
                    },
                },
                {
                    "id": "video-orphan-expiry",
                    "metadata_jsonb": {
                        "reference_access_token_expires_at": valid_expiry,
                    },
                },
            ],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

        migration.upgrade()

        image_rows = dict(
            connection.execute(
                sa.select(images.c.id, images.c.metadata_jsonb)
            ).all()
        )
        video_rows = dict(
            connection.execute(
                sa.select(videos.c.id, videos.c.metadata_jsonb)
            ).all()
        )

    assert image_rows["image-missing-expiry"] == {"unrelated": "kept"}
    assert image_rows["image-valid"] == {
        "video_reference_access_token": "bounded",
        "video_reference_access_token_expires_at": valid_expiry,
    }
    assert video_rows["video-bad-expiry"] == {
        "unrelated": {"kept": True},
    }
    assert video_rows["video-orphan-expiry"] == {}
    assert migration.revision == "0059_reference_token_expiry"
    assert migration.down_revision == "0058_storage_apply_operations"
