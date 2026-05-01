from sqlalchemy import Float

from lumen_core.models import Image, SoftDeleteMixin, User


def test_image_nsfw_score_uses_explicit_float_column_type():
    assert isinstance(Image.__table__.c.nsfw_score.type, Float)


def test_user_reuses_soft_delete_mixin_column():
    assert issubclass(User, SoftDeleteMixin)
    assert "deleted_at" in User.__table__.c
    assert User.__table__.c.deleted_at.nullable is True
