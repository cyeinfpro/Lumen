from lumen_core import byok, byok_sse, models, schemas
from lumen_core.billing_schemas import MoneyOut, WalletActivity24hOut, WalletOut
from lumen_core.memory_extraction_models import MemoryExtractionRun
from lumen_core.message_content import public_message_content
from lumen_core.model_base import Base, SoftDeleteMixin, TimestampMixin, new_uuid7


def test_models_reexports_split_model_primitives() -> None:
    assert models.Base is Base
    assert models.SoftDeleteMixin is SoftDeleteMixin
    assert models.TimestampMixin is TimestampMixin
    assert models.new_uuid7 is new_uuid7
    assert models.MemoryExtractionRun is MemoryExtractionRun


def test_schemas_reexports_split_schema_primitives() -> None:
    assert schemas.MoneyOut is MoneyOut
    assert schemas.WalletActivity24hOut is WalletActivity24hOut
    assert schemas.WalletOut is WalletOut
    assert schemas.public_message_content is public_message_content


def test_byok_reexports_split_output_parsers() -> None:
    assert byok.extract_response_output_text is byok_sse.extract_response_output_text
    assert byok.extract_sse_output_text is byok_sse.extract_sse_output_text
