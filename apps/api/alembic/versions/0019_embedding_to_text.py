"""Revert user_memories(_staging).embedding to text.

ORM 把列声明为 ``mapped_column(Text)`` 但 0018 把 DB 列 ALTER 成 ``vector(3072)``,
asyncpg 在 INSERT/UPDATE 时按 VARCHAR 绑定参数,PG 不会自动 cast 到 vector,直接报
``DatatypeMismatchError: column "embedding" is of type vector but expression is of
type character varying``,显式抽取 / reembed / staging 写入全部 500.

我们的 cosine 在 Python 端用 ``parse_embedding_literal`` 算,不依赖 PG 向量 ops
(0018 已经放弃 HNSW 索引,brute-force 即可),把列类型改回 ``text`` 最干净。
存进去仍是形如 ``"[0.123, 0.456, ...]"`` 的字符串,SELECT 回来一致。

Revision ID: 0019_embedding_to_text
Revises: 0018_account_memory
Create Date: 2026-05-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0019_embedding_to_text"
down_revision: str | Sequence[str] | None = "0018_account_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE user_memories ALTER COLUMN embedding TYPE text USING embedding::text"
    )
    op.execute(
        "ALTER TABLE user_memory_staging ALTER COLUMN embedding TYPE text USING embedding::text"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE user_memory_staging ALTER COLUMN embedding TYPE vector(3072) USING embedding::vector"
    )
    op.execute(
        "ALTER TABLE user_memories ALTER COLUMN embedding TYPE vector(3072) USING embedding::vector"
    )
