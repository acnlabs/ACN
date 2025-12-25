"""WebSocket API Routes"""

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .dependencies import WsManagerDep, get_ws_manager  # type: ignore[import-untyped]

router = APIRouter(tags=["websocket"])
logger = structlog.get_logger()


@router.websocket("/ws/{agent_id}")
async def websocket_endpoint(websocket: WebSocket, agent_id: str):
    """WebSocket endpoint for real-time communication"""
    ws_manager = get_ws_manager()

    await websocket.accept()
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
async def get_active_connections(ws_manager: WsManagerDep = None):
    """Get active WebSocket connections"""
    connections = await ws_manager.get_active_connections()
    return {"connections": connections, "count": len(connections)}


@router.get("/api/v1/websocket/agent/{agent_id}/status")
async def get_agent_websocket_status(agent_id: str, ws_manager: WsManagerDep = None):
    """Check if agent has active WebSocket connection"""
    is_connected = await ws_manager.is_connected(agent_id)
    return {"agent_id": agent_id, "connected": is_connected}

