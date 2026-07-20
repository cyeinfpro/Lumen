from pathlib import Path

from lumen_core import byok, byok_sse, models, schemas
from lumen_core.billing_schemas import MoneyOut, WalletActivity24hOut, WalletOut
from lumen_core.canvas_models import CanvasDocument
from lumen_core.memory_extraction_models import MemoryExtractionRun
from lumen_core.message_content import public_message_content
from lumen_core.model_base import Base, SoftDeleteMixin, TimestampMixin, new_uuid7
from lumen_core.model_entities.accounts import User
from lumen_core.model_entities.tasks import Generation
from lumen_core.schema_models.common import BaseOut
from lumen_core.schema_models.messaging import PostMessageIn, TaskListOut
from lumen_core.schema_models.providers import ProviderProxyIn
from lumen_core.schema_models.video import VideoUploadOut
from lumen_core.schema_models.workflows import WorkflowType


CORE_PACKAGE = Path(__file__).resolve().parents[1] / "lumen_core"


def test_models_reexports_split_model_primitives() -> None:
    assert models.Base is Base
    assert models.SoftDeleteMixin is SoftDeleteMixin
    assert models.TimestampMixin is TimestampMixin
    assert models.new_uuid7 is new_uuid7
    assert models.MemoryExtractionRun is MemoryExtractionRun
    assert models.User is User
    assert models.Generation is Generation


def test_split_models_share_one_metadata_registry() -> None:
    assert models.User.__table__ is Base.metadata.tables["users"]
    assert models.Generation.__table__ is Base.metadata.tables["generations"]
    assert CanvasDocument.__table__ is Base.metadata.tables["canvas_documents"]


def test_schemas_reexports_split_schema_primitives() -> None:
    assert schemas.MoneyOut is MoneyOut
    assert schemas.WalletActivity24hOut is WalletActivity24hOut
    assert schemas.WalletOut is WalletOut
    assert schemas.public_message_content is public_message_content
    assert schemas.BaseOut is BaseOut
    assert schemas.PostMessageIn is PostMessageIn
    assert schemas.TaskListOut is TaskListOut
    assert schemas.VideoUploadOut is VideoUploadOut
    assert schemas.WorkflowType is WorkflowType
    assert schemas.ProviderProxyIn is ProviderProxyIn


def test_legacy_facades_stay_bounded_and_canvas_dependency_is_one_way() -> None:
    models_source = (CORE_PACKAGE / "models.py").read_text(encoding="utf-8")
    schemas_source = (CORE_PACKAGE / "schemas.py").read_text(encoding="utf-8")
    canvas_source = (CORE_PACKAGE / "canvas_models.py").read_text(encoding="utf-8")

    assert len(models_source.splitlines()) < 1500
    assert len(schemas_source.splitlines()) < 1500
    assert "from .models import" not in canvas_source
    assert "from .model_base import" in canvas_source
    assert "canvas_models" in models_source


def test_byok_reexports_split_output_parsers() -> None:
    assert byok.extract_response_output_text is byok_sse.extract_response_output_text
    assert byok.extract_sse_output_text is byok_sse.extract_sse_output_text
