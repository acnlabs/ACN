"""Payment System API Routes"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ...payments import (
    PaymentCapability,
    PaymentTaskStatus,
    SupportedNetwork,
    SupportedPaymentMethod,
)
from ..dependencies import PaymentDiscoveryDep, PaymentTasksDep, RegistryDep

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


class PaymentCapabilityRequest(BaseModel):
    supported_methods: list[SupportedPaymentMethod]
    supported_networks: list[SupportedNetwork]
    wallet_address: str | None = None
    api_endpoint: str | None = None
    webhook_url: str | None = None


class CreatePaymentTaskRequest(BaseModel):
    from_agent: str
    to_agent: str
    amount: float
    currency: str
    payment_method: SupportedPaymentMethod
    network: SupportedNetwork
    description: str | None = None
    metadata: dict | None = None


@router.post("/{agent_id}/payment-capability")
async def set_payment_capability(
    agent_id: str,
    request: PaymentCapabilityRequest,
    registry: RegistryDep = None,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """Set payment capability for agent"""
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        capability = PaymentCapability(
            agent_id=agent_id,
            supported_methods=request.supported_methods,
            supported_networks=request.supported_networks,
            wallet_address=request.wallet_address,
            api_endpoint=request.api_endpoint,
            webhook_url=request.webhook_url,
        )

        await payment_discovery.register_capability(capability)
        return {"status": "registered", "agent_id": agent_id}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/{agent_id}/payment-capability")
async def get_payment_capability(
    agent_id: str,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """Get payment capability for agent"""
    capability = await payment_discovery.get_capability(agent_id)
    if not capability:
        raise HTTPException(status_code=404, detail="Payment capability not found")
    return capability


@router.get("/discover")
async def discover_payment_agents(
    method: SupportedPaymentMethod | None = None,
    network: SupportedNetwork | None = None,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """Discover agents with payment capabilities"""
    agents = await payment_discovery.discover_agents(method=method, network=network)
    return {"agents": agents, "count": len(agents)}


@router.post("/tasks")
async def create_payment_task(
    request: CreatePaymentTaskRequest,
    payment_tasks: PaymentTasksDep = None,
):
    """Create a payment task"""
    try:
        task_id = await payment_tasks.create_task(
            from_agent=request.from_agent,
            to_agent=request.to_agent,
            amount=request.amount,
            currency=request.currency,
            payment_method=request.payment_method,
            network=request.network,
            description=request.description,
            metadata=request.metadata,
        )

        return {"task_id": task_id, "status": "created"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/tasks/{task_id}")
async def get_payment_task(task_id: str, payment_tasks: PaymentTasksDep = None):
    """Get payment task status"""
    task = await payment_tasks.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Payment task not found")
    return task


@router.get("/tasks/agent/{agent_id}")
async def get_agent_payment_tasks(
    agent_id: str,
    status: PaymentTaskStatus | None = None,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    payment_tasks: PaymentTasksDep = None,
):
    """Get payment tasks for agent"""
    tasks = await payment_tasks.get_agent_tasks(
        agent_id=agent_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"agent_id": agent_id, "tasks": tasks}


@router.get("/stats/{agent_id}")
async def get_agent_payment_stats(
    agent_id: str,
    payment_tasks: PaymentTasksDep = None,
):
    """Get payment statistics for agent"""
    stats = await payment_tasks.get_agent_stats(agent_id)
    return stats

