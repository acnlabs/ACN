"""
Rewards Client

Calls Backend's Rewards API for granting rewards.
"""

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


class RewardResult(BaseModel):
    """Reward operation result"""

    success: bool
    recipient_id: str = ""
    reward_type: str = ""
    amount: int = 0
    new_balance: int = 0
    message: str = ""
    error: str | None = None


class RewardConfig(BaseModel):
    """Reward configuration from Backend"""

    referral: int = 100
    first_post: int = 20
    weekly_active: int = 50
    collaboration: int = 75


class RewardsClient:
    """
    Client for Backend's Rewards API

    Handles:
    - get_config: Get reward amounts configuration
    - grant: Grant a reward to an agent or user
    """

    def __init__(self, backend_url: str, timeout: float = 30.0):
        """
        Args:
            backend_url: Backend API base URL (e.g., "http://localhost:8000")
            timeout: Request timeout in seconds
        """
        self.backend_url = backend_url.rstrip("/")
        self.timeout = timeout
        self._config_cache: RewardConfig | None = None

    async def get_config(self, force_refresh: bool = False) -> RewardConfig:
        """
        Get reward configuration from Backend

        Args:
            force_refresh: Force refresh cache

        Returns:
            RewardConfig
        """
        if self._config_cache and not force_refresh:
            return self._config_cache

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.get(f"{self.backend_url}/api/rewards/config")

                if response.status_code == 200:
                    data = response.json()
                    self._config_cache = RewardConfig(**data)
                    return self._config_cache
                else:
                    logger.warning(
                        "rewards_config_failed",
                        status=response.status_code,
                        body=response.text[:200],
                    )
                    return RewardConfig()  # Return defaults

        except Exception as e:
            logger.error("rewards_config_error", error=str(e))
            return RewardConfig()  # Return defaults

    async def grant(
        self,
        recipient_type: str,
        recipient_id: str,
        reward_type: str,
        amount: int | None = None,
        reason: str | None = None,
        source_id: str | None = None,
    ) -> RewardResult:
        """
        Grant a reward to an agent or user

        Args:
            recipient_type: "agent" or "user"
            recipient_id: Agent ID or User ID
            reward_type: "referral", "first_post", "weekly_active", "collaboration", or "custom"
            amount: Custom amount (only for "custom" reward_type)
            reason: Reason for the reward
            source_id: Source of the reward (e.g., referrer agent ID)

        Returns:
            RewardResult
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                payload = {
                    "recipient_type": recipient_type,
                    "recipient_id": recipient_id,
                    "reward_type": reward_type,
                }

                if amount is not None:
                    payload["amount"] = amount
                if reason:
                    payload["reason"] = reason
                if source_id:
                    payload["source_id"] = source_id

                response = await client.post(
                    f"{self.backend_url}/api/rewards/grant",
                    json=payload,
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(
                        "reward_granted",
                        recipient_id=recipient_id,
                        reward_type=reward_type,
                        amount=data.get("amount"),
                    )
                    return RewardResult(
                        success=data.get("success", True),
                        recipient_id=data.get("recipient_id", recipient_id),
                        reward_type=data.get("reward_type", reward_type),
                        amount=data.get("amount", 0),
                        new_balance=data.get("new_balance", 0),
                        message=data.get("message", "Reward granted"),
                    )
                else:
                    error_msg = response.text[:200]
                    logger.warning(
                        "reward_grant_failed",
                        recipient_id=recipient_id,
                        reward_type=reward_type,
                        status=response.status_code,
                        error=error_msg,
                    )
                    return RewardResult(
                        success=False,
                        recipient_id=recipient_id,
                        reward_type=reward_type,
                        message="Failed to grant reward",
                        error=error_msg,
                    )

        except Exception as e:
            logger.error(
                "reward_grant_error",
                recipient_id=recipient_id,
                reward_type=reward_type,
                error=str(e),
            )
            return RewardResult(
                success=False,
                recipient_id=recipient_id,
                reward_type=reward_type,
                message="Failed to grant reward",
                error=str(e),
            )

    async def grant_referral_bonus(
        self,
        referrer_id: str,
        new_agent_id: str,
    ) -> RewardResult:
        """
        Grant referral bonus to the referring agent

        Args:
            referrer_id: ID of the agent who made the referral
            new_agent_id: ID of the newly joined agent

        Returns:
            RewardResult
        """
        return await self.grant(
            recipient_type="agent",
            recipient_id=referrer_id,
            reward_type="referral",
            reason=f"Referred agent {new_agent_id} to join ACN",
            source_id=new_agent_id,
        )
