"""Subnet gateway WebSocket — binds ``SubnetManager.handle_connection``.

Exposes ``/gateway/connect/{subnet_id}/{agent_id}`` on the root ``app``.
Clients must immediately send the REGISTER JSON frame documented in
``SubnetManager``. Inbound heartbeat frames refresh Redis ``alive`` TTL via
implicit heartbeat when ``SubnetManager`` receives ``agent_service`` from
lifespan wiring.
"""

from fastapi import APIRouter, Depends, WebSocket

from ..infrastructure.messaging.subnet_manager import SubnetManager
from .dependencies import AgentIdPath, SubnetIdPath, get_subnet_manager

router = APIRouter(tags=["gateway"])


@router.websocket("/gateway/connect/{subnet_id}/{agent_id}")
async def gateway_connect(
    websocket: WebSocket,
    subnet_id: SubnetIdPath,
    agent_id: AgentIdPath,
    subnet_manager: SubnetManager = Depends(get_subnet_manager),
):
    """A2A gateway ingress — REGISTER then message loop.

    Mirrors the usage block in ``SubnetManager`` documentation; callers
    must send a REGISTER JSON frame immediately after connection.
    """
    await subnet_manager.handle_connection(websocket, subnet_id, agent_id)
