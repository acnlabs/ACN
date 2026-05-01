"""Follow Repository Interface.

Defines contract for the agent-follow relation (single-direction "X follows Y").

Follows are intentionally narrow:
  - Pure intent expression — no permission grant, no inbox impact.
  - Public on read; write requires the follower's API key (enforced at the
    route layer, not here).
  - Idempotent: repeating a follow / unfollow is a no-op.

The interface is storage-agnostic so the Redis implementation (sorted sets,
see ``RedisFollowRepository``) can be swapped for Postgres later without
touching the service layer.
"""

from abc import ABC, abstractmethod


class IFollowRepository(ABC):
    """Abstract interface for the follow graph persistence layer."""

    @abstractmethod
    async def add(self, follower_id: str, followee_id: str) -> bool:
        """Persist ``follower_id`` follows ``followee_id``.

        Returns:
            True if a NEW follow edge was created, False if the edge already
            existed (idempotent path). Service layer uses this to decide
            whether to fire side-effects (audit, analytics, …).
        """

    @abstractmethod
    async def remove(self, follower_id: str, followee_id: str) -> bool:
        """Drop the follow edge if present.

        Returns:
            True if an edge was removed, False if no edge existed.
        """

    @abstractmethod
    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        """Check whether ``follower_id`` currently follows ``followee_id``."""

    @abstractmethod
    async def list_following(
        self,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        """Return ids of agents that ``agent_id`` follows.

        Ordering: most-recently followed first (LIFO by timestamp), so the
        common UI of "latest follows" needs no client-side sort.
        """

    @abstractmethod
    async def list_followers(
        self,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        """Return ids of agents that follow ``agent_id`` (LIFO by timestamp)."""

    @abstractmethod
    async def count_following(self, agent_id: str) -> int:
        """Number of agents ``agent_id`` follows. O(1)."""

    @abstractmethod
    async def count_followers(self, agent_id: str) -> int:
        """Number of agents that follow ``agent_id``. O(1)."""

    @abstractmethod
    async def count_follows_batch(self, agent_ids: list[str]) -> dict[str, tuple[int, int]]:
        """Batch-fetch ``(following, followers)`` counts for many agents.

        Returns a dict mapping ``agent_id`` → ``(following_count, followers_count)``.
        Implementations should use a pipeline / single round-trip; missing
        agents map to ``(0, 0)``.
        """

    @abstractmethod
    async def cleanup_agent(self, agent_id: str) -> None:
        """Remove every follow edge that references ``agent_id``.

        Called when an agent is unregistered/deleted. Removes:
          - This agent's own ``follows`` index (everyone they were following
            forgets them in the reverse ``followers`` index).
          - This agent's own ``followers`` index (each follower's ``follows``
            index forgets this agent).
        """
