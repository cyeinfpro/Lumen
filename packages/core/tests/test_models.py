from sqlalchemy import CheckConstraint, Float

from lumen_core.models import Image, SoftDeleteMixin, User, Video, VideoGeneration


def test_image_nsfw_score_uses_explicit_float_column_type():
    assert isinstance(Image.__table__.c.nsfw_score.type, Float)


def test_user_reuses_soft_delete_mixin_column():
    assert issubclass(User, SoftDeleteMixin)
    assert "deleted_at" in User.__table__.c
    assert User.__table__.c.deleted_at.nullable is True


def test_user_email_unique_only_for_active_users():
    assert User.__table__.c.email.unique is not True
    index = next(
        idx for idx in User.__table__.indexes if idx.name == "uq_users_email_active"
    )
    assert index.unique is True
    assert [col.name for col in index.columns] == ["email"]
    assert str(index.dialect_options["postgresql"]["where"]) == "deleted_at IS NULL"
    assert str(index.dialect_options["sqlite"]["where"]) == "deleted_at IS NULL"


def test_video_generation_has_idempotency_and_provider_task_guards():
    constraint_names = {
        constraint.name for constraint in VideoGeneration.__table__.constraints
    }
    index_names = {index.name for index in VideoGeneration.__table__.indexes}
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in VideoGeneration.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "uq_video_gen_user_idemp" in constraint_names
    assert "uq_video_gen_provider_task" in index_names
    assert "ix_video_gen_user_status_created" in index_names
    assert "ix_video_gen_status_next_poll" in index_names
    assert "duration_s = -1" in checks["ck_video_gen_duration_positive"]
    assert "duration_s >= 3" in checks["ck_video_gen_duration_positive"]
    assert "duration_s <= 15" in checks["ck_video_gen_duration_positive"]
    assert "progress_pct >= 0" in checks["ck_video_gen_progress_pct"]
    assert "progress_pct <= 100" in checks["ck_video_gen_progress_pct"]


def test_video_asset_is_private_soft_deleted_storage_record():
    constraint_names = {constraint.name for constraint in Video.__table__.constraints}
    index_names = {index.name for index in Video.__table__.indexes}

    assert issubclass(Video, SoftDeleteMixin)
    assert "uq_videos_storage_key" in constraint_names
    assert "uq_videos_poster_storage_key" in constraint_names
    assert "ix_videos_user_alive_created" in index_names
    assert Video.__table__.c.visibility.default.arg == "private"
