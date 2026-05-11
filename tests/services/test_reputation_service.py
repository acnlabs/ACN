"""Smoke tests for ``ReputationService`` and ``ReputationQueryService``.

Scope (Saga v0.1, Todo 5):

1. ``ReputationService.record_feedback`` — input validation, smoke flag
   propagation, idempotent repository call shape.
2. ``ReputationService.record_validation`` — attestation requirement.
3. ``ReputationQueryService.get_summary`` — degraded paths (no repo,
   no chain client, chain RPC failure) and the happy merged path.

What these tests deliberately do NOT cover:

* PostgreSQL repository behaviour. ``PostgresReputationRepository``
  needs a live DB; that's a Todo 8 integration test.
* Route-level auth. ``test_onchain_reputation_routes.py`` is the right
  place for endpoint tests; those need a FastAPI ``TestClient`` and
  the full DI graph.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from acn.core.interfaces.reputation_repository import (
    REPUTATION_KIND_FEEDBACK,
    REPUTATION_KIND_VALIDATION,
    IReputationRepository,
    ReputationEvent,
)
from acn.services.reputation_query_service import (
    OnChainReputationSummary,
    ReputationQueryService,
)
from acn.services.reputation_service import ReputationService

# =============================================================================
# Fixtures
# =============================================================================


def _build_event(
    *,
    agent_id: str = "agent-target",
    task_id: str = "task-1",
    kind: str = REPUTATION_KIND_FEEDBACK,
    signer: str = "agent-creator",
    score: int | None = None,
    smoke_test: bool = False,
    event_id: int = 42,
) -> ReputationEvent:
    """Helper to fabricate a fully-populated ReputationEvent as if the
    repository had just persisted it. ``event_id`` becomes the DB
    surrogate key; ``created_at`` is fixed for deterministic assertions.
    """
    metadata = {"smoke_test": True} if smoke_test else {}
    return ReputationEvent(
        id=event_id,
        agent_id=agent_id,
        task_id=task_id,
        kind=kind,
        signer=signer,
        score=score,
        evidence_uri=None,
        attestation=None,
        event_metadata=metadata,
        created_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def mock_repo() -> IReputationRepository:
    """Mock IReputationRepository. ``record`` defaults to echoing the
    event back with id=99 so tests don't have to set this up unless
    they care about idempotency semantics.
    """
    repo = AsyncMock(spec=IReputationRepository)
    repo.record.return_value = _build_event()
    return repo


# =============================================================================
# ReputationService.record_feedback
# =============================================================================


class TestRecordFeedbackValidation:
    """Input validation lives in the service layer, not the repository.

    Each violation maps to a ``ValueError`` — the route handler
    translates that to HTTP 400 ``INVALID_REQUEST``; the worker treats
    it as non-retriable (dead state, no exponential backoff).
    """

    @pytest.mark.asyncio
    async def test_missing_agent_id_raises(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        with pytest.raises(ValueError, match="agent_id is required"):
            await service.record_feedback(
                agent_id="", task_id="t-1", signer="signer-1"
            )
        # Repository must NOT be touched when validation fails — the
        # contract is "validation rejects before reaching the DB".
        mock_repo.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_task_id_raises(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        with pytest.raises(ValueError, match="task_id is required"):
            await service.record_feedback(
                agent_id="agent-1", task_id="", signer="signer-1"
            )
        mock_repo.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_signer_raises(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        with pytest.raises(ValueError, match="signer is required"):
            await service.record_feedback(
                agent_id="agent-1", task_id="t-1", signer=""
            )
        mock_repo.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_feedback_forbidden(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        with pytest.raises(ValueError, match="Self-feedback forbidden"):
            await service.record_feedback(
                agent_id="agent-1", task_id="t-1", signer="agent-1"
            )
        mock_repo.record.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_score", [-1, 101, 200])
    async def test_score_out_of_range_raises(self, mock_repo, bad_score) -> None:
        service = ReputationService(mock_repo)
        with pytest.raises(ValueError, match="score must be"):
            await service.record_feedback(
                agent_id="agent-1",
                task_id="t-1",
                signer="agent-2",
                score=bad_score,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ok_score", [0, 50, 100, None])
    async def test_score_in_range_or_none_accepted(
        self, mock_repo, ok_score
    ) -> None:
        service = ReputationService(mock_repo)
        result = await service.record_feedback(
            agent_id="agent-1",
            task_id="t-1",
            signer="agent-2",
            score=ok_score,
        )
        # Mock returns the canned event — service didn't reject.
        assert result.id == 42
        mock_repo.record.assert_called_once()


class TestRecordFeedbackSmokeFlag:
    """Smoke flag plumbing — the production-isolation contract (plan §7).

    The repository's read filter checks ``event_metadata->>'smoke_test'``;
    those rows only get the flag when the producer copies it into the
    event. ``record_feedback`` is one of the two producers
    (worker step is the other), so we cover the copy here.
    """

    @pytest.mark.asyncio
    async def test_smoke_flag_propagated(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        await service.record_feedback(
            agent_id="agent-1",
            task_id="t-1",
            signer="agent-2",
            task_metadata={"smoke_test": True, "other_key": "ignored"},
        )
        sent_event: ReputationEvent = mock_repo.record.call_args.args[0]
        assert sent_event.event_metadata == {"smoke_test": True}, (
            "Only the smoke_test flag should be propagated — other "
            "task metadata keys are intentionally NOT copied to keep "
            "reputation rows lean."
        )

    @pytest.mark.asyncio
    async def test_smoke_flag_absent_when_not_smoke(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        await service.record_feedback(
            agent_id="agent-1",
            task_id="t-1",
            signer="agent-2",
            task_metadata={"other_key": "ignored"},
        )
        sent_event: ReputationEvent = mock_repo.record.call_args.args[0]
        assert sent_event.event_metadata == {}, (
            "Non-smoke tasks must produce an empty metadata dict so "
            "the JSONB ``->>'smoke_test'`` filter falls back to NULL "
            "rather than 'false' string (subtle filter semantics)."
        )

    @pytest.mark.asyncio
    async def test_smoke_flag_with_no_metadata(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        await service.record_feedback(
            agent_id="agent-1",
            task_id="t-1",
            signer="agent-2",
            task_metadata=None,
        )
        sent_event: ReputationEvent = mock_repo.record.call_args.args[0]
        assert sent_event.event_metadata == {}


class TestRecordFeedbackHappyPath:
    @pytest.mark.asyncio
    async def test_passes_session_through(self, mock_repo) -> None:
        """Service must NOT swallow the optional outer session — composing
        reputation writes into a wider transaction is a v1 requirement
        (dispute arbitration writes refund + reputation atomically)."""
        service = ReputationService(mock_repo)
        sentinel_session = object()
        await service.record_feedback(
            agent_id="agent-1",
            task_id="t-1",
            signer="agent-2",
            session=sentinel_session,  # type: ignore[arg-type]
        )
        kwargs = mock_repo.record.call_args.kwargs
        assert kwargs["session"] is sentinel_session

    @pytest.mark.asyncio
    async def test_kind_is_feedback(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        await service.record_feedback(
            agent_id="agent-1", task_id="t-1", signer="agent-2"
        )
        sent_event: ReputationEvent = mock_repo.record.call_args.args[0]
        assert sent_event.kind == REPUTATION_KIND_FEEDBACK
        assert sent_event.attestation is None, (
            "Feedback events must leave attestation None — only "
            "validation rows carry attestation payloads."
        )


# =============================================================================
# ReputationService.record_validation
# =============================================================================


class TestRecordValidation:
    @pytest.mark.asyncio
    async def test_attestation_required(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        with pytest.raises(ValueError, match="attestation is required"):
            await service.record_validation(
                agent_id="agent-1",
                task_id="t-1",
                signer="validator-1",
                attestation={},  # empty dict counts as missing
            )
        mock_repo.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_validation_forbidden(self, mock_repo) -> None:
        service = ReputationService(mock_repo)
        with pytest.raises(ValueError, match="Self-validation forbidden"):
            await service.record_validation(
                agent_id="agent-1",
                task_id="t-1",
                signer="agent-1",
                attestation={"tag": "successful"},
            )

    @pytest.mark.asyncio
    async def test_validation_kind_and_attestation_preserved(
        self, mock_repo
    ) -> None:
        mock_repo.record.return_value = _build_event(
            kind=REPUTATION_KIND_VALIDATION
        )
        service = ReputationService(mock_repo)
        attestation = {
            "tag": "successful",
            "status": "validated",
            "signature": "0xdeadbeef",
        }
        await service.record_validation(
            agent_id="agent-1",
            task_id="t-1",
            signer="validator-1",
            attestation=attestation,
        )
        sent_event: ReputationEvent = mock_repo.record.call_args.args[0]
        assert sent_event.kind == REPUTATION_KIND_VALIDATION
        assert sent_event.attestation == attestation


# =============================================================================
# ReputationQueryService.get_summary
# =============================================================================


class TestQueryServiceDegradation:
    """The query service must degrade gracefully so neither a missing
    PG repo nor a missing / failing chain client breaks the API.
    """

    @pytest.mark.asyncio
    async def test_no_repo_returns_zero_counts(self) -> None:
        service = ReputationQueryService(repository=None, erc8004_client=None)
        result = await service.get_summary("agent-1")
        assert result.off_chain.feedback_count == 0
        assert result.off_chain.validation_count == 0
        assert result.off_chain.recent_events == []
        assert result.on_chain is None
        assert result.source == "off_chain"

    @pytest.mark.asyncio
    async def test_no_chain_returns_off_chain_only(self) -> None:
        repo = AsyncMock(spec=IReputationRepository)
        repo.count_for_agent.side_effect = [3, 1]
        repo.list_for_agent.return_value = [_build_event()]
        service = ReputationQueryService(repository=repo, erc8004_client=None)
        result = await service.get_summary("agent-1")
        assert result.off_chain.feedback_count == 3
        assert result.off_chain.validation_count == 1
        assert len(result.off_chain.recent_events) == 1
        assert result.on_chain is None
        assert result.source == "off_chain"

    @pytest.mark.asyncio
    async def test_chain_failure_degrades_to_off_chain(self) -> None:
        """A chain RPC outage must NOT break the off-chain summary —
        the testnet RPCs we use are flaky and a 500 here would knock
        production reputation pages offline whenever the RPC blips.
        """
        repo = AsyncMock(spec=IReputationRepository)
        repo.count_for_agent.side_effect = [3, 1]
        repo.list_for_agent.return_value = []
        chain = AsyncMock()
        chain.get_reputation_summary.side_effect = RuntimeError("RPC down")
        service = ReputationQueryService(repository=repo, erc8004_client=chain)
        result = await service.get_summary("agent-1", on_chain_token_id=42)
        assert result.off_chain.feedback_count == 3
        assert result.on_chain is None
        assert result.source == "off_chain", (
            "Chain failure must NOT advertise 'merged' — source field "
            "is the SDK's signal to know if chain data is included."
        )

    @pytest.mark.asyncio
    async def test_chain_success_marks_merged(self) -> None:
        repo = AsyncMock(spec=IReputationRepository)
        repo.count_for_agent.side_effect = [5, 2]
        repo.list_for_agent.return_value = []
        chain = AsyncMock()
        chain.get_reputation_summary.return_value = {
            "token_id": 42,
            "count": 7,
            "avg_value": 88.0,
            "by_tag": {"successful": 7},
        }
        service = ReputationQueryService(repository=repo, erc8004_client=chain)
        result = await service.get_summary("agent-1", on_chain_token_id=42)
        assert result.source == "merged"
        assert isinstance(result.on_chain, OnChainReputationSummary)
        assert result.on_chain.count == 7
        assert result.on_chain.avg_value == 88.0


class TestQueryServiceParameters:
    """Filter / pagination parameters must reach the repository
    untouched — these are the knobs the route exposes and tests are
    the only way to lock the contract.
    """

    @pytest.mark.asyncio
    async def test_include_smoke_test_passed_through(self) -> None:
        repo = AsyncMock(spec=IReputationRepository)
        repo.count_for_agent.return_value = 0
        repo.list_for_agent.return_value = []
        service = ReputationQueryService(repository=repo, erc8004_client=None)
        await service.get_summary("agent-1", include_smoke_test=True)
        for call in repo.count_for_agent.call_args_list:
            assert call.kwargs["include_smoke_test"] is True
        repo.list_for_agent.assert_called_once()
        assert (
            repo.list_for_agent.call_args.kwargs["include_smoke_test"] is True
        )

    @pytest.mark.asyncio
    async def test_recent_limit_zero_skips_list(self) -> None:
        """recent_limit=0 is the SDK's "give me counts only" mode — the
        list query is expensive (touches the heap), counts use the
        narrow agent_id index. The optimization is real, lock the
        contract here.
        """
        repo = AsyncMock(spec=IReputationRepository)
        repo.count_for_agent.return_value = 0
        service = ReputationQueryService(repository=repo, erc8004_client=None)
        await service.get_summary("agent-1", recent_limit=0)
        repo.list_for_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_attach_erc8004_client_post_init(self) -> None:
        """The lifespan hook attaches the ERC-8004 client after init —
        ensure the setter actually works for the merge path."""
        repo = AsyncMock(spec=IReputationRepository)
        repo.count_for_agent.return_value = 0
        repo.list_for_agent.return_value = []
        service = ReputationQueryService(repository=repo, erc8004_client=None)
        chain = AsyncMock()
        chain.get_reputation_summary.return_value = {
            "token_id": 1,
            "count": 0,
            "avg_value": None,
            "by_tag": {},
        }
        service.attach_erc8004_client(chain)
        result = await service.get_summary("agent-1", on_chain_token_id=1)
        assert result.source == "merged"
        assert result.on_chain is not None
