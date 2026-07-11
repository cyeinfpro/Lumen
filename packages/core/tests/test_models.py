from sqlalchemy import CheckConstraint, Float

from lumen_core.models import (
    BillingWindowUsageEvent,
    Generation,
    Image,
    RedemptionBatch,
    SoftDeleteMixin,
    User,
    UserApiCredential,
    Video,
    VideoGeneration,
)


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


def test_user_extraction_threshold_default_matches_latest_migration():
    column = User.__table__.c.extraction_threshold

    assert column.default is not None
    assert column.default.arg == 0.80
    assert str(column.server_default.arg) == "0.80"


def test_generation_has_user_created_index_for_history_queries():
    index = next(
        idx
        for idx in Generation.__table__.indexes
        if idx.name == "ix_generations_user_created"
    )

    assert [col.name for col in index.columns] == ["user_id", "created_at"]


def test_generation_persists_billing_retry_identity():
    column = Generation.__table__.c.billing_retry_count

    assert column.nullable is False
    assert column.default is not None
    assert column.default.arg == 0
    assert str(column.server_default.arg) == "0"


def test_billing_window_usage_enforces_credential_ownership_and_positive_amount():
    event_constraints = {
        constraint.name: constraint
        for constraint in BillingWindowUsageEvent.__table__.constraints
    }
    credential_constraints = {
        constraint.name for constraint in UserApiCredential.__table__.constraints
    }

    assert "uq_user_api_credentials_id_user" in credential_constraints
    owner_fk = event_constraints["fk_billing_window_credential_user"]
    assert [element.parent.name for element in owner_fk.elements] == [
        "credential_id",
        "user_id",
    ]
    assert [element.target_fullname for element in owner_fk.elements] == [
        "user_api_credentials.id",
        "user_api_credentials.user_id",
    ]
    assert (
        str(event_constraints["ck_billing_window_amount_positive"].sqltext)
        == "amount_micro > 0"
    )


def test_redemption_batch_has_persistent_creator_idempotency_guard():
    constraint_names = {
        constraint.name for constraint in RedemptionBatch.__table__.constraints
    }
    index_names = {index.name for index in RedemptionBatch.__table__.indexes}

    assert "uq_redemption_batch_creator_idemp" in constraint_names
    assert "ix_redemption_batches_creator_request_created" in index_names
    assert RedemptionBatch.__table__.c.idempotency_key.nullable is False
    assert RedemptionBatch.__table__.c.request_hash.nullable is False
    assert RedemptionBatch.__table__.c.code_count.nullable is False


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
