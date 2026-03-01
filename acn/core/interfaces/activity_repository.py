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
