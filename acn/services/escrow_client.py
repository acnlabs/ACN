"""
Escrow Client

Calls Backend's Labs Escrow API for task budget management.
"""

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


class EscrowResult(BaseModel):
    """Escrow operation result"""

    success: bool
    message: str
    credits_after: float | None = None
    earnings_after: float | None = None
    error: str | None = None


class EscrowClient:
    """
    Client for Backend's Labs Escrow API

    Handles:
    - lock: Lock funds when task is created
    - release: Release reward to agent when task is approved
    - refund: Refund remaining budget when task is cancelled
    """

    def __init__(self, backend_url: str, timeout: float = 30.0):
        """
        Args:
            backend_url: Backend API base URL (e.g., "http://localhost:8000")
            timeout: Request timeout in seconds
        """
        self.backend_url = backend_url.rstrip("/")
        self.timeout = timeout

    async def lock(
        self,
        user_id: str,
        task_id: str,
        amount: float,
        description: str | None = None,
    ) -> EscrowResult:
        """
        Lock funds for task

        Args:
            user_id: Task creator's user_id
            task_id: Task ID
            amount: Amount to lock (total_budget)
            description: Optional description

        Returns:
            EscrowResult
        """
        if amount <= 0:
            return EscrowResult(
                success=True,
                message="No budget to lock",
                credits_after=None,
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/lock",
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
                        credits_after=data.get("credits_after"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    logger.warning(
                        "escrow_lock_failed",
                        task_id=task_id,
                        user_id=user_id,
                        error=error,
                    )
                    return EscrowResult(
                        success=False,
                        message="Failed to lock funds",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "escrow_lock_error",
                task_id=task_id,
                error=str(e),
            )
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
    ) -> EscrowResult:
        """
        Release reward to agent owner

        Args:
            creator_user_id: Task creator's user_id
            agent_owner_user_id: Agent owner's user_id
            task_id: Task ID
            amount: Reward amount
            description: Optional description

        Returns:
            EscrowResult
        """
        if amount <= 0:
            return EscrowResult(
                success=True,
                message="No reward to release",
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/release",
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
                    logger.info(
                        "escrow_released",
                        task_id=task_id,
                        to_user=agent_owner_user_id,
                        amount=amount,
                    )
                    return EscrowResult(
                        success=True,
                        message=data.get("message", "Released"),
                        earnings_after=data.get("earnings_after"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    logger.warning(
                        "escrow_release_failed",
                        task_id=task_id,
                        error=error,
                    )
                    return EscrowResult(
                        success=False,
                        message="Failed to release reward",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "escrow_release_error",
                task_id=task_id,
                error=str(e),
            )
            return EscrowResult(
                success=False,
                message="Escrow service unavailable",
                error=str(e),
            )

    async def refund(
        self,
        user_id: str,
        task_id: str,
        amount: float,
        description: str | None = None,
    ) -> EscrowResult:
        """
        Refund remaining budget to creator

        Args:
            user_id: Task creator's user_id
            task_id: Task ID
            amount: Amount to refund (remaining budget)
            description: Optional description

        Returns:
            EscrowResult
        """
        if amount <= 0:
            return EscrowResult(
                success=True,
                message="No budget to refund",
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/refund",
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
                        credits_after=data.get("credits_after"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    logger.warning(
                        "escrow_refund_failed",
                        task_id=task_id,
                        error=error,
                    )
                    return EscrowResult(
                        success=False,
                        message="Failed to refund",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "escrow_refund_error",
                task_id=task_id,
                error=str(e),
            )
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
        """
        Check if user has enough balance

        Args:
            user_id: User ID
            amount: Required amount

        Returns:
            (is_sufficient, current_credits)
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/check",
                    json={
                        "user_id": user_id,
                        "amount": amount,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    return data.get("sufficient", False), data.get("current_credits", 0)
                else:
                    return False, 0

        except httpx.RequestError:
            return False, 0
