"""Restore historical concurrent-index migrations to a retryable entry state."""

from __future__ import annotations

from collections.abc import Iterable

from concurrent_index_state import (
    PostgresqlIndexSpec,
    PostgresqlUniqueConstraintSpec,
    drop_postgresql_index,
    drop_postgresql_unique_constraint,
    ensure_postgresql_index,
    ensure_postgresql_unique_constraint,
)


USERS_ACTIVE_EMAIL_REVISION = "0025_users_active_email_unique"
GENERATIONS_CANCEL_REVISION = "0053_cancel_intent_generations"
COMPLETIONS_CANCEL_REVISION = "0054_cancel_intent_completions"

_PARENTS = {
    USERS_ACTIVE_EMAIL_REVISION: "0024_billing_cache_tokens",
    GENERATIONS_CANCEL_REVISION: "0052_task_execution_epoch",
    COMPLETIONS_CANCEL_REVISION: GENERATIONS_CANCEL_REVISION,
}
_INDEXES = {
    USERS_ACTIVE_EMAIL_REVISION: PostgresqlIndexSpec(
        name="uq_users_email_active",
        table_name="users",
        columns=("email",),
        predicate="deleted_at IS NULL",
        unique=True,
    ),
    GENERATIONS_CANCEL_REVISION: PostgresqlIndexSpec(
        name="ix_generations_cancel_requested",
        table_name="generations",
        columns=("cancel_requested_at", "id"),
        predicate=(
            "cancel_requested_at IS NOT NULL AND status IN ('queued', 'running')"
        ),
    ),
    COMPLETIONS_CANCEL_REVISION: PostgresqlIndexSpec(
        name="ix_completions_cancel_requested",
        table_name="completions",
        columns=("cancel_requested_at", "id"),
        predicate=(
            "cancel_requested_at IS NOT NULL AND status IN ('queued', 'streaming')"
        ),
    ),
}
_LEGACY_EMAIL_CONSTRAINT = PostgresqlUniqueConstraintSpec(
    name="users_email_key",
    table_name="users",
    columns=("email",),
)


def prepare_concurrent_index_retry_boundary(
    operations: object,
    *,
    command: str,
    current_revision: str | None,
    pending_revision_ids: Iterable[str],
) -> None:
    """Undo only exact partial DDL so immutable historical steps can rerun."""

    if current_revision is None:
        return
    context = operations.get_context()  # type: ignore[attr-defined]
    if operations.get_bind().dialect.name != "postgresql":  # type: ignore[attr-defined]
        return
    if context.as_sql:
        raise RuntimeError(
            "concurrent-index retry preparation requires an online migration"
        )
    pending = frozenset(pending_revision_ids)

    if command == "upgrade":
        for revision, parent in _PARENTS.items():
            if current_revision != parent or revision not in pending:
                continue
            if revision == USERS_ACTIVE_EMAIL_REVISION:
                ensure_postgresql_unique_constraint(
                    operations,
                    _LEGACY_EMAIL_CONSTRAINT,
                )
            drop_postgresql_index(operations, _INDEXES[revision])
            return

    if command == "downgrade" and current_revision in pending:
        index = _INDEXES.get(current_revision)
        if index is None:
            return
        ensure_postgresql_index(operations, index)
        if current_revision == USERS_ACTIVE_EMAIL_REVISION:
            drop_postgresql_unique_constraint(
                operations,
                _LEGACY_EMAIL_CONSTRAINT,
            )


__all__ = [
    "COMPLETIONS_CANCEL_REVISION",
    "GENERATIONS_CANCEL_REVISION",
    "USERS_ACTIVE_EMAIL_REVISION",
    "prepare_concurrent_index_retry_boundary",
]
