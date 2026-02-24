"""
Escrow Client

Calls Backend's Labs Escrow API for task budget management.
Supports both v1 (backward compat) and v2 (full lifecycle) endpoints.
"""

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


class EscrowResult(BaseModel):
    """Escrow operation result"""

    success: bool
    message: str
    balance_after: float | None = None
    escrow_id: str | None = None
    error: str | None = None


class EscrowDetailResult(BaseModel):
    """v2 Escrow detail result"""

    success: bool
    escrow_id: str | None = None
    task_id: str | None = None
    status: str | None = None
    total_amount: float | None = None
    released_amount: float | None = None
    refunded_amount: float | None = None
    auto_release_at: str | None = None
    error: str | None = None


class EscrowClient:
    """
    Client for Backend's Labs Escrow API

    Handles:
    - lock: Lock funds when task is created (v2: supports agent creators)
    - release: Release reward to agent when task is approved
    - refund: Refund remaining budget when task is cancelled
    - accept: Agent accepts a task (v2)
    - submit: Agent submits deliverable (v2)
    """

    def __init__(
        self,
        backend_url: str,
        timeout: float = 30.0,
        internal_token: str | None = None,
    ):
        """
        Args:
            backend_url: Backend API base URL (e.g., "http://localhost:8000")
            timeout: Request timeout in seconds
            internal_token: Internal API token for authentication
        """
        self.backend_url = backend_url.rstrip("/")
        self.timeout = timeout
        self.internal_token = internal_token

    def _get_headers(self) -> dict:
        """获取请求头（包含 Internal Token）"""
        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    # ========== v2 Endpoints (推荐使用) ==========

    async def lock_v2(
        self,
        task_id: str,
        creator_id: str,
        creator_type: str,
        amount: float,
        auto_release_days: int = 7,
        description: str | None = None,
    ) -> EscrowDetailResult:
        """
        v2: Lock funds for task (supports both human and agent creators)

        Args:
            task_id: Task ID
            creator_id: Creator ID (user_id or agent_id)
            creator_type: "human" | "agent"
            amount: Amount to lock
            auto_release_days: Days before auto-release
            description: Optional description

        Returns:
            EscrowDetailResult
        """
        if amount <= 0:
            return EscrowDetailResult(
                success=True,
                message="No budget to lock",
            )

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
                    return EscrowDetailResult(
                        success=False,
                        error=str(error),
                    )

        except httpx.RequestError as e:
            logger.error("escrow_lock_v2_error", task_id=task_id, error=str(e))
            return EscrowDetailResult(
                success=False,
                error=str(e),
            )

    async def accept_v2(
        self,
        escrow_id: str,
        assignee_id: str,
        assignee_type: str = "agent",
    ) -> EscrowDetailResult:
        """v2: Agent accepts a task"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/v2/{escrow_id}/accept",
                    headers=self._get_headers(),
                    json={
                        "assignee_id": assignee_id,
                        "assignee_type": assignee_type,
                    },
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
        """v2: Agent submits deliverable"""
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
        """v2: Get escrow by task ID"""
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
                    return EscrowDetailResult(
                        success=False,
                        error="Escrow not found",
                    )
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
    ) -> EscrowDetailResult:
        """
        v2: Release a partial amount from escrow pool (multi-participant mode).

        Unlike release() which releases all remaining funds, this releases
        a specific amount to a specific recipient and keeps the escrow active.

        Args:
            escrow_id: Escrow ID
            recipient_id: Recipient agent/user ID
            recipient_type: "agent" or "human"
            amount: Amount to release for this completion
            notes: Optional notes

        Returns:
            EscrowDetailResult
        """
        if amount <= 0:
            return EscrowDetailResult(success=True, escrow_id=escrow_id)

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
                    logger.info(
                        "escrow_release_partial",
                        escrow_id=escrow_id,
                        recipient_id=recipient_id,
                        amount=amount,
                    )
                    return EscrowDetailResult(
                        success=True,
                        escrow_id=data.get("escrow_id"),
                        status=data.get("status"),
                        total_amount=data.get("total_amount"),
                        released_amount=data.get("released_amount"),
                    )
                else:
                    error = self._extract_error(response)
                    logger.warning(
                        "escrow_release_partial_failed",
                        escrow_id=escrow_id,
                        status_code=response.status_code,
                        error=error,
                    )
                    return EscrowDetailResult(
                        success=False,
                        error=str(error),
                    )

        except httpx.RequestError as e:
            logger.error("escrow_release_partial_error", escrow_id=escrow_id, error=str(e))
            return EscrowDetailResult(success=False, error=str(e))

    # ========== v1 Endpoints (ACN 兼容) ==========

    async def lock(
        self,
        user_id: str,
        task_id: str,
        amount: float,
        description: str | None = None,
    ) -> EscrowResult:
        """
        v1: Lock funds for task (human creators only)

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
                balance_after=None,
            )

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
    ) -> EscrowResult:
        """
        v1: Release reward to agent owner

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
                    logger.info(
                        "escrow_released",
                        task_id=task_id,
                        to_user=agent_owner_user_id,
                        amount=amount,
                    )
                    return EscrowResult(
                        success=True,
                        message=data.get("message", "Released"),
                        balance_after=data.get("balance_after"),
                    )
                else:
                    error = self._extract_error(response)
                    logger.warning(
                        "escrow_release_failed",
                        task_id=task_id,
                        error=error,
                    )
                    return EscrowResult(
                        success=False,
                        message="Failed to release reward",
                        error=str(error),
                    )

        except httpx.RequestError as e:
            logger.error("escrow_release_error", task_id=task_id, error=str(e))
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
        v1: Refund remaining budget to creator

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
        """
        Check if user has enough balance

        Args:
            user_id: User ID
            amount: Required amount

        Returns:
            (is_sufficient, current_balance)
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.backend_url}/api/labs/escrow/check",
                    headers=self._get_headers(),
                    json={
                        "user_id": user_id,
                        "amount": amount,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    return data.get("sufficient", False), data.get("current_balance", 0)
                else:
                    return False, 0

        except httpx.RequestError:
            return False, 0

    # ========== Helpers ==========

    @staticmethod
    def _extract_error(response: httpx.Response) -> str:
        """Extract error message from response"""
        try:
            return response.json().get("detail", response.text)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
