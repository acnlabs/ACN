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
from typing import Any

from redis.asyncio import Redis


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

    def __init__(self, redis: Redis):
        """
        Initialize analytics service.

        Args:
            redis: Redis client for data access
        """
        self.redis = redis
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
            - by_status: Count by status (active, inactive, etc.)
            - by_subnet: Count by subnet
            - by_skill: Count by skill
            - recent_registrations: Recently registered agents
        """
        # Get all agent keys
        agent_keys = await self.redis.keys("acn:agents:*:info")

        stats = {
            "total": len(agent_keys),
            "by_status": {"active": 0, "inactive": 0, "unknown": 0},
            "by_subnet": {},
            "by_skill": {},
            "recent_registrations": [],
        }

        for key in agent_keys:
            try:
                agent_data = await self.redis.hgetall(key)
                if not agent_data:
                    continue

                # Decode Redis data
                agent = {
                    k.decode() if isinstance(k, bytes) else k: (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in agent_data.items()
                }

                # Count by status
                status = agent.get("status", "unknown")
                stats["by_status"][status] = stats["by_status"].get(status, 0) + 1

                # Count by subnet
                subnet = agent.get("subnet_id", "public")
                stats["by_subnet"][subnet] = stats["by_subnet"].get(subnet, 0) + 1

                # Count by skill (from skills JSON)
                skills_str = agent.get("skills", "[]")
                try:
                    skills = json.loads(skills_str)
                    for skill in skills:
                        skill_name = (
                            skill.get("name", skill) if isinstance(skill, dict) else str(skill)
                        )
                        stats["by_skill"][skill_name] = stats["by_skill"].get(skill_name, 0) + 1
                except (json.JSONDecodeError, TypeError):
                    pass

            except Exception:
                continue

        # Get recent registrations from audit log
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
        Get activity statistics for a specific agent.

        Args:
            agent_id: Agent ID to analyze
            hours: Number of hours to look back

        Returns:
            Activity statistics including messages sent/received, errors, etc.
        """
        # Get metrics for this agent
        prefix = f"acn:metrics:acn_messages_total:from_agent={agent_id}"
        sent_keys = await self.redis.keys(f"{prefix}*")

        messages_sent = 0
        for key in sent_keys:
            value = await self.redis.get(key)
            messages_sent += int(value) if value else 0

        prefix = f"acn:metrics:acn_messages_total:*to_agent={agent_id}*"
        recv_keys = await self.redis.keys(prefix)

        messages_received = 0
        for key in recv_keys:
            value = await self.redis.get(key)
            messages_received += int(value) if value else 0

        # Get error count
        error_prefix = f"acn:metrics:acn_errors_total:*{agent_id}*"
        error_keys = await self.redis.keys(error_prefix)

        errors = 0
        for key in error_keys:
            value = await self.redis.get(key)
            errors += int(value) if value else 0

        # Get last heartbeat
        last_heartbeat = await self.redis.get(f"acn:heartbeat:{agent_id}")

        return {
            "agent_id": agent_id,
            "period_hours": hours,
            "messages_sent": messages_sent,
            "messages_received": messages_received,
            "errors": errors,
            "last_heartbeat": last_heartbeat.decode() if last_heartbeat else None,
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
        keys = await self.redis.keys(total_pattern)

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

        # Get broadcast stats
        broadcast_pattern = "acn:metrics:acn_broadcasts_total:*"
        broadcast_keys = await self.redis.keys(broadcast_pattern)
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
            - total: Total subnets
            - subnets: List of subnet details
        """
        # Get all subnet keys
        subnet_keys = await self.redis.keys("acn:subnets:*:info")

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

                # Get agent count for this subnet
                subnet_id = subnet.get("subnet_id", "unknown")
                agent_count = await self._count_agents_in_subnet(subnet_id)

                # Get gateway connection count
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

        # Add public subnet
        public_count = await self._count_agents_in_subnet("public")

        return {
            "total": len(subnets) + 1,  # +1 for public
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
        error_keys = await self.redis.keys("acn:metrics:acn_errors_total:*")
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
                "agents_active": agent_stats["by_status"].get("active", 0),
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
        """Count agents in a specific subnet"""
        # Get all agents and filter by subnet
        agent_keys = await self.redis.keys("acn:agents:*:info")
        count = 0

        for key in agent_keys:
            agent_data = await self.redis.hgetall(key)
            if agent_data:
                agent_subnet = agent_data.get(b"subnet_id", b"public")
                if isinstance(agent_subnet, bytes):
                    agent_subnet = agent_subnet.decode()
                if agent_subnet == subnet_id:
                    count += 1

        return count

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
