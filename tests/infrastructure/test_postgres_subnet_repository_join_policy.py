"""PostgresSubnetRepository — ADR-0004 ``join_policy`` regressions.

Three contracts pinned here:

1. ``_subnet_to_model`` carries ``join_policy`` into the ORM row.
2. ``_model_to_subnet`` reads ``join_policy`` back from a healthy
   row unchanged.
3. **Defensive auto-upgrade**: ``_model_to_subnet`` survives the
   pathological case where ``row.join_policy is None`` (e.g. a
   migration mishap that dropped the ``server_default``) by falling
   back to ``"approval"`` for private subnets and ``"open"`` for
   public ones, mirroring the entity-layer ``from_dict`` rule and
   the Alembic backfill ``WHERE is_private=true`` predicate. Without
   this guard, ``Subnet(is_private=True, join_policy=None)`` would
   trip the ``_JOIN_POLICY_VALUES`` invariant and refuse to
   reconstruct, breaking every read of an affected row.
4. ``save()`` 's UPDATE branch carries ``join_policy`` so subsequent
   mutations (any future ``promote-to-approval`` / ``rotate_policy``
   flow — neither shipped yet, but Phase 2's admission gate already
   reads the field and a future toggle endpoint must round-trip it)
   actually persist a changed value rather than silently no-op'ing
   on existing rows.

Exercised against a mock session — schema correctness lives in
``test_alembic_subnet_join_policy_migration.py``.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql import Update

from acn.core.entities import Subnet
from acn.infrastructure.persistence.postgres.models import SubnetModel
from acn.infrastructure.persistence.postgres.subnet_repository import (
    PostgresSubnetRepository,
)


def _make_session_factory():
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    factory = MagicMock(return_value=session)
    return factory, session


def _make_model(**overrides) -> SubnetModel:
    """Build a fully-populated ``SubnetModel`` (SQLAlchemy column
    defaults don't fire until INSERT, so direct-instantiation paths
    have to pass every NOT NULL column explicitly — same idiom
    ``test_postgres_subnet_repository_nesting.py`` uses)."""
    defaults: dict = {
        "slug": "subnet-x",
        "name": "x",
        "owner": "agent-owner",
        "description": None,
        "is_private": False,
        "security_config": None,
        "member_agent_ids": None,
        "subnet_metadata": None,
        "harness_url": None,
        "harness_secret": None,
        "parent_slug": None,
        "lifecycle": "persistent",
        "linked_task_id": None,
        "join_policy": "open",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return SubnetModel(**defaults)


# ---------------------------------------------------------------------------
# Mapper — entity → model
# ---------------------------------------------------------------------------


def test_subnet_to_model_carries_join_policy():
    factory, _ = _make_session_factory()
    repo = PostgresSubnetRepository(session_factory=factory)
    subnet = Subnet(
        slug="subnet-x",
        name="x",
        owner="agent-owner",
        is_private=True,
        join_policy="approval",
    )

    model = repo._subnet_to_model(subnet)

    assert model.join_policy == "approval"


def test_subnet_to_model_carries_open_for_public_default():
    factory, _ = _make_session_factory()
    repo = PostgresSubnetRepository(session_factory=factory)
    subnet = Subnet(
        slug="subnet-pub",
        name="pub",
        owner="agent-owner",
    )

    model = repo._subnet_to_model(subnet)

    assert model.join_policy == "open"


# ---------------------------------------------------------------------------
# Mapper — model → entity, healthy path
# ---------------------------------------------------------------------------


def test_model_to_subnet_carries_join_policy():
    factory, _ = _make_session_factory()
    repo = PostgresSubnetRepository(session_factory=factory)
    model = _make_model(is_private=True, join_policy="approval")

    entity = repo._model_to_subnet(model)

    assert entity.join_policy == "approval"
    assert entity.is_private is True


# ---------------------------------------------------------------------------
# Defensive auto-upgrade — the pathological migration-mishap path
# ---------------------------------------------------------------------------


class TestModelToSubnetNullJoinPolicyAutoUpgrade:
    """``row.join_policy is None`` should never happen in a healthy
    deployment — the column is NOT NULL with ``server_default='open'``.
    But the mapper's defensive auto-upgrade is the difference between
    a recoverable read and a corrupted entity on a Postgres rolled
    back mid-migration. Pin both branches of the fallback explicitly
    so the defence stays alive against future refactors that might
    "simplify" the ``or`` expression away."""

    def test_null_join_policy_on_public_row_falls_back_to_open(self):
        factory, _ = _make_session_factory()
        repo = PostgresSubnetRepository(session_factory=factory)
        model = _make_model(
            slug="subnet-broken-pub",
            is_private=False,
            join_policy=None,  # the pathological case
        )

        entity = repo._model_to_subnet(model)

        assert entity.join_policy == "open"
        assert entity.is_private is False

    def test_null_join_policy_on_private_row_falls_back_to_approval(self):
        """**The critical branch.** Without this fallback, a private
        row with NULL ``join_policy`` would reconstruct as
        ``(is_private=True, join_policy='open')`` — the exact
        invariant-violating combination ADR-0004 exists to close —
        and ``Subnet.__post_init__`` would refuse to instantiate.
        Every PostgreSQL read of such a row would 500 until the
        migration mishap is repaired."""
        factory, _ = _make_session_factory()
        repo = PostgresSubnetRepository(session_factory=factory)
        model = _make_model(
            slug="subnet-broken-priv",
            is_private=True,
            join_policy=None,
        )

        entity = repo._model_to_subnet(model)

        assert entity.join_policy == "approval"
        assert entity.is_private is True


# ---------------------------------------------------------------------------
# save() — UPDATE branch must carry ``join_policy``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_update_path_includes_join_policy():
    """When ``session.get`` finds an existing row, ``save()`` issues
    an ``UPDATE`` rather than ``session.add``. The UPDATE's value
    bag MUST include ``join_policy`` — otherwise a future
    policy-rotation flow (or any caller that just wants to flip
    ``open`` → ``approval``) would silently no-op on rows that
    already exist in the DB."""
    factory, session = _make_session_factory()
    session.get.return_value = MagicMock(spec=SubnetModel)

    repo = PostgresSubnetRepository(session_factory=factory)
    subnet = Subnet(
        slug="subnet-existing",
        name="x",
        owner="agent-owner",
        is_private=True,
        join_policy="approval",
    )

    await repo.save(subnet)

    session.execute.assert_awaited_once()
    update_stmt = session.execute.await_args_list[0].args[0]
    assert isinstance(update_stmt, Update)
    # ``Update._values`` is a Column → BindParameter map. We extract
    # column names through ``.name`` rather than using ``in`` against
    # the dict directly (which would invoke Column.__eq__ and trigger
    # SQLAlchemy's "Boolean value of this clause is not defined").
    set_columns = {col.name for col in update_stmt._values.keys()}  # type: ignore[attr-defined]
    assert "join_policy" in set_columns, (
        f"UPDATE statement must set join_policy; got columns: "
        f"{sorted(set_columns)}"
    )
