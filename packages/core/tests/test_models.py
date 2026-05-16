from sqlalchemy import Float

from lumen_core.models import Image, SoftDeleteMixin, User


def test_image_nsfw_score_uses_explicit_float_column_type():
    assert isinstance(Image.__table__.c.nsfw_score.type, Float)


def test_user_reuses_soft_delete_mixin_column():
    assert issubclass(User, SoftDeleteMixin)
    assert "deleted_at" in User.__table__.c
    assert User.__table__.c.deleted_at.nullable is True


def test_user_email_unique_only_for_active_users():
    assert User.__table__.c.email.unique is not True
    index = next(idx for idx in User.__table__.indexes if idx.name == "uq_users_email_active")
    assert index.unique is True
    assert [col.name for col in index.columns] == ["email"]
    assert str(index.dialect_options["postgresql"]["where"]) == "deleted_at IS NULL"
    assert str(index.dialect_options["sqlite"]["where"]) == "deleted_at IS NULL"
