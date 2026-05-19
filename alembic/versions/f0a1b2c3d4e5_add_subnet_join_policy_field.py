"""add subnet join_policy field + backfill existing private subnets

ADR-0004 Phase 1: extends the ``subnets`` table with a single new
column ``join_policy`` (default ``'open'``) that decouples subnet
admission from discoverability, and **atomically backfills every
existing row with ``is_private = true``** to ``join_policy =
'approval'`` so the historical "private but joinable by anyone"
semantic gap closes the moment the migration runs.

This migration ships only the column and backfill. The new
``subnet_join_requests`` and ``subnet_allowlist`` tables — together
with the service / route / CLI consumers that drive the request /
invitation / allowlist state machine — land in Phase 2 once the
service layer is wired to read ``join_policy`` on the join path.

Schema change
-------------
``join_policy VARCHAR(16) NOT NULL DEFAULT 'open'``. The length cap
matches the ADR's data-model table and prevents the column from
silently absorbing free-form strings if a future caller bypasses
the entity-layer ``Literal`` check. Default keeps every existing
row eligible for fast-path inserts under the legacy "open"
semantics; the immediately-following backfill flips ``is_private =
true`` rows to ``'approval'`` so the post-migration database
satisfies the entity-layer invariant
(``is_private = true`` ⇒ ``join_policy = 'approval'``) for every
single row before any new code path can read it.

PostgreSQL version requirement
------------------------------
**Requires PostgreSQL ≥11.** On PG 11+ the ``ALTER TABLE ... ADD
COLUMN ... NOT NULL DEFAULT 'open'`` is a metadata-only "fast
default" operation (Tom Lane's PG-11 feature) that completes in
O(1) regardless of table size. On PG ≤10 the same statement
rewrites the entire ``subnets`` table while holding ACCESS
EXCLUSIVE on it, locking writes for minutes on production-sized
tables. Verify ``SHOW server_version`` before invoking
``alembic upgrade head``; reject the upgrade if version < 11.

Backfill in the same revision
-----------------------------
The ``UPDATE`` runs inside ``upgrade()`` immediately after the
column add, in the same Alembic transaction. Three reasons to keep
them coupled rather than splitting into two revisions:

1. **No transient window.** If we shipped column-add as one
   migration and the backfill as the next, a deployment that paused
   between them would have every private subnet at the entity-
   invariant-violating ``private + open`` combination — exactly the
   gap this ADR exists to close, transiently re-introduced.
2. **Idempotency is trivial.** The ``WHERE is_private = true``
   predicate makes the update naturally idempotent (re-running
   ``upgrade()`` on already-backfilled data is a no-op), so no
   sentinel column or audit row is needed.
3. **Downgrade is clean.** ``downgrade()`` only needs to drop the
   column. The backfilled values vanish with it, leaving rows
   structurally identical to the pre-upgrade state.

Coordination with Redis
-----------------------
Redis-fallback deployments (per ``acn/AGENTS.md`` — Redis is the
canonical store, Postgres is optional) must also run
``scripts/backfill_subnet_join_policy.py`` after this migration
completes. The repo layer auto-upgrades missing ``join_policy`` on
read for ``is_private = true`` rows (see ``Subnet.from_dict`` and
``RedisSubnetRepository._dict_to_subnet``) so reads remain
self-consistent during the migration window, but writes go through
``save()`` which assumes the field is set on the entity — running
the backfill script makes the stored representation match.

Revision ID: f0a1b2c3d4e5
Revises: e1f2a3b4c5d6
Create Date: 2026-05-18 14:50:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Column first: NOT NULL with ``server_default='open'`` so the
    # ALTER fills every existing row in a single statement (PG would
    # otherwise refuse a NOT NULL column on a non-empty table).
    op.add_column(
        "subnets",
        sa.Column(
            "join_policy",
            sa.String(length=16),
            nullable=False,
            server_default="open",
        ),
    )

    # Backfill: flip every existing ``is_private = true`` row to
    # ``approval``. This closes the historical "private but joinable
    # by anyone" gap atomically with the schema change — every read
    # path that lands after this migration sees a row that satisfies
    # the entity invariant.
    op.execute(
        sa.text(
            "UPDATE subnets SET join_policy = 'approval' "
            "WHERE is_private = true"
        )
    )


def downgrade() -> None:
    # Drop the column. The backfilled values vanish with it; the
    # downgraded database is structurally identical to the
    # pre-upgrade state, with the historical bug re-exposed.
    # ``server_default`` is dropped automatically alongside the
    # column on PostgreSQL.
    op.drop_column("subnets", "join_policy")
