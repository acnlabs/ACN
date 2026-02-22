"""Activity Service

Records and retrieves task lifecycle activities for the Labs activity feed.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger()

# Redis keys
ACTIVITY_PREFIX = "labs_activity:"
ACTIVITY_LIST = "labs_activities"
ACTIVITY_BY_USER = "labs_activities:user:"
ACTIVITY_BY_TASK = "labs_activities:task:"
ACTIVITY_BY_AGENT = "labs_activities:agent:"

# Activity types
ACTIVITY_TYPES = [
    "task_created",      # Human/agent created a task
    "task_accepted",     # Agent accepted a task
    "task_submitted",    # Agent submitted result
    "task_approved",     # Creator approved submission (+ reward)
    "task_rejected",     # Creator rejected submission
    "task_cancelled",    # Creator cancelled task
    "agent_joined",      # New agent registered
    "payment_sent",      # Payment/reward sent
]


class ActivityService:
    """
    Service for recording and retrieving activities.
    
    Activities represent task lifecycle events visible in the Labs feed.
    """
    
    def __init__(self, redis: Redis, max_activities: int = 100):
        """
        Initialize Activity Service
        
        Args:
            redis: Redis client
            max_activities: Maximum activities to keep in list
        """
        self.redis = redis
        self.max_activities = max_activities
    
    async def record(
        self,
        event_type: str,
        actor_type: str,  # "human" or "agent"
        actor_id: str,
        actor_name: str,
        description: str,
        points: int | None = None,
        task_id: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """
        Record an activity event
        
        Args:
            event_type: Type of event (task_created, task_accepted, etc.)
            actor_type: "human" or "agent"
            actor_id: Actor identifier
            actor_name: Actor display name
            description: Human-readable description
            points: Points involved (if any)
            task_id: Related task ID (if any)
            metadata: Additional data
            
        Returns:
            Event ID
        """
        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        event_key = f"{ACTIVITY_PREFIX}{event_id}"
        timestamp = datetime.now(UTC).isoformat()
        
        event_data = {
            "event_id": event_id,
            "type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "description": description,
            "timestamp": timestamp,
        }
        
        if points is not None:
            event_data["points"] = str(points)
        
        if task_id:
            event_data["task_id"] = task_id
            
        if metadata:
            event_data["metadata"] = str(metadata)
        
        # Store event
        await self.redis.hset(event_key, mapping=event_data)
        await self.redis.expire(event_key, 86400 * 30)  # 30 days TTL
        
        # Add to global list
        await self.redis.lpush(ACTIVITY_LIST, event_id)
        await self.redis.ltrim(ACTIVITY_LIST, 0, self.max_activities - 1)
        
        # Add to user index
        user_key = f"{ACTIVITY_BY_USER}{actor_id}"
        await self.redis.lpush(user_key, event_id)
        await self.redis.ltrim(user_key, 0, self.max_activities - 1)
        
        # Add to agent index if actor is an agent
        if actor_type == "agent":
            agent_key = f"{ACTIVITY_BY_AGENT}{actor_id}"
            await self.redis.lpush(agent_key, event_id)
            await self.redis.ltrim(agent_key, 0, self.max_activities - 1)
        
        # Add to task index if task_id provided
        if task_id:
            task_key = f"{ACTIVITY_BY_TASK}{task_id}"
            await self.redis.lpush(task_key, event_id)
            await self.redis.ltrim(task_key, 0, 50)
        
        logger.info(
            "activity_recorded",
            event_id=event_id,
            event_type=event_type,
            actor_name=actor_name,
        )
        
        return event_id
    
    async def list_activities(
        self,
        limit: int = 20,
        user_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        agent_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get recent activities
        
        Args:
            limit: Maximum activities to return
            user_id: Filter by user/actor (optional)
            task_id: Filter by task (optional)
            agent_id: Filter by single agent (optional)
            agent_ids: Filter by multiple agents (optional)
            
        Returns:
            List of activity events
        """
        event_ids: list = []
        
        # Handle multiple agent IDs - fetch and merge activities
        if agent_ids:
            all_event_ids = set()
            for aid in agent_ids:
                list_key = f"{ACTIVITY_BY_AGENT}{aid}"
                ids = await self.redis.lrange(list_key, 0, limit - 1)
                for eid in ids:
                    all_event_ids.add(eid.decode() if isinstance(eid, bytes) else eid)
            event_ids = list(all_event_ids)[:limit]
        else:
            # Select the right list based on single filter
            if user_id:
                list_key = f"{ACTIVITY_BY_USER}{user_id}"
            elif task_id:
                list_key = f"{ACTIVITY_BY_TASK}{task_id}"
            elif agent_id:
                list_key = f"{ACTIVITY_BY_AGENT}{agent_id}"
            else:
                list_key = ACTIVITY_LIST
            
            # Get event IDs
            event_ids = await self.redis.lrange(list_key, 0, limit - 1)
        
        activities = []
        for event_id in event_ids:
            event_id = event_id.decode() if isinstance(event_id, bytes) else event_id
            event_data = await self.redis.hgetall(f"{ACTIVITY_PREFIX}{event_id}")
            
            if event_data:
                event_dict = {
                    k.decode() if isinstance(k, bytes) else k: 
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in event_data.items()
                }
                
                # Convert points to int if present
                if "points" in event_dict:
                    try:
                        event_dict["points"] = int(event_dict["points"])
                    except ValueError:
                        pass
                
                activities.append(event_dict)
        
        return activities
    
    # ========== Convenience Methods ==========
    
    async def record_task_created(
        self,
        creator_type: str,
        creator_id: str,
        creator_name: str,
        task_id: str,
        task_title: str,
        reward_amount: str = "0",
        reward_currency: str = "points",
    ) -> str:
        """Record task creation"""
        reward_str = f"{reward_amount} {reward_currency}" if float(reward_amount) > 0 else "No reward"
        return await self.record(
            event_type="task_created",
            actor_type=creator_type,
            actor_id=creator_id,
            actor_name=creator_name,
            description=f"Created task: {task_title} ({reward_str})",
            task_id=task_id,
        )
    
    async def record_task_accepted(
        self,
        agent_id: str,
        agent_name: str,
        task_id: str,
        task_title: str,
    ) -> str:
        """Record task acceptance"""
        return await self.record(
            event_type="task_accepted",
            actor_type="agent",
            actor_id=agent_id,
            actor_name=agent_name,
            description=f"Accepted task: {task_title}",
            task_id=task_id,
        )
    
    async def record_task_submitted(
        self,
        agent_id: str,
        agent_name: str,
        task_id: str,
        task_title: str,
    ) -> str:
        """Record task submission"""
        return await self.record(
            event_type="task_submitted",
            actor_type="agent",
            actor_id=agent_id,
            actor_name=agent_name,
            description=f"Submitted: {task_title}",
            task_id=task_id,
        )
    
    async def record_task_approved(
        self,
        approver_type: str,
        approver_id: str,
        approver_name: str,
        agent_id: str,
        agent_name: str,
        task_id: str,
        task_title: str,
        reward_amount: str = "0",
        reward_currency: str = "points",
    ) -> str:
        """Record task approval (with reward)"""
        reward_int = int(float(reward_amount)) if reward_amount else 0
        return await self.record(
            event_type="task_approved",
            actor_type=approver_type,
            actor_id=approver_id,
            actor_name=approver_name,
            description=f"Approved {agent_name}'s submission: {task_title}",
            points=reward_int if reward_currency == "points" else None,
            task_id=task_id,
            metadata={"agent_id": agent_id, "reward": f"{reward_amount} {reward_currency}"},
        )
    
    async def record_task_rejected(
        self,
        reviewer_type: str,
        reviewer_id: str,
        reviewer_name: str,
        agent_id: str,
        task_id: str,
        task_title: str,
        reason: str = "",
    ) -> str:
        """Record task rejection"""
        desc = f"Rejected submission: {task_title}"
        if reason:
            desc += f" - {reason}"
        return await self.record(
            event_type="task_rejected",
            actor_type=reviewer_type,
            actor_id=reviewer_id,
            actor_name=reviewer_name,
            description=desc,
            task_id=task_id,
            metadata={"agent_id": agent_id},
        )
    
    async def record_task_cancelled(
        self,
        canceller_type: str,
        canceller_id: str,
        canceller_name: str,
        task_id: str,
        task_title: str,
    ) -> str:
        """Record task cancellation"""
        return await self.record(
            event_type="task_cancelled",
            actor_type=canceller_type,
            actor_id=canceller_id,
            actor_name=canceller_name,
            description=f"Cancelled task: {task_title}",
            task_id=task_id,
        )
