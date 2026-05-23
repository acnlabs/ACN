"""Session Layer API Routes — Phase 3

Real-time session negotiation between agents. A session is a lightweight
negotiation token that lets two agents agree on a bilateral channel before
committing resources.

Lifecycle:

    POST /sessions/invite/{target_agent_id}   → create pending session
    POST /sessions/{session_id}/accept        → invitee accepts
    POST /sessions/{session_id}/reject        → invitee rejects (deletes)
    DELETE /sessions/{session_id}             → either party closes

Auth model:
  All endpoints require an Agent API Key. The authenticated agent's id is
  derived from the key (never from the request body), so session ownership
  cannot be forged.

Rate limits:
  invite: 30/minute (intent-heavy, creates Redis state)
  accept/reject/close: 60/minute (lightweight state transitions)
"""

from __future__ import annotations

from typing import Any

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..infrastructure.messaging.websocket_manager import MessageType
from .dependencies import (
    AgentApiKeyDep,
    SessionServiceDep,
    WsManagerDep,
    limiter,
)

router = APIRouter(
    prefix="/api/v1/sessions",
    tags=["sessions"],
    responses=ACN_DEFAULT_RESPONSES,
)
logger = structlog.get_logger()


class SessionInviteRequest(BaseModel):
    """Body for POST /sessions/invite/{target_agent_id}."""

    ttl_seconds: int | None = Field(
        default=None,
        ge=60,
        le=1800,
        description=(
            "Session TTL in seconds. Clamped to [60s, 1800s]. "
            "Defaults to 300s (5 minutes)."
        ),
    )
    metadata: dict | None = Field(
        default=None,
        description=(
            "Optional JSON context attached to the invitation "
            "(task description, capabilities, etc.). Max 4 KB."
        ),
    )

    @field_validator("metadata")
    @classmethod
    def _check_metadata_size(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        import json
        size = len(json.dumps(v).encode())
        if size > 4096:
            raise ValueError(f"metadata exceeds 4 KB limit ({size} bytes)")
        return v


def _entry_to_dict(entry, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialise a ``SessionEntry`` to a response-safe dict."""
    out: dict[str, Any] = {
        "session_id": entry.session_id,
        "inviter_id": entry.inviter_id,
        "invitee_id": entry.invitee_id,
        "status": entry.status,
        "created_at": entry.created_at_ms,
        "expires_at": entry.expires_at_ms,
    }
    if entry.metadata:
        out["metadata"] = entry.metadata
    if extra:
        out.update(extra)
    return out


@router.post("/invite/{target_agent_id}")
@limiter.limit("30/minute")
async def invite_session(
    request: Request,
    target_agent_id: str,
    body: SessionInviteRequest,
    agent_info: AgentApiKeyDep,
    session_service: SessionServiceDep,
    ws_manager: WsManagerDep,
):
    """Create a pending session invitation.

    Authenticated agent becomes the *inviter*; ``target_agent_id`` is the
    *invitee*. A WS ``session_invite`` event is pushed to the invitee's
    connection so they can react in real time.

    Returns:
        ``{"session_id": ..., "status": "pending", ...}``
    """
    inviter_id = agent_info["agent_id"]
    if inviter_id == target_agent_id:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "An agent cannot invite itself to a session"},
        )

    try:
        entry = await session_service.invite(
            inviter_id=inviter_id,
            invitee_id=target_agent_id,
            ttl_seconds=body.ttl_seconds,
            metadata=body.metadata,
        )
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            400,
            details={"reason": "invalid_request"},
        ) from e

    # Push WS notification to invitee (best-effort — no error if offline).
    try:
        await ws_manager.send_to_user(
            target_agent_id,
            {
                "type": MessageType.SESSION_INVITE,
                "session_id": entry.session_id,
                "from_agent": inviter_id,
                "expires_at": entry.expires_at_ms,
                **({"metadata": entry.metadata} if entry.metadata else {}),
            },
        )
    except Exception:
        logger.warning(
            "session_invite_ws_push_failed",
            session_id=entry.session_id,
            invitee_id=target_agent_id,
        )

    logger.info(
        "session_invited",
        session_id=entry.session_id,
        inviter_id=inviter_id,
        invitee_id=target_agent_id,
    )
    return _entry_to_dict(entry)


@router.post("/{session_id}/accept")
@limiter.limit("60/minute")
async def accept_session(
    request: Request,
    session_id: str,
    agent_info: AgentApiKeyDep,
    session_service: SessionServiceDep,
    ws_manager: WsManagerDep,
):
    """Accept a pending session invitation.

    Only the *invitee* may call this. A WS ``session_accepted`` event is
    pushed to the inviter so they know the channel is ready.

    Returns:
        ``{"session_id": ..., "status": "accepted", ...}``
    """
    acceptor_id = agent_info["agent_id"]

    try:
        entry = await session_service.accept(session_id, acceptor_id)
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.SESSION_FORBIDDEN,
            403,
            details={"session_id": session_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        msg = str(e)
        # ``SessionService.accept`` embeds the current status in the
        # message: "...is in status 'accepted'...". Parse it to pick
        # the most informative error code rather than always falling
        # back to SESSION_EXPIRED.
        if "'accepted'" in msg:
            code = ErrorCode.SESSION_ALREADY_ACCEPTED
        else:
            # rejected / closed → SESSION_EXPIRED signals "no longer
            # actionable" without leaking internal state machine names.
            code = ErrorCode.SESSION_EXPIRED
        raise ACNHTTPError(
            code,
            400,
            details={"session_id": session_id, "reason": "invalid_request"},
        ) from e

    if entry is None:
        raise ACNHTTPError(
            ErrorCode.SESSION_NOT_FOUND,
            404,
            details={"session_id": session_id},
        )

    # Notify inviter.
    try:
        await ws_manager.send_to_user(
            entry.inviter_id,
            {
                "type": MessageType.SESSION_ACCEPTED,
                "session_id": session_id,
                "accepted_by": acceptor_id,
            },
        )
    except Exception:
        logger.warning(
            "session_accepted_ws_push_failed",
            session_id=session_id,
            inviter_id=entry.inviter_id,
        )

    logger.info("session_accepted", session_id=session_id, acceptor_id=acceptor_id)
    return _entry_to_dict(entry)


@router.post("/{session_id}/reject")
@limiter.limit("60/minute")
async def reject_session(
    request: Request,
    session_id: str,
    agent_info: AgentApiKeyDep,
    session_service: SessionServiceDep,
    ws_manager: WsManagerDep,
):
    """Reject a pending session invitation.

    Only the *invitee* may call this. The session is deleted immediately.
    A WS ``session_rejected`` event is pushed to the inviter.

    Returns:
        ``{"session_id": ..., "status": "rejected", ...}``
    """
    rejector_id = agent_info["agent_id"]

    try:
        entry = await session_service.reject(session_id, rejector_id)
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.SESSION_FORBIDDEN,
            403,
            details={"session_id": session_id, "reason": "permission_denied"},
        ) from e
    except ValueError as e:
        raise ACNHTTPError(
            ErrorCode.SESSION_EXPIRED,
            400,
            details={"session_id": session_id, "reason": "invalid_request"},
        ) from e

    if entry is None:
        raise ACNHTTPError(
            ErrorCode.SESSION_NOT_FOUND,
            404,
            details={"session_id": session_id},
        )

    try:
        await ws_manager.send_to_user(
            entry.inviter_id,
            {
                "type": MessageType.SESSION_REJECTED,
                "session_id": session_id,
                "rejected_by": rejector_id,
            },
        )
    except Exception:
        logger.warning(
            "session_rejected_ws_push_failed",
            session_id=session_id,
            inviter_id=entry.inviter_id,
        )

    logger.info("session_rejected", session_id=session_id, rejector_id=rejector_id)
    return _entry_to_dict(entry)


@router.delete("/{session_id}")
@limiter.limit("60/minute")
async def close_session(
    request: Request,
    session_id: str,
    agent_info: AgentApiKeyDep,
    session_service: SessionServiceDep,
    ws_manager: WsManagerDep,
):
    """Close a session (either participant may close it).

    The session is deleted from Redis. A WS ``session_closed`` event is
    pushed to the *other* participant.

    Returns:
        ``{"session_id": ..., "status": "closed", ...}``
    """
    closer_id = agent_info["agent_id"]

    try:
        entry = await session_service.close(session_id, closer_id)
    except PermissionError as e:
        raise ACNHTTPError(
            ErrorCode.SESSION_FORBIDDEN,
            403,
            details={"session_id": session_id, "reason": "permission_denied"},
        ) from e

    if entry is None:
        raise ACNHTTPError(
            ErrorCode.SESSION_NOT_FOUND,
            404,
            details={"session_id": session_id},
        )

    # Notify the other participant.
    other_party = (
        entry.invitee_id if closer_id == entry.inviter_id else entry.inviter_id
    )
    try:
        await ws_manager.send_to_user(
            other_party,
            {
                "type": MessageType.SESSION_CLOSED,
                "session_id": session_id,
                "closed_by": closer_id,
            },
        )
    except Exception:
        logger.warning(
            "session_closed_ws_push_failed",
            session_id=session_id,
            other_party=other_party,
        )

    logger.info("session_closed", session_id=session_id, closer_id=closer_id)
    return _entry_to_dict(entry)


@router.get("/pending")
@limiter.limit("60/minute")
async def list_pending_sessions(
    request: Request,
    agent_info: AgentApiKeyDep,
    session_service: SessionServiceDep,
):
    """List pending session invitations for the authenticated agent.

    Returns invitations where the agent is the *invitee* and the
    status is still ``pending`` (not expired).

    Returns:
        ``{"agent_id": ..., "count": N, "sessions": [...]}``
    """
    agent_id = agent_info["agent_id"]
    sessions = await session_service.list_pending(agent_id)
    return {
        "agent_id": agent_id,
        "count": len(sessions),
        "sessions": [_entry_to_dict(e) for e in sessions],
    }
