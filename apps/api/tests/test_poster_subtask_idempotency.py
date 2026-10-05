"""SQLite-enforced poster key regression, without providers or user data."""
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, String, Table, UniqueConstraint, create_engine
from sqlalchemy.exc import IntegrityError

from app.workflows.adapters.paid_idempotency import (
    SQLAlchemyPaidOperationPort, current_paid_operation_subtask_key,
)
from app.workflows.application.paid_idempotency import paid_operation_request
from app.workflows.application.poster_generation import GeneratePosterRenders, generate_poster_renders
from lumen_core.models import Generation


def bound_db(key):
    db = SimpleNamespace(info={})
    SQLAlchemyPaidOperationPort(db).bind(paid_operation_request(
        user_id="user", idempotency_key=key,
        operation_namespace="workflow.poster_design.renders.create", payload={},
    ))
    return db


@pytest.mark.asyncio
async def test_failed_render_new_attempt_preserves_history_and_unique_constraint():
    constraint = next(c for c in Generation.__table__.constraints
                      if isinstance(c, UniqueConstraint) and c.name == "uq_gen_user_idemp")
    table = Table("generations", MetaData(),
                  Column("user_id", String(36)), Column("idempotency_key", String(64)),
                  UniqueConstraint(*(c.name for c in constraint.columns), name=constraint.name))
    engine = create_engine("sqlite://")
    table.create(engine)
    command = GeneratePosterRenders(
        run_id="same-prefix-full-run-id", master_id="same-prefix-full-master-id",
        pending_aspects=["1:1"], style_summary={}, copy_analysis={},
        reference_image_ids=[], quality_mode="1K", use_master_as_reference=False,
        adjustments="",
    )
    keys = []
    with engine.begin() as connection:
        class Port:
            def __init__(self, db):
                self.db = db

            async def submit_render(self, task):
                key = current_paid_operation_subtask_key(self.db, task.idempotency_key)
                connection.execute(table.insert().values(user_id="user", idempotency_key=key))
                keys.append(key)
                return SimpleNamespace(bundle=None, generation_ids=(key,))

        first = bound_db("first-attempt")
        await generate_poster_renders(command, port=Port(first))
        await generate_poster_renders(command, port=Port(bound_db("retry-attempt")))
        assert len(set(keys)) == 2
        assert all(len(key) == 64 for key in keys)
        with pytest.raises(IntegrityError):
            await generate_poster_renders(command, port=Port(first))
    engine.dispose()


@pytest.mark.parametrize("action", ["rv", "in"])
def test_full_render_identity_and_outer_operation_are_preserved(action):
    db = bound_db("one")
    a = f"wf:06ac2fb5-shared-run:{action}:06ac2fb5-render-a"
    b = f"wf:06ac2fb5-shared-run:{action}:06ac2fb5-render-b"
    assert current_paid_operation_subtask_key(db, a) != current_paid_operation_subtask_key(db, b)
    assert current_paid_operation_subtask_key(db, a) == current_paid_operation_subtask_key(db, a)
    assert current_paid_operation_subtask_key(db, a) != current_paid_operation_subtask_key(bound_db("two"), a)


def test_direct_internal_calls_are_independent_attempts():
    db = SimpleNamespace(info={})
    assert current_paid_operation_subtask_key(db, "same") != current_paid_operation_subtask_key(db, "same")
