"""Agent Repository Interface

Defines contract for agent persistence operations.
"""

from abc import ABC, abstractmethod

from ..entities import Agent


class IAgentRepository(ABC):
    """
    Abstract interface for Agent persistence

    Infrastructure layer provides concrete implementation (e.g., Redis, PostgreSQL).
    This allows business logic to be independent of storage details.
    """

    @abstractmethod
    async def save(self, agent: Agent) -> None:
        """
        Save or update an agent

        Args:
            agent: Agent entity to save
        """
        pass

    @abstractmethod
    async def find_by_id(self, agent_id: str) -> Agent | None:
        """
        Find agent by ID

        Args:
            agent_id: Agent identifier

        Returns:
            Agent entity or None if not found
        """
        pass

    @abstractmethod
    async def find_by_owner_and_endpoint(self, owner: str, endpoint: str) -> Agent | None:
        """
        Find agent by owner and endpoint (for re-registration check)

        Args:
            owner: Agent owner
            endpoint: Agent endpoint URL

        Returns:
            Agent entity or None if not found
        """
        pass

    @abstractmethod
    async def find_all(self) -> list[Agent]:
        """
        Find all agents

        Returns:
            List of all agent entities
        """
        pass

    @abstractmethod
    async def find_by_subnet(self, subnet_id: str) -> list[Agent]:
        """
        Find all agents in a subnet

        Args:
            subnet_id: Subnet identifier

        Returns:
            List of agents in the subnet
        """
        pass

    @abstractmethod
    async def find_by_tags(self, tags: list[str], status: str = "all") -> list[Agent]:
        """
        Find agents with ALL of the given tags.

        Args:
            tags: List of required tag IDs
            status: **Deprecated.** Kept for ABI compatibility; both the
                Redis and PostgreSQL implementations ignore this argument.
                Filter by online/offline at the service layer via
                ``AgentService._filter_by_status`` so the single source
                of truth (Redis alive key) is consulted exactly once per
                query. To be removed in Phase 2 along with the legacy
                ``Agent.status`` DB column.

        Returns:
            List of agents matching the tag criteria.
        """
        pass

    @abstractmethod
    async def find_by_owner(self, owner: str) -> list[Agent]:
        """
        Find all agents owned by a user/system

        Args:
            owner: Agent owner identifier

        Returns:
            List of agents owned by the user
        """
        pass

    @abstractmethod
    async def delete(self, agent_id: str) -> bool:
        """
        Delete an agent

        Args:
            agent_id: Agent identifier

        Returns:
            True if deleted, False if not found
        """
        pass

    @abstractmethod
    async def exists(self, agent_id: str) -> bool:
        """
        Check if agent exists

        Args:
            agent_id: Agent identifier

        Returns:
            True if agent exists
        """
        pass

    @abstractmethod
    async def count_by_subnet(self, subnet_id: str) -> int:
        """
        Count agents in a subnet

        Args:
            subnet_id: Subnet identifier

        Returns:
            Number of agents in the subnet
        """
        pass

    @abstractmethod
    async def find_by_api_key(self, key_hash: str) -> Agent | None:
        """Find agent by SHA-256 hash of their API key.

        Args:
            key_hash: SHA-256 hex digest of the raw API key

        Returns:
            Agent entity or None if not found
        """
        pass

    @abstractmethod
    async def find_unclaimed(self, limit: int = 100) -> list[Agent]:
        """
        Find all unclaimed agents

        Args:
            limit: Maximum number of agents to return

        Returns:
            List of unclaimed agents
        """
        pass

    @abstractmethod
    async def set_alive(self, agent_id: str, ttl: int) -> None:
        """
        Set or renew the alive signal key for an agent.

        Args:
            agent_id: Agent identifier
            ttl: Time-to-live in seconds
        """
        pass

    @abstractmethod
    async def filter_alive(self, agent_ids: list[str]) -> set[str]:
        """
        Return the subset of agent_ids whose alive key exists in Redis.
        Uses a PIPELINE for efficiency.

        Args:
            agent_ids: List of agent identifiers to check

        Returns:
            Set of agent_ids that are currently alive
        """
        pass

    @abstractmethod
    async def mark_offline_stale(self) -> int:
        """
        Mark agents whose alive key has expired as offline in Redis.
        Used by the background watchdog task.

        Returns:
            Number of agents marked offline
        """
        pass
