"""Redis Implementation of Subnet Repository

Concrete implementation using Redis for subnet persistence.
"""

import json
import logging
from datetime import UTC, datetime

import redis.asyncio as redis  # type: ignore[import-untyped]

from ....core.entities import Subnet
from ....core.interfaces import ISubnetRepository

logger = logging.getLogger(__name__)


class RedisSubnetRepository(ISubnetRepository):
    """
    Redis-based Subnet Repository

    Implements ISubnetRepository using Redis as storage backend.
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Initialize Redis Subnet Repository

        Args:
            redis_client: Redis async client instance
        """
        self.redis = redis_client

    async def save(self, subnet: Subnet) -> None:
        """Save or update a subnet in Redis"""
        subnet_key = f"acn:subnets:info:{subnet.subnet_id}"

        # Serialize subnet to dict
        subnet_dict = subnet.to_dict()
        # Convert complex types to JSON
        subnet_dict["security_config"] = json.dumps(subnet_dict["security_config"])
        subnet_dict["metadata"] = json.dumps(subnet_dict["metadata"])
        subnet_dict["member_agent_ids"] = json.dumps(subnet_dict["member_agent_ids"])

        # Save to Redis hash
        await self.redis.hset(subnet_key, mapping=subnet_dict)  # type: ignore[arg-type]

        # Update indices via pipeline
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.sadd(f"acn:subnets:by_owner:{subnet.owner}", subnet.subnet_id)
            await pipe.execute()

    async def find_by_id(self, subnet_id: str) -> Subnet | None:
        """Find subnet by ID"""
        subnet_key = f"acn:subnets:info:{subnet_id}"
        subnet_dict = await self.redis.hgetall(subnet_key)

        if not subnet_dict:
            return None

        return self._dict_to_subnet(subnet_dict)

    async def find_all(self) -> list[Subnet]:
        """Find all subnets"""
        subnets = []
        async for key in self.redis.scan_iter("acn:subnets:info:*"):
            subnet_dict = await self.redis.hgetall(key)
            if subnet_dict:
                subnets.append(self._dict_to_subnet(subnet_dict))
        return subnets

    async def find_by_owner(self, owner: str) -> list[Subnet]:
        """Find all subnets owned by a user"""
        subnet_ids = await self.redis.smembers(f"acn:subnets:by_owner:{owner}")
        subnets = []
        for subnet_id in subnet_ids:
            subnet = await self.find_by_id(subnet_id)
            if subnet:
                subnets.append(subnet)
        return subnets

    async def find_public_subnets(self) -> list[Subnet]:
        """Find all public subnets"""
        all_subnets = await self.find_all()
        return [s for s in all_subnets if s.is_public()]

    async def delete(self, subnet_id: str) -> bool:
        """Delete a subnet"""
        subnet = await self.find_by_id(subnet_id)
        if not subnet:
            return False

        # Remove from Redis
        subnet_key = f"acn:subnets:info:{subnet_id}"
        await self.redis.delete(subnet_key)

        # Remove from owner index
        await self.redis.srem(f"acn:subnets:by_owner:{subnet.owner}", subnet_id)

        return True

    async def exists(self, subnet_id: str) -> bool:
        """Check if subnet exists"""
        return await self.redis.exists(f"acn:subnets:info:{subnet_id}") > 0

    @staticmethod
    def _safe_loads(raw: str | None, default):
        """Safely parse a JSON string; return default on any error."""
        try:
            return json.loads(raw) if raw else default
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("subnet_repository: corrupted JSON field, using default", extra={"raw": str(raw)[:200]})
            return default

    @staticmethod
    def _safe_fromisoformat(raw: str | None, default):
        """Safely parse an ISO datetime string; return default on any error."""
        if not raw:
            return default
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            logger.warning("subnet_repository: invalid datetime field, using default", extra={"raw": str(raw)[:50]})
            return default

    def _dict_to_subnet(self, subnet_dict: dict) -> Subnet:
        """Convert Redis dict to Subnet entity"""
        data = {
            "subnet_id": subnet_dict["subnet_id"],
            "name": subnet_dict["name"],
            "owner": subnet_dict["owner"],
            "description": subnet_dict.get("description"),
            "is_private": subnet_dict.get("is_private") == "True",
            "security_config": self._safe_loads(subnet_dict.get("security_config", "{}"), {}),
            "member_agent_ids": set(self._safe_loads(subnet_dict.get("member_agent_ids", "[]"), [])),
            "created_at": self._safe_fromisoformat(subnet_dict.get("created_at"), datetime.now(UTC)),
            "metadata": self._safe_loads(subnet_dict.get("metadata", "{}"), {}),
        }

        return Subnet(**data)
