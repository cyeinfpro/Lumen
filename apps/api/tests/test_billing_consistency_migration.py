from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema


ROOT = Path(__file__).resolve().parents[3]
API_ROOT = ROOT / "apps/api"
RETRY_MIGRATION = API_ROOT / "alembic/versions/0042_generation_billing_retry.py"
CONSISTENCY_MIGRATION = API_ROOT / "alembic/versions/0043_billing_consistency.py"

USER_1 = "00000000-0000-4000-8000-000000000101"
USER_2 = "00000000-0000-4000-8000-000000000102"
CREDENTIAL_1 = "00000000-0000-4000-8000-000000000301"
CREDENTIAL_2 = "00000000-0000-4000-8000-000000000302"
GENERATION = "00000000-0000-4000-8000-000000000601"
TX_RETRY_2 = "00000000-0000-4000-8000-000000000701"
TX_RETRY_MAX = "00000000-0000-4000-8000-000000000702"
TX_COST_BREAKDOWN = "00000000-0000-4000-8000-000000000703"
TX_ACTUAL = "00000000-0000-4000-8000-000000000704"
TX_COST = "00000000-0000-4000-8000-000000000705"
TX_FALLBACK = "00000000-0000-4000-8000-000000000706"
TX_NON_COMPLETION = "00000000-0000-4000-8000-000000000707"
BATCH_1 = "00000000-0000-4000-8000-000000000801"
BATCH_2 = "00000000-0000-4000-8000-000000000802"


def test_generation_billing_retry_migration_static_guard() -> None:
    source = RETRY_MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0041_billing_window_ledger"' in source
    assert '"billing_retry_count"' in source
    assert "max(" in source
    assert "ref_id ~ '^[^:]+:retry:[0-9]+$'" in source


def test_billing_consistency_migration_static_guard() -> None:
    source = CONSISTENCY_MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0042_generation_billing_retry"' in source
    assert '"redemption_batches"' in source
    assert '"uq_redemption_batch_creator_idemp"' in source
    assert "DELETE FROM billing_window_usage_events" in source
    assert "wallet_tx.meta ->> 'actual_micro'" in source
    assert "ON CONFLICT (wallet_transaction_id) DO UPDATE" in source
    assert '"uq_user_api_credentials_id_user"' in source
    assert '"fk_billing_window_credential_user"' in source
    assert '"ck_billing_window_amount_positive"' in source


def _postgres_url() -> URL:
    raw_url = os.environ.get("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip(
            "LUMEN_TEST_POSTGRES_URL is not set; "
            "PostgreSQL migration round-trip skipped"
        )
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")
    return url.set(drivername="postgresql+psycopg2")


def _schema_url(base_url: URL, schema: str) -> URL:
    query = dict(base_url.query)
    existing_options = str(query.get("options", "")).strip()
    search_path_option = f"-csearch_path={schema},public"
    query["options"] = " ".join(
        item for item in (existing_options, search_path_option) if item
    )
    return base_url.set(query=query)


def _run_alembic(database_url: URL, action: str, revision: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "BYOK_API_KEY_MASTER_SECRET": ("test-byok-master-secret-0123456789-test"),
            "DATABASE_URL": database_url.render_as_string(hide_password=False),
            "PUBLIC_BASE_URL": "http://localhost:3000",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            action,
            revision,
        ],
        cwd=API_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"alembic {action} {revision} failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


def _insert_base_fixture(connection: Connection) -> None:
    connection.execute(
        text("INSERT INTO users (id, email) VALUES (:id, :email)"),
        [
            {"id": USER_1, "email": "migration-user-1@example.test"},
            {"id": USER_2, "email": "migration-user-2@example.test"},
        ],
    )
    connection.execute(
        text(
            """
            INSERT INTO user_api_credentials (
                id,
                user_id
            )
            VALUES (
                :id,
                :user_id
            )
            """
        ),
        [
            {
                "id": CREDENTIAL_1,
                "user_id": USER_1,
            },
            {
                "id": CREDENTIAL_2,
                "user_id": USER_2,
            },
        ],
    )
    connection.execute(
        text("INSERT INTO generations (id) VALUES (:id)"),
        {"id": GENERATION},
    )


def _create_0041_baseline(engine: Engine) -> None:
    statements = (
        """
        CREATE TABLE users (
            id varchar(36) PRIMARY KEY,
            email varchar(255) NOT NULL UNIQUE
        )
        """,
        """
        CREATE TABLE user_api_credentials (
            id varchar(36) PRIMARY KEY,
            user_id varchar(36) NOT NULL
                REFERENCES users(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE generations (
            id varchar(36) PRIMARY KEY
        )
        """,
        """
        CREATE TABLE wallet_transactions (
            id varchar(36) PRIMARY KEY,
            user_id varchar(36) NOT NULL
                REFERENCES users(id) ON DELETE CASCADE,
            kind varchar(32) NOT NULL,
            amount_micro bigint NOT NULL,
            balance_after bigint NOT NULL,
            hold_after bigint NOT NULL,
            ref_type varchar(32),
            ref_id varchar(64),
            idempotency_key varchar(96) NOT NULL,
            meta jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (user_id, idempotency_key)
        )
        """,
        """
        CREATE TABLE billing_window_usage_events (
            wallet_transaction_id varchar(36) PRIMARY KEY
                REFERENCES wallet_transactions(id) ON DELETE CASCADE,
            user_id varchar(36) NOT NULL
                REFERENCES users(id) ON DELETE CASCADE,
            credential_id varchar(36) NOT NULL,
            amount_micro bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE INDEX ix_billing_window_credential_created
        ON billing_window_usage_events (credential_id, created_at)
        """,
        """
        CREATE INDEX ix_billing_window_user_created
        ON billing_window_usage_events (user_id, created_at)
        """,
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _insert_wallet_fixture(connection: Connection) -> None:
    rows = [
        {
            "id": TX_RETRY_2,
            "ref_type": "generation",
            "ref_id": f"{GENERATION}:retry:2",
            "idempotency_key": "migration-retry-2",
            "amount_micro": -1,
            "meta": {},
        },
        {
            "id": TX_RETRY_MAX,
            "ref_type": "generation",
            "ref_id": f"{GENERATION}:retry:5",
            "idempotency_key": "migration-retry-max",
            "amount_micro": -1,
            "meta": {},
        },
        {
            "id": TX_COST_BREAKDOWN,
            "ref_type": "completion",
            "ref_id": "completion-cost-breakdown",
            "idempotency_key": "migration-cost-breakdown",
            "amount_micro": -999,
            "meta": {
                "api_key_id": CREDENTIAL_1,
                "cost_breakdown": {"actual_cost_micro": "111"},
            },
        },
        {
            "id": TX_ACTUAL,
            "ref_type": "completion",
            "ref_id": "completion-actual",
            "idempotency_key": "migration-actual",
            "amount_micro": -999,
            "meta": {
                "api_key_id": CREDENTIAL_1,
                "actual_micro": "222",
            },
        },
        {
            "id": TX_COST,
            "ref_type": "completion",
            "ref_id": "completion-cost",
            "idempotency_key": "migration-cost",
            "amount_micro": -999,
            "meta": {
                "api_key_id": CREDENTIAL_1,
                "cost_micro": "333",
            },
        },
        {
            "id": TX_FALLBACK,
            "ref_type": "completion",
            "ref_id": "completion-fallback",
            "idempotency_key": "migration-fallback",
            "amount_micro": -444,
            "meta": {"api_key_id": CREDENTIAL_1},
        },
        {
            "id": TX_NON_COMPLETION,
            "ref_type": "generation",
            "ref_id": GENERATION,
            "idempotency_key": "migration-non-completion",
            "amount_micro": -777,
            "meta": {"api_key_id": CREDENTIAL_1},
        },
    ]
    connection.execute(
        text(
            """
            INSERT INTO wallet_transactions (
                id,
                user_id,
                kind,
                amount_micro,
                balance_after,
                hold_after,
                ref_type,
                ref_id,
                idempotency_key,
                meta
            )
            VALUES (
                :id,
                :user_id,
                'charge',
                :amount_micro,
                0,
                0,
                :ref_type,
                :ref_id,
                :idempotency_key,
                CAST(:meta AS jsonb)
            )
            """
        ),
        [
            {
                **row,
                "user_id": USER_1,
                "meta": json.dumps(row["meta"], separators=(",", ":")),
            }
            for row in rows
        ],
    )


def _replace_dirty_ledger(connection: Connection) -> None:
    connection.execute(text("DELETE FROM billing_window_usage_events"))
    connection.execute(
        text(
            """
            INSERT INTO billing_window_usage_events (
                wallet_transaction_id,
                user_id,
                credential_id,
                amount_micro
            )
            VALUES (
                :wallet_transaction_id,
                :user_id,
                :credential_id,
                :amount_micro
            )
            """
        ),
        [
            {
                "wallet_transaction_id": TX_COST_BREAKDOWN,
                "user_id": USER_1,
                "credential_id": CREDENTIAL_2,
                "amount_micro": 999,
            },
            {
                "wallet_transaction_id": TX_ACTUAL,
                "user_id": USER_1,
                "credential_id": CREDENTIAL_1,
                "amount_micro": 0,
            },
            {
                "wallet_transaction_id": TX_COST,
                "user_id": USER_1,
                "credential_id": CREDENTIAL_1,
                "amount_micro": 999,
            },
            {
                "wallet_transaction_id": TX_NON_COMPLETION,
                "user_id": USER_1,
                "credential_id": CREDENTIAL_1,
                "amount_micro": 777,
            },
        ],
    )


def _assert_constraint_rejected(
    connection: Connection,
    statement: str,
    params: dict[str, object],
    constraint_name: str,
) -> None:
    try:
        with connection.begin_nested():
            connection.execute(text(statement), params)
    except IntegrityError as exc:
        assert constraint_name in str(exc.orig)
    else:  # pragma: no cover - exercised only by a broken migration
        pytest.fail(f"{constraint_name} did not reject invalid data")


def _assert_head_state(engine: Engine, expected_retry_count: int) -> None:
    with engine.begin() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "0043_billing_consistency"
        retry_count = connection.scalar(
            text(
                """
                SELECT billing_retry_count
                FROM generations
                WHERE id = :generation_id
                """
            ),
            {"generation_id": GENERATION},
        )
        assert retry_count == expected_retry_count

        rows = connection.execute(
            text(
                """
                SELECT
                    wallet_transaction_id,
                    user_id,
                    credential_id,
                    amount_micro
                FROM billing_window_usage_events
                """
            )
        ).mappings()
        ledger = {
            row["wallet_transaction_id"]: (
                row["user_id"],
                row["credential_id"],
                row["amount_micro"],
            )
            for row in rows
        }
        assert ledger == {
            TX_COST_BREAKDOWN: (USER_1, CREDENTIAL_1, 111),
            TX_ACTUAL: (USER_1, CREDENTIAL_1, 222),
            TX_COST: (USER_1, CREDENTIAL_1, 333),
            TX_FALLBACK: (USER_1, CREDENTIAL_1, 444),
        }

        constraint_names = set(
            connection.scalars(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE connamespace = current_schema()::regnamespace
                    """
                )
            )
        )
        assert {
            "uq_redemption_batch_creator_idemp",
            "uq_user_api_credentials_id_user",
            "fk_billing_window_credential_user",
            "ck_billing_window_amount_positive",
        } <= constraint_names

        ledger_insert = """
            INSERT INTO billing_window_usage_events (
                wallet_transaction_id,
                user_id,
                credential_id,
                amount_micro
            )
            VALUES (
                :wallet_transaction_id,
                :user_id,
                :credential_id,
                :amount_micro
            )
        """
        _assert_constraint_rejected(
            connection,
            ledger_insert,
            {
                "wallet_transaction_id": TX_NON_COMPLETION,
                "user_id": USER_1,
                "credential_id": CREDENTIAL_1,
                "amount_micro": 0,
            },
            "ck_billing_window_amount_positive",
        )
        _assert_constraint_rejected(
            connection,
            ledger_insert,
            {
                "wallet_transaction_id": TX_NON_COMPLETION,
                "user_id": USER_1,
                "credential_id": CREDENTIAL_2,
                "amount_micro": 1,
            },
            "fk_billing_window_credential_user",
        )

        connection.execute(
            text(
                """
                INSERT INTO redemption_batches (
                    id,
                    created_by,
                    idempotency_key,
                    request_hash,
                    amount_micro,
                    code_count,
                    max_redemptions
                )
                VALUES (
                    :id,
                    :created_by,
                    :idempotency_key,
                    :request_hash,
                    1,
                    1,
                    1
                )
                """
            ),
            {
                "id": BATCH_1,
                "created_by": USER_1,
                "idempotency_key": "migration-batch",
                "request_hash": "a" * 64,
            },
        )
        _assert_constraint_rejected(
            connection,
            """
            INSERT INTO redemption_batches (
                id,
                created_by,
                idempotency_key,
                request_hash,
                amount_micro,
                code_count,
                max_redemptions
            )
            VALUES (
                :id,
                :created_by,
                :idempotency_key,
                :request_hash,
                1,
                1,
                1
            )
            """,
            {
                "id": BATCH_2,
                "created_by": USER_1,
                "idempotency_key": "migration-batch",
                "request_hash": "b" * 64,
            },
            "uq_redemption_batch_creator_idemp",
        )


def _assert_downgraded_to_0041(engine: Engine) -> None:
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "0041_billing_window_ledger"
        retry_column_count = connection.scalar(
            text(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'generations'
                  AND column_name = 'billing_retry_count'
                """
            )
        )
        assert retry_column_count == 0
        assert (
            connection.scalar(text("SELECT to_regclass('redemption_batches')")) is None
        )


def test_billing_migrations_round_trip_postgres() -> None:
    base_url = _postgres_url()
    schema = f"lumen_billing_migration_{uuid4().hex[:16]}"
    admin_engine = create_engine(base_url, poolclass=NullPool)
    schema_engine: Engine | None = None
    schema_created = False

    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        schema_created = True

        database_url = _schema_url(base_url, schema)
        schema_engine = create_engine(database_url, poolclass=NullPool)

        _create_0041_baseline(schema_engine)
        _run_alembic(database_url, "stamp", "0041_billing_window_ledger")
        with schema_engine.begin() as connection:
            _insert_base_fixture(connection)
            _insert_wallet_fixture(connection)
            _replace_dirty_ledger(connection)

        _run_alembic(database_url, "upgrade", "0043_billing_consistency")
        _assert_head_state(schema_engine, expected_retry_count=5)

        _run_alembic(
            database_url,
            "downgrade",
            "0041_billing_window_ledger",
        )
        _assert_downgraded_to_0041(schema_engine)
        with schema_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE wallet_transactions
                    SET ref_id = :ref_id
                    WHERE id = :transaction_id
                    """
                ),
                {
                    "ref_id": f"{GENERATION}:retry:7",
                    "transaction_id": TX_RETRY_MAX,
                },
            )
            _replace_dirty_ledger(connection)

        _run_alembic(database_url, "upgrade", "0043_billing_consistency")
        _assert_head_state(schema_engine, expected_retry_count=7)
    finally:
        if schema_engine is not None:
            schema_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()
