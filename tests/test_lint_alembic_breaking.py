from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "lumen_lint_alembic_breaking",
    ROOT / "scripts" / "lint_alembic_breaking.py",
)
assert SPEC is not None and SPEC.loader is not None
lint_alembic = module_from_spec(SPEC)
sys.modules[SPEC.name] = lint_alembic
SPEC.loader.exec_module(lint_alembic)


def _write_migration(
    path: Path,
    upgrade_body: str,
    *,
    imports: str = "",
    helpers: str = "",
    revision: str = "test_revision",
    down_revision: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    source = f"{imports}\n\n" if imports else ""
    source += f"revision = {revision!r}\ndown_revision = {down_revision!r}\n\n"
    if helpers:
        source += f"{helpers.rstrip()}\n\n"
    source += f"def upgrade():\n{upgrade_body}\n"
    path.write_text(source, encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_batch_alter_table_alias_is_audited(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    with op.batch_alter_table('users') as batch_op:\n"
        "        batch_op.drop_column('secret')",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "drop_column is a breaking drop column operation"
    ]


def test_transferred_batch_context_alias_is_audited(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    table_ops = op.batch_alter_table('users')\n"
        "    with table_ops as batch_op:\n"
        "        batch_op.drop_column('secret')",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "drop_column is a breaking drop column operation"
    ]


def test_aliased_alembic_operation_is_audited(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    migration_op.drop_column('users', 'secret')",
        imports="from alembic import op as migration_op",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "drop_column is a breaking drop column operation"
    ]


def test_imported_alembic_op_module_alias_is_audited(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    migration_op.drop_column('users', 'secret')",
        imports="import alembic.op as migration_op",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "drop_column is a breaking drop column operation"
    ]


def test_operation_receiver_transfer_is_audited(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    migration_op = op\n    migration_op.drop_column('users', 'secret')",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "drop_column is a breaking drop column operation"
    ]


def test_operation_method_alias_is_audited(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    drop_table = op.drop_table\n    drop_table('users')",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "drop_table is a breaking drop table operation"
    ]


def test_reachable_migration_helper_is_audited(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    drop_legacy_table()",
        helpers=("def drop_legacy_table() -> None:\n    op.drop_table('legacy_users')"),
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "drop_table is a breaking drop table operation"
    ]


def test_raw_op_execute_breaking_ddl_is_rejected(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    op.execute('ALTER TABLE users DROP COLUMN secret')",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "raw SQL DDL via Alembic execution is not an approved expand operation"
    ]


def test_raw_get_bind_execute_breaking_ddl_is_rejected(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    op.get_bind().execute('ALTER TABLE users DROP COLUMN secret')",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "raw SQL DDL via Alembic execution is not an approved expand operation"
    ]


def test_get_bind_method_alias_is_audited(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    get_bind = op.get_bind\n"
        "    get_bind().execute('ALTER TABLE users DROP COLUMN secret')",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "raw SQL DDL via Alembic execution is not an approved expand operation"
    ]


def test_helper_get_bind_execute_breaking_ddl_is_rejected(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    remove_legacy_column(op.get_bind())",
        helpers=(
            "def remove_legacy_column(bind) -> None:\n"
            "    bind.execute('ALTER TABLE users DROP COLUMN secret')"
        ),
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "raw SQL DDL via Alembic execution is not an approved expand operation"
    ]


def test_unclassified_raw_execution_fails_closed(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    statement = build_statement()\n    op.execute(statement)",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "raw SQL passed to Alembic execution cannot be classified safely"
    ]


def test_safe_raw_dml_and_validate_constraint_are_accepted(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    op.execute(sa.text('UPDATE users SET active = TRUE'))\n"
        "    table_name = 'users'\n"
        "    constraint_name = 'ck_users_active'\n"
        "    op.execute(\n"
        "        sa.text(\n"
        "            f'ALTER TABLE \"{table_name}\" '\n"
        "            f'VALIDATE CONSTRAINT \"{constraint_name}\"'\n"
        "        )\n"
        "    )",
        imports="import sqlalchemy as sa",
    )

    assert lint_alembic.lint_file(migration) == []


def test_unimported_operation_alias_is_not_treated_as_alembic_alias(
    tmp_path: Path,
) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    migration_op.drop_column('not-an-alembic-operation')",
    )

    assert lint_alembic.lint_file(migration) == []


def test_batch_check_constraint_requires_not_valid(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    with op.batch_alter_table('users') as batch_op:\n"
        "        batch_op.create_check_constraint('ck_users', 'id > 0')",
    )

    violations = lint_alembic.lint_file(migration)

    assert [violation.message for violation in violations] == [
        "create_check_constraint must use a NOT VALID/VALIDATE pattern "
        "for rolling deploys"
    ]


def test_batch_check_constraint_accepts_explicit_not_valid(tmp_path: Path) -> None:
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(
        migration,
        "    with op.batch_alter_table('users') as batch_op:\n"
        "        batch_op.create_check_constraint(\n"
        "            'ck_users', 'id > 0', postgresql_not_valid=True\n"
        "        )",
    )

    assert lint_alembic.lint_file(migration) == []


def test_clean_checkout_uses_explicit_base_and_head(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(migration, "    op.add_column('users', object())")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    _write_migration(migration, "    op.drop_table('users')")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "breaking")
    head = _git(tmp_path, "rev-parse", "HEAD")

    previous = Path.cwd()
    try:
        __import__("os").chdir(tmp_path)
        assert lint_alembic._git_changed_files(base=base, head=head) == [
            Path("apps/api/alembic/versions/0001_test.py")
        ]
        assert lint_alembic.main(["--base", base, "--head", head]) == 1
    finally:
        __import__("os").chdir(previous)


def test_renamed_historical_migration_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    original = tmp_path / "apps/api/alembic/versions/0001_test.py"
    renamed = tmp_path / "apps/api/alembic/versions/0002_renamed.py"
    _write_migration(original, "    op.add_column('users', object())")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    _git(
        tmp_path,
        "mv",
        str(original.relative_to(tmp_path)),
        str(renamed.relative_to(tmp_path)),
    )
    _git(tmp_path, "commit", "-m", "rename migration")
    head = _git(tmp_path, "rev-parse", "HEAD")

    assert _git(tmp_path, "diff", "--name-status", base, head).startswith("R100\t")

    monkeypatch.chdir(tmp_path)
    assert lint_alembic._git_changed_files(base=base, head=head) == [
        Path("apps/api/alembic/versions/0002_renamed.py")
    ]
    assert lint_alembic.main(["--base", base, "--head", head]) == 1
    assert "existing Alembic revision was moved" in capsys.readouterr().err


def test_historical_revision_deletion_and_graph_rewrite_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    first = tmp_path / "apps/api/alembic/versions/0001_initial.py"
    second = tmp_path / "apps/api/alembic/versions/0002_follow_up.py"
    _write_migration(
        first,
        "    op.add_column('users', object())",
        revision="0001",
    )
    _write_migration(
        second,
        "    op.add_column('users', object())",
        revision="0002",
        down_revision="0001",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    first.unlink()
    _write_migration(
        second,
        "    op.add_column('users', object())",
        revision="0002",
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "rewrite migration graph")
    head = _git(tmp_path, "rev-parse", "HEAD")
    acknowledgment = tmp_path / "commit-message.txt"
    acknowledgment.write_text(
        "BREAKING: planned downtime migration runbook\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    assert lint_alembic.main(["--base", base, "--head", head]) == 1
    output = capsys.readouterr().err
    assert "existing Alembic revision was deleted" in output
    assert "down_revision of an existing Alembic revision was rewritten" in output
    assert (
        lint_alembic.main(
            [
                "--base",
                base,
                "--head",
                head,
                "--commit-message-file",
                str(acknowledgment),
            ]
        )
        == 0
    )


def test_historical_migration_content_rewrite_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    migration = tmp_path / "apps/api/alembic/versions/0001_initial.py"
    _write_migration(
        migration,
        "    op.add_column('users', object())",
        revision="0001",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    _write_migration(
        migration,
        "    op.add_column('users', object())\n"
        "    op.create_index('ix_users_id', 'users', ['id'])",
        revision="0001",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "rewrite migration body")
    head = _git(tmp_path, "rev-parse", "HEAD")

    monkeypatch.chdir(tmp_path)
    assert lint_alembic.main(["--base", base, "--head", head]) == 1
    assert "existing Alembic revision content was changed" in capsys.readouterr().err


def test_unknown_clean_baseline_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("clean\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "clean")
    monkeypatch.chdir(tmp_path)

    assert lint_alembic.main([]) == 2
    assert "no explicit --base/--head" in capsys.readouterr().err


def test_untracked_migration_is_linted_without_a_git_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git(tmp_path, "init")
    migration = tmp_path / "apps/api/alembic/versions/0001_test.py"
    _write_migration(migration, "    op.drop_table('users')")
    monkeypatch.chdir(tmp_path)

    assert lint_alembic.main([]) == 1
