"""add reputation_events table (Saga v0.1, off-chain reputation)

Backs the off-chain reputation container described in
``acn/docs/_drafts/settlement-saga-design.md`` §5 and the v0.1 plan
``.cursor/plans/settlement_saga_mvp_*.plan.md``.

Why an off-chain table for v0.1 rather than direct ERC-8004 chain writes:
the chain-write adapter requires private-key custody, gas budgeting,
and a refund flow for failed writes — none of which are on the v0.1
critical path. The schema here matches ERC-8004 semantics so the v1
chain-write upgrade is "replay these rows onto chain" not "redesign
the events".

Why ``UNIQUE(agent_id, task_id, kind)``:
``SettlementWorker.reputation_write`` step is idempotent at the worker
level (it checks ``step_status['reputation_write']`` before calling),
but worker retries that crash before updating ``step_status`` would
duplicate without DB-level protection. The unique constraint plus
``INSERT ... ON CONFLICT DO NOTHING`` collapses retries into one row.

Why ``event_metadata`` rather than joining ``tasks`` for smoke filtering:
the smoke-test isolation contract (plan §7) says reputation queries
filter out events whose source task was a smoke task. Joining
``tasks`` couples every reputation read to the ``tasks`` schema and
makes ``tasks`` archival impossible. Instead, the producer copies
the ``smoke_test`` flag into the event's own ``event_metadata`` JSONB
at write time. ``reputation_events`` is then self-contained.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-05-11 13:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reputation_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "agent_id",
            sa.String(),
            nullable=False,
            comment=(
                "Agent being reviewed / validated. Unbounded to match "
                "agents.agent_id and tasks.task_id (review fix R11)."
            ),
        ),
        sa.Column(
            "task_id",
            sa.String(),
            nullable=False,
            comment="Originating task. Unbounded. Not a FK — see model comment.",
        ),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            comment="'feedback' | 'validation'. v0.1 emits 'feedback' only.",
        ),
        sa.Column(
            "score",
            sa.Integer(),
            nullable=True,
            comment="v0.1 always NULL; v0.2 0-100 graded score.",
        ),
        sa.Column(
            "evidence_uri",
            sa.Text(),
            nullable=True,
            comment="Optional off-chain pointer to evidence (image, IPFS).",
        ),
        sa.Column(
            "signer",
            sa.String(),
            nullable=False,
            comment=(
                "Who issued the event (creator for feedback, validator "
                "for validation). Unbounded — signer IS an agent_id."
            ),
        ),
        sa.Column(
            "attestation",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Validation signed JSON payload; NULL for feedback rows.",
        ),
        sa.Column(
            "event_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Copied subset of task metadata (smoke_test flag, ...).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        # Idempotency safety net for worker retries.
        sa.UniqueConstraint(
            "agent_id",
            "task_id",
            "kind",
            name="uq_reputation_events_agent_task_kind",
        ),
    )

    # Primary read path: "reputation history of agent X".
    op.create_index(
        "ix_reputation_events_agent_id",
        "reputation_events",
        ["agent_id"],
    )
    # Secondary read path: "events for task X" (ops, smoke backfill).
    op.create_index(
        "ix_reputation_events_task_id",
        "reputation_events",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_reputation_events_task_id", table_name="reputation_events")
    op.drop_index("ix_reputation_events_agent_id", table_name="reputation_events")
    op.drop_table("reputation_events")
