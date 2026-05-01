"""add agent_allowlist capacity trigger (Phase 2 PR #2 v3)

PR #2 v3 review fix P1-A1 — close the TOCTOU race in
``AllowlistService.add``. The service layer's
``count_for_owner() >= MAX_ALLOWLIST_SIZE`` pre-check happens in a
separate Postgres round-trip from the ``INSERT``: two concurrent
``add()`` calls for the same owner can both observe ``count=499`` and
both insert, ending up at 501.

We close the race at the database with a per-owner
``BEFORE INSERT`` trigger:

* ``pg_advisory_xact_lock(hashtext(owner_id))`` serialises concurrent
  inserts targeting the same owner — transaction-scoped, released on
  COMMIT/ROLLBACK, no per-row penalty for different owners.
* The trigger then re-counts under the lock and ``RAISE``s
  ``check_violation`` (SQLSTATE 23514) if the cap is reached.
* Idempotent re-add path is preserved: when ``(owner_id, target_id)``
  already exists, the trigger returns NEW without applying the cap so
  the outer ``ON CONFLICT DO NOTHING`` can silently no-op.

The trigger constant ``cap = 500`` mirrors
``acn.services.allowlist_service.MAX_ALLOWLIST_SIZE``. If you change
one, change both — there is a corresponding warning comment in the
service module.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-01 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Forward migration: install function + trigger
# ---------------------------------------------------------------------------
#
# Notes on the SQL design:
#
# 1. ``pg_advisory_xact_lock`` is preferred over advisory_lock because
#    it auto-releases on COMMIT/ROLLBACK — no risk of an aborted
#    transaction leaving a stuck lock.
# 2. ``hashtext(NEW.owner_id::text)`` reduces an arbitrary-length owner
#    id to a 32-bit int compatible with the single-arg lock variant.
#    Two owner ids collide on hash with probability 2^-32 ≈ 0; collision
#    only causes spurious serialisation, never a missed cap check.
# 3. Idempotent path: when the (owner_id, target_id) row already
#    exists, the trigger short-circuits BEFORE the count check so a
#    full allowlist can still receive repeat-add 200 responses
#    (matches the service-layer ``is_member`` short-circuit).
# 4. ``ERRCODE = 'check_violation'`` (SQLSTATE 23514) is the SQL-
#    standard slot for "row violated a check constraint". The Python
#    repo layer (postgres/allowlist_repository.py) detects this code
#    and re-raises ``AllowlistCapacityExceededError``.
_CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_agent_allowlist_capacity()
RETURNS trigger AS $$
DECLARE
    cap CONSTANT integer := 500;
    cur integer;
BEGIN
    -- Per-owner advisory lock: serialises concurrent INSERTs for the
    -- same owner. Different owners proceed in parallel.
    PERFORM pg_advisory_xact_lock(hashtext(NEW.owner_id::text));

    -- Idempotent skip: pre-existing (owner_id, target_id) means the
    -- outer ON CONFLICT DO NOTHING will silence the INSERT. Don't
    -- apply the capacity check, otherwise a full allowlist's
    -- repeat-add would falsely 429.
    IF EXISTS(
        SELECT 1 FROM agent_allowlist
        WHERE owner_id = NEW.owner_id
          AND target_id = NEW.target_id
    ) THEN
        RETURN NEW;
    END IF;

    SELECT count(*) INTO cur
    FROM agent_allowlist
    WHERE owner_id = NEW.owner_id;

    IF cur >= cap THEN
        RAISE EXCEPTION
            'agent_allowlist capacity exceeded for owner % (limit %)',
            NEW.owner_id, cap
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_CREATE_TRIGGER = """
CREATE TRIGGER trg_agent_allowlist_capacity
BEFORE INSERT ON agent_allowlist
FOR EACH ROW
EXECUTE FUNCTION enforce_agent_allowlist_capacity();
"""

_DROP_TRIGGER = """
DROP TRIGGER IF EXISTS trg_agent_allowlist_capacity ON agent_allowlist;
"""

_DROP_FUNCTION = """
DROP FUNCTION IF EXISTS enforce_agent_allowlist_capacity();
"""


def upgrade() -> None:
    op.execute(_CREATE_FUNCTION)
    op.execute(_CREATE_TRIGGER)


def downgrade() -> None:
    # Drop trigger first (depends on function).
    op.execute(_DROP_TRIGGER)
    op.execute(_DROP_FUNCTION)
