"""add settlement_outbox table (Saga v0.1)

Backs the transactional outbox described in
``acn/docs/_drafts/settlement-saga-design.md`` and the v0.1 plan
``.cursor/plans/settlement_saga_mvp_*.plan.md``.

The whole point of this table is to participate in the *same*
PostgreSQL ACID transaction that flips ``tasks.status = 'completed'``
in ``task_service.complete_task``. With the outbox row written
alongside the CAS, the two outcomes can no longer diverge — either
both rows land or neither does. The settlement steps themselves
(escrow release / reward distribute / reputation write) run
asynchronously in ``SettlementWorker``, which reads this table.

Why a partial index on (state, next_attempt_at):
- ``done`` rows accumulate forever (audit value), but ``done`` is the
  vast majority of rows in steady state.
- The worker's hot query is
  ``WHERE state IN ('pending','retrying') AND next_attempt_at <= now()``
  — the partial index filters ``done`` out at the index level so the
  query cost stays O(active) regardless of total row count.
- A non-partial index on ``state`` would still work but bloats with
  ``done`` entries (we expect millions over the lifetime of the
  product). Partial keeps the index small AND the query plan stable.

Why ``event_id UUID UNIQUE`` rather than a generic text natural key:
- Producer generates ``event_id`` as ``uuid5(NS, "task_id:trigger")``
  so the same (task, trigger) pair always derives the same UUID.
- UNIQUE means a buggy producer (or the API edge retrying a
  ``complete_task`` call) cannot enqueue twice. The conflict happens
  at the DB layer, no race window.

Revision ID: a7b8c9d0e1f2
Revises: e5f6a7b8c9d1
Create Date: 2026-05-11 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7b8c9d0e1f2"
# Linearise behind the social_card_url head so the chain stays
# single-headed. settlement_outbox creates a new table that has no
# relationship with agents.social_card_url, so the ordering is
# arbitrary; we pick the later head to avoid a separate merge revision.
down_revision: str | None = "e5f6a7b8c9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settlement_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            comment="Idempotency key, uuid5(NS, 'task_id:trigger'). UNIQUE.",
        ),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column(
            "trigger",
            sa.String(64),
            nullable=False,
            comment="Why the event was emitted. v0.1: 'review_pass' only.",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="All context for the worker to settle without re-reading tasks.",
        ),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="pending | paying | retrying | done | dead",
        ),
        sa.Column(
            "step_status",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="{step_name: 'done'|'pending'|'skipped'} — for mid-saga resume.",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_settlement_outbox_event_id"),
    )

    # Hot path index — partial so ``done`` rows don't bloat it. Worker's
    # claim_batch query keys off (state, next_attempt_at), ORDER BY
    # next_attempt_at.
    op.execute(
        """
        CREATE INDEX ix_settlement_outbox_state_next
        ON settlement_outbox (state, next_attempt_at)
        WHERE state IN ('pending', 'retrying')
        """
    )

    # Lookup by task — used by DLQ tools, replay, and reporting. Not on
    # the hot worker path, so a regular b-tree is fine.
    op.create_index("ix_settlement_outbox_task_id", "settlement_outbox", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_settlement_outbox_task_id", table_name="settlement_outbox")
    op.execute("DROP INDEX IF EXISTS ix_settlement_outbox_state_next")
    op.drop_table("settlement_outbox")
