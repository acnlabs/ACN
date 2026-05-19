"""add subnet_join_requests + subnet_allowlist tables (ADR-0004 Phase 2 Slice 2.1)

Two new tables backing the join-policy state machine and the
admission-allowlist. See ADR-0004 §"SubnetJoinRequest schema
(three-in-one table)" and §"SubnetAllowlist schema" for the field
rationale, and ``acn/core/entities/subnet_join_request.py`` /
``subnet_allowlist.py`` for the entity-layer invariants.

Schema decisions
----------------
1. ``subnet_join_requests``:
   - ``request_id`` ``VARCHAR`` (not ``UUID``) — matches the
     codebase-wide convention of storing UUID4 values as lower-case
     hex strings.
   - ``kind``, ``status`` capped at ``VARCHAR(16)`` to match the ADR
     data-model table. Caps block free-form strings if a future
     caller bypasses the entity ``Literal`` check.
   - ``note`` is ``TEXT`` (unbounded at the DB layer); the
     application enforces the 500-char cap. Mirrors the pattern
     ``agent_allowlist.reason`` uses.
   - **Unique partial index** ``WHERE status='pending'`` on
     ``(subnet_id, agent_id)``. THE invariant of the table:
     terminal rows accumulate for audit, but at most one pending
     row per ``(subnet, agent)`` across all kinds. Without this,
     a self-join racing an invitation could create two concurrent
     pending rows that both try to transition to approved.
   - **Partial index** ``WHERE kind='invitation' AND
     status='pending'`` on ``agent_id``. Sized proportional to
     in-flight invitations (not full audit history), which keeps
     the invitee-facing ``GET /agents/{a}/subnet-invitations``
     fast on subnets with deep audit logs.

2. ``subnet_allowlist``:
   - Composite primary key ``(subnet_id, agent_id)`` — natural
     key, no surrogate id. Same shape ``agent_allowlist`` uses for
     ``(owner_id, target_id)``.
   - **No FK** to ``subnets.subnet_id`` or ``agents.agent_id``.
     ADR §"Cascade deletion" explicitly chooses a manual cascade
     for observability symmetry with ADR-0003's parent-subnet
     cascade; route-layer existence checks return clean 404s
     instead of raw IntegrityErrors.

Migration safety
----------------
Both tables are brand new — no backfill, no in-place rewrite, no
``ALTER`` against a populated table. Pure ``CREATE TABLE`` +
``CREATE INDEX`` against empty schema. Safe on every PostgreSQL
version ≥10 (no version-sensitive DDL like the Phase 1 fast-
default). Downgrade is a clean ``DROP TABLE`` — no audit data is
preserved across a downgrade because by definition no row pre-
dates the table.

Backed-up Redis layout is created lazily by the Redis repositories
on first write — there is no Redis-side equivalent of this DDL
migration. See ``RedisSubnetJoinRequestRepository`` and
``RedisSubnetAllowlistRepository`` for the key layout.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3b5c7d9e1f2"
down_revision: str | None = "f7b9c2d4e8a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # subnet_join_requests
    # ------------------------------------------------------------------
    op.create_table(
        "subnet_join_requests",
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("subnet_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("initiated_by", sa.String(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    # The "at most one pending per (subnet, agent) across all kinds"
    # invariant — the schema-level enforcement that backs the §join
    # branch decision and prevents two-pending races.
    op.create_index(
        "subnet_join_requests_pending_unique",
        "subnet_join_requests",
        ["subnet_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Owner-facing listing (GET /subnets/{s}/join-requests, /invitations).
    op.create_index(
        "ix_subnet_join_requests_subnet_id",
        "subnet_join_requests",
        ["subnet_id"],
    )
    # Invitee-facing listing (GET /agents/{a}/subnet-invitations).
    # Partial index keeps size proportional to in-flight invitations.
    op.create_index(
        "ix_subnet_join_requests_agent_pending_invitations",
        "subnet_join_requests",
        ["agent_id"],
        postgresql_where=sa.text(
            "kind = 'invitation' AND status = 'pending'"
        ),
    )

    # ------------------------------------------------------------------
    # subnet_allowlist
    # ------------------------------------------------------------------
    op.create_table(
        "subnet_allowlist",
        sa.Column("subnet_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("added_by", sa.String(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("subnet_id", "agent_id"),
    )
    # Reverse lookup: "which subnets is agent X preauthorised on?"
    op.create_index(
        "ix_subnet_allowlist_agent_id",
        "subnet_allowlist",
        ["agent_id"],
    )


def downgrade() -> None:
    # Reverse order — drop indexes before their tables (good hygiene
    # even though ``DROP TABLE`` would cascade them; explicit form
    # also documents the intent for anyone reading the migration).
    op.drop_index(
        "ix_subnet_allowlist_agent_id",
        table_name="subnet_allowlist",
    )
    op.drop_table("subnet_allowlist")

    op.drop_index(
        "ix_subnet_join_requests_agent_pending_invitations",
        table_name="subnet_join_requests",
    )
    op.drop_index(
        "ix_subnet_join_requests_subnet_id",
        table_name="subnet_join_requests",
    )
    op.drop_index(
        "subnet_join_requests_pending_unique",
        table_name="subnet_join_requests",
    )
    op.drop_table("subnet_join_requests")
