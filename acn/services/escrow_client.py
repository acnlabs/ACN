"""
Agent Planet Escrow Provider

Implements IEscrowProvider by calling Backend's Labs Escrow API.
Supports both v1 (backward compat) and v2 (full lifecycle) endpoints.

The 3-way fee split (agent / ACN / provider) is enforced atomically by the
Backend's EscrowService.release() and EscrowService.release_partial() methods.
ACN receives the breakdown in ReleaseResult and logs it for P&L tracking.
"""

import httpx
import structlog

from acn.core.interfaces.escrow_provider import (
    EscrowDetailResult,
    EscrowResult,
    IEscrowProvider,
    ReleaseResult,
)
from acn.protocols.ap2.core import AP_POINTS

logger = structlog.get_logger()


class AgentPlanetEscrowProvider(IEscrowProvider):
    """
    IEscrowProvider implementation backed by Agent Planet's Backend.

    Fee split ratios (ESCROW_FEE_RATE, ACN_REFERRAL_RATE) live in the
    Backend's config — this client simply parses and surfaces ReleaseResult.
    """

    def __init__(
        self,
        backend_url: str,
        timeout: float = 30.0,
        internal_token: str | None = None,
    ):
        self.backend_url = backend_url.rstrip("/")
        self.timeout = timeout
        self.internal_token = internal_token

    @property
    def supported_currencies(self) -> list[str]:
        return [AP_POINTS]

    def _get_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    # -------------------------------------------------------------------------
    # v2 Lifecycle
    # -------------------------------------------------------------------------

    async def lock_v2(
        self,
        task_id: str,
        creator_id: str,
        creator_type: str,
        amount: float,
        auto_release_days: int = 7,
        description: str | None = None,
    ) -> EscrowDetailResult:
        if amount <= 0:
            return EscrowDetailResult(success=True)

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/v2/lock",
                    headers=self._get_headers(),
                    json={
                        "task_id": task_id,
                        "creator_id": creator_id,
                        "creator_type": creator_type,
                        "amount": amount,
                        "auto_release_days": auto_release_days,
                        "description": description,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "escrow_locked_v2",
                        task_id=task_id,
                        creator_id=creator_id,
                        creator_type=creator_type,
                        amount=amount,
                        escrow_id=data.get("escrow_id"),
                    )
                    return EscrowDetailResult(
                        success=True,
                        escrow_id=data.get("escrow_id"),
                        task_id=data.get("task_id"),
                        status=data.get("status"),
                        total_amount=data.get("total_amount"),
                    )
                else:
                    error = self._extract_error(response)
                    logger.warning(
                        "escrow_lock_v2_failed",
                        task_id=task_id,
                        status_code=response.status_code,
                        error=error,
                    )
                    return EscrowDetailResult(success=False, error=str(error))

        except httpx.RequestError as e:
            logger.error("escrow_lock_v2_error", task_id=task_id, error=str(e))
            return EscrowDetailResult(success=False, error=str(e))

    async def accept_v2(
        self,
        escrow_id: str,
        assignee_id: str,
        assignee_type: str = "agent",
    ) -> EscrowDetailResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/v2/{escrow_id}/accept",
                    headers=self._get_headers(),
                    json={"assignee_id": assignee_id, "assignee_type": assignee_type},
                )

                if response.status_code == 200:
                    data = response.json()
                    return EscrowDetailResult(
                        success=True,
                        escrow_id=data.get("escrow_id"),
                        status=data.get("status"),
                    )
                else:
                    return EscrowDetailResult(
                        success=False,
                        error=self._extract_error(response),
                    )

        except httpx.RequestError as e:
            return EscrowDetailResult(success=False, error=str(e))

    async def submit_v2(self, escrow_id: str) -> EscrowDetailResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/v2/{escrow_id}/submit",
                    headers=self._get_headers(),
                )

                if response.status_code == 200:
                    data = response.json()
                    return EscrowDetailResult(
                        success=True,
                        escrow_id=data.get("escrow_id"),
                        status=data.get("status"),
                        auto_release_at=data.get("auto_release_at"),
                    )
                else:
                    return EscrowDetailResult(
                        success=False,
                        error=self._extract_error(response),
                    )

        except httpx.RequestError as e:
            return EscrowDetailResult(success=False, error=str(e))

    async def get_by_task(self, task_id: str) -> EscrowDetailResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.get(
                    f"{self.backend_url}/api/labs/escrow/v2/task/{task_id}",
                    headers=self._get_headers(),
                )

                if response.status_code == 200:
                    data = response.json()
                    return EscrowDetailResult(
                        success=True,
                        escrow_id=data.get("escrow_id"),
                        task_id=data.get("task_id"),
                        status=data.get("status"),
                        total_amount=data.get("total_amount"),
                        released_amount=data.get("released_amount"),
                        refunded_amount=data.get("refunded_amount"),
                        auto_release_at=data.get("auto_release_at"),
                    )
                elif response.status_code == 404:
                    return EscrowDetailResult(success=False, error="Escrow not found")
                else:
                    return EscrowDetailResult(
                        success=False,
                        error=self._extract_error(response),
                    )

        except httpx.RequestError as e:
            return EscrowDetailResult(success=False, error=str(e))

    async def release_partial(
        self,
        escrow_id: str,
        recipient_id: str,
        recipient_type: str = "agent",
        amount: float = 0,
        notes: str | None = None,
    ) -> ReleaseResult:
        """
        Release a partial amount with atomic 3-way split enforced by Backend.

        Backend response (new contract):
            {agent_amount, acn_amount, escrow_revenue_amount, receipt_id}
        """
        if amount <= 0:
            return ReleaseResult(success=True)

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/v2/{escrow_id}/release_partial",
                    headers=self._get_headers(),
                    json={
                        "recipient_id": recipient_id,
                        "recipient_type": recipient_type,
                        "amount": amount,
                        "notes": notes,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    result = ReleaseResult(
                        success=True,
                        agent_amount=data.get("agent_amount", 0.0),
                        acn_amount=data.get("acn_amount", 0.0),
                        provider_amount=data.get("escrow_revenue_amount", 0.0),
                        proof=data.get("receipt_id"),
                    )
                    logger.info(
                        "escrow_release_partial",
                        escrow_id=escrow_id,
                        recipient_id=recipient_id,
                        amount=amount,
                        agent_amount=result.agent_amount,
                        acn_amount=result.acn_amount,
                        provider_amount=result.provider_amount,
                        receipt_id=result.proof,
                    )
                    return result
                else:
                    error = self._extract_error(response)
                    logger.warning(
                        "escrow_release_partial_failed",
                        escrow_id=escrow_id,
                        status_code=response.status_code,
                        error=error,
                    )
                    return ReleaseResult(success=False, error=str(error))

        except httpx.RequestError as e:
            logger.error("escrow_release_partial_error", escrow_id=escrow_id, error=str(e))
            return ReleaseResult(success=False, error=str(e))

    # -------------------------------------------------------------------------
    # v1 Compatibility
    # -------------------------------------------------------------------------

    async def lock(
        self,
        user_id: str,
        task_id: str,
        amount: float,
        description: str | None = None,
    ) -> EscrowResult:
        if amount <= 0:
            return EscrowResult(success=True, message="No budget to lock")

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/lock",
                    headers=self._get_headers(),
                    json={
                        "user_id": user_id,
                        "task_id": task_id,
                        "amount": amount,
                        "description": description,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "escrow_locked",
                        task_id=task_id,
                        user_id=user_id,
                        amount=amount,
                    )
                    return EscrowResult(
                        success=True,
                        message=data.get("message", "Locked"),
                        balance_after=data.get("balance_after"),
                    )
                else:
                    error = self._extract_error(response)
                    logger.warning(
                        "escrow_lock_failed",
                        task_id=task_id,
                        user_id=user_id,
                        status_code=response.status_code,
                        error=error,
                    )
                    return EscrowResult(
                        success=False,
                        message="Failed to lock funds",
                        error=str(error),
                    )

        except httpx.RequestError as e:
            logger.error("escrow_lock_error", task_id=task_id, error=str(e))
            return EscrowResult(
                success=False,
                message="Escrow service unavailable",
                error=str(e),
            )

    async def release(
        self,
        creator_user_id: str,
        agent_owner_user_id: str,
        task_id: str,
        amount: float,
        description: str | None = None,
    ) -> ReleaseResult:
        """
        Release full reward with atomic 3-way split enforced by Backend.

        Backend response (new contract):
            {agent_amount, acn_amount, escrow_revenue_amount, receipt_id}
        """
        if amount <= 0:
            return ReleaseResult(success=True)

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/release",
                    headers=self._get_headers(),
                    json={
                        "creator_user_id": creator_user_id,
                        "agent_owner_user_id": agent_owner_user_id,
                        "task_id": task_id,
                        "amount": amount,
                        "description": description,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    result = ReleaseResult(
                        success=True,
                        agent_amount=data.get("agent_amount", 0.0),
                        acn_amount=data.get("acn_amount", 0.0),
                        provider_amount=data.get("escrow_revenue_amount", 0.0),
                        proof=data.get("receipt_id"),
                    )
                    logger.info(
                        "escrow_released",
                        task_id=task_id,
                        to_user=agent_owner_user_id,
                        amount=amount,
                        agent_amount=result.agent_amount,
                        acn_amount=result.acn_amount,
                        provider_amount=result.provider_amount,
                        receipt_id=result.proof,
                    )
                    return result
                else:
                    error = self._extract_error(response)
                    logger.warning(
                        "escrow_release_failed",
                        task_id=task_id,
                        error=error,
                    )
                    return ReleaseResult(success=False, error=str(error))

        except httpx.RequestError as e:
            logger.error("escrow_release_error", task_id=task_id, error=str(e))
            return ReleaseResult(success=False, error=str(e))

    async def refund(
        self,
        user_id: str,
        task_id: str,
        amount: float,
        description: str | None = None,
    ) -> EscrowResult:
        if amount <= 0:
            return EscrowResult(success=True, message="No budget to refund")

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/refund",
                    headers=self._get_headers(),
                    json={
                        "user_id": user_id,
                        "task_id": task_id,
                        "amount": amount,
                        "description": description,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "escrow_refunded",
                        task_id=task_id,
                        user_id=user_id,
                        amount=amount,
                    )
                    return EscrowResult(
                        success=True,
                        message=data.get("message", "Refunded"),
                        balance_after=data.get("balance_after"),
                    )
                else:
                    error = self._extract_error(response)
                    logger.warning(
                        "escrow_refund_failed",
                        task_id=task_id,
                        error=error,
                    )
                    return EscrowResult(
                        success=False,
                        message="Failed to refund",
                        error=str(error),
                    )

        except httpx.RequestError as e:
            logger.error("escrow_refund_error", task_id=task_id, error=str(e))
            return EscrowResult(
                success=False,
                message="Escrow service unavailable",
                error=str(e),
            )

    async def check_balance(
        self,
        user_id: str,
        amount: float,
    ) -> tuple[bool, float]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/check",
                    headers=self._get_headers(),
                    json={"user_id": user_id, "amount": amount},
                )

                if response.status_code == 200:
                    data = response.json()
                    return data.get("sufficient", False), data.get("current_balance", 0)
                else:
                    return False, 0

        except httpx.RequestError:
            return False, 0

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_error(response: httpx.Response) -> str:
        try:
            return response.json().get("detail", response.text)
        except Exception:
            return response.text or f"HTTP {response.status_code}"


# ---------------------------------------------------------------------------
# Backward compatibility aliases
# Keep EscrowClient pointing to AgentPlanetEscrowProvider so that any external
# code that imports EscrowClient continues to work without modification.
# ---------------------------------------------------------------------------
EscrowClient = AgentPlanetEscrowProvider

__all__ = [
    "AgentPlanetEscrowProvider",
    "EscrowClient",
    # Re-export DTOs for backward compat (previously defined in this module)
    "EscrowResult",
    "EscrowDetailResult",
    "ReleaseResult",
]
