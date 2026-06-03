"""drop agents.auth0_client_id and auth0_token_endpoint columns (ADR-0007 Phase 3 follow-up)

These columns were part of the Auth0 M2M credential flow for agent identity,
decommissioned in ADR-0007 Phase 3 (PR #151). The columns were retained
after Phase 3 to bound the blast radius of that change. Agent registration
no longer writes these fields (auth0_client_id was never written after
Phase 3 shipped). This migration drops them now that all references have
been removed from the entity, model, and both repository layers (#152).

``auth0_client_secret`` was already not persisted to the DB (in-memory only
per the entity comment); no column exists for it.

Safety
------
- Both columns are nullable with no NOT NULL / DEFAULT constraints, so the
  DROP is non-blocking on Postgres 11+ (no table rewrite).
- No indexes or foreign keys reference these columns.
- Railway's deploy model is single-instance stop-old/start-new; alembic
  upgrade head runs before uvicorn binds the port.

Revision ID: a1b2c3d4e5f6
Revises: f7b9c2d4e8a1
"""

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f7b9c2d4e8a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("agents", "auth0_client_id")
    op.drop_column("agents", "auth0_token_endpoint")


def downgrade() -> None:
    import sqlalchemy as sa

    op.add_column("agents", sa.Column("auth0_token_endpoint", sa.String(), nullable=True))
    op.add_column("agents", sa.Column("auth0_client_id", sa.String(), nullable=True))
