"""Reputation query service (Saga v0.1, off-chain + chain merge).

Read-side counterpart of ``ReputationService``. Merges:

1. **Off-chain** ``reputation_events`` (the authoritative store in v0.1).
2. **On-chain** ERC-8004 Reputation Registry (read-only, optional —
   only callable when the agent has a bound ``token_id``).

Why a dedicated service rather than extending ``ERC8004Client``:
``ERC8004Client`` is read-only and currently only knows about chain
state. Routes that need to surface "agent's full reputation" want
both off-chain + chain in one response. Putting that orchestration in
``ERC8004Client`` would either pollute its responsibilities (chain
client now needs DB access) or force route handlers to do two-step
fetches and merge logic themselves. The plan (§2) explicitly says
"don't touch ``erc8004_client`` to avoid breaking change" — this
service is the new shoreline.

v0.1 vs. v1
-----------
v0.1 emits feedback rows to ``reputation_events`` and exposes them
here. Chain writes are reserved for v1 (no key custody yet), so the
chain-side numbers we merge in are historical only — they were
written by earlier off-chain ACN deployments or by other ERC-8004
participants, never by this v0.1 worker. ``get_summary`` returns both
projections side-by-side; the API surface in ``routes/onchain.py``
exposes the off-chain side under POST endpoints today.

When v1 ships the chain-write adapter, this service stays the canonical
merge point: it just sees the same fields on both sides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from ..core.interfaces.reputation_repository import (
    REPUTATION_KIND_FEEDBACK,
    REPUTATION_KIND_VALIDATION,
    IReputationRepository,
    ReputationEvent,
)

if TYPE_CHECKING:
    from .erc8004_client import ERC8004Client

logger = structlog.get_logger()


# =============================================================================
# Response DTOs
# =============================================================================


class OffChainReputationSummary(BaseModel):
    """v0.1 authoritative summary, read from ``reputation_events``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    feedback_count: int = Field(
        ..., description="Total feedback rows (excludes smoke_test by default)."
    )
    validation_count: int = Field(
        ..., description="Total validation rows (excludes smoke_test by default)."
    )
    recent_events: list[ReputationEvent] = Field(
        default_factory=list,
        description=(
            "Newest events first. Page size capped by ``recent_limit``. "
            "Stored events are immutable so pagination here is stable."
        ),
    )


class OnChainReputationSummary(BaseModel):
    """ERC-8004 Reputation Registry projection.

    Shape matches ``ERC8004Client.get_reputation_summary`` so the
    transformation here is a passthrough — keeps the contract testable.
    """

    token_id: int
    count: int
    avg_value: float | None
    by_tag: dict


class ReputationSummary(BaseModel):
    """Merged off-chain + on-chain view.

    ``on_chain`` is None when the agent has no bound token id (typical
    v0.1 case) or when the chain RPC call failed (best-effort merge —
    the chain side is not allowed to break the off-chain summary).
    """

    agent_id: str
    off_chain: OffChainReputationSummary
    on_chain: OnChainReputationSummary | None = None
    # Plain-text flag instead of a typed enum: easier to expose in JSON
    # and easier for v1 to add new modes (e.g. "merged-pending-chain")
    # without a breaking enum change.
    source: str = Field(
        default="off_chain",
        description=(
            "Where the canonical signals come from. v0.1 always returns "
            "'off_chain' or 'merged'; v1 introduces 'on_chain_primary'."
        ),
    )


# =============================================================================
# Service
# =============================================================================


class ReputationQueryService:
    """Reputation reader. Construct with whichever sources are available.

    Both inputs are optional so the service degrades gracefully in
    deployments without one or the other:

    - No DB repository (Redis-only deployments): ``feedback_count`` /
      ``validation_count`` come back as 0 with ``recent_events=[]``.
      Useful only as a no-op stub; v0.1 production deployments always
      have the PG repo.
    - No ERC-8004 client (chain-disabled deployments): on-chain summary
      is omitted (``on_chain=None``). Local-dev and integration tests
      run this way.
    """

    def __init__(
        self,
        repository: IReputationRepository | None = None,
        erc8004_client: ERC8004Client | None = None,
    ) -> None:
        self._repository = repository
        self._erc8004 = erc8004_client

    def attach_erc8004_client(self, client: ERC8004Client | None) -> None:
        """Late-binding hook for the ERC-8004 client.

        Why this exists rather than requiring the client in __init__:
        in ``acn/api.py`` lifespan the reputation services are wired
        before the ERC-8004 client (whose pre-warm depends on a network
        round-trip we want to do *after* services are up). This setter
        lets lifespan patch the client in once it's ready.

        Passing ``None`` is a deliberate no-op for redeployments that
        want to disable chain merge at runtime; the service degrades to
        ``off_chain`` source.
        """
        self._erc8004 = client

    async def get_summary(
        self,
        agent_id: str,
        *,
        on_chain_token_id: int | None = None,
        include_smoke_test: bool = False,
        recent_limit: int = 20,
    ) -> ReputationSummary:
        """Build a merged summary for ``agent_id``.

        Args:
            agent_id: ACN agent id.
            on_chain_token_id: Optional ERC-8004 token id. When passed
                together with a configured ``erc8004_client``, the
                chain summary is fetched and merged in. When None or
                the client isn't configured, ``on_chain`` returns None.
            include_smoke_test: Off-chain filter knob — defaults to
                False so production reputation summaries don't include
                smoke rows. Tests / ops can opt in.
            recent_limit: Cap on ``recent_events`` returned. 0 returns
                no recent events (counts only).

        Returns:
            ``ReputationSummary``. Off-chain fields are always populated
            (zero values when the repo is missing); on-chain is None
            unless both ``on_chain_token_id`` and ``erc8004_client``
            are present AND the chain call succeeded.
        """
        off_chain = await self._build_off_chain(
            agent_id,
            include_smoke_test=include_smoke_test,
            recent_limit=recent_limit,
        )
        on_chain: OnChainReputationSummary | None = None
        source = "off_chain"

        if on_chain_token_id is not None and self._erc8004 is not None:
            try:
                raw = await self._erc8004.get_reputation_summary(on_chain_token_id)
                on_chain = OnChainReputationSummary(**raw)
                source = "merged"
            except Exception as exc:  # noqa: BLE001 — chain calls are best-effort
                # Chain failure must not break the off-chain summary —
                # users on testnet RPC outages would otherwise see 500s
                # on the agent reputation page. Log + degrade.
                logger.warning(
                    "reputation_query_chain_summary_failed",
                    agent_id=agent_id,
                    token_id=on_chain_token_id,
                    error=str(exc)[:200],
                )

        return ReputationSummary(
            agent_id=agent_id,
            off_chain=off_chain,
            on_chain=on_chain,
            source=source,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _build_off_chain(
        self,
        agent_id: str,
        *,
        include_smoke_test: bool,
        recent_limit: int,
    ) -> OffChainReputationSummary:
        if self._repository is None:
            # Redis-only deployment or test stub. Returning zeros is
            # safer than raising — callers shouldn't have to branch on
            # configuration state.
            return OffChainReputationSummary(
                feedback_count=0,
                validation_count=0,
                recent_events=[],
            )

        feedback_count = await self._repository.count_for_agent(
            agent_id,
            kind=REPUTATION_KIND_FEEDBACK,
            include_smoke_test=include_smoke_test,
        )
        validation_count = await self._repository.count_for_agent(
            agent_id,
            kind=REPUTATION_KIND_VALIDATION,
            include_smoke_test=include_smoke_test,
        )

        # recent_limit=0 means counts-only; skip the list query.
        recent: list[ReputationEvent] = []
        if recent_limit > 0:
            recent = await self._repository.list_for_agent(
                agent_id,
                include_smoke_test=include_smoke_test,
                limit=recent_limit,
                offset=0,
            )

        return OffChainReputationSummary(
            feedback_count=feedback_count,
            validation_count=validation_count,
            recent_events=recent,
        )
