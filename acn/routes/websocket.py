"""WebSocket API Routes"""

import json

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ..config import get_settings
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    InternalTokenDep,
    WsManagerDep,
    get_agent_service,
    get_ws_manager,
)

router = APIRouter(tags=["websocket"])
logger = structlog.get_logger()


def _extract_bearer_token(websocket: WebSocket) -> str | None:
    """Pull a Bearer token out of the WebSocket handshake's Authorization header.

    Browser ``new WebSocket()`` cannot set arbitrary headers, but proxies,
    server-side clients (httpx_ws, websockets, aiohttp), and SDKs all
    can. Supporting it gives every non-browser caller a path that keeps
    the API key out of access logs and Referer headers.
    """
    auth = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
    if not auth:
        return None
    auth = auth.strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


async def _safe_close(websocket: WebSocket, *, code: int, reason: str) -> None:
    """``websocket.close()`` that never raises out of an auth-failure path.

    Pre-launch audit backlog #3: starlette's ``close()`` is documented as
    idempotent on already-disconnected sockets via an internal
    ``application_state`` check, but relying on a private state machine is
    fragile. If the peer hard-closes mid-handshake or our send buffer is
    already torn down, ``close()`` can raise ``RuntimeError`` /
    ``ConnectionClosed`` / ``WebSocketDisconnect``.

    Swallowing those failures is correct because we're already on a path
    that has decided to terminate the socket; surfacing the error to
    FastAPI just turns a clean reject into a noisy 500 in logs without
    changing the wire outcome.
    """
    try:
        await websocket.close(code=code, reason=reason)
    except Exception as exc:  # noqa: BLE001 — defensive: we are tearing down anyway
        logger.debug(
            "websocket_close_swallowed",
            code=code,
            reason=reason,
            error=str(exc),
            error_type=type(exc).__name__,
        )


@router.websocket("/ws/{agent_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    agent_id: str,
    token: str | None = Query(None, description="[Deprecated] Agent API key — use first-message auth instead"),
):
    """WebSocket endpoint for real-time communication.

    Authentication options (in order of preference):

    1. **Authorization header** (recommended for non-browser clients):
       handshake with ``Authorization: Bearer <API_KEY>``.  The header
       never appears in server access logs and is not sniffable from a
       browser tab's URL bar.
    2. **First-message auth** (recommended for browsers):
       after connecting, send JSON ``{"type": "auth", "token": "<API_KEY>"}``.
       Server responds with ``{"type": "auth_ok"}`` or closes with 4401.
       This is the only option in browsers (the WebSocket constructor
       can't set headers).
    3. **URL query param** (deprecated, off by default in production):
       ``?token=<API_KEY>``.  Disabled unless ``WEBSOCKET_ALLOW_QUERY_TOKEN=true``
       (auto-enabled in ``DEV_MODE=true``).  The key appears in server
       access logs, browser Referer headers, and shoulder-surfable URL
       bars — see security audit M14.
    """
    ws_manager = get_ws_manager()
    agent_service = get_agent_service()
    settings = get_settings()

    # --- Resolve token (priority: header > query > first-message) ---
    header_token = _extract_bearer_token(websocket)
    resolved_token: str | None = None
    via: str = "first_message"

    if header_token is not None:
        resolved_token = header_token
        via = "authorization_header"
    elif token is not None:
        # Query-string token is gated by the operator-controlled flag.
        # We accept the WS handshake first so we can send a clean close
        # frame with a structured reason rather than a raw 403 — the
        # client otherwise just sees ``ConnectionClosed`` with no detail.
        if not settings.websocket_allow_query_token:
            logger.warning(
                "websocket_query_token_rejected",
                agent_id=agent_id,
                reason=(
                    "WEBSOCKET_ALLOW_QUERY_TOKEN=false: query-string tokens "
                    "are disabled in production. Use Authorization: Bearer "
                    "or first-message auth."
                ),
            )
            await websocket.accept()
            await _safe_close(
                websocket,
                code=4401,
                reason="Unauthorized: query-string token disabled",
            )
            return
        resolved_token = token
        via = "query_string"
        # Even when allowed, every query-token use leaves a deprecation
        # breadcrumb so operators can see the migration tail in dashboards.
        logger.warning(
            "websocket_token_in_url_deprecated",
            agent_id=agent_id,
            message="API key passed as URL param — migrate to header / first-message auth",
        )

    await websocket.accept()

    if resolved_token is None:
        # First-message auth: wait for {"type": "auth", "token": "..."}
        try:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "auth" and msg.get("token"):
                resolved_token = msg["token"]
            else:
                await _safe_close(
                    websocket, code=4401, reason="Unauthorized: expected auth message"
                )
                return
        except Exception:  # noqa: BLE001 — JSON parse / disconnect / timeout
            await _safe_close(
                websocket, code=4401, reason="Unauthorized: invalid auth message"
            )
            return

    # Validate API key
    agent = await agent_service.get_agent_by_api_key(resolved_token)
    if not agent or agent.agent_id != agent_id:
        await _safe_close(
            websocket, code=4401, reason="Unauthorized: invalid API key"
        )
        return

    # Notify client auth succeeded — only meaningful for the
    # first-message flow, where the client explicitly waits for the
    # ``auth_ok`` echo before sending application messages. Header /
    # query auth callers don't expect it.
    if via == "first_message":
        await websocket.send_text(json.dumps({"type": "auth_ok"}))

    logger.info("websocket_connected", agent_id=agent_id, via=via)

    # websocket.accept() was already called above for the auth handshake,
    # so tell the manager to skip its own accept() call.
    # The try block starts here so that if connect() itself raises, the
    # except clause can still call disconnect() with a defined connection_id
    # (empty string is a safe no-op in WebSocketManager.disconnect).
    connection_id = ""
    try:
        connection_id = await ws_manager.connect(
            websocket,
            user_id=agent_id,
            metadata={"principal_type": "agent"},
            already_accepted=True,
        )

        # Keep connection alive and handle messages
        while True:
            data = await websocket.receive_text()
            logger.debug("websocket_message_received", agent_id=agent_id, data=data)

            # Echo back for now (can extend with message routing)
            await websocket.send_text(f"Received: {data}")

    except WebSocketDisconnect:
        logger.info("websocket_disconnected", agent_id=agent_id)
        await ws_manager.disconnect(connection_id)

    except Exception as e:
        logger.error("websocket_error", agent_id=agent_id, error=str(e))
        await ws_manager.disconnect(connection_id)
        raise


@router.get("/api/v1/websocket/connections")
async def get_active_connections(_: InternalTokenDep, ws_manager: WsManagerDep = None):
    """Get active WebSocket connections summary (requires X-Internal-Token)"""
    stats = ws_manager.get_stats()
    return stats


@router.get("/api/v1/websocket/agent/{agent_id}/status")
async def get_agent_websocket_status(
    agent_id: str,
    agent_info: AgentApiKeyDep,
    ws_manager: WsManagerDep = None,
):
    """Check if agent has active WebSocket connection (requires Agent API Key)

    An agent may only query its own connection status.
    """
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")
    is_connected = ws_manager.is_user_connected(agent_id)
    return {"agent_id": agent_id, "connected": is_connected}
