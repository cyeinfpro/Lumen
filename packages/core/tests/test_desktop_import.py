from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from lumen_core import desktop_import


def _copy_dump() -> str:
    return "\n".join(
        [
            "COPY public.users (id, email, display_name, email_verified, notification_email, role, account_mode, deleted_at) FROM stdin;",
            "u1\tone@example.com\tOne\tt\tf\tadmin\tbyok\t\\N",
            "u2\ttwo@example.com\tTwo\tt\tf\tadmin\tbyok\t\\N",
            "\\.",
            "COPY public.conversations (id, user_id, title, pinned, archived, default_params, memory_disabled, active_scope_id, default_system_prompt_id, last_activity_at, created_at, updated_at, deleted_at) FROM stdin;",
            "c1\tu1\tKeep me\tt\tf\t{}\tf\t\\N\t\\N\t2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00\t\\N",
            "c2\tu2\tDrop me\tf\tf\t{}\tf\t\\N\t\\N\t2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00\t\\N",
            "\\.",
            "COPY public.messages (id, conversation_id, role, content, parent_message_id, created_at, updated_at, deleted_at) FROM stdin;",
            'm1\tc1\tuser\t{"type":"text","text":"hi"}\t\\N\t2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00\t\\N',
            'm2\tc2\tuser\t{"type":"text","text":"bye"}\t\\N\t2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00\t\\N',
            "\\.",
            "COPY public.images (id, user_id, source, visibility, storage_key, mime, width, height, size_bytes, sha256, parent_image_id, owner_generation_id, metadata_jsonb, created_at, updated_at, deleted_at) FROM stdin;",
            'img1\tu1\tgenerated\tprivate\timages/img1.png\timage/png\t1024\t1024\t12\tsha\t\\N\tgen1\t{"kind":"test"}\t2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00\t\\N',
            "img2\tu2\tgenerated\tprivate\timages/img2.png\timage/png\t1024\t1024\t12\tsha\t\\N\t\\N\t{}\t2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00\t\\N",
            "\\.",
            "COPY public.generations (id, message_id, user_id, action, model, prompt, size_requested, aspect_ratio, input_image_ids, primary_input_image_id, mask_image_id, upstream_request, user_api_credential_id, upstream_supplier_id, status, progress_stage, attempt, idempotency_key, created_at, updated_at) FROM stdin;",
            'gen1\tm1\tu1\tgenerate\tgpt-image-1\tprompt\t1024x1024\t1:1\t{img1,img2}\timg1\timg2\t{"ok":true}\tcred1\tsupplier1\tsucceeded\tdone\t1\tidem1\t2026-01-01 00:00:00+00\t2026-01-01 00:00:00+00',
            "\\.",
            "COPY public.system_settings (id, key, value) FROM stdin;",
            's1\tproviders\t{"providers":[{"name":"OpenAI","base_url":"https://api.openai.com/v1","api_key":"sk-secret","enabled":true}]}',
            "\\.",
            "",
        ]
    )


def _create_desktop_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                email TEXT,
                display_name TEXT,
                email_verified INTEGER,
                notification_email INTEGER,
                role TEXT,
                account_mode TEXT,
                deleted_at TEXT
            );
            CREATE TABLE user_memory_scopes (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                name TEXT,
                emoji TEXT,
                is_default INTEGER,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                title TEXT,
                pinned INTEGER,
                archived INTEGER,
                default_params TEXT,
                memory_disabled INTEGER,
                active_scope_id TEXT,
                default_system_prompt_id TEXT,
                last_activity_at TEXT,
                created_at TEXT,
                updated_at TEXT,
                deleted_at TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                role TEXT,
                content TEXT,
                parent_message_id TEXT,
                created_at TEXT,
                updated_at TEXT,
                deleted_at TEXT
            );
            CREATE TABLE images (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                source TEXT,
                visibility TEXT,
                storage_key TEXT,
                mime TEXT,
                width INTEGER,
                height INTEGER,
                size_bytes INTEGER,
                sha256 TEXT,
                parent_image_id TEXT,
                owner_generation_id TEXT,
                metadata_jsonb TEXT,
                created_at TEXT,
                updated_at TEXT,
                deleted_at TEXT
            );
            CREATE TABLE generations (
                id TEXT PRIMARY KEY,
                message_id TEXT,
                user_id TEXT,
                action TEXT,
                model TEXT,
                prompt TEXT,
                size_requested TEXT,
                aspect_ratio TEXT,
                input_image_ids TEXT,
                primary_input_image_id TEXT,
                mask_image_id TEXT,
                upstream_request TEXT,
                user_api_credential_id TEXT,
                upstream_supplier_id TEXT,
                status TEXT,
                progress_stage TEXT,
                attempt INTEGER,
                idempotency_key TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_copy_dump_import_rewrites_single_desktop_user(tmp_path: Path) -> None:
    tables = desktop_import.parse_pg_copy_dump(_copy_dump())
    plan = desktop_import.build_import_plan(tables, "u1")

    assert plan.provider_keys_dropped == 1
    assert plan.provider_keys == [{"name": "OpenAI", "api_key": "sk-secret"}]
    assert plan.discarded["users"] == 1
    assert len(plan.rows["conversations"]) == 1
    assert len(plan.rows["messages"]) == 1

    db_path = tmp_path / "data/db/lumen.sqlite"
    _create_desktop_db(db_path)
    summary = desktop_import.apply_import_plan(db_path, plan, replace=True)

    assert summary["imported"]["users"] == 1
    assert summary["imported"]["generations"] == 1
    conn = sqlite3.connect(db_path)
    try:
        user = conn.execute("SELECT id, email FROM users").fetchone()
        assert user == ("local-user", "one@example.com")
        conv_count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        assert conv_count == 1
        generation = conn.execute(
            "SELECT user_id, input_image_ids, primary_input_image_id, mask_image_id, user_api_credential_id FROM generations"
        ).fetchone()
    finally:
        conn.close()

    assert generation == ("local-user", '["img1"]', "img1", None, None)

    providers_path = desktop_import.write_provider_metadata(tmp_path, plan.providers)
    assert providers_path is not None
    providers = json.loads(providers_path.read_text(encoding="utf-8"))
    assert providers["providers"][0]["enabled"] is True
    assert "api_key" not in providers["providers"][0]

    key_path = desktop_import.write_provider_key_output(
        tmp_path / "keys.json", plan.provider_keys
    )
    assert key_path is not None
    provider_keys = json.loads(key_path.read_text(encoding="utf-8"))
    assert provider_keys == {
        "provider_keys": [{"name": "OpenAI", "api_key": "sk-secret"}]
    }


def test_run_import_reads_plain_copy_sql_without_pg_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "data/db/lumen.sqlite"
    _create_desktop_db(db_path)
    copy_path = tmp_path / "lumen-export.copy.sql"
    copy_path.write_text(_copy_dump(), encoding="utf-8")

    args = desktop_import.build_parser().parse_args(
        [
            "--dump",
            str(copy_path),
            "--data-root",
            str(tmp_path),
            "--sqlite",
            str(db_path),
            "--user-id",
            "u1",
            "--replace",
        ]
    )
    summary = desktop_import.run_import(args)

    assert summary["selected_user_id"] == "u1"
    assert summary["imported"]["users"] == 1


def test_multi_user_dump_requires_selected_user() -> None:
    tables = desktop_import.parse_pg_copy_dump(_copy_dump())

    with pytest.raises(ValueError, match="--user-id is required"):
        desktop_import.build_import_plan(tables, None)


def test_storage_tar_rejects_path_traversal(tmp_path: Path) -> None:
    tar_path = tmp_path / "storage.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        payload = b"bad"
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(RuntimeError, match="unsafe storage tar entry"):
        desktop_import.extract_storage_tar(tar_path, tmp_path, replace=True)
