"""PostgresSubnetAllowlistRepository regressions (ADR-0004 Slice 2.1).

Two contract surfaces pinned here:

1. **Mapper** — ``_model_to_entity`` round-trips every column.
2. **ON CONFLICT DO NOTHING idempotency** — ``add`` returns
   ``True`` for new inserts, ``False`` for re-adds (the route
   layer's signal for 201 vs 200). The repo uses
   ``pg_insert(...).on_conflict_do_nothing()`` with a
   ``returning(...)`` clause; presence of any returned row is the
   "did insert" signal. Pinning this contract guards against a
   refactor that drops the ``returning()`` clause (which would
   silently start returning ``False`` for every add, including
   first-time inserts).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities import SubnetAllowlist
from acn.infrastructure.persistence.postgres.models import SubnetAllowlistModel
from acn.infrastructure.persistence.postgres.subnet_allowlist_repository import (
    PostgresSubnetAllowlistRepository,
)


def _make_session_factory():
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    factory = MagicMock(return_value=session)
    return factory, session


def _make_entry(**overrides) -> SubnetAllowlist:
    defaults: dict = {
        "slug": "s-1",
        "agent_id": "a-1",
        "added_by": "owner-1",
    }
    defaults.update(overrides)
    return SubnetAllowlist(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


def test_model_to_entity_round_trip():
    factory, _ = _make_session_factory()
    repo = PostgresSubnetAllowlistRepository(session_factory=factory)
    ts = datetime.now(UTC)
    model = SubnetAllowlistModel(
        slug="s-1",
        agent_id="a-1",
        added_by="owner-1",
        added_at=ts,
    )
    entity = repo._model_to_entity(model)
    assert entity.slug == "s-1"
    assert entity.agent_id == "a-1"
    assert entity.added_by == "owner-1"
    assert entity.added_at == ts


# ---------------------------------------------------------------------------
# ON CONFLICT DO NOTHING — idempotency contract
# ---------------------------------------------------------------------------


class TestAddIdempotency:
    @pytest.mark.asyncio
    async def test_new_insert_returns_true(self):
        """``RETURNING slug`` produces a row → new insert."""
        factory, session = _make_session_factory()
        execute_result = MagicMock()
        # ``.first()`` returns a non-None row when ON CONFLICT didn't fire.
        execute_result.first = MagicMock(return_value=("s-1",))
        session.execute = AsyncMock(return_value=execute_result)
        session.commit = AsyncMock()

        repo = PostgresSubnetAllowlistRepository(session_factory=factory)
        was_new = await repo.add(_make_entry())
        assert was_new is True

    @pytest.mark.asyncio
    async def test_existing_insert_returns_false(self):
        """ON CONFLICT swallowed the insert → ``RETURNING`` empty."""
        factory, session = _make_session_factory()
        execute_result = MagicMock()
        execute_result.first = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=execute_result)
        session.commit = AsyncMock()

        repo = PostgresSubnetAllowlistRepository(session_factory=factory)
        was_new = await repo.add(_make_entry())
        assert was_new is False


# ---------------------------------------------------------------------------
# delete_for_subnet — cascade count
# ---------------------------------------------------------------------------


class TestCascade:
    @pytest.mark.asyncio
    async def test_delete_for_subnet_returns_count(self):
        factory, session = _make_session_factory()
        result = MagicMock()
        result.rowcount = 3
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()

        repo = PostgresSubnetAllowlistRepository(session_factory=factory)
        n = await repo.delete_for_subnet("s-1")
        assert n == 3


# ---------------------------------------------------------------------------
# remove + is_member — basic mock contracts
# ---------------------------------------------------------------------------


class TestRemoveAndIsMember:
    @pytest.mark.asyncio
    async def test_remove_true_when_row_existed(self):
        factory, session = _make_session_factory()
        result = MagicMock()
        result.rowcount = 1
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()

        repo = PostgresSubnetAllowlistRepository(session_factory=factory)
        assert await repo.remove("s-1", "a-1") is True

    @pytest.mark.asyncio
    async def test_remove_false_when_row_absent(self):
        factory, session = _make_session_factory()
        result = MagicMock()
        result.rowcount = 0
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()

        repo = PostgresSubnetAllowlistRepository(session_factory=factory)
        assert await repo.remove("s-1", "ghost") is False
