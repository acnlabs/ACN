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
    async def find_by_subnet(self, slug: str) -> list[Agent]:
        """
        Find all agents in a subnet

        Args:
            slug: Subnet slug identifier

        Returns:
            List of agents in the subnet
        """
        pass

    @abstractmethod
    async def find_by_tags(self, tags: list[str]) -> list[Agent]:
        """
        Find agents with ALL of the given tags.

        Online/offline filtering is intentionally NOT a parameter
        here. The single source of truth is the Redis alive key;
        wrap this call with ``AgentService._filter_by_status`` to
        apply that filter exactly once per query.

        Args:
            tags: List of required tag IDs

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
    async def count_by_subnet(self, slug: str) -> int:
        """
        Count agents in a subnet

        Args:
            slug: Subnet slug identifier

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
    async def record_inbound_delivery(
        self,
        agent_id: str,
        *,
        ok: bool,
        probe_ms: float | None = None,
        error: str | None = None,
        ttl: int,
    ) -> None:
        """Record the outcome of a real inbound direct-push to *agent_id*.

        This is the source of truth for **inbound reachability**, deliberately
        kept separate from the ``alive`` key (which conflates outbound liveness
        — heartbeats / authenticated calls — with inbound deliverability). Only
        ``MessageRouter.route()``'s actual Mode-A push result writes here.

        On success: stamp ``last_ok_at`` and reset the consecutive-failure
        counter. On failure: increment ``consec_fail`` and stamp
        ``last_fail_at`` / ``last_error``. Best-effort; callers swallow errors.
        """
        pass

    @abstractmethod
    async def get_inbound_health(self, agent_id: str) -> dict[str, object] | None:
        """Return the raw inbound-reachability record for *agent_id*.

        Keys (all optional): ``last_ok_at``, ``last_fail_at``, ``consec_fail``,
        ``last_error``, ``last_probe_ms``. Returns ``None`` if nothing has ever
        been recorded (no direct push has been attempted).
        """
        pass

