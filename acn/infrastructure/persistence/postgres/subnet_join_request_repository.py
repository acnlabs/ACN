"""PostgreSQL implementation of ``ISubnetJoinRequestRepository``.

The ``save`` path is wrapped to translate the partial-index
``IntegrityError`` into a domain-meaningful exception
(``SubnetJoinRequestPendingError``) so the service layer doesn't
have to import ``sqlalchemy.exc`` to recognise a duplicate-pending
collision. The exception carries the colliding ``(subnet_id,
agent_id)`` for the route layer's 409 envelope.

Mapping discipline
------------------
``_model_to_entity`` round-trips every column without defaults. The
entity layer's ``__post_init__`` runs on the rebuilt object, so a
storage row that violates the (status, decided_by) coherence check
fails fast at read time rather than silently propagating. The
deliberate trade-off: corrupt rows surface as 500s on the first
read instead of silently flowing through; combined with the unique
partial index this is the dual-layer defence ADR §"Redis layout
and atomicity" describes.
"""

from contextlib import asynccontextmanager

from sqlalchemy import delete, desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....core.entities import SubnetJoinRequest
from ....core.interfaces import ISubnetJoinRequestRepository
from .models import SubnetJoinRequestModel


class SubnetJoinRequestPendingError(Exception):
    """Raised when ``save`` collides with the unique partial index.

    Service layer catches this and surfaces ``409`` with the stable
    reason token (``JOIN_REQUEST_PENDING`` /
    ``INVITATION_PENDING``); the route layer maps the token to the
    appropriate envelope per ADR §HTTP status code conventions."""

    def __init__(self, slug: str, agent_id: str) -> None:
        super().__init__(
            f"pending join request already exists for "
            f"(slug={slug!r}, agent_id={agent_id!r})"
        )
        self.slug = slug
        self.agent_id = agent_id


class PostgresSubnetJoinRequestRepository(ISubnetJoinRequestRepository):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _session_scope(self, session: AsyncSession | None):
        """Yield ``session`` if passed (no commit / no close) — caller
        owns the transaction. Otherwise open + commit + close ourselves.

        Identical shape to
        ``PostgresSettlementOutboxRepository._session_scope`` — picked
        deliberately so the saga and the ADR-0004 cascade share one
        outer-session contract: passing a session means "this call is
        a brick in someone else's transaction; do not commit". The
        outer Unit-of-Work owns commit-on-clean-exit and
        rollback-on-exception; we just stay out of its way.
        """
        if session is not None:
            yield session
            return
        async with self._session_factory() as own_session:
            yield own_session
            await own_session.commit()

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------

    def _model_to_entity(self, row: SubnetJoinRequestModel) -> SubnetJoinRequest:
        return SubnetJoinRequest(
            request_id=row.request_id,
            slug=row.slug,
            agent_id=row.agent_id,
            kind=row.kind,  # type: ignore[arg-type]
            status=row.status,  # type: ignore[arg-type]
            initiated_by=row.initiated_by,
            decided_by=row.decided_by,
            created_at=row.created_at,
            decided_at=row.decided_at,
            note=row.note,
        )

    def _entity_to_model(
        self, request: SubnetJoinRequest
    ) -> SubnetJoinRequestModel:
        return SubnetJoinRequestModel(
            request_id=request.request_id,
            slug=request.slug,
            agent_id=request.agent_id,
            kind=request.kind,
            status=request.status,
            initiated_by=request.initiated_by,
            decided_by=request.decided_by,
            created_at=request.created_at,
            decided_at=request.decided_at,
            note=request.note,
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def save(self, request: SubnetJoinRequest) -> None:
        """Insert or update the row.

        On insert collision with the unique partial pending index,
        ``IntegrityError`` translates to
        ``SubnetJoinRequestPendingError`` so the service layer can
        treat the collision as a domain event rather than a raw SQL
        error. Update (``existing`` branch) does NOT raise the
        translated error — the only legal mutation path is a
        transition out of ``pending``, which by definition removes
        the partial-index row and frees the slot.

        Why not use ``ON CONFLICT DO NOTHING / UPDATE``: the partial
        index makes the conflict target conditional (``WHERE
        status='pending'``), and ``INSERT ... ON CONFLICT`` requires
        a constraint name or column list that matches an existing
        constraint exactly. Our index is partial, not full, so the
        ``ON CONFLICT`` shortcut doesn't apply cleanly. The explicit
        get-then-insert / get-then-update pattern is one extra round
        trip but composes with the entity's ``__post_init__``
        coherence check.
        """
        async with self._session_factory() as session:
            existing = await session.get(
                SubnetJoinRequestModel, request.request_id
            )
            try:
                if existing:
                    # Update in place — every mutable field. ``request_id`` /
                    # ``subnet_id`` / ``agent_id`` / ``kind`` /
                    # ``initiated_by`` are immutable in practice (the entity
                    # state machine never rewrites them on a transition) but
                    # included here for completeness / defence in depth.
                    existing.slug = request.slug
                    existing.agent_id = request.agent_id
                    existing.kind = request.kind
                    existing.status = request.status
                    existing.initiated_by = request.initiated_by
                    existing.decided_by = request.decided_by
                    existing.created_at = request.created_at
                    existing.decided_at = request.decided_at
                    existing.note = request.note
                else:
                    session.add(self._entity_to_model(request))
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                # Surface the well-known partial-index violation with a
                # domain-meaningful exception. Other IntegrityErrors (FK
                # violations, NOT NULL — neither of which we should ever
                # hit because the entity layer rejects empties first)
                # re-raise unchanged so the service layer surfaces them
                # as 500s.
                if "subnet_join_requests_pending_unique" in str(
                    e.orig
                ) or "subnet_join_requests_pending_unique" in str(e):
                    raise SubnetJoinRequestPendingError(
                        request.slug, request.agent_id
                    ) from e
                raise

    async def find_by_id(self, request_id: str) -> SubnetJoinRequest | None:
        async with self._session_factory() as session:
            row = await session.get(SubnetJoinRequestModel, request_id)
            return self._model_to_entity(row) if row else None

    async def find_pending_for(
        self, slug: str, agent_id: str
    ) -> SubnetJoinRequest | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetJoinRequestModel).where(
                    SubnetJoinRequestModel.slug == slug,
                    SubnetJoinRequestModel.agent_id == agent_id,
                    SubnetJoinRequestModel.status == "pending",
                )
            )
            row = result.scalar_one_or_none()
            return self._model_to_entity(row) if row else None

    async def list_by_subnet(
        self,
        slug: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SubnetJoinRequest]:
        async with self._session_factory() as session:
            stmt = select(SubnetJoinRequestModel).where(
                SubnetJoinRequestModel.slug == slug
            )
            if kind is not None:
                stmt = stmt.where(SubnetJoinRequestModel.kind == kind)
            if status is not None:
                stmt = stmt.where(SubnetJoinRequestModel.status == status)
            stmt = (
                stmt.order_by(desc(SubnetJoinRequestModel.created_at))
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return [self._model_to_entity(r) for r in result.scalars().all()]

    async def list_pending_invitations_for_agent(
        self, agent_id: str
    ) -> list[SubnetJoinRequest]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SubnetJoinRequestModel)
                .where(
                    SubnetJoinRequestModel.agent_id == agent_id,
                    SubnetJoinRequestModel.kind == "invitation",
                    SubnetJoinRequestModel.status == "pending",
                )
                .order_by(desc(SubnetJoinRequestModel.created_at))
            )
            return [self._model_to_entity(r) for r in result.scalars().all()]

    async def delete_for_subnet(
        self, slug: str, *, session: AsyncSession | None = None
    ) -> int:
        """Cascade-delete all rows for a subnet. Returns count deleted.

        When ``session`` is passed (the production cascade path —
        ``SubnetService.delete_subnet`` opens a single
        ``uow.transaction()`` and threads it through here, the
        allowlist repo, and the ``subnets`` DELETE), this call
        participates in that outer transaction: no internal commit,
        no internal close, so a failure on any of the sibling DELETEs
        rolls the whole batch back. This is what ADR §"Cascade
        deletion: Postgres" actually promises ("any failure rolls back
        the whole batch") — Slice 2.1 shipped this without the outer
        session and issue #75 tracked the gap until Slice 2.1.1 (this
        change) closed it.

        When ``session`` is ``None`` (legacy fixtures, ad-hoc tools)
        the call falls back to the original self-managed
        ``self._session_factory() → execute → commit`` shape — the
        cascade still works, just at lower atomicity. The behaviour
        is byte-for-byte the same as the pre-Slice-2.1.1 code so
        out-of-tree callers that constructed the repo bare keep
        working unchanged.

        Returns the deleted-row count for audit logging; the service
        layer logs it but doesn't gate on it (zero rows is a valid
        outcome — subnets without any pending or terminal requests).
        """
        async with self._session_scope(session) as sess:
            result = await sess.execute(
                delete(SubnetJoinRequestModel).where(
                    SubnetJoinRequestModel.slug == slug
                )
            )
            return result.rowcount or 0
