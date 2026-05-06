"""Session Service — Phase 3 real-time session layer.

A *session* is a lightweight negotiation object that allows two agents to
agree on a bilateral real-time channel before committing resources (e.g.
opening a long-running WebSocket connection, starting an LLM inference
loop, or initiating a streaming task).

Lifecycle::

    Inviter                        Recipient
       │  POST /sessions/invite  →  │
       │  ← session_invite WS push  │
       │                            │ POST /sessions/{id}/accept
       │  ← session_accepted WS push│
       │         (or reject)        │
       │  DELETE /sessions/{id}  →  │  (either party can close)
       │  ← session_closed WS push  │

Storage model (Redis-only, no Postgres):

* ``acn:session:{session_id}`` — HASH with fields:
    ``session_id``, ``inviter_id``, ``invitee_id``, ``status``,
    ``created_at_ms``, ``expires_at_ms``, ``metadata`` (JSON).
* ``acn:sessions:pending:{agent_id}`` — ZSET (score = expires_at_ms)
    Index of pending session ids for an agent. Used to list / expire
    invitations without scanning all session keys.

Sessions expire automatically via Redis TTL (``DEFAULT_SESSION_TTL_SECONDS``).
Accepted/rejected/closed sessions are also deleted from Redis immediately
(the final WS push is the durable receipt; no long-term persistence needed
for v1). Callers that need audit trails should persist the WS events on
the client side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import redis.asyncio as redis  # type: ignore[import-untyped]

DEFAULT_SESSION_TTL_SECONDS = 5 * 60  # 5 minutes — invitation must be acted on quickly
MAX_SESSION_TTL_SECONDS = 30 * 60  # 30 minutes ceiling
MIN_SESSION_TTL_SECONDS = 60  # 1 minute floor

MAX_METADATA_BYTES = 4096  # JSON-encoded metadata cap


@dataclass(frozen=True)
class SessionEntry:
    """In-memory representation of a session record."""

    session_id: str
    inviter_id: str
    invitee_id: str
    status: str  # "pending" | "accepted" | "rejected" | "closed"
    created_at_ms: int
    expires_at_ms: int
    metadata: dict = field(default_factory=dict)


def _session_key(session_id: str) -> str:
    return f"acn:session:{session_id}"


def _pending_zset_key(agent_id: str) -> str:
    return f"acn:sessions:pending:{{{agent_id}}}"


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _decode_session(raw: dict) -> SessionEntry:
    """Coerce a Redis HGETALL response into a ``SessionEntry``."""

    def _u(v) -> str:
        return v.decode() if isinstance(v, bytes) else str(v)

    decoded = {_u(k): _u(v) for k, v in raw.items()}
    metadata: dict = {}
    if decoded.get("metadata"):
        try:
            parsed = json.loads(decoded["metadata"])
            if isinstance(parsed, dict):
                metadata = parsed
        except json.JSONDecodeError:
            metadata = {}
    return SessionEntry(
        session_id=decoded.get("session_id", ""),
        inviter_id=decoded.get("inviter_id", ""),
        invitee_id=decoded.get("invitee_id", ""),
        status=decoded.get("status", "pending"),
        created_at_ms=int(decoded.get("created_at_ms", "0") or 0),
        expires_at_ms=int(decoded.get("expires_at_ms", "0") or 0),
        metadata=metadata,
    )


class SessionService:
    """CRUD service for the Session layer.

    All state lives in Redis. Methods are thin wrappers that handle key
    naming, TTL management, and state validation. Higher-level concerns
    (WS notification, metrics, auth checks) live in the route layer.
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    async def invite(
        self,
        inviter_id: str,
        invitee_id: str,
        *,
        ttl_seconds: int | None = None,
        metadata: dict | None = None,
    ) -> SessionEntry:
        """Create a new pending session invitation.

        Args:
            inviter_id: Agent id of the inviter.
            invitee_id: Agent id of the intended recipient.
            ttl_seconds: Expiry TTL. Clamped to ``[MIN, MAX]``. Defaults
                to ``DEFAULT_SESSION_TTL_SECONDS``.
            metadata: Optional JSON-serialisable dict (≤ 4 KB) attached
                to the invitation. Callers can use it to pass context
                (task description, capabilities, etc.).

        Returns:
            The newly created ``SessionEntry`` with ``status="pending"``.
        """
        ttl = _clamp_ttl(ttl_seconds)
        session_id = uuid4().hex
        now_ms = _now_ms()
        expires_at_ms = now_ms + ttl * 1000

        meta_blob: str = "{}"
        if metadata:
            blob = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
            if len(blob.encode()) > MAX_METADATA_BYTES:
                raise ValueError(
                    f"session metadata exceeds {MAX_METADATA_BYTES} bytes"
                )
            meta_blob = blob

        session_hash = {
            "session_id": session_id,
            "inviter_id": inviter_id,
            "invitee_id": invitee_id,
            "status": "pending",
            "created_at_ms": str(now_ms),
            "expires_at_ms": str(expires_at_ms),
            "metadata": meta_blob,
        }

        session_key = _session_key(session_id)
        invitee_zset = _pending_zset_key(invitee_id)

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(session_key, mapping=session_hash)
            pipe.expire(session_key, ttl)
            pipe.zadd(invitee_zset, {session_id: expires_at_ms})
            # Use the ceiling TTL for the ZSET so earlier invitations
            # (potentially with longer individual TTLs) are never evicted
            # prematurely when a newer, shorter invitation resets the key.
            # ZRANGEBYSCORE's ``min=now_ms`` filter handles stale entries at
            # read time; the ZSET just needs to outlive the longest possible
            # session within it.
            pipe.expire(invitee_zset, MAX_SESSION_TTL_SECONDS)
            await pipe.execute()

        return SessionEntry(
            session_id=session_id,
            inviter_id=inviter_id,
            invitee_id=invitee_id,
            status="pending",
            created_at_ms=now_ms,
            expires_at_ms=expires_at_ms,
            metadata=metadata or {},
        )

    async def get(self, session_id: str) -> SessionEntry | None:
        """Fetch a session by id.

        Returns ``None`` when the session has been deleted or has expired.
        """
        raw = await self.redis.hgetall(_session_key(session_id))
        if not raw:
            return None
        return _decode_session(raw)

    async def accept(self, session_id: str, acceptor_id: str) -> SessionEntry | None:
        """Mark a session as accepted.

        Args:
            session_id: Session to accept.
            acceptor_id: The agent accepting the invitation (must be the
                invitee).

        Returns:
            Updated ``SessionEntry`` with ``status="accepted"``, or
            ``None`` when the session was not found / expired.

        Raises:
            PermissionError: when ``acceptor_id`` is not the invitee.
            ValueError: when the session status is not "pending"
                (already accepted, rejected, or closed).
        """
        session = await self.get(session_id)
        if session is None:
            return None
        if acceptor_id != session.invitee_id:
            raise PermissionError(
                f"Only the invitee ({session.invitee_id!r}) can accept "
                f"session {session_id!r}; got {acceptor_id!r}"
            )
        if session.status != "pending":
            raise ValueError(
                f"Session {session_id!r} is in status {session.status!r}; "
                "only 'pending' sessions can be accepted"
            )

        session_key = _session_key(session_id)
        await self.redis.hset(session_key, "status", "accepted")
        return SessionEntry(
            session_id=session.session_id,
            inviter_id=session.inviter_id,
            invitee_id=session.invitee_id,
            status="accepted",
            created_at_ms=session.created_at_ms,
            expires_at_ms=session.expires_at_ms,
            metadata=session.metadata,
        )

    async def reject(self, session_id: str, rejector_id: str) -> SessionEntry | None:
        """Mark a session as rejected and delete it from Redis.

        Args:
            session_id: Session to reject.
            rejector_id: The agent rejecting the invitation (must be the
                invitee).

        Returns:
            The (now-deleted) ``SessionEntry``, or ``None`` when not found.

        Raises:
            PermissionError: when ``rejector_id`` is not the invitee.
            ValueError: when the session status is not "pending".
        """
        session = await self.get(session_id)
        if session is None:
            return None
        if rejector_id != session.invitee_id:
            raise PermissionError(
                f"Only the invitee ({session.invitee_id!r}) can reject "
                f"session {session_id!r}; got {rejector_id!r}"
            )
        if session.status != "pending":
            raise ValueError(
                f"Session {session_id!r} is in status {session.status!r}; "
                "only 'pending' sessions can be rejected"
            )

        await self._delete_session(session_id, session.invitee_id)
        return SessionEntry(
            session_id=session.session_id,
            inviter_id=session.inviter_id,
            invitee_id=session.invitee_id,
            status="rejected",
            created_at_ms=session.created_at_ms,
            expires_at_ms=session.expires_at_ms,
            metadata=session.metadata,
        )

    async def close(self, session_id: str, closer_id: str) -> SessionEntry | None:
        """Close a session (either party may close it).

        Args:
            session_id: Session to close.
            closer_id: The agent requesting closure (must be inviter or
                invitee).

        Returns:
            Updated ``SessionEntry`` with ``status="closed"``, or ``None``
            when not found.

        Raises:
            PermissionError: when ``closer_id`` is neither the inviter
                nor the invitee.
        """
        session = await self.get(session_id)
        if session is None:
            return None
        if closer_id not in (session.inviter_id, session.invitee_id):
            raise PermissionError(
                f"Only a session participant can close {session_id!r}; "
                f"got {closer_id!r}"
            )

        await self._delete_session(session_id, session.invitee_id)
        return SessionEntry(
            session_id=session.session_id,
            inviter_id=session.inviter_id,
            invitee_id=session.invitee_id,
            status="closed",
            created_at_ms=session.created_at_ms,
            expires_at_ms=session.expires_at_ms,
            metadata=session.metadata,
        )

    async def list_pending(
        self,
        agent_id: str,
        *,
        limit: int = 50,
    ) -> list[SessionEntry]:
        """List pending session invitations for an agent (newest-first).

        Skips expired entries (Redis TTL race) silently.
        """
        zset_key = _pending_zset_key(agent_id)
        now_ms = _now_ms()
        # ZRANGEBYSCORE with ``min=now_ms`` returns only entries whose
        # score (= expires_at_ms) is still in the future.
        raw_ids = await self.redis.zrangebyscore(
            zset_key,
            min=now_ms,
            max="+inf",
            start=0,
            num=limit,
        )
        if not raw_ids:
            return []

        async with self.redis.pipeline(transaction=False) as pipe:
            for raw_id in raw_ids:
                sid = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
                pipe.hgetall(_session_key(sid))
            details = await pipe.execute()

        results: list[SessionEntry] = []
        for detail in details:
            if not detail:
                continue
            entry = _decode_session(detail)
            if entry.status == "pending":
                results.append(entry)
        return results

    async def _delete_session(self, session_id: str, invitee_id: str) -> None:
        """Remove the session HASH and the pending-ZSET membership."""
        session_key = _session_key(session_id)
        invitee_zset = _pending_zset_key(invitee_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(session_key)
            pipe.zrem(invitee_zset, session_id)
            await pipe.execute()


def _clamp_ttl(ttl_seconds: int | None) -> int:
    if ttl_seconds is None:
        return DEFAULT_SESSION_TTL_SECONDS
    if ttl_seconds < MIN_SESSION_TTL_SECONDS:
        return MIN_SESSION_TTL_SECONDS
    if ttl_seconds > MAX_SESSION_TTL_SECONDS:
        return MAX_SESSION_TTL_SECONDS
    return ttl_seconds
