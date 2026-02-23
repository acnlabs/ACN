"""WebSocket API Routes"""

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
    token: str = Query(..., description="Agent API key for authentication"),
):
    """WebSocket endpoint for real-time communication.

    Requires `?token=<API_KEY>` query parameter. The API key must belong to
    the agent identified by `agent_id` to prevent connection hijacking.
    """
    ws_manager = get_ws_manager()
    agent_service = get_agent_service()

    # Validate API key; must accept before sending close frame (Starlette requirement)
    agent = await agent_service.get_agent_by_api_key(token)
    if not agent or agent.agent_id != agent_id:
        await websocket.accept()
        await websocket.close(code=4401, reason="Unauthorized: invalid API key")
        return

    await websocket.accept()
    logger.info("websocket_connected", agent_id=agent_id)

    try:
        # Register agent WebSocket connection
        await ws_manager.connect(agent_id, websocket)

        _MAX_WS_MESSAGE_BYTES = 65536

        # Keep connection alive and handle messages
        while True:
            data = await websocket.receive_text()

            if len(data.encode("utf-8")) > _MAX_WS_MESSAGE_BYTES:
                await websocket.close(code=4009, reason="Message too large")
                return

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
