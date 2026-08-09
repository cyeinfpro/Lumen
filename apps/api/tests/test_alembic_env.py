from __future__ import annotations

from contextlib import nullcontext
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import uuid

from alembic import context
from alembic.config import Config
import pytest


ENV_PATH = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


def _load_env_module(monkeypatch: pytest.MonkeyPatch):
    def upgrade(_heads, _context):
        return ()

    migration_context = SimpleNamespace(
        _migrations_fn=upgrade,
        opts={"destination_rev": "head"},
        get_current_heads=lambda: ("0063_storage_apply_retry_fence",),
    )
    config = Config()
    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "configure", lambda **_kwargs: None)
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "get_context", lambda: migration_context)
    monkeypatch.setattr(context, "run_migrations", lambda: None)
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)

    spec = importlib.util.spec_from_file_location(
        f"alembic_env_under_test_{uuid.uuid4().hex}",
        ENV_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, migration_context


def test_alembic_commits_timeout_setup_before_migration_transaction() -> None:
    """Guard against SQLAlchemy 2 autobegin rolling back successful migrations."""
    source = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )
    timeout_pos = source.index("SET statement_timeout")
    commit_pos = source.index("connection.commit()", timeout_pos)
    configure_pos = source.index("context.configure(connection=connection", timeout_pos)

    assert timeout_pos < commit_pos < configure_pos


def test_alembic_prepares_historical_concurrent_index_retry_before_run() -> None:
    source = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )
    prepare_pos = source.index("_prepare_concurrent_index_retry()")
    run_pos = source.index("context.run_migrations()", prepare_pos)

    assert prepare_pos < run_pos
    assert 'getattr(migration_context, "_migrations_fn", None)' in source
    assert "migration_context._migrations_fn = cached_migration_fn" in source
    assert "scripts._upgrade_revs" in source
    assert "scripts._downgrade_revs" in source


def test_alembic_prepares_downgrade_guards_before_any_migration() -> None:
    source = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )
    guard_pos = source.index("_prepare_downgrade_guards(online=True)")
    retry_pos = source.index("_prepare_concurrent_index_retry()", guard_pos)
    run_pos = source.index("context.run_migrations()", retry_pos)

    assert guard_pos < retry_pos < run_pos
    assert "guard_telegram_downgrade" in source
    assert "DESTRUCTIVE_EXPORT_REVISIONS" in source


def test_programmatic_downgrade_with_zero_heads_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, migration_context = _load_env_module(monkeypatch)

    def downgrade(_heads, _context):
        return ()

    migration_context._migrations_fn = downgrade
    migration_context.get_current_heads = lambda: ()

    with pytest.raises(RuntimeError, match="database has 0 Alembic heads"):
        module._prepare_downgrade_guards(online=True)


def test_cli_downgrade_with_multiple_heads_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, migration_context = _load_env_module(monkeypatch)

    def downgrade():
        return None

    migration_context._migrations_fn = None
    migration_context.get_current_heads = lambda: ("head-a", "head-b")
    module.config.cmd_opts = SimpleNamespace(
        cmd=(downgrade,),
        revision="base",
    )

    with pytest.raises(RuntimeError, match="database has 2 Alembic heads"):
        module._prepare_downgrade_guards(online=True)


def test_multiple_head_upgrade_path_is_not_treated_as_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, migration_context = _load_env_module(monkeypatch)

    def upgrade():
        return None

    migration_context._migrations_fn = None
    migration_context.get_current_heads = lambda: ("head-a", "head-b")
    module.config.cmd_opts = SimpleNamespace(
        cmd=(upgrade,),
        revision="heads",
    )

    assert module._prepare_downgrade_guards(online=True) is None


def test_offline_destructive_downgrade_requires_online_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, migration_context = _load_env_module(monkeypatch)

    step = SimpleNamespace(
        info=SimpleNamespace(
            is_stamp=False,
            is_upgrade=False,
            up_revision_ids=("0062_tg_control_effect_fence",),
        )
    )

    def downgrade(_heads, _context):
        return (step,)

    migration_context._migrations_fn = downgrade
    migration_context.get_current_heads = lambda: ("0062_tg_control_effect_fence",)

    with pytest.raises(RuntimeError, match="requires an online database"):
        module._prepare_downgrade_guards(online=False)


def test_alembic_escapes_percent_encoded_socket_urls() -> None:
    source = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )
    assert 'sync_url.replace("%", "%%")' in source

    config = Config()
    url = "postgresql+psycopg2://user@/db?host=%2Ftmp"
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    assert config.get_main_option("sqlalchemy.url") == url


def test_alembic_preserves_application_loggers_during_programmatic_migrations() -> None:
    source = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )

    assert (
        "fileConfig(config.config_file_name, disable_existing_loggers=False)" in source
    )


def test_users_active_email_unique_migration_uses_safe_postgres_ordering() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0025_users_active_email_unique.py"
    ).read_text(encoding="utf-8")

    upgrade_pos = source.index("def upgrade()")
    pg_create_pos = source.index("postgresql_concurrently=True", upgrade_pos)
    drop_constraint_pos = source.index(
        'op.drop_constraint("users_email_key"', upgrade_pos
    )
    downgrade_pos = source.index("def downgrade()")
    duplicate_guard_pos = source.index("duplicate is not None", downgrade_pos)
    recreate_constraint_pos = source.index(
        'op.create_unique_constraint("users_email_key"', downgrade_pos
    )
    concurrent_drop_pos = source.index("postgresql_concurrently=True", downgrade_pos)

    assert "autocommit_block()" in source
    assert pg_create_pos < drop_constraint_pos
    assert duplicate_guard_pos < recreate_constraint_pos < concurrent_drop_pos
