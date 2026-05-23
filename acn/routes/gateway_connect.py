"""Subnet gateway WebSocket — binds ``SubnetManager.handle_connection``.

Exposes ``/gateway/connect/{subnet_id}/{agent_id}`` on the root ``app``.
Clients must immediately send the REGISTER JSON frame documented in
``SubnetManager``. Inbound heartbeat frames refresh Redis ``alive`` TTL via
implicit heartbeat when ``SubnetManager`` receives ``agent_service`` from
lifespan wiring.
"""

import structlog
from fastapi import APIRouter, Depends, WebSocket

from ..infrastructure.messaging.subnet_manager import SubnetManager
from .dependencies import AgentIdPath, SubnetIdPath, get_subnet_manager

logger = structlog.get_logger()

router = APIRouter(tags=["gateway"])


def _extract_gateway_credentials(websocket: WebSocket) -> dict | None:
    """Build the ``credentials`` dict for non-public subnets from the WS handshake.

    ``SubnetManager.validate_credentials`` accepts:

    - ``{"token": "..."}``      — bearer scheme.
    - ``{"api_key": "..."}``    — apiKey scheme.
    - ``{"access_token": "..."}`` — OAuth scheme.

    Browser ``new WebSocket()`` cannot set arbitrary headers, but proxies,
    server-side clients (``httpx_ws``, ``websockets``, ``aiohttp``), and
    SDKs all can. Supporting both ``Authorization: Bearer …`` and the
    ``X-Api-Key`` header keeps the WS gateway accessible from every
    non-browser caller without leaking the secret into the URL (which
    proxies and access logs may capture).

    Returns ``None`` when neither header is present.  Public-subnet
    flows still forward ``None`` to ``handle_connection``; the manager
    then allows the connection but the ``agent_id`` in the URL is
    unverified (known limitation — see threat model in ADR-0006).
    """
    headers = websocket.headers
    auth = headers.get("authorization") or headers.get("Authorization")
    if auth:
        auth = auth.strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token:
                return {"token": token}

    api_key = headers.get("x-api-key") or headers.get("X-Api-Key")
    if api_key:
        api_key = api_key.strip()
        if api_key:
            return {"api_key": api_key}

    return None


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

    Non-public subnets require credentials in the WebSocket handshake;
    we accept ``Authorization: Bearer …`` (bearer scheme) or
    ``X-Api-Key: …`` (apiKey scheme). Without this hand-off,
    non-public subnets reject every connection at
    ``validate_credentials`` because no caller can ever supply
    credentials over the WS path.
    """
    credentials = _extract_gateway_credentials(websocket)
    if credentials is None:
        # No credentials supplied.  Non-public subnets will reject the
        # connection inside handle_connection.  Public subnets allow it,
        # but the agent_id in the URL is then unverified — log so ops
        # can detect abuse patterns.
        logger.info(
            "gateway_connect_unauthenticated",
            subnet_id=subnet_id,
            agent_id=agent_id,
        )
    await subnet_manager.handle_connection(
        websocket, subnet_id, agent_id, credentials=credentials
    )
