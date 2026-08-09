"""Fail-closed guards and durable exports for destructive downgrades."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any
import uuid

import sqlalchemy as sa


TELEGRAM_CONTROL_REVISION = "0060_telegram_delivery_control"
TELEGRAM_EFFECT_REVISION = "0062_tg_control_effect_fence"
STORAGE_RETRY_REVISION = "0063_storage_apply_retry_fence"
DESTRUCTIVE_EXPORT_REVISIONS = frozenset(
    {
        TELEGRAM_CONTROL_REVISION,
        TELEGRAM_EFFECT_REVISION,
        STORAGE_RETRY_REVISION,
    }
)
EXPORT_SCHEMA = "lumen.migration-export"
EXPORT_VERSION = 2

_EXPORT_PATH_ENV = "LUMEN_MIGRATION_EXPORT_PATH"
_MANIFEST_PATH_ENV = "LUMEN_MIGRATION_EXPORT_MANIFEST_PATH"
_REQUEST_ID_ENV = "LUMEN_MIGRATION_EXPORT_REQUEST_ID"
_EXPORT_TABLES = (
    "telegram_control_commands",
    "telegram_delivery_attempts",
    "telegram_delivery_quarantines",
    "storage_apply_operations",
)
_FULL_TABLE_RESTORE_BY_REVISION = {
    TELEGRAM_CONTROL_REVISION: frozenset(
        {
            "telegram_control_commands",
            "telegram_delivery_attempts",
            "telegram_delivery_quarantines",
        }
    ),
}
_PARTIAL_RESTORE_COLUMNS_BY_REVISION = {
    TELEGRAM_EFFECT_REVISION: {
        "telegram_control_commands": frozenset(
            {
                "effect_status",
                "effect_owner",
                "effect_lease_until",
                "effect_fence",
                "effect_attempts",
                "effect_completed_at",
                "effect_error",
            }
        ),
    },
    STORAGE_RETRY_REVISION: {
        "storage_apply_operations": frozenset(
            {
                "next_attempt_at",
                "failure_class",
            }
        ),
    },
}


@dataclass(frozen=True)
class MigrationExportSession:
    path: Path
    request_id: str
    legacy_path: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _encode_value(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value.hex()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, uuid.UUID):
        return {"type": "uuid", "value": str(value)}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"type": "time", "value": value.isoformat()}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "type": "bytes",
            "value": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, Mapping):
        encoded_items = [
            [_encode_value(key), _encode_value(item)] for key, item in value.items()
        ]
        encoded_items.sort(key=lambda item: _canonical_json(item[0]))
        return {"type": "mapping", "value": encoded_items}
    if isinstance(value, Sequence):
        return {
            "type": "sequence",
            "value": [_encode_value(item) for item in value],
        }
    raise TypeError(
        "migration export cannot preserve value type "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _require_dict(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def _decode_value(raw: object) -> object:
    value = _require_dict(raw, label="typed migration value")
    kind = value.get("type")
    encoded = value.get("value")
    if kind == "null":
        if set(value) != {"type"}:
            raise RuntimeError("null migration value has unexpected fields")
        return None
    if kind == "bool" and isinstance(encoded, bool):
        return encoded
    if kind == "int" and isinstance(encoded, str):
        return int(encoded)
    if kind == "float" and isinstance(encoded, str):
        return float.fromhex(encoded)
    if kind == "decimal" and isinstance(encoded, str):
        return Decimal(encoded)
    if kind == "uuid" and isinstance(encoded, str):
        return uuid.UUID(encoded)
    if kind == "datetime" and isinstance(encoded, str):
        return datetime.fromisoformat(encoded)
    if kind == "date" and isinstance(encoded, str):
        return date.fromisoformat(encoded)
    if kind == "time" and isinstance(encoded, str):
        return time.fromisoformat(encoded)
    if kind == "str" and isinstance(encoded, str):
        return encoded
    if kind == "bytes" and isinstance(encoded, str):
        return base64.b64decode(encoded, validate=True)
    if kind == "mapping" and isinstance(encoded, list):
        decoded: dict[object, object] = {}
        for item in encoded:
            if not isinstance(item, list) or len(item) != 2:
                raise RuntimeError("typed mapping entry must contain key and value")
            key = _decode_value(item[0])
            try:
                decoded[key] = _decode_value(item[1])
            except TypeError as exc:
                raise RuntimeError("typed mapping key is not hashable") from exc
        return decoded
    if kind == "sequence" and isinstance(encoded, list):
        return [_decode_value(item) for item in encoded]
    raise RuntimeError(f"unsupported or malformed migration value type: {kind!r}")


def _current_uid() -> int | None:
    return os.getuid() if hasattr(os, "getuid") else None


def _current_gid() -> int | None:
    return os.getgid() if hasattr(os, "getgid") else None


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _validate_export_parent(parent: Path, *, create: bool) -> None:
    parent = _absolute_path(parent)
    if create and not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    try:
        resolved = parent.resolve(strict=True)
        metadata = os.lstat(parent)
    except OSError as exc:
        raise RuntimeError(
            f"migration export directory is unavailable: {parent}"
        ) from exc
    if resolved != parent:
        raise RuntimeError(
            f"migration export directory must not contain symlinks: {parent}"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"migration export parent is not a directory: {parent}")
    uid = _current_uid()
    if uid is not None and metadata.st_uid != uid:
        raise RuntimeError(
            f"migration export directory must be owned by uid {uid}: {parent}"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError(f"migration export directory must have mode 0700: {parent}")


def _open_directory(parent: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(parent, flags)


def _validate_export_file(path: Path) -> os.stat_result:
    path = _absolute_path(path)
    _validate_export_parent(path.parent, create=False)
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"migration export file is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"migration export must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise RuntimeError(f"migration export must not have hard links: {path}")
    uid = _current_uid()
    if uid is not None and metadata.st_uid != uid:
        raise RuntimeError(f"migration export must be owned by uid {uid}: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError(f"migration export must have mode 0600: {path}")
    return metadata


def _read_export_bytes(path: Path) -> bytes:
    path = _absolute_path(path)
    expected = _validate_export_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError("migration export changed during secure open")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _write_descriptor(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while persisting migration export")
        view = view[written:]
    os.fsync(descriptor)


def _create_secure_file(path: Path, payload: bytes) -> None:
    path = _absolute_path(path)
    _validate_export_parent(path.parent, create=True)
    request_suffix = uuid.uuid4().hex
    temporary = path.with_name(f".{path.name}.{request_suffix}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        _write_descriptor(descriptor, payload)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        directory_fd = _open_directory(path.parent)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    _validate_export_file(path)


def _replace_secure_file(path: Path, payload: bytes) -> None:
    path = _absolute_path(path)
    _validate_export_file(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        _write_descriptor(descriptor, payload)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory_fd = _open_directory(path.parent)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    _validate_export_file(path)


def _restore_columns(
    table: sa.Table,
    table_name: str,
    *,
    pending_revision_ids: set[str],
) -> list[str]:
    for revision, table_names in _FULL_TABLE_RESTORE_BY_REVISION.items():
        if revision in pending_revision_ids and table_name in table_names:
            return [column.name for column in table.columns]

    selected: set[str] = set()
    for revision, table_columns in _PARTIAL_RESTORE_COLUMNS_BY_REVISION.items():
        if revision in pending_revision_ids:
            selected.update(table_columns.get(table_name, ()))
    return [column.name for column in table.columns if column.name in selected]


def _table_payload(
    bind: Any,
    table_name: str,
    *,
    pending_revision_ids: set[str],
) -> dict[str, object] | None:
    table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    restore_columns = _restore_columns(
        table,
        table_name,
        pending_revision_ids=pending_revision_ids,
    )
    if not restore_columns:
        return None
    columns = [
        {
            "name": column.name,
            "sql_type": column.type.compile(dialect=bind.dialect),
            "nullable": bool(column.nullable),
        }
        for column in table.columns
    ]
    statement = sa.select(table)
    primary_key = list(table.primary_key.columns)
    if not primary_key:
        raise RuntimeError(
            f"migration export table requires a primary key: {table_name}"
        )
    statement = statement.order_by(*primary_key)
    rows = bind.execute(statement).mappings().all()
    encoded_rows = [
        [_encode_value(row[column.name]) for column in table.columns] for row in rows
    ]
    table_digest_payload = {"columns": columns, "rows": encoded_rows}
    return {
        "columns": columns,
        "primary_key": [column.name for column in primary_key],
        "restore_columns": restore_columns,
        "row_count": len(encoded_rows),
        "sha256": _sha256(table_digest_payload),
        "rows": encoded_rows,
    }


def _manifest_digest_payload(manifest: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in manifest.items() if key != "payload_sha256"}


def _build_manifest(
    bind: Any,
    *,
    request_id: str,
    source_revision: str,
    target_revision: str,
    pending_revision_ids: set[str],
) -> dict[str, object]:
    inspector = sa.inspect(bind)
    tables: dict[str, object] = {}
    for table_name in _EXPORT_TABLES:
        if not inspector.has_table(table_name):
            continue
        payload = _table_payload(
            bind,
            table_name,
            pending_revision_ids=pending_revision_ids,
        )
        if payload is not None:
            tables[table_name] = payload
    manifest: dict[str, object] = {
        "schema": EXPORT_SCHEMA,
        "version": EXPORT_VERSION,
        "status": "pending",
        "request_id": request_id,
        "created_at": _utc_now(),
        "source_revision": source_revision,
        "target_revision": target_revision,
        "pending_revisions": sorted(pending_revision_ids),
        "owner": {"uid": _current_uid(), "gid": _current_gid()},
        "tables": tables,
    }
    manifest["payload_sha256"] = _sha256(_manifest_digest_payload(manifest))
    return manifest


def _validate_manifest_table(
    table_name: str,
    raw_table: object,
) -> None:
    table = _require_dict(raw_table, label=f"manifest table {table_name}")
    columns = table.get("columns")
    primary_key = table.get("primary_key")
    restore_columns = table.get("restore_columns")
    rows = table.get("rows")
    row_count = table.get("row_count")
    digest = table.get("sha256")
    if not isinstance(columns, list) or not columns:
        raise RuntimeError(f"manifest table {table_name} has no columns")
    column_names: list[str] = []
    for raw_column in columns:
        column = _require_dict(
            raw_column,
            label=f"manifest column in {table_name}",
        )
        name = column.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"manifest table {table_name} has invalid column")
        column_names.append(name)
    if len(column_names) != len(set(column_names)):
        raise RuntimeError(f"manifest table {table_name} has duplicate columns")
    if (
        not isinstance(primary_key, list)
        or not primary_key
        or not all(isinstance(item, str) and item for item in primary_key)
        or len(primary_key) != len(set(primary_key))
        or not set(primary_key).issubset(column_names)
    ):
        raise RuntimeError(f"manifest table {table_name} has invalid primary key")
    if (
        not isinstance(restore_columns, list)
        or not restore_columns
        or not all(isinstance(item, str) and item for item in restore_columns)
        or len(restore_columns) != len(set(restore_columns))
        or not set(restore_columns).issubset(column_names)
    ):
        raise RuntimeError(f"manifest table {table_name} has invalid restore columns")
    if not isinstance(rows, list):
        raise RuntimeError(f"manifest table {table_name} rows must be a list")
    if row_count != len(rows):
        raise RuntimeError(f"manifest table {table_name} row count mismatch")
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise RuntimeError(f"manifest table {table_name} has malformed row")
        for value in row:
            _decode_value(value)
    primary_key_indexes = [column_names.index(item) for item in primary_key]
    encoded_primary_keys = [
        _canonical_json([row[index] for index in primary_key_indexes]) for row in rows
    ]
    if len(encoded_primary_keys) != len(set(encoded_primary_keys)):
        raise RuntimeError(f"manifest table {table_name} has duplicate primary keys")
    expected_digest = _sha256({"columns": columns, "rows": rows})
    if digest != expected_digest:
        raise RuntimeError(f"manifest table {table_name} digest mismatch")


def verify_migration_export(
    path: Path,
    *,
    expected_status: str | None = None,
) -> dict[str, object]:
    """Validate file security, schema, row counts, types, and digests."""
    try:
        raw = json.loads(_read_export_bytes(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid migration export JSON: {path}") from exc
    manifest = _require_dict(raw, label="migration export")
    if manifest.get("schema") != EXPORT_SCHEMA:
        raise RuntimeError("unsupported migration export schema")
    if manifest.get("version") != EXPORT_VERSION:
        raise RuntimeError("unsupported migration export version")
    status_value = manifest.get("status")
    if status_value not in {"pending", "committed"}:
        raise RuntimeError("migration export has invalid status")
    if expected_status is not None and status_value != expected_status:
        raise RuntimeError(
            f"migration export status is {status_value!r}, expected {expected_status!r}"
        )
    for field in ("request_id", "source_revision", "target_revision"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise RuntimeError(f"migration export has invalid {field}")
    pending_revisions = manifest.get("pending_revisions")
    if not isinstance(pending_revisions, list) or not all(
        isinstance(item, str) and item for item in pending_revisions
    ):
        raise RuntimeError("migration export has invalid pending revisions")
    owner = _require_dict(manifest.get("owner"), label="migration export owner")
    uid = _current_uid()
    gid = _current_gid()
    if uid is not None and owner.get("uid") != uid:
        raise RuntimeError("migration export owner uid does not match current user")
    if gid is not None and owner.get("gid") != gid:
        raise RuntimeError("migration export owner gid does not match current group")
    tables = _require_dict(manifest.get("tables"), label="migration export tables")
    for table_name, table in tables.items():
        if not isinstance(table_name, str) or table_name not in _EXPORT_TABLES:
            raise RuntimeError(f"migration export has unsupported table {table_name!r}")
        _validate_manifest_table(table_name, table)
    expected_digest = _sha256(_manifest_digest_payload(manifest))
    if manifest.get("payload_sha256") != expected_digest:
        raise RuntimeError("migration export payload digest mismatch")
    return manifest


def _guard_telegram_state(
    bind: Any,
    *,
    pending_revision_ids: set[str],
) -> None:
    inspector = sa.inspect(bind)
    dropping_control = TELEGRAM_CONTROL_REVISION in pending_revision_ids
    dropping_effect = TELEGRAM_EFFECT_REVISION in pending_revision_ids
    if dropping_control:
        unresolved = bind.execute(
            sa.text(
                "SELECT count(*) FROM telegram_delivery_quarantines "
                "WHERE status <> 'resolved'"
            )
        ).scalar_one()
        if unresolved:
            raise RuntimeError(
                "refusing downgrade with unresolved Telegram quarantines; "
                "resolve them before rollback"
            )

        active_commands = bind.execute(
            sa.text(
                "SELECT count(*) FROM telegram_control_commands "
                "WHERE status IN ('pending','published')"
            )
        ).scalar_one()
        if active_commands:
            raise RuntimeError("refusing downgrade with active Telegram commands")

        uncertain_deliveries = bind.execute(
            sa.text(
                "SELECT count(*) FROM telegram_delivery_attempts "
                "WHERE state IN ('dispatching','delivery_result_unknown')"
            )
        ).scalar_one()
        if uncertain_deliveries:
            raise RuntimeError("refusing downgrade with uncertain Telegram deliveries")

    if dropping_effect and inspector.has_table("telegram_control_commands"):
        command_columns = {
            column["name"]
            for column in inspector.get_columns("telegram_control_commands")
        }
        if "effect_status" in command_columns:
            active_effects = bind.execute(
                sa.text(
                    "SELECT count(*) FROM telegram_control_commands "
                    "WHERE status IN ('pending','published') "
                    "AND effect_status IN ('pending','running')"
                )
            ).scalar_one()
            if active_effects:
                raise RuntimeError(
                    "refusing downgrade with active Telegram control effects"
                )
            invalid_terminal_effects = bind.execute(
                sa.text(
                    "SELECT count(*) FROM telegram_control_commands "
                    "WHERE status IN ('accepted','failed') "
                    "AND effect_status IN ('pending','running')"
                )
            ).scalar_one()
            if invalid_terminal_effects:
                raise RuntimeError(
                    "refusing downgrade with non-terminal effects on terminal "
                    "Telegram commands"
                )


def guard_telegram_downgrade(
    bind: Any,
    *,
    pending_revision_ids: set[str],
    source_revision: str | None = None,
    target_revision: str | None = None,
) -> MigrationExportSession | None:
    """Validate destructive state and create a uniquely identified export."""
    destructive_revisions = pending_revision_ids & DESTRUCTIVE_EXPORT_REVISIONS
    if not destructive_revisions:
        return None

    target_raw = os.environ.get(_EXPORT_PATH_ENV, "").strip()
    if not target_raw:
        raise RuntimeError(
            "destructive downgrade requires the explicit export command; "
            f"set {_EXPORT_PATH_ENV}"
        )
    if not source_revision or not target_revision or target_revision == "unknown":
        raise RuntimeError(
            "destructive downgrade export requires explicit source and target revisions"
        )

    _guard_telegram_state(bind, pending_revision_ids=pending_revision_ids)
    target = _absolute_path(Path(target_raw))
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"refusing to overwrite existing migration export: {target}")
    request_id = uuid.uuid4().hex
    manifest = _build_manifest(
        bind,
        request_id=request_id,
        source_revision=source_revision,
        target_revision=target_revision,
        pending_revision_ids=pending_revision_ids,
    )
    _create_secure_file(target, _canonical_json(manifest) + b"\n")
    legacy_path = target.with_name(f".{target.name}.{request_id}.legacy.json")
    return MigrationExportSession(
        path=target,
        request_id=request_id,
        legacy_path=legacy_path,
    )


def require_prepared_downgrade_export(revision: str) -> None:
    """Require the env-level export before a destructive revision runs."""
    path_raw = os.environ.get(_MANIFEST_PATH_ENV, "").strip()
    request_id = os.environ.get(_REQUEST_ID_ENV, "").strip()
    if not path_raw or not request_id:
        raise RuntimeError(
            "destructive downgrade must run through the Alembic environment "
            "export guard"
        )
    manifest = verify_migration_export(
        Path(path_raw),
        expected_status="pending",
    )
    if manifest["request_id"] != request_id:
        raise RuntimeError("migration export request identity mismatch")
    if revision not in manifest["pending_revisions"]:
        raise RuntimeError(
            f"migration export does not cover destructive revision {revision}"
        )


def _cleanup_legacy_export(path: Path) -> None:
    path.unlink(missing_ok=True)
    for temporary in path.parent.glob(f".{path.name}.*.tmp"):
        temporary.unlink(missing_ok=True)


@contextmanager
def migration_export_environment(
    session: MigrationExportSession | None,
) -> Iterator[None]:
    """Isolate legacy per-revision exporters from the durable manifest."""
    if session is None:
        yield
        return
    previous = {
        _EXPORT_PATH_ENV: os.environ.get(_EXPORT_PATH_ENV),
        _MANIFEST_PATH_ENV: os.environ.get(_MANIFEST_PATH_ENV),
        _REQUEST_ID_ENV: os.environ.get(_REQUEST_ID_ENV),
    }
    os.environ[_EXPORT_PATH_ENV] = str(session.legacy_path)
    os.environ[_MANIFEST_PATH_ENV] = str(session.path)
    os.environ[_REQUEST_ID_ENV] = session.request_id
    previous_umask = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous_umask)
        _cleanup_legacy_export(session.legacy_path)
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def commit_migration_export(session: MigrationExportSession) -> None:
    manifest = verify_migration_export(
        session.path,
        expected_status="pending",
    )
    if manifest["request_id"] != session.request_id:
        raise RuntimeError("migration export request identity mismatch")
    manifest["status"] = "committed"
    manifest["committed_at"] = _utc_now()
    manifest.pop("failure", None)
    manifest["payload_sha256"] = _sha256(_manifest_digest_payload(manifest))
    _replace_secure_file(session.path, _canonical_json(manifest) + b"\n")
    verify_migration_export(session.path, expected_status="committed")


def mark_migration_export_failed(
    session: MigrationExportSession,
    error: BaseException,
) -> None:
    manifest = verify_migration_export(
        session.path,
        expected_status="pending",
    )
    if manifest["request_id"] != session.request_id:
        raise RuntimeError("migration export request identity mismatch")
    manifest["failure"] = {
        "at": _utc_now(),
        "type": type(error).__name__,
        "message": str(error)[:2000],
    }
    manifest["payload_sha256"] = _sha256(_manifest_digest_payload(manifest))
    _replace_secure_file(session.path, _canonical_json(manifest) + b"\n")


def import_migration_export(
    bind: Any,
    path: Path,
    *,
    verify_only: bool = False,
    allow_pending: bool = False,
) -> dict[str, int]:
    """Verify or transactionally merge an export by primary key."""
    manifest = verify_migration_export(
        path,
        expected_status=None if allow_pending else "committed",
    )
    if allow_pending:
        if manifest.get("failure") is not None:
            raise RuntimeError("failed pending migration export cannot be imported")
        current_revision = _current_alembic_revision(bind)
        target_revision = str(manifest["target_revision"])
        if current_revision != target_revision:
            raise RuntimeError(
                "pending migration export target revision mismatch: "
                f"current={current_revision!r} target={target_revision!r}"
            )
    tables = _require_dict(manifest["tables"], label="migration export tables")
    imported: dict[str, int] = {}
    inspector = sa.inspect(bind)
    for table_name, raw_table in tables.items():
        table_payload = _require_dict(
            raw_table,
            label=f"manifest table {table_name}",
        )
        if not inspector.has_table(table_name):
            raise RuntimeError(
                f"cannot import migration export; table is missing: {table_name}"
            )
        table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
        raw_columns = table_payload["columns"]
        assert isinstance(raw_columns, list)
        column_names = [
            _require_dict(item, label="manifest column")["name"] for item in raw_columns
        ]
        if not all(isinstance(name, str) for name in column_names):
            raise RuntimeError(f"manifest table {table_name} has invalid columns")
        primary_key = table_payload["primary_key"]
        restore_columns = table_payload["restore_columns"]
        assert isinstance(primary_key, list)
        assert isinstance(restore_columns, list)
        primary_key_names = [str(item) for item in primary_key]
        restore_column_names = [str(item) for item in restore_columns]
        missing_columns = set(column_names) - set(table.columns.keys())
        if missing_columns:
            raise RuntimeError(
                f"cannot import {table_name}; missing columns: "
                f"{sorted(missing_columns)}"
            )
        target_types = {
            column.name: column.type.compile(dialect=bind.dialect)
            for column in table.columns
        }
        for raw_column in raw_columns:
            column = _require_dict(raw_column, label="manifest column")
            column_name = column["name"]
            assert isinstance(column_name, str)
            if column.get("sql_type") != target_types[column_name]:
                raise RuntimeError(
                    f"cannot import {table_name}.{column_name}; SQL type "
                    "does not match the export manifest"
                )
        target_primary_key = [column.name for column in table.primary_key.columns]
        if target_primary_key != primary_key_names:
            raise RuntimeError(
                f"cannot import {table_name}; primary key does not match "
                "the export manifest"
            )
        raw_rows = table_payload["rows"]
        assert isinstance(raw_rows, list)
        decoded_rows = [
            {
                column_name: _decode_value(raw_value)
                for column_name, raw_value in zip(
                    column_names,
                    raw_row,
                    strict=True,
                )
            }
            for raw_row in raw_rows
        ]
        if not verify_only:
            for row in decoded_rows:
                identity = sa.and_(
                    *(
                        table.c[column_name] == row[column_name]
                        for column_name in primary_key_names
                    )
                )
                existing = (
                    bind.execute(sa.select(*table.columns).where(identity))
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    bind.execute(table.insert().values(**row))
                    continue
                updates = {
                    column_name: row[column_name]
                    for column_name in restore_column_names
                    if column_name not in primary_key_names
                }
                if updates:
                    bind.execute(table.update().where(identity).values(**updates))
            restored_rows = sum(
                int(
                    bind.execute(
                        sa.select(
                            sa.exists().where(
                                sa.and_(
                                    *(
                                        table.c[column_name] == row[column_name]
                                        for column_name in primary_key_names
                                    )
                                )
                            )
                        )
                    ).scalar_one()
                )
                for row in decoded_rows
            )
            if restored_rows != len(decoded_rows):
                raise RuntimeError(f"import row count mismatch for table {table_name}")
        imported[table_name] = len(decoded_rows)
    return imported


def _current_alembic_revision(bind: Any) -> str:
    inspector = sa.inspect(bind)
    if not inspector.has_table("alembic_version"):
        raise RuntimeError("current Alembic revision is unavailable")
    revisions = list(
        bind.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
    )
    if len(revisions) != 1 or not revisions[0]:
        raise RuntimeError(
            "pending migration export requires exactly one current Alembic revision"
        )
    return str(revisions[0])
