"""
ACN FastAPI Application

REST API for Agent Collaboration Network.

Provides:
- Layer 1: Agent registration, discovery, and management
- Layer 2: Message routing, broadcasting, and WebSocket
- Layer 3: Monitoring, metrics, and analytics

Based on A2A Protocol: https://github.com/a2aproject/A2A
"""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .auth.middleware import get_subject, require_permission, verify_token
from .communication import (
    BroadcastService,
    BroadcastStrategy,
    MessageRouter,
    SubnetManager,
    WebSocketManager,
    create_data_message,
    create_notification_message,
    create_text_message,
)
from .config import get_settings
from .models import (
    AgentInfo,
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentSearchResponse,
    SubnetCreateRequest,
    SubnetCreateResponse,
)
from .monitoring import Analytics, AuditLogger, MetricsCollector
from .payments import (
    PaymentCapability,
    PaymentDiscoveryService,
    PaymentTaskManager,
    PaymentTaskStatus,
    SupportedNetwork,
    SupportedPaymentMethod,
    WebhookService,
    create_webhook_config_from_settings,
)
from .registry import AgentRegistry

# Settings
settings = get_settings()

# Global instances
registry: AgentRegistry = None  # type: ignore
router: MessageRouter = None  # type: ignore
broadcast: BroadcastService = None  # type: ignore
ws_manager: WebSocketManager = None  # type: ignore
subnet_manager: SubnetManager = None  # type: ignore
metrics: MetricsCollector = None  # type: ignore
audit: AuditLogger = None  # type: ignore
analytics: Analytics = None  # type: ignore

# Layer 4: Payments
payment_discovery: PaymentDiscoveryService = None  # type: ignore
payment_tasks: PaymentTaskManager = None  # type: ignore
webhook_service: WebhookService = None  # type: ignore


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager"""
    global registry, router, broadcast, ws_manager, subnet_manager
    global metrics, audit, analytics
    global payment_discovery, payment_tasks, webhook_service

    # Startup
    registry = AgentRegistry(settings.redis_url)
    router = MessageRouter(registry, registry.redis)
    broadcast = BroadcastService(router, registry.redis)
    ws_manager = WebSocketManager(registry.redis)
    subnet_manager = SubnetManager(
        registry=registry,
        redis_client=registry.redis,
        gateway_base_url=settings.gateway_base_url,
    )

    # Layer 3: Monitoring
    metrics = MetricsCollector(registry.redis)
    audit = AuditLogger(registry.redis)
    analytics = Analytics(registry.redis)

    # Layer 4: Payments (AP2 integration)
    payment_discovery = PaymentDiscoveryService(registry.redis)

    # Webhook for backend integration
    webhook_config = create_webhook_config_from_settings(settings)
    if webhook_config:
        webhook_service = WebhookService(registry.redis, webhook_config)
        await webhook_service.start()
        print(f"   Webhook: {webhook_config.url}")
    else:
        webhook_service = None

    payment_tasks = PaymentTaskManager(registry.redis, payment_discovery, webhook_service)

    # Start managers
    await ws_manager.start()
    await subnet_manager.start()
    await metrics.start()
    await audit.start()

    print("✅ ACN Service started")
    print(f"   Redis: {settings.redis_url}")
    print(f"   Gateway: {settings.gateway_base_url}")
    print(f"   API Docs: http://{settings.host}:{settings.port}/docs")
    print("   Layers: Registry ✓ | Communication ✓ | Gateway ✓ | Monitoring ✓ | Payments ✓")

    yield

    # Shutdown
    if webhook_service:
        await webhook_service.stop()
    await audit.stop()
    await metrics.stop()
    await subnet_manager.stop()
    await ws_manager.stop()
    await registry.redis.close()
    print("👋 ACN Service stopped")


# Create FastAPI app
app = FastAPI(
    title="ACN - Agent Collaboration Network",
    description="Open-source infrastructure for AI Agent registration and discovery",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify allowed origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "ACN - Agent Collaboration Network",
        "version": "0.1.0",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    """Health check"""
    return {"status": "healthy"}


@app.post("/api/v1/agents/register", response_model=AgentRegisterResponse)
async def register_agent(
    request: AgentRegisterRequest,
    payload: dict = Depends(require_permission("acn:write")),
):
    """
    Register an Agent (Idempotent) - Requires Auth0 Token with acn:write permission

    Registers an Agent to ACN and stores its Agent Card.

    Features:
    - Auto-generates Agent Card if not provided (supports any framework)
    - Can join multiple subnets (default: ["public"])
    - Validates Agent Card format
    - **Idempotent**: Multiple registrations with same owner + endpoint will update, not duplicate

    Idempotency:
    ACN automatically handles idempotent registration based on natural keys:
    - If same owner + endpoint exists: Updates existing agent (ID unchanged)
    - If new endpoint: Creates new agent with generated UUID
    - For explicit control: Provide agent_id or external_id

    Agent Card Auto-Generation:
    If your agent doesn't have an A2A Agent Card, ACN will automatically
    generate one based on the provided name, endpoint, and skills.

    Multi-Subnet Support:
    An agent can belong to multiple subnets simultaneously.
    Use `subnet_ids: ["public", "team-a", "team-b"]` to join multiple networks.
    """
    # Extract owner from Auth0 token
    token_owner = await get_subject()
    
    # Validate owner: must match token owner or be explicitly allowed
    if request.owner != token_owner:
        # Check if token has admin permission to register for others
        permissions = payload.get("permissions", []) or payload.get("scope", "").split()
        if "acn:admin" not in permissions:
            raise HTTPException(
                status_code=403,
                detail=f"Cannot register agent for owner '{request.owner}'. Token owner is '{token_owner}'.",
            )
    
    # Get effective subnet IDs (supports both old and new format)
    subnet_ids = request.get_subnet_ids()

    # Validate all subnets exist
    for subnet_id in subnet_ids:
        if subnet_id != "public" and not subnet_manager.subnet_exists(subnet_id):
            raise HTTPException(
                status_code=400,
                detail=f"Subnet not found: {subnet_id}. Create it first via POST /api/v1/subnets",
            )

    try:
        # Register agent (idempotent based on owner + endpoint)
        agent_id = await registry.register_agent(
            owner=request.owner,
            name=request.name,
            endpoint=request.endpoint,
            skills=request.skills,
            agent_id=request.agent_id,
            external_id=request.external_id,
            agent_card=request.agent_card,
            subnet_ids=subnet_ids,
        )

        return AgentRegisterResponse(
            status="registered",
            agent_id=agent_id,
            agent_card_url=f"/api/v1/agents/{agent_id}/.well-known/agent-card.json",
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}") from e


@app.get("/api/v1/agents/{agent_id}", response_model=AgentInfo)
async def get_agent(agent_id: str):
    """
    Get Agent information

    Returns detailed information about an Agent.
    """
    agent_info = await registry.get_agent(agent_id)

    if not agent_info:
        raise HTTPException(status_code=404, detail="Agent not found")

    return agent_info


@app.get("/api/v1/agents/{agent_id}/.well-known/agent-card.json")
async def get_agent_card(agent_id: str):
    """
    Get Agent Card (A2A standard endpoint)

    Returns the A2A-compliant Agent Card.
    """
    agent_card = await registry.get_agent_card(agent_id)

    if not agent_card:
        raise HTTPException(status_code=404, detail="Agent Card not found")

    return agent_card


@app.get("/api/v1/agents", response_model=AgentSearchResponse)
async def search_agents(
    skills: str = None,
    status: str = "online",
    owner: str = None,
    name: str = None,
):
    """
    Search Agents

    Search Agents by skills, status, owner, and name.

    Query Parameters:
    - skills: Comma-separated skill IDs (e.g., "task-planning,coding")
    - status: Agent status filter (default: "online")
    - owner: Owner filter (e.g., "system", "user-123", "provider-x")
    - name: Name filter (partial match, case-insensitive)
    """
    skill_list = skills.split(",") if skills else None

    agents = await registry.search_agents(
        skills=skill_list,
        status=status,
        owner=owner,
        name=name,
    )

    return AgentSearchResponse(agents=agents, total=len(agents))


@app.delete("/api/v1/agents/{agent_id}")
async def unregister_agent(agent_id: str):
    """
    Unregister an Agent

    Removes an Agent from ACN.
    """
    success = await registry.unregister_agent(agent_id)

    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {"status": "unregistered", "agent_id": agent_id}


@app.post("/api/v1/agents/{agent_id}/heartbeat")
async def agent_heartbeat(agent_id: str):
    """
    Agent Heartbeat

    Updates Agent's last heartbeat timestamp.
    """
    success = await registry.heartbeat(agent_id)

    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {"status": "ok", "agent_id": agent_id}


@app.patch("/api/v1/agents/{agent_id}/status")
async def update_agent_status(
    agent_id: str, status: str = Query(..., regex="^(online|offline|busy)$")
):
    """
    Update Agent Status

    Updates an Agent's status (online, offline, busy).
    """
    # Check agent exists
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Update status via heartbeat with status update
    await registry.redis.hset(
        f"acn:agents:{agent_id}",
        "status",
        status,
    )

    return {"status": "updated", "agent_id": agent_id, "new_status": status}


# =============================================================================
# Agent Subnet Membership API
# =============================================================================


@app.post("/api/v1/agents/{agent_id}/subnets/{subnet_id}")
async def join_subnet(agent_id: str, subnet_id: str):
    """
    Join a Subnet

    Add an agent to a subnet. Agents can belong to multiple subnets.
    """
    # Check agent exists
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check subnet exists
    if subnet_id != "public" and not subnet_manager.subnet_exists(subnet_id):
        raise HTTPException(
            status_code=400,
            detail=f"Subnet not found: {subnet_id}",
        )

    success = await registry.add_agent_to_subnet(agent_id, subnet_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to join subnet")

    # Get updated agent info
    agent = await registry.get_agent(agent_id)
    return {
        "status": "joined",
        "agent_id": agent_id,
        "subnet_id": subnet_id,
        "current_subnets": agent.subnet_ids if agent else [subnet_id],
    }


@app.delete("/api/v1/agents/{agent_id}/subnets/{subnet_id}")
async def leave_subnet(agent_id: str, subnet_id: str):
    """
    Leave a Subnet

    Remove an agent from a subnet. Agent will always remain in at least 'public'.
    """
    # Check agent exists
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Cannot leave public if it's the only subnet
    if subnet_id == "public" and len(agent.subnet_ids) == 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot leave public subnet when it's the only subnet",
        )

    success = await registry.remove_agent_from_subnet(agent_id, subnet_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to leave subnet")

    # Get updated agent info
    agent = await registry.get_agent(agent_id)
    return {
        "status": "left",
        "agent_id": agent_id,
        "subnet_id": subnet_id,
        "current_subnets": agent.subnet_ids if agent else ["public"],
    }


@app.get("/api/v1/agents/{agent_id}/subnets")
async def get_agent_subnets(agent_id: str):
    """
    Get Agent's Subnets

    Returns all subnets an agent belongs to.
    """
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {
        "agent_id": agent_id,
        "subnet_ids": agent.subnet_ids,
        "count": len(agent.subnet_ids),
    }


@app.get("/api/v1/stats")
async def get_stats():
    """
    Get ACN Statistics

    Returns registry statistics including agent counts.
    """
    # Get all agent IDs
    all_agents = await registry.redis.smembers("acn:agents:all")

    # Count by status
    online_count = 0
    offline_count = 0
    busy_count = 0

    for agent_id in all_agents:
        data = await registry.redis.hget(f"acn:agents:{agent_id}", "status")
        if data == "online":
            online_count += 1
        elif data == "offline":
            offline_count += 1
        elif data == "busy":
            busy_count += 1

    return {
        "total_agents": len(all_agents),
        "online_agents": online_count,
        "offline_agents": offline_count,
        "busy_agents": busy_count,
    }


@app.get("/api/v1/skills")
async def list_skills():
    """
    List All Skills

    Returns all registered skills and their agent counts.
    """
    # Get all skill keys
    skill_keys = await registry.redis.keys("acn:skills:*")

    skills = {}
    for key in skill_keys:
        skill_name = key.replace("acn:skills:", "")
        agent_count = await registry.redis.scard(key)
        skills[skill_name] = agent_count

    return {
        "skills": skills,
        "total_skills": len(skills),
    }


@app.get("/api/v1/agents/{agent_id}/endpoint")
async def get_agent_endpoint(agent_id: str):
    """
    Get Agent Endpoint

    Returns just the A2A endpoint for an Agent.
    Useful for A2A SDK integration.
    """
    agent = await registry.get_agent(agent_id)

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {
        "agent_id": agent_id,
        "endpoint": agent.endpoint,
        "status": agent.status,
    }


# =============================================================================
# Layer 2: Communication API
# =============================================================================


class SendMessageRequest(BaseModel):
    """Request to send A2A message"""

    from_agent: str
    to_agent: str
    content: str
    data: dict | None = None
    notification_type: str | None = None


class BroadcastRequest(BaseModel):
    """Request to broadcast message"""

    from_agent: str
    to_agents: list[str]
    content: str
    data: dict | None = None
    notification_type: str | None = None
    strategy: str | None = "parallel"


class BroadcastBySkillRequest(BaseModel):
    """Request to broadcast by skill"""

    from_agent: str
    skills: list[str]
    content: str
    data: dict | None = None
    notification_type: str | None = None
    status_filter: str | None = "online"


@app.post("/api/v1/communication/send")
async def send_message(request: SendMessageRequest):
    """
    Send A2A Message

    Routes a message from one agent to another using A2A protocol.
    Uses ACN Registry for endpoint discovery.
    """
    try:
        # Build A2A message using helper functions
        if request.notification_type:
            message = create_notification_message(
                notification_type=request.notification_type,
                content=request.content,
                metadata=request.data or {},
            )
        elif request.data:
            message = create_data_message(
                data=request.data,
                text=request.content,
            )
        else:
            message = create_text_message(request.content)

        # Route message
        response = await router.route(
            from_agent=request.from_agent,
            to_agent=request.to_agent,
            message=message,
        )

        # Serialize response
        if hasattr(response, "model_dump"):
            response_data = response.model_dump()
        else:
            response_data = response

        return {
            "status": "delivered",
            "from_agent": request.from_agent,
            "to_agent": request.to_agent,
            "response": response_data,
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delivery failed: {str(e)}") from e


@app.post("/api/v1/communication/broadcast")
async def broadcast_message(request: BroadcastRequest):
    """
    Broadcast Message

    Sends a message to multiple agents simultaneously.
    """
    try:
        # Build A2A message using helper functions
        if request.notification_type:
            message = create_notification_message(
                notification_type=request.notification_type,
                content=request.content,
                metadata=request.data or {},
            )
        elif request.data:
            message = create_data_message(
                data=request.data,
                text=request.content,
            )
        else:
            message = create_text_message(request.content)

        # Determine strategy
        strategy = BroadcastStrategy(request.strategy or "parallel")

        # Broadcast
        result = await broadcast.send(
            from_agent=request.from_agent,
            to_agents=request.to_agents,
            message=message,
            strategy=strategy,
        )

        return result.to_dict()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Broadcast failed: {str(e)}") from e


@app.post("/api/v1/communication/broadcast-by-skill")
async def broadcast_by_skill(request: BroadcastBySkillRequest):
    """
    Broadcast by Skill

    Broadcasts a message to all agents with specified skills.
    """
    try:
        # Build A2A message using helper functions
        if request.notification_type:
            message = create_notification_message(
                notification_type=request.notification_type,
                content=request.content,
                metadata=request.data or {},
            )
        elif request.data:
            message = create_data_message(
                data=request.data,
                text=request.content,
            )
        else:
            message = create_text_message(request.content)

        # Broadcast by skill
        result = await broadcast.send_by_skill(
            from_agent=request.from_agent,
            skills=request.skills,
            message=message,
            status_filter=request.status_filter,
        )

        return result.to_dict()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Broadcast failed: {str(e)}") from e


@app.get("/api/v1/communication/history/{agent_id}")
async def get_message_history(
    agent_id: str,
    limit: int = Query(default=100, le=500),
):
    """
    Get Message History

    Returns recent message history for an agent.
    """
    history = await router.get_message_history(agent_id, limit=limit)

    return {
        "agent_id": agent_id,
        "messages": history,
        "count": len(history),
    }


@app.post("/api/v1/communication/retry-dlq")
async def retry_dead_letter_queue(max_retries: int = Query(default=3, le=10)):
    """
    Retry Dead Letter Queue

    Retries failed messages in the dead letter queue.
    """
    success_count = await router.retry_dlq(max_retries=max_retries)

    return {
        "status": "completed",
        "retried_successfully": success_count,
    }


# =============================================================================
# WebSocket API
# =============================================================================


@app.websocket("/ws/{channel}")
async def websocket_endpoint(websocket: WebSocket, channel: str):
    """
    WebSocket Connection

    Connect to receive real-time updates for a channel.

    Channels:
    - chat:{chat_id} - Chat messages
    - agent:{agent_id} - Agent status updates

    Message Types:
    - message: Chat message
    - agent_status: Agent status change
    - agent_typing: Agent typing indicator
    - system: System notification
    """
    # Get user_id from query params
    user_id = websocket.query_params.get("user_id")

    # Connect
    conn_id = await ws_manager.connect(
        websocket=websocket,
        user_id=user_id,
        metadata={"initial_channel": channel},
    )

    # Subscribe to channel
    await ws_manager.subscribe(conn_id, channel)

    try:
        while True:
            # Receive and handle messages
            data = await websocket.receive_json()
            await ws_manager.handle_message(conn_id, data)

    except WebSocketDisconnect:
        await ws_manager.disconnect(conn_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        await ws_manager.disconnect(conn_id)


@app.get("/api/v1/websocket/stats")
async def get_websocket_stats():
    """
    Get WebSocket Statistics

    Returns current WebSocket connection statistics.
    """
    return ws_manager.get_stats()


@app.post("/api/v1/websocket/broadcast/{channel}")
async def websocket_broadcast(channel: str, message: dict):
    """
    Broadcast to WebSocket Channel

    Sends a message to all connections subscribed to a channel.
    Used by services to push real-time updates.
    """
    await ws_manager.broadcast(channel, message)

    return {
        "status": "broadcast_sent",
        "channel": channel,
    }


# =============================================================================
# Subnet Management API
# =============================================================================


@app.post("/api/v1/subnets", response_model=SubnetCreateResponse)
async def create_subnet(request: SubnetCreateRequest):
    """
    Create Subnet with A2A-style Security

    Creates a new subnet for agents to connect to.

    Security Options (A2A securitySchemes format):
    - No security_schemes = Public subnet (anyone can join)
    - Bearer token = {"bearer": {"type": "http", "scheme": "bearer"}}
    - API Key = {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Key"}}
    - OAuth = {"oauth": {"type": "openIdConnect", "openIdConnectUrl": "..."}}

    Examples:
        # Public subnet
        {"subnet_id": "demo", "name": "Demo"}

        # Private subnet with bearer token
        {
            "subnet_id": "team-a",
            "name": "Team A",
            "security_schemes": {
                "bearer": {"type": "http", "scheme": "bearer"}
            }
        }
    """
    try:
        info, generated_token = await subnet_manager.create_subnet(
            subnet_id=request.subnet_id,
            name=request.name,
            description=request.description,
            security_schemes=request.security_schemes,
            default_security=request.default_security,
            metadata=request.metadata,
        )

        is_public = request.security_schemes is None

        return SubnetCreateResponse(
            status="created",
            subnet_id=info.subnet_id,
            is_public=is_public,
            security_schemes=request.security_schemes,
            gateway_ws_url=f"{settings.gateway_base_url}/gateway/connect/{info.subnet_id}/{{agent_id}}",
            gateway_a2a_url=f"{settings.gateway_base_url}/gateway/a2a/{info.subnet_id}/{{agent_id}}",
            generated_token=generated_token,  # Only for bearer/apiKey
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/api/v1/subnets")
async def list_subnets():
    """
    List Subnets

    Returns all available subnets.
    """
    subnets = subnet_manager.list_subnets()
    return {
        "subnets": [s.model_dump() for s in subnets],
        "total": len(subnets),
    }


@app.get("/api/v1/subnets/{subnet_id}")
async def get_subnet(subnet_id: str):
    """
    Get Subnet

    Returns information about a specific subnet.
    """
    info = subnet_manager.get_subnet(subnet_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"Subnet not found: {subnet_id}")

    agents = subnet_manager.get_subnet_agents(subnet_id)
    return {
        **info.model_dump(),
        "agents": agents,
        "agent_count": len(agents),
    }


@app.delete("/api/v1/subnets/{subnet_id}")
async def delete_subnet(subnet_id: str, force: bool = Query(default=False)):
    """
    Delete Subnet

    Deletes a subnet. Use force=true to disconnect all agents first.
    """
    try:
        await subnet_manager.delete_subnet(subnet_id, force=force)
        return {"status": "deleted", "subnet_id": subnet_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/api/v1/subnets/{subnet_id}/agents")
async def get_subnet_agents(subnet_id: str):
    """
    List Subnet Agents

    Returns all agents connected to a specific subnet.
    """
    if not subnet_manager.subnet_exists(subnet_id):
        raise HTTPException(status_code=404, detail=f"Subnet not found: {subnet_id}")

    agents = subnet_manager.get_subnet_agents(subnet_id)
    return {
        "subnet_id": subnet_id,
        "agents": agents,
        "count": len(agents),
    }


# =============================================================================
# Gateway API (Multi-Subnet)
# =============================================================================


@app.websocket("/gateway/connect/{subnet_id}/{agent_id}")
async def gateway_agent_connect(websocket: WebSocket, subnet_id: str, agent_id: str):
    """
    Gateway WebSocket Connection (Multi-Subnet) with A2A Security

    Endpoint for agents to connect to a subnet via gateway.

    Authentication (for non-public subnets):
    - Pass credentials via query params:
      /gateway/connect/{subnet_id}/{agent_id}?token=sk_subnet_xxx
      /gateway/connect/{subnet_id}/{agent_id}?api_key=ak_subnet_xxx
      /gateway/connect/{subnet_id}/{agent_id}?access_token=oauth_token

    Protocol:
    1. Connect via WebSocket with credentials (if subnet requires auth)
    2. Send REGISTER message with agent_info
    3. Receive REGISTER_ACK with gateway_endpoint
    4. Receive A2A_REQUEST messages, respond with A2A_RESPONSE
    5. Send HEARTBEAT periodically to stay connected

    Message Types:
    - register: {"type": "register", "agent_info": {...}}
    - register_ack: {"type": "register_ack", "subnet_id": "...", "gateway_endpoint": "..."}
    - a2a_request: {"type": "a2a_request", "request_id": "...", "message": {...}}
    - a2a_response: {"type": "a2a_response", "request_id": "...", "response": {...}}
    - heartbeat: {"type": "heartbeat"}
    - heartbeat_ack: {"type": "heartbeat_ack"}
    - error: {"type": "error", "error": "..."}
    """
    # Extract credentials from query params
    credentials = {}
    if websocket.query_params.get("token"):
        credentials["token"] = websocket.query_params.get("token")
    if websocket.query_params.get("api_key"):
        credentials["api_key"] = websocket.query_params.get("api_key")
    if websocket.query_params.get("access_token"):
        credentials["access_token"] = websocket.query_params.get("access_token")

    await subnet_manager.handle_connection(
        websocket, subnet_id, agent_id, credentials=credentials if credentials else None
    )


@app.post("/gateway/a2a/{subnet_id}/{agent_id}")
async def gateway_a2a_forward(subnet_id: str, agent_id: str, message: dict):
    """
    Gateway A2A Forward (Multi-Subnet)

    Endpoint for external agents to send A2A messages to subnet agents.
    The gateway forwards the message via WebSocket and returns the response.

    This is the public endpoint that gets registered in ACN for subnet agents.
    """
    try:
        response = await subnet_manager.forward_request(subnet_id, agent_id, message)
        return response
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e)) from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from None


@app.get("/api/v1/gateway/stats")
async def get_gateway_stats():
    """
    Get Gateway Statistics

    Returns information about all subnets and connected agents.
    """
    return subnet_manager.get_stats()


@app.get("/api/v1/gateway/agents")
async def get_gateway_agents():
    """
    List All Gateway Agents

    Returns all agents connected via gateway, grouped by subnet.
    """
    agents_by_subnet = subnet_manager.get_all_agents()
    total = sum(len(agents) for agents in agents_by_subnet.values())
    return {
        "agents_by_subnet": agents_by_subnet,
        "total": total,
    }


@app.get("/api/v1/gateway/agents/{subnet_id}/{agent_id}")
async def get_gateway_agent_info(subnet_id: str, agent_id: str):
    """
    Get Gateway Agent Info

    Returns connection info for a specific gateway agent.
    """
    info = subnet_manager.get_connection_info(subnet_id, agent_id)
    if not info:
        raise HTTPException(
            status_code=404,
            detail=f"Agent not connected: {subnet_id}/{agent_id}",
        )
    return info


# =============================================================================
# Layer 3: Monitoring & Analytics API
# =============================================================================


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """
    Prometheus Metrics Endpoint

    Returns metrics in Prometheus exposition format for scraping.
    """
    return await metrics.prometheus_export()


@app.get("/api/v1/monitoring/metrics")
async def get_all_metrics():
    """
    Get All Metrics

    Returns all ACN metrics as JSON.
    """
    return await metrics.get_all_metrics()


@app.get("/api/v1/monitoring/health")
async def get_system_health():
    """
    Get System Health

    Returns overall system health status with score and issues.
    """
    return await analytics.get_system_health()


@app.get("/api/v1/monitoring/dashboard")
async def get_dashboard_data():
    """
    Get Dashboard Data

    Returns comprehensive data for monitoring dashboard.
    """
    return await analytics.get_dashboard_data()


# Agent Analytics
@app.get("/api/v1/analytics/agents")
async def get_agent_analytics():
    """
    Get Agent Analytics

    Returns agent statistics including counts by status, subnet, and skill.
    """
    return await analytics.get_agent_stats()


@app.get("/api/v1/analytics/agents/{agent_id}")
async def get_agent_activity(
    agent_id: str,
    hours: int = Query(default=24, ge=1, le=720),
):
    """
    Get Agent Activity

    Returns activity statistics for a specific agent.
    """
    return await analytics.get_agent_activity(agent_id, hours)


# Message Analytics
@app.get("/api/v1/analytics/messages")
async def get_message_analytics():
    """
    Get Message Analytics

    Returns message statistics including success rate and volume.
    """
    return await analytics.get_message_stats()


@app.get("/api/v1/analytics/latency")
async def get_latency_analytics():
    """
    Get Latency Analytics

    Returns latency statistics by operation type.
    """
    return await analytics.get_latency_stats()


# Subnet Analytics
@app.get("/api/v1/analytics/subnets")
async def get_subnet_analytics():
    """
    Get Subnet Analytics

    Returns statistics for all subnets.
    """
    return await analytics.get_subnet_stats()


# Audit Logs
class AuditQueryRequest(BaseModel):
    """Request model for audit log queries"""

    event_type: str | None = None
    actor_id: str | None = None
    target_id: str | None = None
    subnet_id: str | None = None
    level: str | None = None
    limit: int = 100
    offset: int = 0


@app.get("/api/v1/audit/events")
async def get_audit_events(
    event_type: str | None = None,
    actor_id: str | None = None,
    target_id: str | None = None,
    subnet_id: str | None = None,
    level: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """
    Query Audit Events

    Returns audit events matching the specified criteria.
    """
    from .monitoring.audit import AuditEventType, AuditLevel

    # Convert string to enum if provided
    event_type_enum = None
    if event_type:
        try:
            event_type_enum = AuditEventType(event_type)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid event_type: {event_type}",
            ) from None

    level_enum = None
    if level:
        try:
            level_enum = AuditLevel(level)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid level: {level}",
            ) from None

    events = await audit.query_events(
        event_type=event_type_enum,
        actor_id=actor_id,
        target_id=target_id,
        subnet_id=subnet_id,
        level=level_enum,
        limit=limit,
        offset=offset,
    )

    return {
        "events": [e.model_dump() for e in events],
        "count": len(events),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/v1/audit/events/recent")
async def get_recent_audit_events(
    limit: int = Query(default=50, ge=1, le=500),
):
    """
    Get Recent Audit Events

    Returns the most recent audit events.
    """
    events = await audit.get_recent_events(limit=limit)
    return {
        "events": [e.model_dump() for e in events],
        "count": len(events),
    }


@app.get("/api/v1/audit/stats")
async def get_audit_stats(
    start_time: str | None = None,
    end_time: str | None = None,
):
    """
    Get Audit Statistics

    Returns statistics about audit events by type, level, and subnet.
    """
    start_dt = datetime.fromisoformat(start_time) if start_time else None
    end_dt = datetime.fromisoformat(end_time) if end_time else None

    return await audit.get_event_stats(start_time=start_dt, end_time=end_dt)


@app.get("/api/v1/audit/export")
async def export_audit_events(
    start_time: str | None = None,
    end_time: str | None = None,
    format: str = Query(default="json", regex="^(json|csv)$"),
):
    """
    Export Audit Events

    Exports audit events in JSON or CSV format.
    """
    start_dt = datetime.fromisoformat(start_time) if start_time else None
    end_dt = datetime.fromisoformat(end_time) if end_time else None

    data = await audit.export_events(
        start_time=start_dt,
        end_time=end_dt,
        format=format,
    )

    if format == "csv":
        return PlainTextResponse(content=data, media_type="text/csv")
    return PlainTextResponse(content=data, media_type="application/json")


# Reports
@app.get("/api/v1/reports/{report_type}")
async def generate_report(
    report_type: str,
    start_date: str | None = None,
    end_date: str | None = None,
):
    """
    Generate Report

    Generates a report for the specified period.
    Report types: daily, weekly, monthly
    """
    if report_type not in ["daily", "weekly", "monthly"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid report_type: {report_type}. Must be daily, weekly, or monthly.",
        )

    start_dt = datetime.fromisoformat(start_date) if start_date else None
    end_dt = datetime.fromisoformat(end_date) if end_date else None

    return await analytics.generate_report(
        report_type=report_type,
        start_date=start_dt,
        end_date=end_dt,
    )


# =============================================================================
# Layer 4: Payments API (AP2 Integration - ACN Unique Value)
# =============================================================================


class PaymentCapabilityRequest(BaseModel):
    """Request to set payment capability"""

    accepts_payment: bool = True
    payment_methods: list[str]  # ["usdc", "eth", "credit_card"]
    wallet_address: str | None = None
    supported_networks: list[str] | None = None  # ["base", "ethereum"]
    pricing: dict[str, str] | None = None  # {"coding": "50.00"}


class CreatePaymentTaskRequest(BaseModel):
    """Request to create a payment task"""

    buyer_agent: str
    seller_agent: str
    task_description: str
    amount: str
    currency: str = "USD"
    payment_method: str | None = None
    task_type: str | None = None
    metadata: dict | None = None


@app.post("/api/v1/agents/{agent_id}/payment-capability")
async def set_payment_capability(agent_id: str, request: PaymentCapabilityRequest):
    """
    Set Payment Capability for an Agent (ACN Unique Value)

    This allows other agents to discover this agent by payment capability.
    Example: "Find all agents accepting USDC on Base network"
    """
    # Verify agent exists
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Parse payment methods
    try:
        methods = [SupportedPaymentMethod(m) for m in request.payment_methods]
        networks = [SupportedNetwork(n) for n in (request.supported_networks or [])]
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid payment method or network: {e}"
        ) from None

    # Create capability
    capability = PaymentCapability(
        accepts_payment=request.accepts_payment,
        payment_methods=methods,
        wallet_address=request.wallet_address,
        supported_networks=networks,
        pricing=request.pricing or {},
    )

    # Index for discovery
    await payment_discovery.index_payment_capability(agent_id, capability)

    # Update agent info
    await registry.redis.hset(
        f"acn:agents:{agent_id}",
        mapping={
            "accepts_payment": "true" if request.accepts_payment else "false",
            "wallet_address": request.wallet_address or "",
            "payment_methods": ",".join(request.payment_methods),
        },
    )

    return {
        "status": "updated",
        "agent_id": agent_id,
        "payment_capability": capability.model_dump(),
    }


@app.get("/api/v1/agents/{agent_id}/payment-capability")
async def get_payment_capability(agent_id: str):
    """
    Get Payment Capability for an Agent

    Returns the agent's payment configuration.
    """
    capability = await payment_discovery.get_agent_payment_capability(agent_id)

    if not capability:
        return {
            "agent_id": agent_id,
            "accepts_payment": False,
            "payment_capability": None,
        }

    return {
        "agent_id": agent_id,
        "accepts_payment": capability.accepts_payment,
        "payment_capability": capability.model_dump(),
    }


@app.get("/api/v1/payments/discover")
async def discover_payment_agents(
    payment_method: str | None = Query(None, description="Payment method (usdc, eth, etc.)"),
    network: str | None = Query(None, description="Blockchain network (base, ethereum, etc.)"),
    currency: str | None = Query(None, description="Currency (USD, USDC, etc.)"),
):
    """
    Discover Agents by Payment Capability (ACN Unique Value)

    Find agents that accept specific payment methods.
    This is something AP2 alone cannot do - it requires ACN's registry.

    Examples:
    - GET /api/v1/payments/discover?payment_method=usdc
    - GET /api/v1/payments/discover?network=base
    - GET /api/v1/payments/discover?payment_method=usdc&network=base
    """
    try:
        method = SupportedPaymentMethod(payment_method) if payment_method else None
        net = SupportedNetwork(network) if network else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid parameter: {e}") from None

    agent_ids = await payment_discovery.find_agents_accepting_payment(
        payment_method=method,
        network=net,
        currency=currency,
    )

    # Get agent details
    agents = []
    for agent_id in agent_ids:
        agent = await registry.get_agent(agent_id)
        capability = await payment_discovery.get_agent_payment_capability(agent_id)
        if agent and capability:
            agents.append(
                {
                    "agent_id": agent_id,
                    "name": agent.name,
                    "skills": agent.skills,
                    "endpoint": agent.endpoint,
                    "payment_capability": capability.model_dump(),
                }
            )

    return {
        "agents": agents,
        "count": len(agents),
        "filters": {
            "payment_method": payment_method,
            "network": network,
            "currency": currency,
        },
    }


@app.post("/api/v1/payments/tasks")
async def create_payment_task(request: CreatePaymentTaskRequest):
    """
    Create a Payment Task (ACN Unique Value - A2A + AP2 Fusion)

    Creates a task with associated payment in one request.
    Automatically resolves seller's wallet address from ACN Registry.

    This combines A2A task protocol with AP2 payment protocol.
    """
    try:
        method = SupportedPaymentMethod(request.payment_method) if request.payment_method else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid payment method: {e}") from None

    try:
        task = await payment_tasks.create_payment_task(
            buyer_agent=request.buyer_agent,
            seller_agent=request.seller_agent,
            task_description=request.task_description,
            amount=request.amount,
            currency=request.currency,
            payment_method=method,
            task_type=request.task_type,
            metadata=request.metadata,
        )

        return {
            "status": "created",
            "task": task.model_dump(),
            "a2a_message": payment_tasks.build_a2a_payment_message(task),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/api/v1/payments/tasks/{task_id}")
async def get_payment_task(task_id: str):
    """
    Get Payment Task Details

    Returns task details including payment status.
    """
    task = await payment_tasks.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "task": task.model_dump(),
        "a2a_message": payment_tasks.build_a2a_payment_message(task),
    }


@app.patch("/api/v1/payments/tasks/{task_id}/status")
async def update_payment_task_status(
    task_id: str,
    status: str = Query(..., description="New status"),
    tx_hash: str | None = Query(None, description="Blockchain transaction hash"),
):
    """
    Update Payment Task Status

    Updates the status of a payment task.
    """
    try:
        new_status = PaymentTaskStatus(status)
    except ValueError:
        valid = [s.value for s in PaymentTaskStatus]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status: {status}. Valid: {valid}",
        ) from None

    try:
        task = await payment_tasks.update_task_status(task_id, new_status, tx_hash)
        return {
            "status": "updated",
            "task": task.model_dump(),
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@app.get("/api/v1/payments/tasks/agent/{agent_id}")
async def get_agent_payment_tasks(
    agent_id: str,
    role: str = Query(default="all", pattern="^(buyer|seller|all)$"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Get Payment Tasks for an Agent

    Returns all payment tasks for an agent.
    """
    status_filter = None
    if status:
        try:
            status_filter = PaymentTaskStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}") from None

    tasks = await payment_tasks.get_tasks_by_agent(
        agent_id=agent_id,
        role=role,
        status=status_filter,
        limit=limit,
    )

    return {
        "agent_id": agent_id,
        "tasks": [t.model_dump() for t in tasks],
        "count": len(tasks),
    }


@app.get("/api/v1/payments/stats/{agent_id}")
async def get_agent_payment_stats(agent_id: str):
    """
    Get Payment Statistics for an Agent

    Returns payment statistics including total transactions,
    amounts as buyer/seller, and status breakdown.
    """
    stats = await payment_tasks.get_payment_stats(agent_id)
    return {
        "agent_id": agent_id,
        "stats": stats,
    }


# =============================================================================
# Webhooks API (Backend Integration)
# =============================================================================


@app.get("/api/v1/webhooks/config")
async def get_webhook_config():
    """
    Get Current Webhook Configuration

    Returns the configured webhook URL and settings.
    Useful for verifying webhook setup.
    """
    if not webhook_service or not webhook_service.default_config:
        return {
            "configured": False,
            "url": None,
            "message": "No webhook configured. Set WEBHOOK_URL environment variable.",
        }

    config = webhook_service.default_config
    return {
        "configured": True,
        "url": config.url,
        "timeout": config.timeout,
        "retry_count": config.retry_count,
        "retry_delay": config.retry_delay,
        "enabled": config.enabled,
        "has_secret": config.secret is not None,
    }


@app.get("/api/v1/webhooks/history")
async def get_webhook_history(
    task_id: str | None = Query(None, description="Filter by task ID"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Get Webhook Delivery History

    Returns recent webhook deliveries with their status.
    Useful for debugging failed deliveries.
    """
    if not webhook_service:
        return {
            "deliveries": [],
            "count": 0,
            "message": "Webhook not configured",
        }

    deliveries = await webhook_service.get_delivery_history(
        task_id=task_id,
        limit=limit,
    )

    return {
        "deliveries": [d.model_dump() for d in deliveries],
        "count": len(deliveries),
    }


@app.post("/api/v1/webhooks/retry/{delivery_id}")
async def retry_webhook_delivery(delivery_id: str):
    """
    Retry a Failed Webhook Delivery

    Manually retry a webhook that failed to deliver.
    """
    if not webhook_service:
        raise HTTPException(status_code=400, detail="Webhook not configured")

    try:
        success = await webhook_service.retry_failed_delivery(delivery_id)
        return {
            "delivery_id": delivery_id,
            "success": success,
            "message": "Delivery retried successfully" if success else "Delivery failed again",
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@app.post("/api/v1/webhooks/test")
async def test_webhook():
    """
    Send a Test Webhook

    Sends a test event to verify webhook connectivity.
    """
    if not webhook_service:
        raise HTTPException(status_code=400, detail="Webhook not configured")

    from .payments import WebhookEventType

    success = await webhook_service.send_event(
        event=WebhookEventType.TASK_CREATED,
        task_id="test-task-" + datetime.now().strftime("%Y%m%d%H%M%S"),
        data={
            "test": True,
            "message": "This is a test webhook from ACN",
        },
        buyer_agent="test-buyer",
        seller_agent="test-seller",
        amount="0.00",
        currency="TEST",
    )

    return {
        "success": success,
        "message": "Test webhook sent successfully" if success else "Test webhook failed",
    }
