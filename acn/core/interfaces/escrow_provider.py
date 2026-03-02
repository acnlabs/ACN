"""
Escrow Provider Interface

Defines the contract for escrow providers used by ACN's task system.

Design principles:
- Split ratios (fee_rate, acn_referral_rate) are decided by the provider internally.
  ACN does not maintain a copy — it trusts the ReleaseResult returned by the provider.
- All release operations must atomically complete the 3-way split:
    agent + ACN revenue wallet + provider revenue. No post-hoc settlement.
- ACN only accepts providers that can enforce the split at release time
  (on-chain: baked into the smart contract; off-chain: same DB transaction).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


# =============================================================================
# Return Types (migrated from services/escrow_client.py to avoid layer violation)
# =============================================================================


class EscrowResult(BaseModel):
    """Escrow operation result (v1 endpoints)"""

    success: bool
    message: str = ""
    balance_after: float | None = None
    escrow_id: str | None = None
    error: str | None = None


class EscrowDetailResult(BaseModel):
    """Escrow detail result (v2 endpoints)"""

    success: bool
    escrow_id: str | None = None
    task_id: str | None = None
    status: str | None = None
    total_amount: float | None = None
    released_amount: float | None = None
    refunded_amount: float | None = None
    auto_release_at: str | None = None
    error: str | None = None


class ReleaseResult(BaseModel):
    """
    Result of a release or release_partial operation.

    The split amounts are computed by the provider (Backend or smart contract)
    and returned here for ACN to log. ACN does not recompute them.

    Split example (100 ap_points, fee_rate=0.15, acn_referral_rate=0.20):
        agent_amount   = 85.0   (100 * 0.85)
        acn_amount     =  3.0   (100 * 0.15 * 0.20)
        provider_amount = 12.0  (100 * 0.15 * 0.80)
    """

    success: bool
    agent_amount: float = 0.0
    acn_amount: float = 0.0       # ACN referral share (logged for P&L tracking)
    provider_amount: float = 0.0  # Escrow provider net fee
    proof: str | None = None      # Chain tx_hash or off-chain receipt_id
    error: str | None = None


# =============================================================================
# IEscrowProvider
# =============================================================================


class IEscrowProvider(ABC):
    """
    Abstract interface for escrow providers.

    Fee rates and split ratios are provider-internal concerns.
    ACN does not configure or validate them — it only records
    the ReleaseResult returned after each release operation.

    Implementations:
    - AgentPlanetEscrowProvider: HTTP client → Backend /api/labs/escrow/*
    - Web3EscrowProvider (future): calls on-chain smart contract
    """

    @property
    @abstractmethod
    def supported_currencies(self) -> list[str]:
        """
        Currency identifiers this provider handles.
        Example: ["ap_points"] for AgentPlanetEscrowProvider.
        """
        ...

    # -------------------------------------------------------------------------
    # v2 Lifecycle (recommended)
    # -------------------------------------------------------------------------

    @abstractmethod
    async def lock_v2(
        self,
        task_id: str,
        creator_id: str,
        creator_type: str,
        amount: float,
        auto_release_days: int = 7,
        description: str | None = None,
    ) -> EscrowDetailResult:
        """Lock funds when a task is created (supports both human and agent creators)."""
        ...

    @abstractmethod
    async def accept_v2(
        self,
        escrow_id: str,
        assignee_id: str,
        assignee_type: str = "agent",
    ) -> EscrowDetailResult:
        """Agent accepts a task; transitions escrow to IN_PROGRESS."""
        ...

    @abstractmethod
    async def submit_v2(self, escrow_id: str) -> EscrowDetailResult:
        """Agent submits deliverable; transitions escrow to SUBMITTED."""
        ...

    @abstractmethod
    async def release_partial(
        self,
        escrow_id: str,
        recipient_id: str,
        recipient_type: str = "agent",
        amount: float = 0,
        notes: str | None = None,
    ) -> ReleaseResult:
        """
        Release a partial amount for one completion (multi-participant tasks).

        The provider atomically splits the amount into agent + ACN + provider shares.
        Escrow stays active (IN_PROGRESS) until fully exhausted.
        """
        ...

    @abstractmethod
    async def get_by_task(self, task_id: str) -> EscrowDetailResult:
        """Retrieve escrow state by task ID."""
        ...

    # -------------------------------------------------------------------------
    # v1 Compatibility
    # -------------------------------------------------------------------------

    @abstractmethod
    async def lock(
        self,
        user_id: str,
        task_id: str,
        amount: float,
        description: str | None = None,
    ) -> EscrowResult:
        """Lock funds (v1, human creators only)."""
        ...

    @abstractmethod
    async def release(
        self,
        creator_user_id: str,
        agent_owner_user_id: str,
        task_id: str,
        amount: float,
        description: str | None = None,
    ) -> ReleaseResult:
        """
        Release full reward to agent owner (single-participant tasks).

        The provider atomically splits amount into agent + ACN + provider shares
        and transitions escrow to RELEASED.
        """
        ...

    @abstractmethod
    async def refund(
        self,
        user_id: str,
        task_id: str,
        amount: float,
        description: str | None = None,
    ) -> EscrowResult:
        """Refund remaining budget to task creator (on cancellation)."""
        ...

    @abstractmethod
    async def check_balance(
        self,
        user_id: str,
        amount: float,
    ) -> tuple[bool, float]:
        """
        Check if user has sufficient balance to fund a task.

        Returns:
            (is_sufficient, current_balance)
        """
        ...
