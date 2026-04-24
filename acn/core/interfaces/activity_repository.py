"""Activity Repository Interface

Defines contract for activity event persistence operations.
"""

from abc import ABC, abstractmethod
from typing import Any


class IActivityRepository(ABC):
    """
    Abstract interface for Activity persistence.

    Infrastructure layer provides concrete implementation (Redis or PostgreSQL).
    """

    @abstractmethod
    async def save(
        self,
        event_id: str,
        event_type: str,
        actor_type: str,
        actor_id: str,
        actor_name: str,
        description: str,
        timestamp: str,
        points: int | None = None,
        task_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Persist an activity event"""
        pass

    @abstractmethod
    async def find_recent(
        self,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find most recent activities (global feed)"""
        pass

    @abstractmethod
    async def find_by_user(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Find activities for a specific user/actor"""
        pass

    @abstractmethod
    async def find_by_task(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Find activities for a specific task"""
        pass

    @abstractmethod
    async def find_by_agent(self, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Find activities for a specific agent"""
        pass

    @abstractmethod
    async def find_by_agents(
        self, agent_ids: list[str], limit: int = 20
    ) -> list[dict[str, Any]]:
        """Find activities for multiple agents (merged, deduplicated)"""
        pass

    @abstractmethod
    async def count_by_agent_and_type(
        self,
        agent_id: str,
        since: str,
    ) -> dict[str, int]:
        """
        Return a mapping of {event_type: count} for events where
        ``actor_id == agent_id`` and ``timestamp >= since`` (ISO-8601 string).

        Only counts rows where the agent is the *actor* (outbound events).
        """
        pass

    @abstractmethod
    async def get_last_activity_at(self, agent_id: str) -> str | None:
        """
        Return the ISO-8601 timestamp of the most recent event where
        ``actor_id == agent_id``, or None if no events exist.
        """
        pass

    @abstractmethod
    async def count_received_by_agent(
        self,
        agent_id: str,
        since: str,
    ) -> int:
        """
        Count inbound activity events directed at ``agent_id`` since
        ``since`` (ISO-8601 string).

        "Inbound" is defined as events where the agent is the *subject*
        rather than the *actor*:
        - ``task_approved``: agent's submission was approved; actor is
          the reviewer.  ``event_metadata["agent_id"]`` identifies which
          agent was approved.
        - ``task_rejected``: agent's submission was rejected; actor is
          the reviewer.  ``event_metadata["agent_id"]`` identifies which
          agent was rejected.

        Note: ``task_cancelled`` inbound (creator cancels a task the
        agent had joined) is not counted here because those events carry
        no ``agent_id`` in metadata and would require a JOIN on
        ``participations`` — tracked in docs/BACKLOG.md.
        """
        pass
