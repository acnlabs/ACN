"""
Wallet Client

Calls Backend's Agent Wallet API for managing Agent platform credits.
"""

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


class WalletResult(BaseModel):
    """Wallet operation result"""

    success: bool
    message: str
    credits: float | None = None
    earnings: float | None = None
    balance: float | None = None
    error: str | None = None


class EarningsResult(BaseModel):
    """Earnings distribution result"""

    success: bool
    message: str
    agent_amount: float = 0.0
    owner_amount: float = 0.0
    credits: float | None = None
    earnings: float | None = None
    error: str | None = None


class WalletClient:
    """
    Client for Backend's Agent Wallet API

    Handles:
    - get_balance: Get agent wallet balance
    - spend: Deduct credits for agent actions
    - receive: Add credits to agent wallet
    - add_earnings: Distribute earnings with owner share split
    - topup: Owner tops up agent wallet
    - withdraw: Owner withdraws from agent wallet
    - set_owner_share: Set owner's earnings share ratio
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
        """Get request headers with internal token"""
        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    async def get_balance(self, agent_id: str) -> tuple[bool, float, float]:
        """
        Get agent wallet balance

        Args:
            agent_id: Agent ID

        Returns:
            (exists, credits, earnings)
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.backend_url}/api/agent-wallets/{agent_id}",
                    headers=self._get_headers(),
                )

                if response.status_code == 200:
                    data = response.json()
                    return True, data.get("credits", 0), data.get("earnings", 0)
                elif response.status_code == 404:
                    return False, 0, 0
                else:
                    logger.warning(
                        "wallet_get_balance_failed",
                        agent_id=agent_id,
                        status=response.status_code,
                    )
                    return False, 0, 0

        except httpx.RequestError as e:
            logger.error(
                "wallet_get_balance_error",
                agent_id=agent_id,
                error=str(e),
            )
            return False, 0, 0

    async def create_wallet(
        self,
        agent_id: str,
        owner_id: str | None = None,
        owner_share: float = 0.0,
    ) -> WalletResult:
        """
        Create agent wallet

        Args:
            agent_id: Agent ID
            owner_id: Agent owner's user_id
            owner_share: Owner's earnings share ratio (0-1)

        Returns:
            WalletResult
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/agent-wallets/{agent_id}",
                    headers=self._get_headers(),
                    json={
                        "owner_id": owner_id,
                        "owner_share": owner_share,
                    },
                )

                if response.status_code in (200, 201):
                    data = response.json()
                    logger.info(
                        "wallet_created",
                        agent_id=agent_id,
                        owner_id=owner_id,
                    )
                    return WalletResult(
                        success=True,
                        message="Wallet created",
                        credits=data.get("credits"),
                        earnings=data.get("earnings"),
                        balance=data.get("balance"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    return WalletResult(
                        success=False,
                        message="Failed to create wallet",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "wallet_create_error",
                agent_id=agent_id,
                error=str(e),
            )
            return WalletResult(
                success=False,
                message="Wallet service unavailable",
                error=str(e),
            )

    async def spend(
        self,
        agent_id: str,
        amount: float,
        description: str,
    ) -> WalletResult:
        """
        Agent spends credits

        Args:
            agent_id: Agent ID
            amount: Amount to spend
            description: Description of the spending

        Returns:
            WalletResult
        """
        if amount <= 0:
            return WalletResult(
                success=True,
                message="No amount to spend",
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/agent-wallets/{agent_id}/spend",
                    headers=self._get_headers(),
                    json={
                        "amount": amount,
                        "description": description,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "wallet_spend",
                        agent_id=agent_id,
                        amount=amount,
                        credits_after=data.get("credits"),
                    )
                    return WalletResult(
                        success=True,
                        message="Spent successfully",
                        credits=data.get("credits"),
                        earnings=data.get("earnings"),
                        balance=data.get("balance"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    logger.warning(
                        "wallet_spend_failed",
                        agent_id=agent_id,
                        amount=amount,
                        error=error,
                    )
                    return WalletResult(
                        success=False,
                        message="Failed to spend",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "wallet_spend_error",
                agent_id=agent_id,
                error=str(e),
            )
            return WalletResult(
                success=False,
                message="Wallet service unavailable",
                error=str(e),
            )

    async def receive(
        self,
        agent_id: str,
        amount: float,
        description: str,
    ) -> WalletResult:
        """
        Agent receives credits

        Args:
            agent_id: Agent ID
            amount: Amount to receive
            description: Description

        Returns:
            WalletResult
        """
        if amount <= 0:
            return WalletResult(
                success=True,
                message="No amount to receive",
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/agent-wallets/{agent_id}/receive",
                    headers=self._get_headers(),
                    json={
                        "amount": amount,
                        "description": description,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "wallet_receive",
                        agent_id=agent_id,
                        amount=amount,
                        credits_after=data.get("credits"),
                    )
                    return WalletResult(
                        success=True,
                        message="Received successfully",
                        credits=data.get("credits"),
                        earnings=data.get("earnings"),
                        balance=data.get("balance"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    logger.warning(
                        "wallet_receive_failed",
                        agent_id=agent_id,
                        amount=amount,
                        error=error,
                    )
                    return WalletResult(
                        success=False,
                        message="Failed to receive",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "wallet_receive_error",
                agent_id=agent_id,
                error=str(e),
            )
            return WalletResult(
                success=False,
                message="Wallet service unavailable",
                error=str(e),
            )

    async def add_earnings(
        self,
        agent_id: str,
        amount: float,
        description: str | None = None,
    ) -> EarningsResult:
        """
        Add earnings to agent (with owner share split)

        Args:
            agent_id: Agent ID
            amount: Total earnings amount
            description: Description

        Returns:
            EarningsResult with agent_amount and owner_amount
        """
        if amount <= 0:
            return EarningsResult(
                success=True,
                message="No earnings to add",
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/agent-wallets/{agent_id}/earnings",
                    headers=self._get_headers(),
                    json={
                        "amount": amount,
                        "description": description,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    agent_wallet = data.get("agent_wallet", {})
                    logger.info(
                        "wallet_earnings_added",
                        agent_id=agent_id,
                        amount=amount,
                        agent_amount=data.get("agent_amount"),
                        owner_amount=data.get("owner_amount"),
                    )
                    return EarningsResult(
                        success=True,
                        message="Earnings added",
                        agent_amount=data.get("agent_amount", 0),
                        owner_amount=data.get("owner_amount", 0),
                        credits=agent_wallet.get("credits"),
                        earnings=agent_wallet.get("earnings"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    logger.warning(
                        "wallet_earnings_failed",
                        agent_id=agent_id,
                        amount=amount,
                        error=error,
                    )
                    return EarningsResult(
                        success=False,
                        message="Failed to add earnings",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "wallet_earnings_error",
                agent_id=agent_id,
                error=str(e),
            )
            return EarningsResult(
                success=False,
                message="Wallet service unavailable",
                error=str(e),
            )

    async def topup(
        self,
        agent_id: str,
        amount: float,
        owner_id: str,
        description: str | None = None,
    ) -> WalletResult:
        """
        Owner tops up agent wallet

        Args:
            agent_id: Agent ID
            amount: Amount to top up
            owner_id: Owner's user_id
            description: Description

        Returns:
            WalletResult
        """
        if amount <= 0:
            return WalletResult(
                success=True,
                message="No amount to topup",
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/agent-wallets/{agent_id}/topup",
                    headers=self._get_headers(),
                    json={
                        "amount": amount,
                        "owner_id": owner_id,
                        "description": description,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "wallet_topup",
                        agent_id=agent_id,
                        amount=amount,
                        owner_id=owner_id,
                    )
                    return WalletResult(
                        success=True,
                        message="Topped up successfully",
                        credits=data.get("credits"),
                        earnings=data.get("earnings"),
                        balance=data.get("balance"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    logger.warning(
                        "wallet_topup_failed",
                        agent_id=agent_id,
                        error=error,
                    )
                    return WalletResult(
                        success=False,
                        message="Failed to topup",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "wallet_topup_error",
                agent_id=agent_id,
                error=str(e),
            )
            return WalletResult(
                success=False,
                message="Wallet service unavailable",
                error=str(e),
            )

    async def withdraw(
        self,
        agent_id: str,
        amount: float,
        owner_id: str,
        description: str | None = None,
    ) -> WalletResult:
        """
        Owner withdraws from agent wallet

        Args:
            agent_id: Agent ID
            amount: Amount to withdraw
            owner_id: Owner's user_id
            description: Description

        Returns:
            WalletResult
        """
        if amount <= 0:
            return WalletResult(
                success=True,
                message="No amount to withdraw",
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/agent-wallets/{agent_id}/withdraw",
                    headers=self._get_headers(),
                    json={
                        "amount": amount,
                        "owner_id": owner_id,
                        "description": description,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "wallet_withdraw",
                        agent_id=agent_id,
                        amount=amount,
                        owner_id=owner_id,
                    )
                    return WalletResult(
                        success=True,
                        message="Withdrawn successfully",
                        credits=data.get("credits"),
                        earnings=data.get("earnings"),
                        balance=data.get("balance"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    logger.warning(
                        "wallet_withdraw_failed",
                        agent_id=agent_id,
                        error=error,
                    )
                    return WalletResult(
                        success=False,
                        message="Failed to withdraw",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "wallet_withdraw_error",
                agent_id=agent_id,
                error=str(e),
            )
            return WalletResult(
                success=False,
                message="Wallet service unavailable",
                error=str(e),
            )

    async def set_owner_share(
        self,
        agent_id: str,
        owner_share: float,
        owner_id: str,
    ) -> WalletResult:
        """
        Set owner's earnings share ratio

        Args:
            agent_id: Agent ID
            owner_share: Share ratio (0-1)
            owner_id: Owner's user_id (for permission check)

        Returns:
            WalletResult
        """
        if owner_share < 0 or owner_share > 1:
            return WalletResult(
                success=False,
                message="Owner share must be between 0 and 1",
                error="Invalid owner_share value",
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.put(
                    f"{self.backend_url}/api/agent-wallets/{agent_id}/share",
                    headers=self._get_headers(),
                    json={
                        "owner_share": owner_share,
                        "owner_id": owner_id,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "wallet_owner_share_set",
                        agent_id=agent_id,
                        owner_share=owner_share,
                    )
                    return WalletResult(
                        success=True,
                        message="Owner share set successfully",
                        credits=data.get("credits"),
                        earnings=data.get("earnings"),
                        balance=data.get("balance"),
                    )
                else:
                    error = response.json().get("detail", response.text)
                    logger.warning(
                        "wallet_owner_share_failed",
                        agent_id=agent_id,
                        error=error,
                    )
                    return WalletResult(
                        success=False,
                        message="Failed to set owner share",
                        error=error,
                    )

        except httpx.RequestError as e:
            logger.error(
                "wallet_owner_share_error",
                agent_id=agent_id,
                error=str(e),
            )
            return WalletResult(
                success=False,
                message="Wallet service unavailable",
                error=str(e),
            )

    async def check_balance(
        self,
        agent_id: str,
        amount: float,
    ) -> tuple[bool, float]:
        """
        Check if agent has enough balance

        Args:
            agent_id: Agent ID
            amount: Required amount

        Returns:
            (is_sufficient, current_credits)
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.backend_url}/api/agent-wallets/{agent_id}/check",
                    headers=self._get_headers(),
                    json={"amount": amount},
                )

                if response.status_code == 200:
                    data = response.json()
                    return data.get("sufficient", False), data.get("current_credits", 0)
                else:
                    return False, 0

        except httpx.RequestError:
            return False, 0
