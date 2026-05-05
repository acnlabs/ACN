"""
ACN Audit Logger

Records all significant events for compliance and debugging.
Supports structured logging with timestamps, actors, and details.

Event types:
- agent_registered / agent_unregistered
- agent_heartbeat_missed
- message_sent / message_received / message_failed
- subnet_created / subnet_deleted
- gateway_connected / gateway_disconnected
- security_auth_success / security_auth_failure

Architecture:
    ┌──────────────────────────────────────────────────────┐
    │                    AuditLogger                        │
    │                                                        │
    │  ┌─────────────┐     ┌─────────────┐                 │
    │  │  log_event  │ --> │   Redis     │ (primary)       │
    │  └─────────────┘     │   Stream    │                 │
    │         │            └─────────────┘                 │
    │         v                                             │
    │  ┌─────────────┐                                     │
    │  │  Optional   │ --> File / Database / External      │
    │  │  Sinks      │                                     │
    │  └─────────────┘                                     │
    └──────────────────────────────────────────────────────┘
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from redis.asyncio import Redis

_logger = logging.getLogger(__name__)


class AuditEventType(StrEnum):
    """Types of audit events"""

    # Agent lifecycle
    AGENT_REGISTERED = "agent_registered"
    AGENT_UNREGISTERED = "agent_unregistered"
    AGENT_HEARTBEAT = "agent_heartbeat"
    AGENT_HEARTBEAT_MISSED = "agent_heartbeat_missed"
    AGENT_STATUS_CHANGED = "agent_status_changed"

    # Messaging
    MESSAGE_SENT = "message_sent"
    MESSAGE_RECEIVED = "message_received"
    MESSAGE_FAILED = "message_failed"
    MESSAGE_RETRY = "message_retry"
    # Rejected by recipient's communication_policy at the gateway.
    # Only emitted from single-send paths (`POST /communication/send` and
    # `POST /communication/internal/send`) — see "Phase 1 metrics + audit 落点"
    # in docs/features/acn-communication-economic-model.md for why broadcast
    # per-target rejections, subnet WS rejections and DLQ drops deliberately
    # don't write audit (volume would dominate the stream and the metric
    # already covers them).
    MESSAGE_REJECTED = "message_rejected"
    BROADCAST_SENT = "broadcast_sent"

    # Communication policy
    # Emitted whenever an agent's `communication_policy.mode` flips between
    # `open` / `closed` / `manifest` / `allowlist`. Captures both the previous
    # and new mode in `details` so audit consumers can correlate sudden traffic
    # drops with policy transitions. See Phase 2 Group B decision #3-bis in
    # docs/features/acn-communication-economic-model.md.
    POLICY_CHANGED = "policy_changed"

    # Subnet
    SUBNET_CREATED = "subnet_created"
    SUBNET_DELETED = "subnet_deleted"
    SUBNET_AGENT_JOINED = "subnet_agent_joined"
    SUBNET_AGENT_LEFT = "subnet_agent_left"

    # Gateway
    GATEWAY_CONNECTED = "gateway_connected"
    GATEWAY_DISCONNECTED = "gateway_disconnected"
    GATEWAY_MESSAGE_FORWARDED = "gateway_message_forwarded"

    # Security
    SECURITY_AUTH_SUCCESS = "security_auth_success"
    SECURITY_AUTH_FAILURE = "security_auth_failure"
    SECURITY_TOKEN_GENERATED = "security_token_generated"
    SECURITY_SSRF_BLOCKED = "security_ssrf_blocked"

    # Admin
    ADMIN_BULK_DELETE = "admin_bulk_delete"

    # System
    SYSTEM_STARTED = "system_started"
    SYSTEM_STOPPED = "system_stopped"
    CONFIG_CHANGED = "config_changed"
    ERROR_OCCURRED = "error_occurred"


class AuditLevel(StrEnum):
    """Severity levels for audit events"""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditEvent(BaseModel):
    """Model for audit log entries"""

    id: str = Field(..., description="Unique event ID")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: AuditEventType
    level: AuditLevel = AuditLevel.INFO

    # Actor information
    actor_id: str | None = Field(None, description="ID of the actor (agent/user)")
    actor_type: str | None = Field(None, description="Type: agent, user, system")

    # Target information
    target_id: str | None = Field(None, description="ID of the target entity")
    target_type: str | None = Field(None, description="Type: agent, subnet, message")

    # Context
    subnet_id: str | None = Field(None, description="Related subnet ID")
    message_id: str | None = Field(None, description="Related message ID")

    # Details
    details: dict[str, Any] = Field(default_factory=dict)

    # Source information
    source_ip: str | None = None
    user_agent: str | None = None


class AuditLogger:
    """
    Audit logging service for ACN.

    Records all significant events with full context for:
    - Compliance and security auditing
    - Debugging and troubleshooting
    - Usage analytics

    Example:
        audit = AuditLogger(redis_client)
        await audit.start()

        # Log agent registration
        await audit.log_event(
            event_type=AuditEventType.AGENT_REGISTERED,
            actor_id="admin",
            target_id="cursor-agent",
            details={"tags": ["code", "test"]}
        )

        # Query recent events
        events = await audit.query_events(
            event_type=AuditEventType.AGENT_REGISTERED,
            limit=100
        )
    """

    def __init__(
        self,
        redis: Redis,
        stream_name: str = "acn:audit:stream",
        max_entries: int = 100000,
        retention_days: int = 90,
    ):
        """
        Initialize audit logger.

        Args:
            redis: Redis client
            stream_name: Redis stream name for audit logs
            max_entries: Maximum entries to keep in stream
            retention_days: How long to keep audit logs (for cleanup)
        """
        self.redis = redis
        self.stream_name = stream_name
        self.max_entries = max_entries
        self.retention_days = retention_days
        self._started = False
        self._event_counter = 0

    async def start(self):
        """Start the audit logger"""
        self._started = True
        await self.log_event(
            event_type=AuditEventType.SYSTEM_STARTED,
            actor_id="acn",
            actor_type="system",
            details={"service": "audit_logger"},
        )

    async def stop(self):
        """Stop the audit logger"""
        await self.log_event(
            event_type=AuditEventType.SYSTEM_STOPPED,
            actor_id="acn",
            actor_type="system",
            details={"service": "audit_logger"},
        )
        self._started = False

    # =========================================================================
    # Logging Methods
    # =========================================================================

    async def log_event(
        self,
        event_type: AuditEventType,
        actor_id: str | None = None,
        actor_type: str | None = None,
        target_id: str | None = None,
        target_type: str | None = None,
        subnet_id: str | None = None,
        message_id: str | None = None,
        details: dict[str, Any] | None = None,
        level: AuditLevel = AuditLevel.INFO,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        """
        Log an audit event.

        Args:
            event_type: Type of event
            actor_id: ID of the actor performing the action
            actor_type: Type of actor (agent, user, system)
            target_id: ID of the target entity
            target_type: Type of target entity
            subnet_id: Related subnet ID
            message_id: Related message ID
            details: Additional event details
            level: Severity level
            source_ip: Source IP address
            user_agent: User agent string

        Returns:
            Event ID
        """
        self._event_counter += 1
        event_id = f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{self._event_counter:06d}"

        event = AuditEvent(
            id=event_id,
            event_type=event_type,
            level=level,
            actor_id=actor_id,
            actor_type=actor_type,
            target_id=target_id,
            target_type=target_type,
            subnet_id=subnet_id,
            message_id=message_id,
            details=details or {},
            source_ip=source_ip,
            user_agent=user_agent,
        )

        # Add to Redis stream
        await self.redis.xadd(
            self.stream_name,
            {"data": event.model_dump_json()},
            maxlen=self.max_entries,
        )

        # Also store in a daily index for faster queries.
        # Cap length like `type_key` so a single high-traffic day can't
        # grow the list to multi-GB and stall Redis on the hot path.
        day_key = f"acn:audit:day:{event.timestamp.strftime('%Y%m%d')}"
        await self.redis.lpush(day_key, event_id)
        await self.redis.ltrim(day_key, 0, self.max_entries - 1)
        await self.redis.expire(day_key, self.retention_days * 24 * 3600)

        # Store by event type for type-based queries.
        # Mirror day_key's TTL: without an explicit expire, cold event types
        # (early experimental events, deprecated flows) accumulate forever
        # under the 100k entry cap and dilute analyses with stale rows.
        type_key = f"acn:audit:type:{event_type.value}"
        await self.redis.lpush(type_key, event_id)
        await self.redis.ltrim(type_key, 0, self.max_entries - 1)
        await self.redis.expire(type_key, self.retention_days * 24 * 3600)

        return event_id

    # =========================================================================
    # Convenience Logging Methods
    # =========================================================================

    async def log_agent_registered(
        self,
        agent_id: str,
        subnet_id: str = "public",
        tags: list[str] | None = None,
        source_ip: str | None = None,
    ) -> str:
        """Log agent registration"""
        return await self.log_event(
            event_type=AuditEventType.AGENT_REGISTERED,
            target_id=agent_id,
            target_type="agent",
            subnet_id=subnet_id,
            details={"tags": tags or []},
            source_ip=source_ip,
        )

    async def log_agent_unregistered(
        self,
        agent_id: str,
        reason: str = "normal",
    ) -> str:
        """Log agent unregistration"""
        return await self.log_event(
            event_type=AuditEventType.AGENT_UNREGISTERED,
            target_id=agent_id,
            target_type="agent",
            details={"reason": reason},
        )

    async def log_message_sent(
        self,
        from_agent: str,
        to_agent: str,
        message_id: str,
        message_type: str = "a2a",
    ) -> str:
        """Log message sent"""
        return await self.log_event(
            event_type=AuditEventType.MESSAGE_SENT,
            actor_id=from_agent,
            actor_type="agent",
            target_id=to_agent,
            target_type="agent",
            message_id=message_id,
            details={"message_type": message_type},
        )

    async def log_message_failed(
        self,
        from_agent: str,
        to_agent: str,
        message_id: str,
        error: str,
    ) -> str:
        """Log message failure"""
        return await self.log_event(
            event_type=AuditEventType.MESSAGE_FAILED,
            actor_id=from_agent,
            actor_type="agent",
            target_id=to_agent,
            target_type="agent",
            message_id=message_id,
            level=AuditLevel.ERROR,
            details={"error": error},
        )

    async def log_subnet_created(
        self,
        subnet_id: str,
        name: str,
        created_by: str = "system",
        has_security: bool = False,
    ) -> str:
        """Log subnet creation"""
        return await self.log_event(
            event_type=AuditEventType.SUBNET_CREATED,
            actor_id=created_by,
            target_id=subnet_id,
            target_type="subnet",
            subnet_id=subnet_id,
            details={"name": name, "has_security": has_security},
        )

    async def log_gateway_connected(
        self,
        agent_id: str,
        subnet_id: str,
        source_ip: str | None = None,
    ) -> str:
        """Log gateway connection"""
        return await self.log_event(
            event_type=AuditEventType.GATEWAY_CONNECTED,
            target_id=agent_id,
            target_type="agent",
            subnet_id=subnet_id,
            source_ip=source_ip,
        )

    async def log_auth_success(
        self,
        agent_id: str,
        subnet_id: str,
        auth_method: str,
    ) -> str:
        """Log successful authentication"""
        return await self.log_event(
            event_type=AuditEventType.SECURITY_AUTH_SUCCESS,
            target_id=agent_id,
            target_type="agent",
            subnet_id=subnet_id,
            details={"auth_method": auth_method},
        )

    async def log_auth_failure(
        self,
        agent_id: str | None,
        subnet_id: str,
        reason: str,
        source_ip: str | None = None,
    ) -> str:
        """Log authentication failure"""
        return await self.log_event(
            event_type=AuditEventType.SECURITY_AUTH_FAILURE,
            target_id=agent_id,
            target_type="agent",
            subnet_id=subnet_id,
            level=AuditLevel.WARNING,
            details={"reason": reason},
            source_ip=source_ip,
        )

    async def log_error(
        self,
        error: str,
        component: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Log an error"""
        return await self.log_event(
            event_type=AuditEventType.ERROR_OCCURRED,
            actor_type="system",
            level=AuditLevel.ERROR,
            details={"error": error, "component": component, **(details or {})},
        )

    # =========================================================================
    # Query Methods
    # =========================================================================

    async def query_events(
        self,
        event_type: AuditEventType | None = None,
        actor_id: str | None = None,
        target_id: str | None = None,
        subnet_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        level: AuditLevel | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        """
        Query audit events.

        Args:
            event_type: Filter by event type
            actor_id: Filter by actor ID
            target_id: Filter by target ID
            subnet_id: Filter by subnet ID
            start_time: Start of time range
            end_time: End of time range
            level: Filter by severity level
            limit: Maximum results to return
            offset: Offset for pagination

        Returns:
            List of matching audit events
        """
        # Read from stream
        start_id = "-"
        end_id = "+"

        if start_time:
            start_id = f"{int(start_time.timestamp() * 1000)}-0"
        if end_time:
            end_id = f"{int(end_time.timestamp() * 1000)}-0"

        # Read from stream (newest first)
        raw_events = await self.redis.xrevrange(
            self.stream_name,
            max=end_id,
            min=start_id,
            count=limit + offset + 1000,  # Get extra for filtering
        )

        events = []
        skipped = 0

        for _entry_id, data in raw_events:
            try:
                event_data = json.loads(data[b"data"])
                event = AuditEvent(**event_data)

                # Apply filters
                if event_type and event.event_type != event_type:
                    continue
                if actor_id and event.actor_id != actor_id:
                    continue
                if target_id and event.target_id != target_id:
                    continue
                if subnet_id and event.subnet_id != subnet_id:
                    continue
                if level and event.level != level:
                    continue

                # Handle offset
                if skipped < offset:
                    skipped += 1
                    continue

                events.append(event)

                if len(events) >= limit:
                    break

            except (json.JSONDecodeError, KeyError):
                continue

        return events

    async def get_event(self, event_id: str) -> AuditEvent | None:
        """Get a specific event by ID"""
        # Search in stream (not efficient, but works)
        events = await self.query_events(limit=10000)
        for event in events:
            if event.id == event_id:
                return event
        return None

    async def get_recent_events(
        self,
        limit: int = 50,
        event_types: list[AuditEventType] | None = None,
    ) -> list[AuditEvent]:
        """Get most recent events"""
        raw_events = await self.redis.xrevrange(
            self.stream_name,
            count=limit * 2 if event_types else limit,
        )

        events = []
        for _entry_id, data in raw_events:
            try:
                event_data = json.loads(data[b"data"])
                event = AuditEvent(**event_data)

                if event_types and event.event_type not in event_types:
                    continue

                events.append(event)

                if len(events) >= limit:
                    break

            except (json.JSONDecodeError, KeyError):
                continue

        return events

    async def count_events(
        self,
        event_type: AuditEventType | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> int:
        """Count events matching criteria"""
        if not event_type and not start_time and not end_time:
            return await self.redis.xlen(self.stream_name)

        events = await self.query_events(
            event_type=event_type,
            start_time=start_time,
            end_time=end_time,
            limit=100000,
        )
        return len(events)

    # =========================================================================
    # Statistics Methods
    # =========================================================================

    async def get_event_stats(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Get statistics about audit events.

        Returns:
            Dictionary with event counts by type, level, etc.
        """
        events = await self.query_events(
            start_time=start_time,
            end_time=end_time,
            limit=100000,
        )

        stats = {
            "total": len(events),
            "by_type": {},
            "by_level": {},
            "by_subnet": {},
            "time_range": {
                "start": start_time.isoformat() if start_time else None,
                "end": end_time.isoformat() if end_time else None,
            },
        }

        for event in events:
            # Count by type
            type_key = event.event_type.value
            stats["by_type"][type_key] = stats["by_type"].get(type_key, 0) + 1

            # Count by level
            level_key = event.level.value
            stats["by_level"][level_key] = stats["by_level"].get(level_key, 0) + 1

            # Count by subnet
            if event.subnet_id:
                stats["by_subnet"][event.subnet_id] = stats["by_subnet"].get(event.subnet_id, 0) + 1

        return stats

    # =========================================================================
    # Export Methods
    # =========================================================================

    async def export_events(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        format: str = "json",
    ) -> str:
        """
        Export audit events.

        Args:
            start_time: Start of time range
            end_time: End of time range
            format: Export format (json, csv)

        Returns:
            Exported data as string
        """
        events = await self.query_events(
            start_time=start_time,
            end_time=end_time,
            limit=100000,
        )

        if format == "json":
            return json.dumps([e.model_dump() for e in events], indent=2, default=str)
        elif format == "csv":
            lines = ["id,timestamp,event_type,level,actor_id,target_id,subnet_id"]
            for event in events:
                lines.append(
                    f"{event.id},{event.timestamp.isoformat()},{event.event_type.value},"
                    f"{event.level.value},{event.actor_id or ''},{event.target_id or ''},{event.subnet_id or ''}"
                )
            return "\n".join(lines)
        else:
            raise ValueError(f"Unsupported format: {format}")

    # =========================================================================
    # Cleanup Methods
    # =========================================================================

    async def cleanup_old_events(self, days: int | None = None) -> int:
        """
        Remove events older than specified days.

        Args:
            days: Days to retain (default: retention_days)

        Returns:
            Number of events removed
        """
        days = days or self.retention_days
        cutoff = datetime.now(UTC).timestamp() - (days * 24 * 3600)
        cutoff_id = f"{int(cutoff * 1000)}-0"

        # Trim stream
        deleted = await self.redis.xtrim(
            self.stream_name,
            minid=cutoff_id,
        )

        return deleted


# ---------------------------------------------------------------------------
# Fire-and-forget helper (security hot paths)
# ---------------------------------------------------------------------------


# Strong references to in-flight fire-and-forget tasks.
#
# CPython's asyncio event loop only keeps a *weak* reference to tasks
# created by ``create_task``; without an external strong reference the
# task can be garbage-collected before its first ``await`` resolves —
# silently dropping audit events under load. We retain the Task here
# until it finishes and discard it via ``add_done_callback``.
_PENDING_AUDIT_TASKS: set[asyncio.Task[Any]] = set()

# Per-call ceiling for a single audit write.  Bounds the time a hung
# Redis call can pin one ``_PENDING_AUDIT_TASKS`` entry: under a Redis
# stall + heavy auth-failure load, an unbounded write would let the
# set grow without limit (each task pins its closure + redis client
# objects).  2 s comfortably covers a healthy 4-op write while still
# bleeding off attempts during outages.
_AUDIT_WRITE_TIMEOUT_S: float = 2.0


async def drain_pending_audit_tasks(timeout: float = 3.0) -> tuple[int, int]:
    """Wait for in-flight fire-and-forget audit writes during shutdown.

    Without a drain step, lifespan teardown closes Redis (and the event
    loop shortly after) while ``_PENDING_AUDIT_TASKS`` may still hold
    coroutines that haven't reached their first ``await``. Those tasks
    are then cancelled by the loop, dropping audit events that were
    *already accepted* by the helper.

    Returns ``(drained, dropped)`` for caller logging:
      - ``drained`` — tasks that completed within the timeout.
      - ``dropped`` — tasks still in flight when the timeout fired
        (these are cancelled to release Redis client / closure refs
        promptly; the underlying audit write is lost — best-effort).

    Safe to call multiple times.  The fire-and-forget helper checks
    ``audit._started``; callers should flip it off (via ``audit.stop()``)
    *before* awaiting the drain so no new tasks queue up after we
    snapshot the set.
    """
    pending = list(_PENDING_AUDIT_TASKS)
    if not pending:
        return 0, 0
    # Use ``asyncio.wait`` (not ``wait_for(gather(...))``) because the
    # latter cancels and re-awaits the inner tasks before returning,
    # making "still running at deadline" indistinguishable from
    # "finished cleanly". ``wait`` returns the (done, pending) split
    # at the deadline without cancelling pending tasks itself.
    done, still_running = await asyncio.wait(pending, timeout=timeout)
    if still_running:
        for task in still_running:
            task.cancel()
        # ``cancel()`` is a *request*; without awaiting, the cancellation
        # only takes effect when the loop next schedules each task — and
        # in lifespan teardown that may be after Redis is closed, which
        # produces noisy "coroutine was never awaited" warnings.  Await
        # them now so cancellation propagates before the caller continues
        # the shutdown sequence.  ``return_exceptions`` swallows the
        # ``CancelledError`` re-raise from each task.
        await asyncio.gather(*still_running, return_exceptions=True)
    return len(done), len(still_running)


def fire_and_forget_event(
    audit: "AuditLogger | None",
    *,
    event_type: AuditEventType,
    actor_id: str | None = None,
    actor_type: str | None = None,
    target_id: str | None = None,
    target_type: str | None = None,
    subnet_id: str | None = None,
    level: AuditLevel = AuditLevel.INFO,
    details: dict[str, Any] | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Schedule an audit write without awaiting.

    Why fire-and-forget on auth-failure / SSRF-block paths:
      Each ``log_event`` performs ~4 Redis ops (xadd + lpush + ltrim + expire).
      Synchronously awaiting that on every 401/403 lets credential-stuffing
      traffic amplify into a Redis DoS vector. We additionally never want an
      audit-pipeline hiccup to turn a clean 401 into a confused 500.

    The helper:
      - returns immediately if ``audit`` is ``None`` or not started.
      - swallows scheduling errors (e.g. no running loop in test setups).
      - retains a strong reference to each spawned task to prevent the
        well-known "create_task + GC = silently dropped task" trap.
      - swallows write errors inside the spawned task and logs at debug level.

    Callers should treat the audit write as best-effort — it is *not*
    suitable for compliance-critical events that must never be lost.
    """
    if audit is None or not getattr(audit, "_started", False):
        return

    async def _safe_write() -> None:
        try:
            await asyncio.wait_for(
                audit.log_event(
                    event_type=event_type,
                    actor_id=actor_id,
                    actor_type=actor_type,
                    target_id=target_id,
                    target_type=target_type,
                    subnet_id=subnet_id,
                    level=level,
                    details=details or {},
                    source_ip=source_ip,
                    user_agent=user_agent,
                ),
                timeout=_AUDIT_WRITE_TIMEOUT_S,
            )
        except TimeoutError:
            # Redis stalled — never let a hung audit write pin the task
            # entry indefinitely; ``_PENDING_AUDIT_TASKS`` would otherwise
            # grow unbounded under attack.  ``asyncio.TimeoutError`` is
            # aliased to the builtin since 3.11; use the canonical name.
            _logger.debug(
                "audit_fire_and_forget_timeout",
                extra={
                    "event_type": event_type.value,
                    "timeout_s": _AUDIT_WRITE_TIMEOUT_S,
                },
            )
        except Exception as exc:  # noqa: BLE001 — must never raise on hot path
            _logger.debug(
                "audit_fire_and_forget_failed",
                extra={"event_type": event_type.value, "error": str(exc)},
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — happens only in synchronous test paths.
        # Nothing useful we can do; drop the event silently.
        return

    task = loop.create_task(_safe_write())
    _PENDING_AUDIT_TASKS.add(task)
    task.add_done_callback(_PENDING_AUDIT_TASKS.discard)


# ---------------------------------------------------------------------------
# Module-level singleton for cross-module access (auth middleware, etc.)
# ---------------------------------------------------------------------------
#
# Why a singleton lives here and not only in ``routes.dependencies``:
#   ``acn.auth.middleware`` is imported *by* ``routes.dependencies`` (for
#   ``get_subject``). Pushing the audit accessor into ``dependencies`` would
#   create an import cycle when middleware needs to record JWT failures.
#   Hosting the singleton next to the AuditLogger class keeps the dependency
#   graph one-way: ``dependencies -> monitoring`` and ``auth -> monitoring``.

_AUDIT_SINGLETON: "AuditLogger | None" = None


def set_audit_singleton(audit: "AuditLogger | None") -> None:
    """Wire the active AuditLogger so cross-module helpers can reach it.

    Call once during app startup (``dependencies.set_globals``). Safe to call
    again — the latest binding wins.
    """
    global _AUDIT_SINGLETON
    _AUDIT_SINGLETON = audit


def get_audit_singleton() -> "AuditLogger | None":
    """Return the currently wired AuditLogger (may be ``None`` pre-startup)."""
    return _AUDIT_SINGLETON


# ---------------------------------------------------------------------------
# Auth-failure sampling — in-process LRU dedup
# ---------------------------------------------------------------------------
# A single attacking IP with the same reason can emit 1 000+ failures/s.
# Each failure triggers ~4 Redis ops. We deduplicate by (source_ip, reason):
# within a 1-second window only the *first* event is written; subsequent
# duplicates increment an in-memory counter that is flushed at the window
# boundary.  Low-frequency events are always written immediately.
#
# The LRU dict is module-level (single process) and bounded by
# ``_AUTH_SAMPLE_MAX_KEYS`` to prevent memory growth under highly diverse IPs.
# If the limit is hit, the oldest key is evicted (FIFO via dict insertion order
# in Python 3.7+). The dedup window and max keys are intentionally NOT env-
# configurable for now — changing them is a deploy, not a runtime toggle.

_AUTH_SAMPLE_WINDOW_S: float = 1.0        # dedup window in seconds
_AUTH_SAMPLE_MAX_KEYS: int = 1_000        # max distinct (ip, reason) pairs kept

# key → (last_written_at_s, suppressed_count)
_auth_sample_cache: dict[tuple[str, str], tuple[float, int]] = {}


def _auth_sample_key(source_ip: str | None, reason: str) -> tuple[str, str]:
    return (source_ip or "", reason)


def _auth_sample_should_write(source_ip: str | None, reason: str) -> bool:
    """Return True if this event should be written; update the in-memory cache.

    Side effect: evicts the oldest key when the cache is at capacity.
    """
    key = _auth_sample_key(source_ip, reason)
    now = time.monotonic()
    entry = _auth_sample_cache.get(key)

    if entry is None or (now - entry[0]) >= _AUTH_SAMPLE_WINDOW_S:
        # First occurrence or window expired — write and (re)set the window.
        if len(_auth_sample_cache) >= _AUTH_SAMPLE_MAX_KEYS and key not in _auth_sample_cache:
            # Evict the oldest key (first insertion-order entry).
            oldest = next(iter(_auth_sample_cache))
            del _auth_sample_cache[oldest]
        _auth_sample_cache[key] = (now, 0)
        return True

    # Within the window: increment suppressed counter, don't write.
    suppressed = entry[1] + 1
    _auth_sample_cache[key] = (entry[0], suppressed)
    return False


def record_auth_failure(
    *,
    reason: str,
    source_ip: str | None = None,
    actor_id: str | None = None,
    path: str | None = None,
    method: str | None = None,
    subnet_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Best-effort fire-and-forget audit log for any authn/authz failure.

    Delegates to ``fire_and_forget_event``. Callers are responsible for
    extracting ``source_ip`` from the request before calling — that lets
    each call site choose its own proxy-aware vs naive IP strategy without
    forcing a Request parameter through the helper signature.

    Duplicate events from the same (source_ip, reason) pair within a 1-second
    window are suppressed to cap Redis ops under brute-force attacks.

    Args:
        reason: Snake_case tag, e.g. ``"api_key_invalid"``.
        source_ip: Resolved client IP (already proxy-aware where relevant).
        actor_id: ID of the *failing principal* (e.g. JWT ``sub`` for
            permission_denied, target agent_id is intentionally NOT passed
            here — the failure is attributed to the caller, not the asset).
        path / method: Request path + method, recorded into ``details`` so
            analysts can pin attacks to a specific endpoint without
            cross-referencing access logs.
        subnet_id: Subnet context if relevant.
        extra: Additional structured details merged into ``details``.
    """
    audit = _AUDIT_SINGLETON
    if audit is None:
        return

    if not _auth_sample_should_write(source_ip, reason):
        return

    details: dict[str, Any] = {"reason": reason}
    if path:
        details["path"] = path
    if method:
        details["method"] = method
    if extra:
        details.update(extra)
    # Intentionally leave ``actor_type`` unset. ``actor_id`` for an Auth0
    # subject can be a user (``auth0|xxx``), an m2m client (``xxx@clients``),
    # a dev stub (``dev@clients``), or an internal-service marker
    # (``backend@internal``). Tagging all of those as ``"user"`` poisons
    # actor-type analytics; analysts can derive the type from the ``sub``
    # suffix if needed.
    fire_and_forget_event(
        audit,
        event_type=AuditEventType.SECURITY_AUTH_FAILURE,
        actor_id=actor_id,
        subnet_id=subnet_id or None,
        level=AuditLevel.WARNING,
        details=details,
        source_ip=source_ip,
    )
