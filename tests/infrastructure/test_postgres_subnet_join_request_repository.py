"""PostgresSubnetJoinRequestRepository regressions (ADR-0004 Slice 2.1).

Three contract surfaces pinned here:

1. **Mapper round-trip** — ``_model_to_entity`` and
   ``_entity_to_model`` carry every column / field in both
   directions, with the entity ``__post_init__`` running on the
   rebuilt object so a corrupted-in-storage row surfaces as a
   ``ValueError`` at read time instead of silently flowing through.

2. **IntegrityError → SubnetJoinRequestPendingError translation**
   — the partial-index ``UNIQUE … WHERE status='pending'``
   violation is the schema-level enforcement of the "at most one
   pending per (subnet, agent)" invariant. The repo translates it
   into a domain-meaningful exception so the service layer doesn't
   import ``sqlalchemy.exc``. Other IntegrityErrors (FK, NOT NULL)
   re-raise unchanged so they surface as 500s rather than masquerading
   as the well-known race condition.

3. **delete_for_subnet** — the cascade hook. Returns the deleted
   row count for audit logging (the service layer doesn't gate on
   it, but the contract is pinned so a future refactor that changes
   the return shape can't slip through).

Full DDL + actual integrity constraint enforcement is exercised in
CI's PG integration suite; this file pins the repo's translation /
mapping contract against a mock session.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from acn.core.entities import SYSTEM_ALLOWLIST_ACTOR, SubnetJoinRequest
from acn.infrastructure.persistence.postgres.models import (
    SubnetJoinRequestModel,
)
from acn.infrastructure.persistence.postgres.subnet_join_request_repository import (
    PostgresSubnetJoinRequestRepository,
    SubnetJoinRequestPendingError,
)


def _make_session_factory():
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    # ``session.add`` is sync in SQLAlchemy's API — default AsyncMock would
    # produce a coroutine here and trigger a "never awaited" RuntimeWarning.
    session.add = MagicMock()
    factory = MagicMock(return_value=session)
    return factory, session


def _make_request(**overrides) -> SubnetJoinRequest:
    defaults: dict = {
        "request_id": "req-1",
        "slug": "s-1",
        "agent_id": "a-1",
        "kind": "join_request",
        "status": "pending",
        "initiated_by": "a-1",
    }
    defaults.update(overrides)
    return SubnetJoinRequest(**defaults)  # type: ignore[arg-type]


def _make_model(**overrides) -> SubnetJoinRequestModel:
    """Build a fully-populated ``SubnetJoinRequestModel``. SQLAlchemy
    column defaults don't fire until INSERT, so direct-instantiation
    paths have to pass every NOT NULL column explicitly."""
    defaults: dict = {
        "request_id": "req-1",
        "slug": "s-1",
        "agent_id": "a-1",
        "kind": "join_request",
        "status": "pending",
        "initiated_by": "a-1",
        "decided_by": None,
        "created_at": datetime.now(UTC),
        "decided_at": None,
        "note": None,
    }
    defaults.update(overrides)
    return SubnetJoinRequestModel(**defaults)


# ---------------------------------------------------------------------------
# Mapper round-trip
# ---------------------------------------------------------------------------


class TestMapper:
    def test_model_to_entity_pending(self):
        factory, _ = _make_session_factory()
        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)
        model = _make_model()
        entity = repo._model_to_entity(model)
        assert entity.request_id == "req-1"
        assert entity.status == "pending"
        assert entity.decided_by is None

    def test_model_to_entity_terminal(self):
        factory, _ = _make_session_factory()
        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)
        ts = datetime.now(UTC)
        model = _make_model(
            status="approved", decided_by="owner-1", decided_at=ts, note="ok"
        )
        entity = repo._model_to_entity(model)
        assert entity.status == "approved"
        assert entity.decided_by == "owner-1"
        assert entity.decided_at == ts
        assert entity.note == "ok"
        assert entity.is_terminal is True

    def test_model_to_entity_allowlist_auto(self):
        factory, _ = _make_session_factory()
        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)
        ts = datetime.now(UTC)
        model = _make_model(
            kind="allowlist_auto",
            status="approved",
            initiated_by=SYSTEM_ALLOWLIST_ACTOR,
            decided_by=SYSTEM_ALLOWLIST_ACTOR,
            decided_at=ts,
        )
        entity = repo._model_to_entity(model)
        # Round-trip survives the entity's ``allowlist_auto`` shape check.
        assert entity.kind == "allowlist_auto"
        assert entity.initiated_by == SYSTEM_ALLOWLIST_ACTOR

    def test_entity_to_model_carries_all_fields(self):
        factory, _ = _make_session_factory()
        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)
        ts = datetime.now(UTC)
        entity = _make_request(
            status="rejected",
            decided_by="owner-2",
            decided_at=ts,
            note="not now",
        )
        model = repo._entity_to_model(entity)
        assert model.request_id == entity.request_id
        assert model.status == "rejected"
        assert model.decided_by == "owner-2"
        assert model.decided_at == ts
        assert model.note == "not now"


# ---------------------------------------------------------------------------
# IntegrityError translation
# ---------------------------------------------------------------------------


class TestPendingCollisionTranslation:
    """THE defence: the partial-index violation must surface as
    ``SubnetJoinRequestPendingError`` with the colliding
    ``(slug, agent_id)``, NOT as a raw sqlalchemy
    ``IntegrityError``. The service layer matches on the domain
    exception to surface ``409`` with the stable reason token; if a
    future refactor swallows the translation, every duplicate
    join attempt starts surfacing as a 500."""

    @pytest.mark.asyncio
    async def test_pending_index_violation_translates(self):
        factory, session = _make_session_factory()
        session.get = AsyncMock(return_value=None)  # new INSERT path
        # The orig.message contains the partial-index name; the
        # repo's translator pattern-matches on the index name string.
        orig = MagicMock()
        orig.__str__ = lambda self: (
            "duplicate key value violates unique constraint "
            '"subnet_join_requests_pending_unique"'
        )
        session.commit = AsyncMock(
            side_effect=IntegrityError("INSERT ...", {}, orig)
        )
        session.rollback = AsyncMock()

        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)
        with pytest.raises(SubnetJoinRequestPendingError) as exc_info:
            await repo.save(_make_request(slug="s-x", agent_id="a-x"))
        assert exc_info.value.slug == "s-x"
        assert exc_info.value.agent_id == "a-x"
        # Rollback must have been called before the translated raise
        # — otherwise the failed transaction stays open and the next
        # ``session.commit`` blows up.
        session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_other_integrity_errors_reraise_unchanged(self):
        """FK / NOT NULL violations should NOT masquerade as the
        pending-collision race. The repo only translates the
        well-known partial-index violation; everything else
        propagates so the service layer surfaces 500s for true
        invariant breaks instead of misleading 409s."""
        factory, session = _make_session_factory()
        session.get = AsyncMock(return_value=None)
        orig = MagicMock()
        orig.__str__ = lambda self: (
            'null value in column "slug" violates not-null constraint'
        )
        session.commit = AsyncMock(
            side_effect=IntegrityError("INSERT ...", {}, orig)
        )
        session.rollback = AsyncMock()

        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)
        with pytest.raises(IntegrityError):
            await repo.save(_make_request())
        session.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# Update branch — existing row gets every field overwritten
# ---------------------------------------------------------------------------


class TestUpdateBranch:
    @pytest.mark.asyncio
    async def test_existing_row_update_carries_all_mutable_fields(self):
        factory, session = _make_session_factory()
        existing = _make_model(status="pending", decided_by=None, decided_at=None)
        session.get = AsyncMock(return_value=existing)
        session.commit = AsyncMock()

        ts = datetime.now(UTC)
        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)
        await repo.save(
            _make_request(
                status="approved",
                decided_by="owner-1",
                decided_at=ts,
                note="welcome",
            )
        )
        # The existing model was mutated in place; SQLAlchemy will
        # flush the change on commit. Pin the in-place mutation
        # contract rather than expecting a separate UPDATE statement.
        assert existing.status == "approved"
        assert existing.decided_by == "owner-1"
        assert existing.decided_at == ts
        assert existing.note == "welcome"
        session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Cascade — delete_for_subnet returns deleted count
# ---------------------------------------------------------------------------


class TestCascadeDelete:
    @pytest.mark.asyncio
    async def test_delete_for_subnet_returns_row_count(self):
        factory, session = _make_session_factory()
        delete_result = MagicMock()
        delete_result.rowcount = 5
        session.execute = AsyncMock(return_value=delete_result)
        session.commit = AsyncMock()

        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)
        n = await repo.delete_for_subnet("s-cascade")
        assert n == 5

    @pytest.mark.asyncio
    async def test_delete_for_subnet_zero_rows_is_legal(self):
        """A subnet with no pending or terminal requests is a valid
        cascade target — the service layer never gates on the
        delete count, only on cascade ordering (PG transaction
        atomicity, Redis cascade-before-subnet-HASH ordering)."""
        factory, session = _make_session_factory()
        delete_result = MagicMock()
        delete_result.rowcount = 0
        session.execute = AsyncMock(return_value=delete_result)
        session.commit = AsyncMock()

        repo = PostgresSubnetJoinRequestRepository(session_factory=factory)
        n = await repo.delete_for_subnet("s-empty")
        assert n == 0
