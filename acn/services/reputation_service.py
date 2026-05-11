"""Reputation write service (Saga v0.1, off-chain).

Producer side of ``reputation_events``. Two callers today:

1. ``SettlementWorker.reputation_write`` step — records one ``feedback``
   row per accepted task. Worker retries collapse to the same row via
   the repository's idempotency contract.
2. ``routes/onchain.py`` POST endpoints — let external integrators
   submit feedback / validation rows directly. Same code path, same
   idempotency guarantee.

The service is intentionally thin: input validation, smoke-flag
propagation, then delegate to ``IReputationRepository.record``. Chain
write is reserved for v1 — see ``acn/docs/_drafts/settlement-saga-design.md``
§5; v0.1 stays off-chain so we don't need private-key custody yet.

Why ``record_feedback`` and ``record_validation`` rather than one
generic ``record``: the two events have different validation rules
(``feedback`` MUST NOT be self-issued; ``validation`` requires an
``attestation``). Splitting them keeps each method's contract clear
and prevents callers from accidentally mixing schemas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ..core.interfaces.reputation_repository import (
    REPUTATION_KIND_FEEDBACK,
    REPUTATION_KIND_VALIDATION,
    IReputationRepository,
    ReputationEvent,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


# v0.1 ignores the score numerically but persists it for v0.2 replay /
# analytics. Validate the bounds at write time so corrupt scores never
# enter the table.
_MIN_SCORE = 0
_MAX_SCORE = 100

# Hard cap on ``evidence_uri`` length, validated in the service. The
# route layer also enforces ``Field(max_length=512)`` but the worker
# path bypasses route validation entirely — without this service-side
# check, a buggy producer could persist multi-KB URIs and bloat the
# reputation_events.evidence_uri Text column (no DB-level cap on Text).
# 512 chosen to match the route limit; raise both in lockstep if the
# real-world evidence URI shape outgrows it (IPFS CIDv1 + path = ~120,
# signed-JWT-as-URL fragment can be longer).
_MAX_EVIDENCE_URI_LEN = 512


def _extract_smoke_flag(task_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the ``smoke_test`` flag out of a task's metadata into the
    reputation event's own metadata.

    Why we copy instead of joining ``tasks`` at read time: see
    ``infrastructure/persistence/postgres/models.py:ReputationEventModel``
    docstring. Keeping reputation rows self-contained also means smoke
    rows can be archived later without touching the ``tasks`` table.

    Any non-truthy value is treated as "not a smoke task" and produces
    an empty metadata dict. We do NOT propagate other task metadata
    keys because v0.1 doesn't need them and copying everything would
    leak unrelated state (e.g. callback URLs) into reputation history.
    """
    if not task_metadata:
        return {}
    if task_metadata.get("smoke_test"):
        return {"smoke_test": True}
    return {}


class ReputationService:
    """Thin write-side service for reputation events.

    All methods are idempotent at the repository layer
    (``record`` uses ON CONFLICT DO NOTHING on
    ``(agent_id, task_id, kind)``), so callers can safely retry.
    """

    def __init__(self, repository: IReputationRepository) -> None:
        self._repository = repository

    async def record_feedback(
        self,
        *,
        agent_id: str,
        task_id: str,
        signer: str,
        score: int | None = None,
        evidence_uri: str | None = None,
        task_metadata: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> ReputationEvent:
        """Record one feedback row.

        Args:
            agent_id: Agent being reviewed (the task assignee).
            task_id: Task this feedback is about.
            signer: Who issues the feedback. For
                ``SettlementWorker``-driven writes this is the task
                creator. For direct API calls this is the caller's
                authenticated agent id.
            score: Optional 0-100 score. v0.1 stores but doesn't surface
                it; v0.2 will use it. Out-of-range scores raise
                ``ValueError`` rather than silently clamping — a bad
                score in is a bug, not user input.
            evidence_uri: Optional off-chain pointer to evidence.
            task_metadata: Source task's metadata dict. The
                ``smoke_test`` flag is copied out; other keys are
                intentionally NOT propagated.
            session: Optional outer transaction.

        Returns:
            The persisted (or pre-existing) reputation event.

        Raises:
            ValueError: For self-feedback, missing IDs, or out-of-range
                score. The worker treats these as non-retriable —
                Saga step transitions to ``failed`` not ``retrying``.
        """
        if not agent_id:
            raise ValueError("agent_id is required")
        if not task_id:
            raise ValueError("task_id is required")
        if not signer:
            raise ValueError("signer is required")
        if signer == agent_id:
            # Even if the producer is bug-free this catches a malformed
            # outbox payload where ``payload.assignee_id == creator_id``
            # — better to dead-letter the saga than to forge a row.
            raise ValueError(f"Self-feedback forbidden: signer={signer} == agent_id={agent_id}")
        if score is not None and not (_MIN_SCORE <= score <= _MAX_SCORE):
            raise ValueError(f"score must be {_MIN_SCORE}-{_MAX_SCORE}, got {score}")
        if evidence_uri is not None and len(evidence_uri) > _MAX_EVIDENCE_URI_LEN:
            raise ValueError(
                f"evidence_uri must be <= {_MAX_EVIDENCE_URI_LEN} chars, got {len(evidence_uri)}"
            )

        event = ReputationEvent(
            agent_id=agent_id,
            task_id=task_id,
            kind=REPUTATION_KIND_FEEDBACK,
            signer=signer,
            score=score,
            evidence_uri=evidence_uri,
            attestation=None,
            event_metadata=_extract_smoke_flag(task_metadata),
        )
        persisted = await self._repository.record(event, session=session)
        logger.info(
            "reputation_feedback_recorded",
            agent_id=agent_id,
            task_id=task_id,
            signer=signer,
            reputation_event_id=persisted.id,
            smoke_test=persisted.event_metadata.get("smoke_test", False),
        )
        return persisted

    async def record_validation(
        self,
        *,
        agent_id: str,
        task_id: str,
        signer: str,
        attestation: dict[str, Any],
        score: int | None = None,
        evidence_uri: str | None = None,
        task_metadata: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> ReputationEvent:
        """Record one validation row.

        Validation differs from feedback in two ways: ``signer`` is a
        third-party validator (not the task creator), and an
        ``attestation`` (signed JSON) is required. v0.1 doesn't enforce
        the validator's identity here — that's the validator-registry's
        job in v0.2. We DO require ``attestation`` to be non-empty so
        downstream consumers (chain replay, dispute review) always have
        something to verify.

        v0.1 ships the write path but doesn't emit validations from any
        producer — the worker only writes ``feedback``. The endpoint
        exists so integrators (and v0.2 validator agents) can submit
        validations against past tasks.
        """
        if not agent_id:
            raise ValueError("agent_id is required")
        if not task_id:
            raise ValueError("task_id is required")
        if not signer:
            raise ValueError("signer is required")
        if signer == agent_id:
            raise ValueError(f"Self-validation forbidden: signer={signer} == agent_id={agent_id}")
        if not attestation:
            raise ValueError("attestation is required for validation events")
        if score is not None and not (_MIN_SCORE <= score <= _MAX_SCORE):
            raise ValueError(f"score must be {_MIN_SCORE}-{_MAX_SCORE}, got {score}")
        if evidence_uri is not None and len(evidence_uri) > _MAX_EVIDENCE_URI_LEN:
            raise ValueError(
                f"evidence_uri must be <= {_MAX_EVIDENCE_URI_LEN} chars, got {len(evidence_uri)}"
            )

        event = ReputationEvent(
            agent_id=agent_id,
            task_id=task_id,
            kind=REPUTATION_KIND_VALIDATION,
            signer=signer,
            score=score,
            evidence_uri=evidence_uri,
            attestation=attestation,
            event_metadata=_extract_smoke_flag(task_metadata),
        )
        persisted = await self._repository.record(event, session=session)
        logger.info(
            "reputation_validation_recorded",
            agent_id=agent_id,
            task_id=task_id,
            signer=signer,
            reputation_event_id=persisted.id,
            smoke_test=persisted.event_metadata.get("smoke_test", False),
        )
        return persisted
