#!/usr/bin/env python3
"""Convert a Lumen Docker PostgreSQL export into a desktop SQLite database.

The converter intentionally imports only the desktop data surface. SaaS-only
tables such as billing, wallets, invites, BYOK templates, and Telegram state are
ignored. A selected Docker user is rewritten to the desktop local-user identity.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


LOCAL_USER_ID = "local-user"
LOCAL_USER_EMAIL = "local@lumen.desktop"
DEFAULT_SCOPE_ID = "default-memory-scope"

DESKTOP_TABLE_ORDER = [
    "users",
    "system_prompts",
    "user_memory_scopes",
    "conversations",
    "messages",
    "user_memories",
    "images",
    "image_variants",
    "generations",
    "completions",
    "shares",
]

DELETE_ORDER = [
    "shares",
    "completions",
    "generations",
    "image_variants",
    "images",
    "user_memories",
    "messages",
    "conversations",
    "user_memory_scopes",
    "system_prompts",
    "users",
]

JSON_COLUMNS = {
    "users": {"oauth_providers"},
    "conversations": {"default_params", "summary_jsonb"},
    "messages": {"content"},
    "user_memories": set(),
    "images": {"metadata_jsonb"},
    "generations": {"input_image_ids", "upstream_request"},
    "completions": {"input_image_ids", "upstream_request"},
    "shares": {"image_ids"},
}

LIST_COLUMNS = {
    "generations": {"input_image_ids"},
    "completions": {"input_image_ids"},
    "shares": {"image_ids"},
}

BOOL_COLUMNS = {
    "users": {
        "email_verified",
        "notification_email",
        "memory_paused",
        "memory_disabled",
        "confirmation_enabled",
    },
    "conversations": {"pinned", "archived", "memory_disabled"},
    "user_memory_scopes": {"is_default"},
    "user_memories": {"pinned", "disabled"},
    "shares": {"show_prompt"},
}


@dataclass
class CopyTable:
    columns: list[str]
    rows: list[dict[str, str | None]] = field(default_factory=list)


@dataclass
class ImportPlan:
    selected_user_id: str
    selected_user_email: str
    rows: dict[str, list[dict[str, Any]]]
    providers: list[dict[str, Any]]
    provider_keys: list[dict[str, str]]
    provider_keys_dropped: int
    discarded: dict[str, int]


def parse_pg_copy_dump(text: str) -> dict[str, CopyTable]:
    tables: dict[str, CopyTable] = {}
    current_name: str | None = None
    current_columns: list[str] = []
    current_rows: list[dict[str, str | None]] = []

    for raw_line in text.splitlines():
        if current_name is None:
            if not raw_line.startswith("COPY "):
                continue
            table_name, columns = _parse_copy_header(raw_line)
            if table_name is None:
                continue
            current_name = table_name
            current_columns = columns
            current_rows = []
            continue

        if raw_line == r"\.":
            tables[current_name] = CopyTable(current_columns, current_rows)
            current_name = None
            current_columns = []
            current_rows = []
            continue

        values = [_decode_copy_value(value) for value in raw_line.split("\t")]
        current_rows.append(dict(zip(current_columns, values, strict=False)))

    return tables


def _parse_copy_header(line: str) -> tuple[str | None, list[str]]:
    prefix = "COPY "
    suffix = " FROM stdin;"
    if not line.startswith(prefix) or not line.endswith(suffix):
        return None, []
    body = line[len(prefix) : -len(suffix)]
    if " (" not in body or not body.endswith(")"):
        return None, []
    raw_table, raw_columns = body.split(" (", 1)
    table_name = _unquote_identifier(raw_table.split(".")[-1])
    columns = [
        _unquote_identifier(part.strip()) for part in raw_columns[:-1].split(",")
    ]
    return table_name, columns


def _unquote_identifier(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value


def _decode_copy_value(value: str) -> str | None:
    if value == r"\N":
        return None
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char != "\\" or i == len(value) - 1:
            out.append(char)
            i += 1
            continue
        nxt = value[i + 1]
        escapes = {
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "\\": "\\",
        }
        out.append(escapes.get(nxt, nxt))
        i += 2
    return "".join(out)


def pg_restore_copy_text(dump_path: Path) -> str:
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("pg_restore is required to read a PostgreSQL custom dump")
    proc = subprocess.run(
        [
            pg_restore,
            "--data-only",
            "--no-owner",
            "--no-acl",
            str(dump_path),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pg_restore failed with exit {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


def load_copy_text(dump_path: Path, copy_sql_path: Path | None = None) -> str:
    if copy_sql_path is not None:
        return copy_sql_path.read_text(encoding="utf-8")
    if _looks_like_plain_copy_sql(dump_path):
        return dump_path.read_text(encoding="utf-8")
    return pg_restore_copy_text(dump_path)


def _looks_like_plain_copy_sql(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".sql") or name.endswith(".copy") or name.endswith(".copy.sql")


def list_import_users(tables: dict[str, CopyTable]) -> list[dict[str, Any]]:
    users = tables.get("users", CopyTable([], [])).rows
    out: list[dict[str, Any]] = []
    for row in users:
        out.append(
            {
                "id": row.get("id"),
                "email": row.get("email"),
                "display_name": row.get("display_name"),
                "deleted": bool(row.get("deleted_at")),
            }
        )
    return out


def build_import_plan(
    tables: dict[str, CopyTable],
    selected_user_id: str | None,
) -> ImportPlan:
    users = tables.get("users", CopyTable([], [])).rows
    active_users = [row for row in users if not row.get("deleted_at")]
    if selected_user_id is None:
        if len(active_users) != 1:
            available = ", ".join(
                f"{row.get('id')}<{row.get('email', '')}>" for row in active_users[:20]
            )
            raise ValueError(f"--user-id is required; active users: {available}")
        selected_user_id = str(active_users[0].get("id"))
    selected_user = next(
        (
            row
            for row in users
            if row.get("id") == selected_user_id and not row.get("deleted_at")
        ),
        None,
    )
    if selected_user is None:
        raise ValueError(f"selected user not found or deleted: {selected_user_id}")

    selected_email = selected_user.get("email") or LOCAL_USER_EMAIL
    conversation_rows = _select_rows(tables, "conversations", user_id=selected_user_id)
    conversation_ids = _ids(conversation_rows)
    message_rows = [
        row
        for row in tables.get("messages", CopyTable([], [])).rows
        if row.get("conversation_id") in conversation_ids
    ]
    message_ids = _ids(message_rows)
    generation_rows = [
        row
        for row in tables.get("generations", CopyTable([], [])).rows
        if row.get("user_id") == selected_user_id
        and row.get("message_id") in message_ids
    ]
    generation_ids = _ids(generation_rows)
    completion_rows = [
        row
        for row in tables.get("completions", CopyTable([], [])).rows
        if row.get("user_id") == selected_user_id
        and row.get("message_id") in message_ids
    ]
    image_rows = _select_rows(tables, "images", user_id=selected_user_id)
    image_ids = _ids(image_rows)
    variant_rows = [
        row
        for row in tables.get("image_variants", CopyTable([], [])).rows
        if row.get("image_id") in image_ids
    ]
    share_rows = [
        row
        for row in tables.get("shares", CopyTable([], [])).rows
        if row.get("image_id") in image_ids
    ]
    prompt_rows = _select_rows(tables, "system_prompts", user_id=selected_user_id)
    scope_rows = _select_rows(tables, "user_memory_scopes", user_id=selected_user_id)
    scope_ids = _ids(scope_rows)
    if not scope_rows:
        scope_rows = [
            {
                "id": DEFAULT_SCOPE_ID,
                "user_id": selected_user_id,
                "name": "默认",
                "emoji": None,
                "is_default": "t",
                "created_at": None,
                "updated_at": None,
            }
        ]
        scope_ids = {DEFAULT_SCOPE_ID}
    memory_rows = [
        row
        for row in tables.get("user_memories", CopyTable([], [])).rows
        if row.get("user_id") == selected_user_id and row.get("scope_id") in scope_ids
    ]

    rows = {
        "users": [_desktop_user_row(selected_user)],
        "system_prompts": [_rewrite_user(row) for row in prompt_rows],
        "user_memory_scopes": [_rewrite_user(row) for row in scope_rows],
        "conversations": [
            _rewrite_conversation(
                row, scope_ids, set(prompt.get("id") for prompt in prompt_rows)
            )
            for row in conversation_rows
        ],
        "messages": [_rewrite_message(row, message_ids) for row in message_rows],
        "user_memories": [
            _rewrite_memory(row, message_ids, scope_ids) for row in memory_rows
        ],
        "images": [
            _rewrite_image(row, image_ids, generation_ids) for row in image_rows
        ],
        "image_variants": [dict(row) for row in variant_rows],
        "generations": [_rewrite_generation(row, image_ids) for row in generation_rows],
        "completions": [_rewrite_completion(row, image_ids) for row in completion_rows],
        "shares": [_rewrite_share(row, image_ids) for row in share_rows],
    }

    providers, provider_keys = extract_provider_metadata(tables)
    discarded = {
        "users": max(0, len(active_users) - 1),
        "conversations": _discarded_count(
            tables, "conversations", len(conversation_rows)
        ),
        "messages": _discarded_count(tables, "messages", len(message_rows)),
        "generations": _discarded_count(tables, "generations", len(generation_rows)),
        "completions": _discarded_count(tables, "completions", len(completion_rows)),
        "images": _discarded_count(tables, "images", len(image_rows)),
    }
    return ImportPlan(
        selected_user_id=selected_user_id,
        selected_user_email=selected_email,
        rows=rows,
        providers=providers,
        provider_keys=provider_keys,
        provider_keys_dropped=len(provider_keys),
        discarded=discarded,
    )


def _ids(rows: Iterable[dict[str, Any]]) -> set[str]:
    return {str(row.get("id")) for row in rows if row.get("id")}


def _select_rows(
    tables: dict[str, CopyTable], table: str, *, user_id: str
) -> list[dict[str, str | None]]:
    return [
        row
        for row in tables.get(table, CopyTable([], [])).rows
        if row.get("user_id") == user_id
    ]


def _discarded_count(tables: dict[str, CopyTable], table: str, kept: int) -> int:
    return max(0, len(tables.get(table, CopyTable([], [])).rows) - kept)


def _desktop_user_row(row: dict[str, str | None]) -> dict[str, Any]:
    out = dict(row)
    out.update(
        {
            "id": LOCAL_USER_ID,
            "email": row.get("email") or LOCAL_USER_EMAIL,
            "email_verified": True,
            "role": "admin",
            "account_mode": "byok",
            "notification_email": False,
            "deleted_at": None,
        }
    )
    return out


def _rewrite_user(row: dict[str, str | None]) -> dict[str, Any]:
    out = dict(row)
    out["user_id"] = LOCAL_USER_ID
    return out


def _rewrite_conversation(
    row: dict[str, str | None],
    scope_ids: set[str],
    prompt_ids: set[Any],
) -> dict[str, Any]:
    out = _rewrite_user(row)
    if out.get("active_scope_id") not in scope_ids:
        out["active_scope_id"] = None
    if out.get("default_system_prompt_id") not in prompt_ids:
        out["default_system_prompt_id"] = None
    return out


def _rewrite_message(
    row: dict[str, str | None], message_ids: set[str]
) -> dict[str, Any]:
    out = dict(row)
    if out.get("parent_message_id") not in message_ids:
        out["parent_message_id"] = None
    return out


def _rewrite_memory(
    row: dict[str, str | None],
    message_ids: set[str],
    scope_ids: set[str],
) -> dict[str, Any]:
    out = _rewrite_user(row)
    if out.get("source_message_id") not in message_ids:
        out["source_message_id"] = None
    if out.get("scope_id") not in scope_ids:
        out["scope_id"] = DEFAULT_SCOPE_ID
    return out


def _rewrite_image(
    row: dict[str, str | None],
    image_ids: set[str],
    generation_ids: set[str],
) -> dict[str, Any]:
    out = _rewrite_user(row)
    if out.get("parent_image_id") not in image_ids:
        out["parent_image_id"] = None
    if out.get("owner_generation_id") not in generation_ids:
        out["owner_generation_id"] = None
    return out


def _rewrite_generation(
    row: dict[str, str | None], image_ids: set[str]
) -> dict[str, Any]:
    out = _rewrite_user(row)
    out["input_image_ids"] = [
        image_id
        for image_id in _coerce_list(row.get("input_image_ids"))
        if image_id in image_ids
    ]
    for key in ["primary_input_image_id", "mask_image_id"]:
        if out.get(key) not in image_ids:
            out[key] = None
    out["user_api_credential_id"] = None
    out["upstream_supplier_id"] = None
    return out


def _rewrite_completion(
    row: dict[str, str | None], image_ids: set[str]
) -> dict[str, Any]:
    out = _rewrite_user(row)
    out["input_image_ids"] = [
        image_id
        for image_id in _coerce_list(row.get("input_image_ids"))
        if image_id in image_ids
    ]
    out["user_api_credential_id"] = None
    out["upstream_supplier_id"] = None
    return out


def _rewrite_share(row: dict[str, str | None], image_ids: set[str]) -> dict[str, Any]:
    out = dict(row)
    out["image_ids"] = [
        image_id
        for image_id in _coerce_list(row.get("image_ids"))
        if image_id in image_ids
    ]
    return out


def extract_provider_metadata(
    tables: dict[str, CopyTable],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    settings = tables.get("system_settings", CopyTable([], [])).rows
    raw = next(
        (row.get("value") for row in settings if row.get("key") == "providers"), None
    )
    if not raw:
        return [], []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [], []
    providers = parsed.get("providers") if isinstance(parsed, dict) else parsed
    if not isinstance(providers, list):
        return [], []
    sanitized: list[dict[str, Any]] = []
    provider_keys: list[dict[str, str]] = []
    for item in providers:
        if not isinstance(item, dict):
            continue
        clean = dict(item)
        api_key = clean.pop("api_key", None)
        name = str(clean.get("name") or "").strip()
        if name and api_key:
            provider_keys.append({"name": name, "api_key": str(api_key)})
        sanitized.append(clean)
    return sanitized, provider_keys


def apply_import_plan(
    sqlite_path: Path,
    plan: ImportPlan,
    *,
    replace: bool,
) -> dict[str, Any]:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        _assert_desktop_schema(conn)
        with conn:
            if replace:
                for table in DELETE_ORDER:
                    if _table_exists(conn, table):
                        conn.execute(f"DELETE FROM {table}")
            counts: dict[str, int] = {}
            for table in DESKTOP_TABLE_ORDER:
                counts[table] = _insert_rows(conn, table, plan.rows.get(table, []))
        return {
            "selected_user_id": plan.selected_user_id,
            "selected_user_email": plan.selected_user_email,
            "imported": counts,
            "discarded": plan.discarded,
            "providers": len(plan.providers),
            "provider_keys_dropped": plan.provider_keys_dropped,
        }
    finally:
        conn.close()


def _assert_desktop_schema(conn: sqlite3.Connection) -> None:
    required = {"users", "conversations", "messages", "images", "generations"}
    missing = [table for table in sorted(required) if not _table_exists(conn, table)]
    if missing:
        raise RuntimeError(
            f"target SQLite database is missing desktop tables: {', '.join(missing)}"
        )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _insert_rows(
    conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    columns = _table_columns(conn, table)
    inserted = 0
    for row in rows:
        prepared = {
            key: _coerce_sqlite_value(table, key, value)
            for key, value in row.items()
            if key in columns
        }
        if not prepared:
            continue
        names = list(prepared)
        placeholders = ",".join("?" for _ in names)
        escaped_names = ",".join(f'"{name}"' for name in names)
        sql = f'INSERT OR REPLACE INTO "{table}" ({escaped_names}) VALUES ({placeholders})'
        conn.execute(sql, [prepared[name] for name in names])
        inserted += 1
    return inserted


def _coerce_sqlite_value(table: str, column: str, value: Any) -> Any:
    if column in BOOL_COLUMNS.get(table, set()):
        return _coerce_bool(value)
    if column in LIST_COLUMNS.get(table, set()):
        return json.dumps(_coerce_list(value), ensure_ascii=False)
    if column in JSON_COLUMNS.get(table, set()):
        return json.dumps(_coerce_json(value), ensure_ascii=False)
    return value


def _coerce_bool(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if value is None:
        return 0
    raw = str(value).strip().lower()
    return 1 if raw in {"1", "t", "true", "yes", "y", "on"} else 0


def _coerce_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    raw = str(value).strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item is not None]
    if raw.startswith("{") and raw.endswith("}"):
        return _parse_pg_array(raw)
    return [raw]


def _parse_pg_array(raw: str) -> list[str]:
    inner = raw[1:-1]
    if not inner:
        return []
    reader = csv.reader(
        io.StringIO(inner), delimiter=",", quotechar='"', escapechar="\\"
    )
    return [value for row in reader for value in row if value]


def write_provider_metadata(
    data_root: Path, providers: list[dict[str, Any]]
) -> Path | None:
    if not providers:
        return None
    path = data_root / "data" / "providers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"providers": providers}, ensure_ascii=False, indent=2, sort_keys=True
        ),
        encoding="utf-8",
    )
    return path


def write_provider_key_output(
    path: Path, provider_keys: list[dict[str, str]]
) -> Path | None:
    if not provider_keys:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"provider_keys": provider_keys}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def extract_storage_tar(storage_tar: Path, data_root: Path, *, replace: bool) -> int:
    storage_root = data_root / "data" / "storage"
    if replace and storage_root.exists():
        shutil.rmtree(storage_root)
    storage_root.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with tarfile.open(storage_tar, "r:*") as archive:
        for member in archive.getmembers():
            target = (storage_root / member.name).resolve()
            if storage_root.resolve() not in [target, *target.parents]:
                raise RuntimeError(f"refusing unsafe storage tar entry: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted += 1
    return extracted


def run_import(args: argparse.Namespace) -> dict[str, Any]:
    data_root = Path(args.data_root).expanduser().resolve()
    sqlite_path = (
        Path(args.sqlite).expanduser().resolve()
        if args.sqlite
        else data_root / "data/db/lumen.sqlite"
    )
    dump_path = Path(args.dump).expanduser().resolve()
    copy_sql_path = (
        Path(args.copy_sql).expanduser().resolve() if args.copy_sql else None
    )
    copy_text = load_copy_text(dump_path, copy_sql_path)
    tables = parse_pg_copy_dump(copy_text)
    if args.list_users:
        summary = {
            "users": list_import_users(tables),
            "providers": len(extract_provider_metadata(tables)[0]),
        }
        return _write_report(args, summary)
    plan = build_import_plan(tables, args.user_id)
    if args.dry_run:
        summary = {
            "selected_user_id": plan.selected_user_id,
            "selected_user_email": plan.selected_user_email,
            "importable": {table: len(rows) for table, rows in plan.rows.items()},
            "discarded": plan.discarded,
            "providers": len(plan.providers),
            "provider_keys_dropped": plan.provider_keys_dropped,
        }
    else:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        summary = apply_import_plan(sqlite_path, plan, replace=args.replace)
        providers_path = write_provider_metadata(data_root, plan.providers)
        if providers_path is not None:
            summary["providers_path"] = str(providers_path)
        if args.provider_key_output:
            provider_key_path = write_provider_key_output(
                Path(args.provider_key_output).expanduser().resolve(),
                plan.provider_keys,
            )
            if provider_key_path is not None:
                summary["provider_key_output"] = str(provider_key_path)
        if args.storage_tar:
            summary["storage_files"] = extract_storage_tar(
                Path(args.storage_tar).expanduser().resolve(),
                data_root,
                replace=args.replace_storage,
            )
    return _write_report(args, summary)


def _write_report(args: argparse.Namespace, summary: dict[str, Any]) -> dict[str, Any]:
    report_path = Path(args.report).expanduser().resolve() if args.report else None
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dump",
        required=True,
        help="lumen-export.dump or lumen-export.copy.sql from scripts/desktop-export.sh",
    )
    parser.add_argument(
        "--copy-sql", help="pre-rendered pg_restore COPY output, useful for tests"
    )
    parser.add_argument("--storage-tar", help="optional lumen-storage.tar.gz")
    parser.add_argument("--data-root", required=True, help="desktop data root")
    parser.add_argument(
        "--sqlite",
        help="target desktop SQLite path; defaults to data-root/data/db/lumen.sqlite",
    )
    parser.add_argument(
        "--user-id",
        help="Docker user id to import; required when dump has multiple users",
    )
    parser.add_argument(
        "--list-users",
        action="store_true",
        help="list Docker users in the export and exit",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="delete desktop data tables before importing",
    )
    parser.add_argument(
        "--replace-storage",
        action="store_true",
        help="replace data/storage before extracting storage tar",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and report without writing SQLite/storage",
    )
    parser.add_argument(
        "--provider-key-output",
        help="write stripped provider API keys to a private JSON file for OS keychain import",
    )
    parser.add_argument("--report", help="write JSON import report")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    summary = run_import(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
