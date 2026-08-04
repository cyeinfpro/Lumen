"""Fail-closed PostgreSQL state machines for concurrent Alembic indexes."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import sqlalchemy as sa


@dataclass(frozen=True)
class PostgresqlIndexSpec:
    name: str
    table_name: str
    columns: tuple[str, ...]
    predicate: str | None = None
    unique: bool = False
    schema: str | None = None


@dataclass(frozen=True)
class PostgresqlUniqueConstraintSpec:
    name: str
    table_name: str
    columns: tuple[str, ...]
    schema: str | None = None


@dataclass(frozen=True)
class _IndexMetadata:
    is_valid: bool
    is_ready: bool
    is_live: bool
    is_unique: bool
    is_primary: bool
    is_exclusion: bool
    is_replica_identity: bool
    is_clustered: bool
    access_method: str
    key_expressions: tuple[str, ...]
    key_opclasses: tuple[str, ...]
    key_opclasses_are_default: tuple[bool, ...]
    key_collations_match_columns: tuple[bool, ...]
    key_options: tuple[int, ...]
    attribute_count: int
    key_attribute_count: int
    predicate: str | None
    backs_constraint: bool
    uses_default_tablespace: bool
    reloptions: tuple[str, ...]
    is_being_built: bool
    nulls_not_distinct: bool
    definition: str


@dataclass(frozen=True)
class _ConstraintMetadata:
    constraint_type: str
    is_validated: bool
    is_deferrable: bool
    is_initially_deferred: bool
    columns: tuple[str, ...]
    backing_index_name: str | None
    backing_index: _IndexMetadata | None
    definition: str


_INDEX_METADATA_SQL = sa.text(
    """
    SELECT
        index_metadata.indisvalid AS is_valid,
        index_metadata.indisready AS is_ready,
        index_metadata.indislive AS is_live,
        index_metadata.indisunique AS is_unique,
        index_metadata.indisprimary AS is_primary,
        index_metadata.indisexclusion AS is_exclusion,
        index_metadata.indisreplident AS is_replica_identity,
        index_metadata.indisclustered AS is_clustered,
        access_method.amname AS access_method,
        ARRAY(
            SELECT pg_catalog.pg_get_indexdef(
                index_metadata.indexrelid,
                key_position,
                TRUE
            )
            FROM pg_catalog.generate_series(
                1,
                index_metadata.indnkeyatts
            ) AS key_position
            ORDER BY key_position
        ) AS key_expressions,
        ARRAY(
            SELECT operator_class.opcname
            FROM unnest(index_metadata.indclass::oid[])
                WITH ORDINALITY AS index_opclass(
                    operator_class_oid,
                    key_position
                )
            JOIN pg_catalog.pg_opclass AS operator_class
              ON operator_class.oid = index_opclass.operator_class_oid
            WHERE index_opclass.key_position <= index_metadata.indnkeyatts
            ORDER BY index_opclass.key_position
        ) AS key_opclasses,
        ARRAY(
            SELECT operator_class.opcdefault
            FROM unnest(index_metadata.indclass::oid[])
                WITH ORDINALITY AS index_opclass(
                    operator_class_oid,
                    key_position
                )
            JOIN pg_catalog.pg_opclass AS operator_class
              ON operator_class.oid = index_opclass.operator_class_oid
            WHERE index_opclass.key_position <= index_metadata.indnkeyatts
            ORDER BY index_opclass.key_position
        ) AS key_opclasses_are_default,
        ARRAY(
            SELECT
                index_key.collation_oid
                = COALESCE(table_attribute.attcollation, 0::oid)
            FROM unnest(
                index_metadata.indkey::smallint[],
                index_metadata.indcollation::oid[]
            ) WITH ORDINALITY AS index_key(
                attribute_number,
                collation_oid,
                key_position
            )
            LEFT JOIN pg_catalog.pg_attribute AS table_attribute
              ON table_attribute.attrelid = index_metadata.indrelid
             AND table_attribute.attnum = index_key.attribute_number
            WHERE index_key.key_position <= index_metadata.indnkeyatts
            ORDER BY index_key.key_position
        ) AS key_collations_match_columns,
        ARRAY(
            SELECT index_option.option_value
            FROM unnest(index_metadata.indoption::smallint[])
                WITH ORDINALITY AS index_option(option_value, key_position)
            WHERE index_option.key_position <= index_metadata.indnkeyatts
            ORDER BY index_option.key_position
        ) AS key_options,
        index_metadata.indnatts AS attribute_count,
        index_metadata.indnkeyatts AS key_attribute_count,
        pg_catalog.pg_get_expr(
            index_metadata.indpred,
            index_metadata.indrelid,
            TRUE
        ) AS predicate,
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_constraint AS dependent_constraint
            WHERE dependent_constraint.conindid = index_metadata.indexrelid
        ) AS backs_constraint,
        index_relation.reltablespace = 0 AS uses_default_tablespace,
        index_relation.reloptions AS reloptions,
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_stat_progress_create_index AS index_progress
            WHERE index_progress.index_relid = index_metadata.indexrelid
        ) AS is_being_built,
        COALESCE(
            (
                pg_catalog.to_jsonb(index_metadata)
                ->> 'indnullsnotdistinct'
            )::boolean,
            FALSE
        ) AS nulls_not_distinct,
        pg_catalog.pg_get_indexdef(index_metadata.indexrelid) AS definition
    FROM pg_catalog.pg_index AS index_metadata
    JOIN pg_catalog.pg_class AS index_relation
      ON index_relation.oid = index_metadata.indexrelid
    JOIN pg_catalog.pg_class AS table_relation
      ON table_relation.oid = index_metadata.indrelid
    JOIN pg_catalog.pg_am AS access_method
      ON access_method.oid = index_relation.relam
    WHERE table_relation.oid = pg_catalog.to_regclass(:table_name)
      AND index_relation.relnamespace = table_relation.relnamespace
      AND index_relation.relname = :index_name
    """
)

_CONSTRAINT_METADATA_SQL = sa.text(
    """
    SELECT
        constraint_metadata.contype AS constraint_type,
        constraint_metadata.convalidated AS is_validated,
        constraint_metadata.condeferrable AS is_deferrable,
        constraint_metadata.condeferred AS is_initially_deferred,
        ARRAY(
            SELECT table_attribute.attname
            FROM unnest(constraint_metadata.conkey)
                WITH ORDINALITY AS constraint_key(attribute_number, key_position)
            JOIN pg_catalog.pg_attribute AS table_attribute
              ON table_attribute.attrelid = constraint_metadata.conrelid
             AND table_attribute.attnum = constraint_key.attribute_number
            ORDER BY constraint_key.key_position
        ) AS columns,
        backing_index_relation.relname AS backing_index_name,
        backing_index.indisvalid AS backing_index_is_valid,
        backing_index.indisready AS backing_index_is_ready,
        backing_index.indislive AS backing_index_is_live,
        backing_index.indisunique AS backing_index_is_unique,
        backing_index.indisprimary AS backing_index_is_primary,
        backing_index.indisexclusion AS backing_index_is_exclusion,
        backing_index.indisreplident AS backing_index_is_replica_identity,
        backing_index.indisclustered AS backing_index_is_clustered,
        backing_access_method.amname AS backing_index_access_method,
        ARRAY(
            SELECT pg_catalog.pg_get_indexdef(
                backing_index.indexrelid,
                key_position,
                TRUE
            )
            FROM pg_catalog.generate_series(
                1,
                backing_index.indnkeyatts
            ) AS key_position
            ORDER BY key_position
        ) AS backing_index_key_expressions,
        ARRAY(
            SELECT operator_class.opcname
            FROM unnest(backing_index.indclass::oid[])
                WITH ORDINALITY AS index_opclass(
                    operator_class_oid,
                    key_position
                )
            JOIN pg_catalog.pg_opclass AS operator_class
              ON operator_class.oid = index_opclass.operator_class_oid
            WHERE index_opclass.key_position <= backing_index.indnkeyatts
            ORDER BY index_opclass.key_position
        ) AS backing_index_key_opclasses,
        ARRAY(
            SELECT operator_class.opcdefault
            FROM unnest(backing_index.indclass::oid[])
                WITH ORDINALITY AS index_opclass(
                    operator_class_oid,
                    key_position
                )
            JOIN pg_catalog.pg_opclass AS operator_class
              ON operator_class.oid = index_opclass.operator_class_oid
            WHERE index_opclass.key_position <= backing_index.indnkeyatts
            ORDER BY index_opclass.key_position
        ) AS backing_index_key_opclasses_are_default,
        ARRAY(
            SELECT
                index_key.collation_oid
                = COALESCE(table_attribute.attcollation, 0::oid)
            FROM unnest(
                backing_index.indkey::smallint[],
                backing_index.indcollation::oid[]
            ) WITH ORDINALITY AS index_key(
                attribute_number,
                collation_oid,
                key_position
            )
            LEFT JOIN pg_catalog.pg_attribute AS table_attribute
              ON table_attribute.attrelid = backing_index.indrelid
             AND table_attribute.attnum = index_key.attribute_number
            WHERE index_key.key_position <= backing_index.indnkeyatts
            ORDER BY index_key.key_position
        ) AS backing_index_key_collations_match_columns,
        ARRAY(
            SELECT index_option.option_value
            FROM unnest(backing_index.indoption::smallint[])
                WITH ORDINALITY AS index_option(option_value, key_position)
            WHERE index_option.key_position <= backing_index.indnkeyatts
            ORDER BY index_option.key_position
        ) AS backing_index_key_options,
        backing_index.indnatts AS backing_index_attribute_count,
        backing_index.indnkeyatts AS backing_index_key_attribute_count,
        pg_catalog.pg_get_expr(
            backing_index.indpred,
            backing_index.indrelid,
            TRUE
        ) AS backing_index_predicate,
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_constraint AS dependent_constraint
            WHERE dependent_constraint.conindid = backing_index.indexrelid
        ) AS backing_index_backs_constraint,
        backing_index_relation.reltablespace = 0
            AS backing_index_uses_default_tablespace,
        backing_index_relation.reloptions AS backing_index_reloptions,
        EXISTS (
            SELECT 1
            FROM pg_catalog.pg_stat_progress_create_index AS index_progress
            WHERE index_progress.index_relid = backing_index.indexrelid
        ) AS backing_index_is_being_built,
        COALESCE(
            (
                pg_catalog.to_jsonb(backing_index)
                ->> 'indnullsnotdistinct'
            )::boolean,
            FALSE
        ) AS backing_index_nulls_not_distinct,
        pg_catalog.pg_get_indexdef(backing_index.indexrelid)
            AS backing_index_definition,
        pg_catalog.pg_get_constraintdef(
            constraint_metadata.oid,
            TRUE
        ) AS definition
    FROM pg_catalog.pg_constraint AS constraint_metadata
    JOIN pg_catalog.pg_class AS table_relation
      ON table_relation.oid = constraint_metadata.conrelid
    LEFT JOIN pg_catalog.pg_index AS backing_index
      ON backing_index.indexrelid = constraint_metadata.conindid
    LEFT JOIN pg_catalog.pg_class AS backing_index_relation
      ON backing_index_relation.oid = backing_index.indexrelid
    LEFT JOIN pg_catalog.pg_am AS backing_access_method
      ON backing_access_method.oid = backing_index_relation.relam
    WHERE table_relation.oid = pg_catalog.to_regclass(:table_name)
      AND constraint_metadata.conname = :constraint_name
    """
)

_SIMPLE_QUOTED_IDENTIFIER = re.compile(r'^"([a-z_][a-z0-9_]*)"$')
_QUOTED_IDENTIFIERS = re.compile(r'"([a-z_][a-z0-9_]*)"')
_TEXT_CASTS = re.compile(
    r"::\s*(?:pg_catalog\.)?"
    r"(?:text|character\s+varying|varchar)"
    r"(?:\s*\(\s*\d+\s*\))?(?:\s*\[\s*\])?",
    re.IGNORECASE,
)


def _qualified_table_name(
    table_name: str,
    schema: str | None,
) -> str:
    if schema is None:
        return table_name
    return f"{schema}.{table_name}"


def _parameters(
    *,
    object_name: str,
    table_name: str,
    schema: str | None,
    name_key: str,
) -> dict[str, str]:
    return {
        name_key: object_name,
        "table_name": _qualified_table_name(table_name, schema),
    }


def _normalize_key_expression(expression: str) -> str:
    expression = expression.strip()
    simple_identifier = _SIMPLE_QUOTED_IDENTIFIER.fullmatch(expression)
    if simple_identifier is not None:
        return simple_identifier.group(1)
    return expression


def _predicate_fingerprint(predicate: str | None) -> str | None:
    if predicate is None:
        return None
    normalized = predicate.strip().lower()
    normalized = _QUOTED_IDENTIFIERS.sub(r"\1", normalized)
    normalized = _TEXT_CASTS.sub("", normalized)
    normalized = re.sub(r"=\s*any\b", " in ", normalized)
    normalized = re.sub(r"\barray\s*\[", "(", normalized)
    normalized = normalized.replace("]", ")")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.replace("(", "").replace(")", "")


def _definition_fingerprint(definition: str) -> str:
    normalized = definition.strip().lower()
    normalized = _QUOTED_IDENTIFIERS.sub(r"\1", normalized)
    return re.sub(r"\s+", "", normalized)


def _one_mapping(result: Any) -> dict[str, Any] | None:
    row = result.mappings().one_or_none()
    if row is None:
        return None
    return dict(row)


def _index_metadata_from_row(
    row: dict[str, Any],
    *,
    prefix: str = "",
) -> _IndexMetadata | None:
    definition = row.get(f"{prefix}definition")
    if definition is None:
        return None
    return _IndexMetadata(
        is_valid=bool(row[f"{prefix}is_valid"]),
        is_ready=bool(row[f"{prefix}is_ready"]),
        is_live=bool(row[f"{prefix}is_live"]),
        is_unique=bool(row[f"{prefix}is_unique"]),
        is_primary=bool(row[f"{prefix}is_primary"]),
        is_exclusion=bool(row[f"{prefix}is_exclusion"]),
        is_replica_identity=bool(row[f"{prefix}is_replica_identity"]),
        is_clustered=bool(row[f"{prefix}is_clustered"]),
        access_method=str(row[f"{prefix}access_method"]),
        key_expressions=tuple(
            str(item) for item in (row[f"{prefix}key_expressions"] or ())
        ),
        key_opclasses=tuple(
            str(item) for item in (row[f"{prefix}key_opclasses"] or ())
        ),
        key_opclasses_are_default=tuple(
            bool(item) for item in (row[f"{prefix}key_opclasses_are_default"] or ())
        ),
        key_collations_match_columns=tuple(
            bool(item) for item in (row[f"{prefix}key_collations_match_columns"] or ())
        ),
        key_options=tuple(int(item) for item in (row[f"{prefix}key_options"] or ())),
        attribute_count=int(row[f"{prefix}attribute_count"]),
        key_attribute_count=int(row[f"{prefix}key_attribute_count"]),
        predicate=(
            None
            if row[f"{prefix}predicate"] is None
            else str(row[f"{prefix}predicate"])
        ),
        backs_constraint=bool(row[f"{prefix}backs_constraint"]),
        uses_default_tablespace=bool(row[f"{prefix}uses_default_tablespace"]),
        reloptions=tuple(str(item) for item in (row[f"{prefix}reloptions"] or ())),
        is_being_built=bool(row[f"{prefix}is_being_built"]),
        nulls_not_distinct=bool(row[f"{prefix}nulls_not_distinct"]),
        definition=str(definition),
    )


def _read_index_metadata(
    operations: Any,
    spec: PostgresqlIndexSpec,
) -> _IndexMetadata | None:
    row = _one_mapping(
        operations.get_bind().execute(
            _INDEX_METADATA_SQL,
            _parameters(
                object_name=spec.name,
                table_name=spec.table_name,
                schema=spec.schema,
                name_key="index_name",
            ),
        )
    )
    if row is None:
        return None
    return _index_metadata_from_row(row)


def _index_definition_mismatches(
    metadata: _IndexMetadata,
    spec: PostgresqlIndexSpec,
    *,
    expected_backs_constraint: bool = False,
) -> list[str]:
    mismatches: list[str] = []
    expected_columns = tuple(spec.columns)
    actual_columns = tuple(
        _normalize_key_expression(expression) for expression in metadata.key_expressions
    )
    if metadata.is_unique != spec.unique:
        mismatches.append(f"unique={metadata.is_unique!r}, expected {spec.unique!r}")
    if metadata.access_method != "btree":
        mismatches.append(f"access_method={metadata.access_method!r}, expected 'btree'")
    if metadata.attribute_count != len(expected_columns):
        mismatches.append(
            f"attribute_count={metadata.attribute_count}, "
            f"expected {len(expected_columns)}"
        )
    if metadata.key_attribute_count != len(expected_columns):
        mismatches.append(
            f"key_attribute_count={metadata.key_attribute_count}, "
            f"expected {len(expected_columns)}"
        )
    if actual_columns != expected_columns:
        mismatches.append(f"columns={actual_columns!r}, expected {expected_columns!r}")
    if len(metadata.key_opclasses_are_default) != len(expected_columns) or not all(
        metadata.key_opclasses_are_default
    ):
        mismatches.append(
            f"opclasses={metadata.key_opclasses!r}, expected default operator classes"
        )
    if len(metadata.key_collations_match_columns) != len(expected_columns) or not all(
        metadata.key_collations_match_columns
    ):
        mismatches.append("collations do not exactly match the indexed table columns")
    if len(metadata.key_options) != len(expected_columns) or any(
        option != 0 for option in metadata.key_options
    ):
        mismatches.append(
            f"ordering/null options={metadata.key_options!r}, expected all defaults"
        )
    actual_predicate = _predicate_fingerprint(metadata.predicate)
    expected_predicate = _predicate_fingerprint(spec.predicate)
    if actual_predicate != expected_predicate:
        mismatches.append(
            f"predicate={metadata.predicate!r}, expected {spec.predicate!r}"
        )
    if metadata.is_primary:
        mismatches.append("index backs a primary key")
    if metadata.is_exclusion:
        mismatches.append("index backs an exclusion constraint")
    if metadata.is_replica_identity:
        mismatches.append("index is the replica identity")
    if metadata.is_clustered:
        mismatches.append("index is the table clustering index")
    if metadata.backs_constraint != expected_backs_constraint:
        mismatches.append(
            f"backs_constraint={metadata.backs_constraint!r}, "
            f"expected {expected_backs_constraint!r}"
        )
    if not metadata.uses_default_tablespace:
        mismatches.append("index uses a non-default tablespace")
    if metadata.reloptions:
        mismatches.append(f"index reloptions are set: {metadata.reloptions!r}")
    if metadata.nulls_not_distinct:
        mismatches.append("index uses NULLS NOT DISTINCT")
    return mismatches


def _raise_incompatible_index(
    spec: PostgresqlIndexSpec,
    metadata: _IndexMetadata,
    mismatches: list[str],
) -> None:
    detail = "; ".join(mismatches)
    raise RuntimeError(
        f"refusing to replace PostgreSQL index {spec.name!r} on "
        f"{spec.table_name!r}: existing definition is incompatible "
        f"({detail}); existing SQL: {metadata.definition}"
    )


def _matching_index_metadata(
    operations: Any,
    spec: PostgresqlIndexSpec,
) -> _IndexMetadata | None:
    metadata = _read_index_metadata(operations, spec)
    if metadata is None:
        return None
    mismatches = _index_definition_mismatches(metadata, spec)
    if mismatches:
        _raise_incompatible_index(spec, metadata, mismatches)
    return metadata


def _index_is_valid(metadata: _IndexMetadata) -> bool:
    return metadata.is_valid and metadata.is_ready and metadata.is_live


def _create_index(operations: Any, spec: PostgresqlIndexSpec) -> None:
    kwargs: dict[str, Any] = {
        "unique": spec.unique,
        "postgresql_concurrently": True,
    }
    if spec.schema is not None:
        kwargs["schema"] = spec.schema
    if spec.predicate is not None:
        kwargs["postgresql_where"] = sa.text(spec.predicate)
    operations.create_index(
        spec.name,
        spec.table_name,
        list(spec.columns),
        **kwargs,
    )


def _drop_index(operations: Any, spec: PostgresqlIndexSpec) -> None:
    operations.drop_index(
        spec.name,
        table_name=spec.table_name,
        schema=spec.schema,
        postgresql_concurrently=True,
        if_exists=True,
    )


def _read_after_unknown_ack(
    operations: Any,
    spec: PostgresqlIndexSpec,
    original_error: Exception,
) -> _IndexMetadata | None:
    try:
        return _matching_index_metadata(operations, spec)
    except RuntimeError:
        raise
    except Exception:
        raise original_error


def _verify_created_index(
    operations: Any,
    spec: PostgresqlIndexSpec,
) -> None:
    metadata = _matching_index_metadata(operations, spec)
    if metadata is None:
        raise RuntimeError(
            f"PostgreSQL index {spec.name!r} was not present after creation"
        )
    if not _index_is_valid(metadata):
        raise RuntimeError(
            f"PostgreSQL index {spec.name!r} remained invalid after creation"
        )


def ensure_postgresql_index(
    operations: Any,
    spec: PostgresqlIndexSpec,
) -> None:
    """Create a matching concurrent index, repairing only matching invalid state."""

    context = operations.get_context()
    with context.autocommit_block():
        if context.as_sql:
            _create_index(operations, spec)
            return

        metadata = _matching_index_metadata(operations, spec)
        if metadata is not None and _index_is_valid(metadata):
            return

        if metadata is not None:
            if metadata.is_being_built:
                raise RuntimeError(
                    f"refusing to drop PostgreSQL index {spec.name!r}: "
                    "a concurrent index build is still active"
                )
            try:
                _drop_index(operations, spec)
            except Exception as drop_error:
                remaining = _read_after_unknown_ack(
                    operations,
                    spec,
                    drop_error,
                )
                if remaining is not None and _index_is_valid(remaining):
                    return
                if remaining is not None:
                    raise
            else:
                remaining = _matching_index_metadata(operations, spec)
                if remaining is not None and _index_is_valid(remaining):
                    return
                if remaining is not None:
                    raise RuntimeError(
                        f"PostgreSQL index {spec.name!r} still exists after drop"
                    )

        try:
            _create_index(operations, spec)
        except Exception as create_error:
            created = _read_after_unknown_ack(
                operations,
                spec,
                create_error,
            )
            if created is not None and _index_is_valid(created):
                return
            raise
        _verify_created_index(operations, spec)


def drop_postgresql_index(
    operations: Any,
    spec: PostgresqlIndexSpec,
) -> None:
    """Drop only the expected concurrent index; absence is already success."""

    context = operations.get_context()
    with context.autocommit_block():
        if context.as_sql:
            _drop_index(operations, spec)
            return

        metadata = _matching_index_metadata(operations, spec)
        if metadata is None:
            return
        if metadata.is_being_built and not _index_is_valid(metadata):
            raise RuntimeError(
                f"refusing to drop PostgreSQL index {spec.name!r}: "
                "a concurrent index build is still active"
            )
        try:
            _drop_index(operations, spec)
        except Exception as drop_error:
            remaining = _read_after_unknown_ack(
                operations,
                spec,
                drop_error,
            )
            if remaining is None:
                return
            raise
        remaining = _matching_index_metadata(operations, spec)
        if remaining is not None:
            raise RuntimeError(
                f"PostgreSQL index {spec.name!r} still exists after drop"
            )


def _read_constraint_metadata(
    operations: Any,
    spec: PostgresqlUniqueConstraintSpec,
) -> _ConstraintMetadata | None:
    row = _one_mapping(
        operations.get_bind().execute(
            _CONSTRAINT_METADATA_SQL,
            _parameters(
                object_name=spec.name,
                table_name=spec.table_name,
                schema=spec.schema,
                name_key="constraint_name",
            ),
        )
    )
    if row is None:
        return None
    return _ConstraintMetadata(
        constraint_type=str(row["constraint_type"]),
        is_validated=bool(row["is_validated"]),
        is_deferrable=bool(row["is_deferrable"]),
        is_initially_deferred=bool(row["is_initially_deferred"]),
        columns=tuple(str(item) for item in row["columns"]),
        backing_index_name=(
            None
            if row["backing_index_name"] is None
            else str(row["backing_index_name"])
        ),
        backing_index=_index_metadata_from_row(
            row,
            prefix="backing_index_",
        ),
        definition=str(row["definition"]),
    )


def _matching_constraint_metadata(
    operations: Any,
    spec: PostgresqlUniqueConstraintSpec,
) -> _ConstraintMetadata | None:
    metadata = _read_constraint_metadata(operations, spec)
    if metadata is None:
        return None
    mismatches: list[str] = []
    if metadata.constraint_type != "u":
        mismatches.append(
            f"type={metadata.constraint_type!r}, expected unique constraint"
        )
    if metadata.columns != spec.columns:
        mismatches.append(f"columns={metadata.columns!r}, expected {spec.columns!r}")
    if not metadata.is_validated:
        mismatches.append("constraint is not validated")
    if metadata.is_deferrable:
        mismatches.append("constraint is deferrable")
    if metadata.is_initially_deferred:
        mismatches.append("constraint is initially deferred")
    expected_definition = f"UNIQUE ({', '.join(spec.columns)})"
    if _definition_fingerprint(metadata.definition) != _definition_fingerprint(
        expected_definition
    ):
        mismatches.append(
            f"definition={metadata.definition!r}, expected {expected_definition!r}"
        )
    backing_index = metadata.backing_index
    if backing_index is None:
        mismatches.append("constraint has no readable backing index")
    else:
        if metadata.backing_index_name != spec.name:
            mismatches.append(
                f"backing index name={metadata.backing_index_name!r}, "
                f"expected {spec.name!r}"
            )
        backing_spec = PostgresqlIndexSpec(
            name=spec.name,
            table_name=spec.table_name,
            columns=spec.columns,
            unique=True,
            schema=spec.schema,
        )
        mismatches.extend(
            f"backing index {mismatch}"
            for mismatch in _index_definition_mismatches(
                backing_index,
                backing_spec,
                expected_backs_constraint=True,
            )
        )
        if not _index_is_valid(backing_index):
            mismatches.append("constraint backing index is not valid, ready, and live")
    if mismatches:
        detail = "; ".join(mismatches)
        raise RuntimeError(
            f"refusing to replace PostgreSQL constraint {spec.name!r} on "
            f"{spec.table_name!r}: existing definition is incompatible "
            f"({detail}); existing SQL: {metadata.definition}"
        )
    return metadata


def _drop_unique_constraint(
    operations: Any,
    spec: PostgresqlUniqueConstraintSpec,
) -> None:
    operations.drop_constraint(
        spec.name,
        spec.table_name,
        type_="unique",
        schema=spec.schema,
        if_exists=True,
    )


def ensure_postgresql_unique_constraint(
    operations: Any,
    spec: PostgresqlUniqueConstraintSpec,
) -> None:
    """Create the expected unique constraint; matching existing state is success."""

    context = operations.get_context()
    if context.as_sql:
        operations.create_unique_constraint(
            spec.name,
            spec.table_name,
            list(spec.columns),
            schema=spec.schema,
        )
        return
    if _matching_constraint_metadata(operations, spec) is not None:
        return
    try:
        operations.create_unique_constraint(
            spec.name,
            spec.table_name,
            list(spec.columns),
            schema=spec.schema,
        )
    except Exception:
        if _matching_constraint_metadata(operations, spec) is not None:
            return
        raise
    if _matching_constraint_metadata(operations, spec) is None:
        raise RuntimeError(
            f"PostgreSQL constraint {spec.name!r} was not present after creation"
        )


def drop_postgresql_unique_constraint(
    operations: Any,
    spec: PostgresqlUniqueConstraintSpec,
) -> None:
    context = operations.get_context()
    if context.as_sql:
        _drop_unique_constraint(operations, spec)
        return
    if _matching_constraint_metadata(operations, spec) is None:
        return
    try:
        _drop_unique_constraint(operations, spec)
    except Exception:
        if _matching_constraint_metadata(operations, spec) is None:
            return
        raise
    if _matching_constraint_metadata(operations, spec) is not None:
        raise RuntimeError(
            f"PostgreSQL constraint {spec.name!r} still exists after drop"
        )
