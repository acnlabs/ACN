"""
ACN Analytics

Provides analytics and reporting capabilities for ACN.
Aggregates data from registry, communication, and audit logs.

Analytics provided:
- Agent statistics (count, status, skills distribution)
- Message statistics (volume, success rate, latency)
- Subnet statistics (agents per subnet, activity)
- Time series data for dashboards

Architecture:
    ┌──────────────────────────────────────────────────────┐
    │                     Analytics                         │
    │                                                        │
    │  Data Sources:                                        │
    │  ├─ AgentRegistry     -> Agent stats                 │
    │  ├─ MetricsCollector  -> Performance metrics         │
    │  └─ AuditLogger       -> Event analytics             │
    │                                                        │
    │  Outputs:                                             │
    │  ├─ Real-time dashboards                             │
    │  ├─ Historical reports                               │
    │  └─ Alerts and anomaly detection                     │
    └──────────────────────────────────────────────────────┘
"""

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis

from ..core.interfaces import IAgentRepository, ISubnetRepository

if TYPE_CHECKING:
    from ..services.activity_service import ActivityService

# Activity event types that count as "outbound" (agent is the actor).
# task_created: included because a creator-agent posting a task is an outbound
# action; pure solver agents will rarely have this type, so the semantic drift
# is acceptable.  task_accepted: agent claims a task (outbound intent signal).
# task_submitted: agent delivers work (outbound result).  payment_sent: agent
# initiates payment.
_SENT_TYPES = {"task_submitted", "task_accepted", "payment_sent", "task_created"}
# Activity event types treated as errors/cancellations from the agent's side.
# task_cancelled: actor is the cancelling party (agent or human creator).
_ERROR_TYPES = {"task_cancelled"}


class Analytics:
    """
    Analytics service for ACN.

    Provides aggregated statistics and analytics across all ACN components.

    Example:
        analytics = Analytics(redis_client)

        # Get agent statistics
        agent_stats = await analytics.get_agent_stats()

        # Get message volume over time
        volume = await analytics.get_message_volume(hours=24)

        # Get system health overview
        health = await analytics.get_system_health()
    """

    def __init__(
        self,
        redis: Redis,
        activity_service: "ActivityService | None" = None,
        agent_repo: IAgentRepository | None = None,
        subnet_repo: ISubnetRepository | None = None,
    ):
        """
        Initialize analytics service.

        Args:
            redis: Redis client for data access.
            activity_service: Optional ActivityService used to aggregate
                per-agent activity counts from PostgreSQL. When provided,
                ``get_agent_activity`` populates ``messages_sent`` and
                ``errors`` from the PG ``activity_events`` table.
            agent_repo: Optional IAgentRepository.  When provided,
                ``get_agent_stats`` reads from the authoritative PG agents
                table instead of scanning Redis keys.
            subnet_repo: Optional ISubnetRepository.  When provided,
                ``get_subnet_stats`` reads from PG instead of scanning Redis.
        """
        self.redis = redis
        self._activity_service = activity_service
        self._agent_repo = agent_repo
        self._subnet_repo = subnet_repo
        self._prefix = "acn:analytics:"

    # =========================================================================
    # Agent Analytics
    # =========================================================================

    async def get_agent_stats(self) -> dict[str, Any]:
        """
        Get comprehensive agent statistics.

        Returns:
            Dictionary containing:
            - total: Total registered agents
            - by_status: Count by status (online / offline / busy / …)
            - by_subnet: Count by subnet (an agent may belong to several)
            - by_tag: Count by tag
            - recent_registrations: Recently registered agent IDs
        """
        if self._agent_repo:
            return await self._get_agent_stats_from_repo()
        return await self._get_agent_stats_from_redis()

    async def _get_agent_stats_from_repo(self) -> dict[str, Any]:
        """Build agent stats from the IAgentRepository (PG-backed when available).

        Note on ``by_subnet``: an agent may belong to multiple subnets
        (``agent.subnet_ids`` is a list).  Each subnet membership is counted
        independently, so ``sum(by_subnet.values()) >= total`` when any agent
        belongs to more than one subnet.  The Redis fallback counts only the
        agent's primary ``subnet_id`` field (single value), so the two paths
        are not strictly equivalent; this is the more accurate representation.
        """
        agents = await self._agent_repo.find_all()  # type: ignore[union-attr]

        # ``by_status`` is derived from the Redis alive set (single source
        # of truth for online-ness), not the legacy ``Agent.status`` DB
        # column. A single ``filter_alive`` call resolves the entire
        # listing in one Redis round-trip — see ``AgentService.batch_alive``
        # for the wrapper used by route-layer serialization.
        alive_ids: set[str] = (
            await self._agent_repo.filter_alive([a.agent_id for a in agents])  # type: ignore[union-attr]
            if agents
            else set()
        )

        by_status: dict[str, int] = {"online": 0, "offline": 0}
        by_subnet: dict[str, int] = {}
        by_tag: dict[str, int] = {}

        for agent in agents:
            status = "online" if agent.agent_id in alive_ids else "offline"
            by_status[status] += 1

            for sid in (agent.subnet_ids or ["public"]):
                by_subnet[sid] = by_subnet.get(sid, 0) + 1

            for tag in (agent.tags or []):
                by_tag[str(tag)] = by_tag.get(str(tag), 0) + 1

        recent = sorted(agents, key=lambda a: a.registered_at, reverse=True)[:10]
        return {
            "total": len(agents),
            "by_status": by_status,
            "by_subnet": by_subnet,
            "by_tag": by_tag,
            "recent_registrations": [a.agent_id for a in recent],
        }

    async def _get_agent_stats_from_redis(self) -> dict[str, Any]:
        """Fallback: build agent stats by scanning Redis agent hash keys."""
        # The real agent hash key schema is `acn:agents:{uuid}` (3 segments).
        # Index keys (`acn:agents:by_endpoint:…`, `{uuid}:alive`, etc.) have
        # ≥4 segments and are excluded by the length filter.
        agent_keys = [
            k
            async for k in self.redis.scan_iter("acn:agents:*")
            if len((k.decode() if isinstance(k, bytes) else k).split(":")) == 3
        ]

        stats: dict[str, Any] = {
            "total": len(agent_keys),
            "by_status": {"online": 0, "offline": 0, "unknown": 0},
            "by_subnet": {},
            "by_tag": {},
            "recent_registrations": [],
        }

        for key in agent_keys:
            try:
                agent_data = await self.redis.hgetall(key)
                if not agent_data:
                    continue

                agent = {
                    k.decode() if isinstance(k, bytes) else k: (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in agent_data.items()
                }

                status = agent.get("status", "unknown")
                stats["by_status"][status] = stats["by_status"].get(status, 0) + 1

                subnet = agent.get("subnet_id", "public")
                stats["by_subnet"][subnet] = stats["by_subnet"].get(subnet, 0) + 1

                tags_str = agent.get("tags") or agent.get("skills", "[]")
                try:
                    tags = json.loads(tags_str)
                    for tag in tags:
                        tag_name = (
                            tag.get("name", tag) if isinstance(tag, dict) else str(tag)
                        )
                        stats["by_tag"][tag_name] = stats["by_tag"].get(tag_name, 0) + 1
                except (json.JSONDecodeError, TypeError):
                    pass

            except Exception:
                continue

        recent_keys = await self.redis.lrange("acn:audit:type:agent_registered", 0, 9)
        stats["recent_registrations"] = [
            k.decode() if isinstance(k, bytes) else k for k in recent_keys
        ]
        return stats

    async def get_agent_activity(
        self,
        agent_id: str,
        hours: int = 24,
    ) -> dict[str, Any]:
        """
        Get activity for a specific agent.

        Data sources:
        - ``messages_sent`` and ``errors``: aggregated from PG ``activity_events``
          via ``ActivityService.get_activity_counts`` when an ActivityService is
          injected (see ``__init__``). Returns ``None`` when no ActivityService is
          configured.  The mapping from event types to field names is defined by
          ``_SENT_TYPES`` / ``_ERROR_TYPES`` at module level.
        - ``messages_received``: aggregated from PG ``activity_events`` via
          ``ActivityService.get_received_count``.  Counts ``task_approved`` and
          ``task_rejected`` events where ``event_metadata["agent_id"] == agent_id``
          (the agent is the *subject*, not the *actor*).  Returns ``None`` when no
          ActivityService is configured.  Note: ``task_cancelled`` inbound
          (creator cancels a task the agent had joined) is excluded — those
          events carry no ``agent_id`` in metadata.
        - ``last_heartbeat``: always read from ``acn:heartbeat:{agent_id}`` in
          Redis (written by the liveness path, unaffected by metric schema).

        Historical note: before SCALE_AUDIT P1-2, this method scanned Redis
        metric keys per-agent, but P1-2 collapsed ``acn_messages_total`` labels
        to ``status``-only to prevent cardinality blow-up, making those scans
        useless. P1-9 set the fields explicitly to ``None`` with a
        ``data_source_note``. This version wires in PG activity aggregation.

        Args:
            agent_id: Agent ID to look up.
            hours: Reporting window in hours for the activity aggregation.

        Returns:
            {
                "agent_id": str,
                "period_hours": int,
                "messages_sent": int | None,      # int when ActivityService injected
                "messages_received": int | None,  # int when ActivityService injected
                "errors": int | None,             # int when ActivityService injected
                "last_heartbeat": str | None,
                "data_source_note": str,
            }
        """
        last_heartbeat = await self.redis.get(f"acn:heartbeat:{agent_id}")
        if isinstance(last_heartbeat, bytes):
            last_heartbeat = last_heartbeat.decode()

        messages_sent: int | None = None
        errors: int | None = None
        data_source_note: str

        messages_received: int | None = None

        if self._activity_service:
            since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
            counts = await self._activity_service.get_activity_counts(agent_id, since)

            # messages_sent: events where the agent is the actor and the event
            # represents an outbound action (submission, acceptance, payment).
            messages_sent = sum(counts.get(t, 0) for t in _SENT_TYPES)

            # errors: task_cancelled events where this agent is the actor.
            errors = sum(counts.get(t, 0) for t in _ERROR_TYPES)

            # messages_received: task_approved + task_rejected events where
            # event_metadata["agent_id"] == agent_id (agent is the subject,
            # not the actor).  Counts feedback received from reviewers.
            messages_received = await self._activity_service.get_received_count(
                agent_id, since
            )

            data_source_note = (
                "messages_sent and errors are derived from PG activity_events "
                f"(types: sent={sorted(_SENT_TYPES)}, errors={sorted(_ERROR_TYPES)}). "
                "messages_received counts task_approved + task_rejected events "
                "targeting this agent (event_metadata.agent_id)."
            )
        else:
            data_source_note = (
                "per-agent message and error counts require PG "
                "activity_events aggregation (ActivityService not injected); "
                "see docs/BACKLOG.md"
            )

        return {
            "agent_id": agent_id,
            "period_hours": hours,
            "messages_sent": messages_sent,
            "messages_received": messages_received,
            "errors": errors,
            "last_heartbeat": last_heartbeat,
            "data_source_note": data_source_note,
        }

    # =========================================================================
    # Message Analytics
    # =========================================================================

    async def get_message_stats(self) -> dict[str, Any]:
        """
        Get message statistics.

        Returns:
            Dictionary containing:
            - total: Total messages
            - success: Successful messages
            - failed: Failed messages
            - success_rate: Success percentage
            - by_type: Count by message type
        """
        # Get message counters
        total_pattern = "acn:metrics:acn_messages_total:*"
        keys = [k async for k in self.redis.scan_iter(total_pattern)]

        total = 0
        success = 0
        failed = 0

        for key in keys:
            key_str = key.decode() if isinstance(key, bytes) else key
            value = await self.redis.get(key)
            count = int(value) if value else 0
            total += count

            if "status=success" in key_str:
                success += count
            elif "status=failed" in key_str or "status=error" in key_str:
                failed += count

        success_rate = (success / total * 100) if total > 0 else 0

        # Get broadcast stats — keyed by acn_broadcast_sent (type + status labels).
        # Sum all label combinations to get total broadcast attempts.
        broadcast_pattern = "acn:metrics:acn_broadcast_sent:*"
        broadcast_keys = [k async for k in self.redis.scan_iter(broadcast_pattern)]
        broadcasts = 0
        for key in broadcast_keys:
            value = await self.redis.get(key)
            broadcasts += int(value) if value else 0

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "success_rate": round(success_rate, 2),
            "broadcasts": broadcasts,
            "in_dlq": await self._get_dlq_count(),
        }

    async def get_message_volume(
        self,
        hours: int = 24,
        bucket_minutes: int = 60,
    ) -> list[dict[str, Any]]:
        """
        Get message volume over time.

        Args:
            hours: Number of hours to look back
            bucket_minutes: Size of each time bucket in minutes

        Returns:
            List of time buckets with message counts
        """
        # This would require time-series storage
        # For now, return a placeholder structure
        buckets = []
        now = datetime.now(UTC)

        for i in range(hours * 60 // bucket_minutes):
            bucket_time = now - timedelta(minutes=i * bucket_minutes)
            buckets.append(
                {
                    "timestamp": bucket_time.isoformat(),
                    "count": 0,  # Would need time-series data
                }
            )

        return list(reversed(buckets))

    async def get_latency_stats(self) -> dict[str, Any]:
        """
        Get latency statistics.

        Returns:
            Latency percentiles and averages by operation type
        """
        operations = ["route_message", "register", "broadcast", "gateway_forward"]
        stats = {}

        for op in operations:
            key = f"acn:metrics:acn_latency_seconds:operation={op}"

            # Get values
            values_key = f"{key}:values"
            raw_values = await self.redis.lrange(values_key, 0, -1)

            if raw_values:
                values = sorted([float(v) for v in raw_values])
                stats[op] = {
                    "count": len(values),
                    "avg_ms": round(sum(values) / len(values) * 1000, 2),
                    "min_ms": round(min(values) * 1000, 2),
                    "max_ms": round(max(values) * 1000, 2),
                    "p50_ms": round(self._percentile(values, 50) * 1000, 2),
                    "p90_ms": round(self._percentile(values, 90) * 1000, 2),
                    "p99_ms": round(self._percentile(values, 99) * 1000, 2),
                }
            else:
                stats[op] = {
                    "count": 0,
                    "avg_ms": 0,
                    "min_ms": 0,
                    "max_ms": 0,
                    "p50_ms": 0,
                    "p90_ms": 0,
                    "p99_ms": 0,
                }

        return stats

    # =========================================================================
    # Subnet Analytics
    # =========================================================================

    async def get_subnet_stats(self) -> dict[str, Any]:
        """
        Get subnet statistics.

        Returns:
            Dictionary containing:
            - total: Total subnets (including the implicit public network)
            - subnets: List of subnet detail dicts
        """
        if self._subnet_repo:
            return await self._get_subnet_stats_from_repo()
        return await self._get_subnet_stats_from_redis()

    async def _get_subnet_stats_from_repo(self) -> dict[str, Any]:
        """Build subnet stats from ISubnetRepository (PG-backed when available)."""
        subnets = await self._subnet_repo.find_all()  # type: ignore[union-attr]

        subnet_list = []
        for subnet in subnets:
            agent_count = (
                await self._agent_repo.count_by_subnet(subnet.subnet_id)
                if self._agent_repo
                else await self._count_agents_in_subnet(subnet.subnet_id)
            )
            gateway_count = await self._count_gateway_connections(subnet.subnet_id)
            subnet_list.append(
                {
                    "subnet_id": subnet.subnet_id,
                    "name": subnet.name,
                    "agent_count": agent_count,
                    "gateway_connections": gateway_count,
                    "has_security": bool(subnet.security_config),
                }
            )

        public_count = (
            await self._agent_repo.count_by_subnet("public")
            if self._agent_repo
            else await self._count_agents_in_subnet("public")
        )

        return {
            "total": len(subnets) + 1,  # +1 for implicit public network
            "subnets": [
                {
                    "subnet_id": "public",
                    "name": "Public Network",
                    "agent_count": public_count,
                    "gateway_connections": 0,
                    "has_security": False,
                },
                *subnet_list,
            ],
        }

    async def _get_subnet_stats_from_redis(self) -> dict[str, Any]:
        """Fallback: build subnet stats by scanning Redis subnet hash keys."""
        # The real subnet hash key is `acn:subnets:info:{id}`.
        subnet_keys = [k async for k in self.redis.scan_iter("acn:subnets:info:*")]

        subnets = []
        for key in subnet_keys:
            try:
                subnet_data = await self.redis.hgetall(key)
                if not subnet_data:
                    continue

                subnet = {
                    k.decode() if isinstance(k, bytes) else k: (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in subnet_data.items()
                }

                subnet_id = subnet.get("subnet_id", "unknown")
                agent_count = await self._count_agents_in_subnet(subnet_id)
                gateway_count = await self._count_gateway_connections(subnet_id)

                subnets.append(
                    {
                        "subnet_id": subnet_id,
                        "name": subnet.get("name", subnet_id),
                        "agent_count": agent_count,
                        "gateway_connections": gateway_count,
                        "has_security": subnet.get("security_schemes") is not None,
                    }
                )

            except Exception:
                continue

        public_count = await self._count_agents_in_subnet("public")

        return {
            "total": len(subnets) + 1,
            "subnets": [
                {
                    "subnet_id": "public",
                    "name": "Public Network",
                    "agent_count": public_count,
                    "gateway_connections": 0,
                    "has_security": False,
                },
                *subnets,
            ],
        }

    # =========================================================================
    # System Health
    # =========================================================================

    async def get_system_health(self) -> dict[str, Any]:
        """
        Get overall system health status.

        Returns:
            Dictionary with health indicators and status
        """
        # Get basic counts
        agent_stats = await self.get_agent_stats()
        message_stats = await self.get_message_stats()

        # Check error rate
        error_keys = [k async for k in self.redis.scan_iter("acn:metrics:acn_errors_total:*")]
        total_errors = 0
        for key in error_keys:
            value = await self.redis.get(key)
            total_errors += int(value) if value else 0

        # Calculate health score (0-100)
        health_score = 100
        issues = []

        # Deduct for high error rate
        if message_stats["total"] > 0:
            error_rate = total_errors / message_stats["total"]
            if error_rate > 0.1:
                health_score -= 30
                issues.append("High error rate (>10%)")
            elif error_rate > 0.05:
                health_score -= 15
                issues.append("Elevated error rate (>5%)")

        # Deduct for low success rate
        if message_stats["success_rate"] < 90:
            health_score -= 20
            issues.append(f"Low success rate ({message_stats['success_rate']}%)")
        elif message_stats["success_rate"] < 95:
            health_score -= 10
            issues.append(f"Success rate below target ({message_stats['success_rate']}%)")

        # Deduct for DLQ messages
        dlq_count = message_stats.get("in_dlq", 0)
        if dlq_count > 100:
            health_score -= 15
            issues.append(f"High DLQ count ({dlq_count})")
        elif dlq_count > 10:
            health_score -= 5
            issues.append(f"Messages in DLQ ({dlq_count})")

        # Determine status
        if health_score >= 90:
            status = "healthy"
        elif health_score >= 70:
            status = "degraded"
        else:
            status = "unhealthy"

        return {
            "status": status,
            "health_score": max(0, health_score),
            "issues": issues,
            "summary": {
                "agents_total": agent_stats["total"],
                "agents_active": agent_stats["by_status"].get("online", agent_stats["by_status"].get("active", 0)),
                "messages_total": message_stats["total"],
                "success_rate": message_stats["success_rate"],
                "errors_total": total_errors,
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }

    async def get_dashboard_data(self) -> dict[str, Any]:
        """
        Get all data needed for a monitoring dashboard.

        Returns:
            Comprehensive dashboard data
        """
        return {
            "health": await self.get_system_health(),
            "agents": await self.get_agent_stats(),
            "messages": await self.get_message_stats(),
            "latency": await self.get_latency_stats(),
            "subnets": await self.get_subnet_stats(),
            "timestamp": datetime.now(UTC).isoformat(),
        }

    # =========================================================================
    # Reporting
    # =========================================================================

    async def generate_report(
        self,
        report_type: str = "daily",
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Generate a report for the specified period.

        Args:
            report_type: Type of report (daily, weekly, monthly)
            start_date: Start of report period
            end_date: End of report period

        Returns:
            Report data
        """
        now = datetime.now(UTC)

        if not end_date:
            end_date = now

        if not start_date:
            if report_type == "daily":
                start_date = now - timedelta(days=1)
            elif report_type == "weekly":
                start_date = now - timedelta(weeks=1)
            elif report_type == "monthly":
                start_date = now - timedelta(days=30)
            else:
                start_date = now - timedelta(days=1)

        return {
            "report_type": report_type,
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
            "generated_at": now.isoformat(),
            "summary": await self.get_system_health(),
            "agents": await self.get_agent_stats(),
            "messages": await self.get_message_stats(),
            "latency": await self.get_latency_stats(),
            "subnets": await self.get_subnet_stats(),
        }

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    async def _get_dlq_count(self) -> int:
        """Get count of messages in dead letter queue"""
        count = await self.redis.llen("acn:dlq")
        return count

    async def _count_agents_in_subnet(self, subnet_id: str) -> int:
        """Count agents in a specific subnet.

        Reads directly from the authoritative membership set
        `acn:subnets:{subnet_id}:agents` that AgentRepository and the registry
        already maintain (sadd on save, srem on delete). The previous
        implementation scanned every agent hash and filtered in Python,
        which (a) used the wrong key pattern `acn:agents:*:info` (never
        written, so always returned 0) and (b) even if the pattern were
        fixed would be O(N_agents) per call — unacceptable when this
        function is invoked for every subnet in `get_subnet_stats`.
        """
        return await self.redis.scard(f"acn:subnets:{subnet_id}:agents")

    async def _count_gateway_connections(self, subnet_id: str) -> int:
        """Count gateway connections for a subnet"""
        key = f"acn:metrics:acn_gateway_connections:subnet={subnet_id}"
        value = await self.redis.get(key)
        return int(value) if value else 0

    @staticmethod
    def _percentile(sorted_values: list[float], percentile: int) -> float:
        """Calculate percentile from sorted values"""
        if not sorted_values:
            return 0.0
        k = (len(sorted_values) - 1) * percentile / 100
        f = int(k)
        c = f + 1 if f + 1 < len(sorted_values) else f
        return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])
