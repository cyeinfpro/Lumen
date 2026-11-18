from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from lumen_core.memory import (
    deterministic_embedding,
    directive_intent,
    embedding_literal,
    extract_memories,
    has_pii,
    parse_embedding_literal,
)
from lumen_core.model_entities.accounts import User
from lumen_core.model_entities.conversations import (
    Conversation,
    Message,
    UserMemory,
    UserMemoryScope,
    UserMemoryStaging,
)
from lumen_core.sqltypes import EmbeddingVectorType, HalfvecColumnType


def test_explicit_remember_directive_extracts_as_directive() -> None:
    items, rejected_pii = extract_memories(
        "记住：以后回答不要使用感叹号", explicit_only=True
    )

    assert rejected_pii is False
    assert len(items) == 1
    assert items[0].intent_kind == "directive"
    assert items[0].type == "avoid"
    assert "感叹号" in items[0].content


def test_vague_future_statement_is_not_directive() -> None:
    assert directive_intent("我以后都不喝牛奶了") == "statement"


def test_pii_detection_blocks_sensitive_memory() -> None:
    assert has_pii("记住我的密码是 123456") is True
    assert extract_memories("记住我的密码是 123456", explicit_only=True) == ([], True)


def test_pii_detection_does_not_block_plain_six_digit_numbers() -> None:
    assert has_pii("订单号是 202506，项目代号是 123456") is False


def test_deterministic_embedding_literal_is_vector_sized() -> None:
    vector = parse_embedding_literal(
        embedding_literal(deterministic_embedding("我是前端工程师，喜欢简洁回答"))
    )

    assert len(vector) == 3072
    assert any(abs(value) > 0 for value in vector)


def test_embedding_vector_type_compiles_for_postgres_asyncpg_and_sqlite() -> None:
    assert isinstance(UserMemory.__table__.c.embedding.type, EmbeddingVectorType)
    assert UserMemory.__table__.c.embedding.type.dimensions == 3072
    assert isinstance(UserMemoryStaging.__table__.c.embedding.type, EmbeddingVectorType)
    assert UserMemoryStaging.__table__.c.embedding.type.dimensions == 3072
    assert HalfvecColumnType(3072).get_col_spec() == "halfvec(3072)"

    pg_dialect = postgresql.dialect()
    asyncpg_dialect = sa.create_engine(
        "postgresql+asyncpg://user:pass@localhost/lumen"
    ).dialect
    sqlite_dialect = sqlite.dialect()

    pg_ddl = str(
        sa.schema.CreateTable(UserMemory.__table__).compile(dialect=pg_dialect)
    )
    sqlite_ddl = str(
        sa.schema.CreateTable(UserMemory.__table__).compile(dialect=sqlite_dialect)
    )
    assert "embedding halfvec(3072)" in pg_ddl
    assert "embedding TEXT" in sqlite_ddl

    pg_insert = str(
        sa.insert(UserMemory)
        .values(
            id="mem-1",
            user_id="user-1",
            type="preference",
            content="concise",
            source="manual",
            embedding="[0.1,0.2]",
            confidence=0.9,
            scope_id="scope-1",
        )
        .compile(dialect=asyncpg_dialect)
    )
    assert "CAST($" in pg_insert and "AS halfvec(3072))" in pg_insert

    pg_select = str(sa.select(UserMemory).compile(dialect=pg_dialect))
    assert "CAST(user_memories.embedding AS TEXT)" in pg_select


def test_embedding_vector_type_sqlite_roundtrip_and_sidebar_index_metadata() -> None:
    index = next(
        idx
        for idx in Conversation.__table__.indexes
        if idx.name == "ix_conversations_user_sidebar_active"
    )
    pg_index_sql = str(
        sa.schema.CreateIndex(index).compile(dialect=postgresql.dialect())
    )
    assert (
        "(user_id, archived, pinned DESC, last_activity_at DESC, id DESC)"
        in pg_index_sql
    )
    assert "WHERE deleted_at IS NULL" in pg_index_sql

    engine = sa.create_engine("sqlite:///:memory:")
    for table in (
        User.__table__,
        UserMemoryScope.__table__,
        Conversation.__table__,
        Message.__table__,
        UserMemory.__table__,
        UserMemoryStaging.__table__,
    ):
        table.create(engine)

    literal = embedding_literal(deterministic_embedding("test sqlite roundtrip"))
    with Session(engine) as session:
        session.add(User(id="u-1", email="u1@example.test", display_name="U1"))
        session.add(
            UserMemoryScope(id="s-1", user_id="u-1", name="default", is_default=True)
        )
        session.add(
            UserMemory(
                id="m-1",
                user_id="u-1",
                type="preference",
                content="concise",
                source="manual",
                embedding=literal,
                confidence=0.9,
                scope_id="s-1",
            )
        )
        session.commit()

        loaded = session.get(UserMemory, "m-1")
        assert loaded is not None
        assert loaded.embedding == literal
        assert len(parse_embedding_literal(loaded.embedding)) == 3072
