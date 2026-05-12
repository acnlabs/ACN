"""Real-PostgreSQL integration tests for settlement outbox semantics.

The fakes in ``tests/services/_settlement_fakes.py`` cover saga
orchestration without a real database. The two cases below ARE
about PG-specific behaviour that no fake can faithfully reproduce:

1. **Multi-replica SKIP LOCKED**: two ``claim_batch`` callers
   running concurrently must not return the same row. This is
   exactly the ``SELECT ... FOR UPDATE SKIP LOCKED`` contract.
2. **Same-transaction atomicity**: when a UoW transaction wraps
   an ``outbox.enqueue`` and the transaction subsequently raises,
   the outbox row must NOT survive the rollback. This is the
   "atomic save + enqueue" promise the saga design depends on.

These tests are gated behind ``ACN_INTEGRATION_PG_URL``. They skip
silently when the env var is missing so a local ``pytest`` on a
laptop without Postgres still passes the rest of the suite. CI is
expected to set the variable to a disposable database (the tests
DROP and re-CREATE the ``settlement_outbox`` table on setup).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from acn.core.interfaces.settlement_outbox_repository import SettlementEvent
from acn.infrastructure.persistence.postgres.models import (
    SettlementOutboxModel,
)
from acn.infrastructure.persistence.postgres.settlement_outbox_repository import (
    PostgresSettlementOutboxRepository,
)
from acn.infrastructure.persistence.postgres.unit_of_work import (
    PostgresUnitOfWork,
)

# Module-level marker — every test below needs PG. Easier than
# applying the skip decorator to every function and impossible to
# forget when adding a new test.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ACN_INTEGRATION_PG_URL"),
        reason="needs ACN_INTEGRATION_PG_URL pointing at a disposable async PG",
    ),
]


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def engine_and_factory() -> tuple[Any, Any]:
    """Open an engine against the disposable test DB and (re)create
    the ``settlement_outbox`` table. We don't tear down the table
    after the test — leaving rows around makes triage easier when
    a failure happens. CI rotates the DB between runs.
    """
    url = os.environ["ACN_INTEGRATION_PG_URL"]
    engine = create_async_engine(url, future=True, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: SettlementOutboxModel.__table__.drop(c, checkfirst=True)
        )
        await conn.run_sync(
            lambda c: SettlementOutboxModel.__table__.create(c, checkfirst=True)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, factory
    await engine.dispose()


def _event(event_id: str, task_id: str | None = None) -> SettlementEvent:
    return SettlementEvent(
        event_id=event_id,
        task_id=task_id or f"task-{event_id}",
        trigger="review_pass",
        payload={
            "task_id": task_id or f"task-{event_id}",
            "creator_id": "user-creator",
            "assignee_id": "agent-worker",
            "reward": "100",
            "reward_currency": "ap_points",
            "use_escrow": True,
            "is_multi": False,
            "metadata": {},
        },
        step_status={
            "escrow_release": "pending",
            "reward_distribute": "pending",
            "reputation_write": "pending",
        },
    )


# =============================================================================
# 6. SKIP LOCKED — concurrent claim_batch does not double-deliver
# =============================================================================


@pytest.mark.asyncio
async def test_skip_locked_prevents_double_claim_across_replicas(
    engine_and_factory: tuple[Any, Any],
) -> None:
    """Simulate two worker REPLICAS running in lockstep:
      - Insert two ready rows
      - Open two sessions (replica A and replica B)
      - Replica A begins its claim transaction and locks row 1
        (without committing yet)
      - Replica B then runs claim_batch — must see row 2 only,
        NOT row 1 (SKIP LOCKED contract)
      - A commits; B continues; the union of their claims is
        exactly {row 1, row 2} with no duplicates

    We can't drive this through the high-level ``claim_batch`` API
    because that method commits immediately, removing the
    interleaving window. So this test runs raw SQL with the same
    SKIP LOCKED clause the repo uses internally — the contract it
    is asserting is "PG's SKIP LOCKED prevents the double-claim
    scenario the worker depends on".
    """
    _engine, factory = engine_and_factory
    repo = PostgresSettlementOutboxRepository(session_factory=factory)

    # Two ready rows.
    assert await repo.enqueue(_event("evt-A")) is True
    assert await repo.enqueue(_event("evt-B")) is True

    a_started = asyncio.Event()
    a_can_commit = asyncio.Event()

    async def replica_a() -> str:
        """Lock the first ready row but pause before commit."""
        async with factory() as session:
            session: AsyncSession  # type: ignore[no-redef]
            async with session.begin():
                row = (
                    await session.execute(
                        SettlementOutboxModel.__table__.select()
                        .where(SettlementOutboxModel.state == "pending")
                        .order_by(SettlementOutboxModel.id)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                ).first()
                assert row is not None
                event_id = row.event_id
                a_started.set()
                # Hold the row lock until the test releases us.
                await a_can_commit.wait()
            return event_id

    async def replica_b() -> str | None:
        """Race in; must see the OTHER row, not A's locked row."""
        await a_started.wait()
        async with factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        SettlementOutboxModel.__table__.select()
                        .where(SettlementOutboxModel.state == "pending")
                        .order_by(SettlementOutboxModel.id)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                ).first()
                return row.event_id if row is not None else None

    a_task = asyncio.create_task(replica_a())
    b_event = await replica_b()
    a_can_commit.set()
    a_event = await a_task

    assert {a_event, b_event} == {"evt-A", "evt-B"}, (
        f"SKIP LOCKED contract violated: A={a_event}, B={b_event}"
    )


# =============================================================================
# 7. Same-transaction atomicity — UoW rollback drops the outbox row
# =============================================================================


@pytest.mark.asyncio
async def test_uow_rollback_drops_enqueued_outbox_row(
    engine_and_factory: tuple[Any, Any],
) -> None:
    """The whole point of the UoW + outer-session ``enqueue`` is so
    a saga producer can commit ``compare_and_save(task)`` and
    ``outbox.enqueue(event)`` atomically. If the saga body raises
    after enqueue but before UoW commit, the outbox row must NOT
    survive — otherwise a worker would later try to settle a task
    whose status never moved.
    """
    engine, factory = engine_and_factory
    repo = PostgresSettlementOutboxRepository(session_factory=factory)
    uow = PostgresUnitOfWork(session_factory=factory)

    class _SagaSimulatedFailure(RuntimeError):
        pass

    with pytest.raises(_SagaSimulatedFailure):
        async with uow.transaction() as session:
            inserted = await repo.enqueue(_event("evt-rollback"), session=session)
            assert inserted is True
            # Saga producer raises before UoW commits. UoW's
            # exception handler rolls back; the enqueued row must
            # disappear.
            raise _SagaSimulatedFailure("simulated CAS save failure mid-saga")

    # Verify the row never made it to disk.
    async with factory() as session:
        result = await session.execute(
            SettlementOutboxModel.__table__.select().where(
                SettlementOutboxModel.event_id == "evt-rollback"
            )
        )
        rows = result.all()
        assert rows == [], (
            "UoW rollback failed to drop enqueued outbox row — "
            "atomicity contract broken"
        )


# =============================================================================
# Bonus: round-trip sanity — enqueue + claim_batch on real PG
# =============================================================================


@pytest.mark.asyncio
async def test_enqueue_claim_done_round_trip(
    engine_and_factory: tuple[Any, Any],
) -> None:
    """Sanity check that the PG repo's high-level methods produce
    the same observable state machine as the in-memory fake. This
    is cheap insurance against the fake drifting from PG semantics
    over time.
    """
    _engine, factory = engine_and_factory
    repo = PostgresSettlementOutboxRepository(session_factory=factory)

    await repo.enqueue(_event("evt-rt"))

    batch = await repo.claim_batch(limit=10, now=datetime.now(UTC))
    assert len(batch) == 1
    assert batch[0].event_id == "evt-rt"

    await repo.mark_done("evt-rt")
    counts = await repo.count_by_state()
    assert counts["done"] == 1
    assert counts["pending"] == 0
    assert counts["paying"] == 0


# =============================================================================
# 8. update_step_status — JSONB merge round-trip on real PG
# =============================================================================
#
# This guards against a real production regression:
# the original ``update_step_status`` SQL wrote ``to_jsonb(:status::text)``
# but SQLAlchemy's ``text()`` parser refused to recognise ``:status``
# as a bound parameter because the ``::`` PostgreSQL cast suffix
# collided with the ``:param`` placeholder rule, raising
# ``ArgumentError: This text() construct doesn't define a bound
# parameter named 'status'`` at runtime — but ONLY against a real PG;
# every fake-repo test in the suite happily passed.
#
# Production symptom: worker's business steps all succeeded
# (``reputation_write`` wrote the row, ``escrow_release`` /
# ``reward_distribute`` correctly skipped) but ``step_status`` was
# never persisted, so the saga state machine looped at ``retrying``
# until ``max_attempts``. Fix: drop the redundant ``::text`` cast
# (``bindparam`` already binds Python ``str`` to PG ``text``).
#
# This test runs the actual SQL and asserts the JSONB merge
# materialised — guarantees we never regress to the broken form
# without a real-PG test catching it.


@pytest.mark.asyncio
async def test_update_step_status_persists_step_value(
    engine_and_factory: tuple[Any, Any],
) -> None:
    _engine, factory = engine_and_factory
    repo = PostgresSettlementOutboxRepository(session_factory=factory)

    # Start from the default step_status the producer writes.
    await repo.enqueue(_event("evt-step"))

    # Patch one step — this is the path that was broken in prod.
    await repo.update_step_status(
        event_id="evt-step",
        step="reputation_write",
        status="ok",
    )

    # Patch a second step to verify multi-key JSONB merge survives.
    await repo.update_step_status(
        event_id="evt-step",
        step="escrow_release",
        status="skipped",
    )

    # Read back via raw SQL to bypass any ORM-side caching.
    async with factory() as session:
        row = (
            await session.execute(
                SettlementOutboxModel.__table__.select().where(
                    SettlementOutboxModel.event_id == "evt-step"
                )
            )
        ).first()
        assert row is not None
        assert row.step_status == {
            "escrow_release": "skipped",
            "reward_distribute": "pending",
            "reputation_write": "ok",
        }, (
            f"update_step_status JSONB merge broken: got {row.step_status!r}"
        )
