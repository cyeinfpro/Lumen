from __future__ import annotations

from lumen_core.memory import (
    deterministic_embedding,
    directive_intent,
    embedding_literal,
    extract_memories,
    has_pii,
    parse_embedding_literal,
)


def test_explicit_remember_directive_extracts_as_directive() -> None:
    items, rejected_pii = extract_memories("记住：以后回答不要使用感叹号", explicit_only=True)

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


def test_deterministic_embedding_literal_is_vector_sized() -> None:
    vector = parse_embedding_literal(
        embedding_literal(deterministic_embedding("我是前端工程师，喜欢简洁回答"))
    )

    assert len(vector) == 3072
    assert any(abs(value) > 0 for value in vector)
