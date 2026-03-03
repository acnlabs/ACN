"""WebSocket API Routes"""

import json

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    InternalTokenDep,
    WsManagerDep,
    get_agent_service,
    get_ws_manager,
)

router = APIRouter(tags=["websocket"])
logger = structlog.get_logger()


@router.websocket("/ws/{agent_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    agent_id: str,
    token: str | None = Query(None, description="[Deprecated] Agent API key — use first-message auth instead"),
):
    """WebSocket endpoint for real-time communication.

    Authentication options (in order of preference):
    1. First-message auth (recommended): after connecting, send JSON:
       {"type": "auth", "token": "<API_KEY>"}
       Server responds with {"type": "auth_ok"} or closes with code 4401.
    2. URL query param (deprecated): ?token=<API_KEY>
       Still supported for backward compatibility, but the key appears in
       server access logs. Migrate to first-message auth.
    """
    ws_manager = get_ws_manager()
    agent_service = get_agent_service()

    await websocket.accept()

    # --- Resolve token ---
    resolved_token = token  # may be None

    if resolved_token is None:
        # First-message auth: wait for {"type": "auth", "token": "..."}
        try:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "auth" and msg.get("token"):
                resolved_token = msg["token"]
            else:
                await websocket.close(code=4401, reason="Unauthorized: expected auth message")
                return
        except Exception:
            await websocket.close(code=4401, reason="Unauthorized: invalid auth message")
            return
    else:
        logger.warning(
            "websocket_token_in_url_deprecated",
            agent_id=agent_id,
            message="API key passed as URL param — migrate to first-message auth",
        )

    # Validate API key
    agent = await agent_service.get_agent_by_api_key(resolved_token)
    if not agent or agent.agent_id != agent_id:
        await websocket.close(code=4401, reason="Unauthorized: invalid API key")
        return

    # Notify client auth succeeded (first-message auth flow)
    if token is None:
        await websocket.send_text(json.dumps({"type": "auth_ok"}))

    logger.info("websocket_connected", agent_id=agent_id)

    try:
        # Register agent WebSocket connection
        await ws_manager.connect(agent_id, websocket)

        # Keep connection alive and handle messages
        while True:
            data = await websocket.receive_text()
            logger.debug("websocket_message_received", agent_id=agent_id, data=data)

            # Echo back for now (can extend with message routing)
            await websocket.send_text(f"Received: {data}")

    except WebSocketDisconnect:
        logger.info("websocket_disconnected", agent_id=agent_id)
        await ws_manager.disconnect(agent_id)

    except Exception as e:
        logger.error("websocket_error", agent_id=agent_id, error=str(e))
        await ws_manager.disconnect(agent_id)
        raise


@router.get("/api/v1/websocket/connections")
async def get_active_connections(_: InternalTokenDep, ws_manager: WsManagerDep = None):
    """Get active WebSocket connections (requires X-Internal-Token)"""
    connections = await ws_manager.get_active_connections()
    return {"connections": connections, "count": len(connections)}


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
    is_connected = await ws_manager.is_connected(agent_id)
    return {"agent_id": agent_id, "connected": is_connected}
