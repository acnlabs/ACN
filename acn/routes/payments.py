"""Payment System API Routes"""

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..protocols.ap2 import (
    CREDITS_PER_USD,
    NETWORK_FEE_RATE,
    PaymentCapability,
    PaymentTaskStatus,
    SupportedNetwork,
    SupportedPaymentMethod,
    TokenPricing,
)
from ..services.billing_service import BillingTransactionStatus
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    BillingServiceDep,
    InternalTokenDep,
    PaymentDiscoveryDep,
    PaymentTasksDep,
    RegistryDep,
)

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])
logger = structlog.get_logger()


class PaymentCapabilityRequest(BaseModel):
    supported_methods: list[SupportedPaymentMethod]
    supported_networks: list[SupportedNetwork]
    wallet_address: str | None = None
    wallet_addresses: dict[str, str] = Field(
        default_factory=dict,
        description="Per-network wallet addresses, e.g. {'ethereum': '0x...', 'base': '0x...'}",
    )
    accepts_payment: bool = True
    token_pricing: dict | None = Field(
        default=None,
        description="Token-based pricing config, e.g. {'input_price_per_million': 2.5, 'output_price_per_million': 10.0, 'currency': 'USD'}",
    )
    api_endpoint: str | None = None
    webhook_url: str | None = None


class CreatePaymentTaskRequest(BaseModel):
    from_agent: str
    to_agent: str
    amount: float = Field(..., gt=0, description="Payment amount (must be positive)")
    currency: str
    payment_method: SupportedPaymentMethod
    network: SupportedNetwork
    description: str | None = None
    metadata: dict | None = None


# =============================================================================
# Token Billing Models
# =============================================================================


class TokenPricingRequest(BaseModel):
    """Request to set token-based pricing for an agent"""

    input_price_per_million: float = Field(..., ge=0, description="USD per 1M input tokens")
    output_price_per_million: float = Field(..., ge=0, description="USD per 1M output tokens")


class EstimateCostRequest(BaseModel):
    """Request to estimate cost for a service call"""

    agent_id: str
    estimated_input_tokens: int = Field(default=0, ge=0)
    estimated_output_tokens: int = Field(default=0, ge=0)


class BillUsageRequest(BaseModel):
    """Request to bill token usage after a service call"""

    user_id: str = Field(..., description="User being charged")
    agent_id: str = Field(..., description="Agent that provided service")
    task_id: str | None = Field(None, description="Associated task ID")
    input_tokens: int = Field(..., ge=0, description="Actual input tokens used")
    output_tokens: int = Field(..., ge=0, description="Actual output tokens used")


@router.post("/{agent_id}/payment-capability")
async def set_payment_capability(
    agent_id: str,
    request: PaymentCapabilityRequest,
    agent_info: AgentApiKeyDep,
    registry: RegistryDep = None,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """Set payment capability for agent (requires Agent API Key)

    The authenticated agent must match the path `agent_id`.
    Persists wallet_addresses and token_pricing to PostgreSQL and indexes in Redis.
    """
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        # Build wallet_addresses: merge request.wallet_addresses with legacy wallet_address
        wallet_addresses = dict(request.wallet_addresses)
        if request.wallet_address and "ethereum" not in wallet_addresses:
            wallet_addresses["ethereum"] = request.wallet_address

        # Sync back legacy field
        if not request.wallet_address and wallet_addresses:
            legacy_addr = (
                wallet_addresses.get("ethereum")
                or wallet_addresses.get("base")
                or next(iter(wallet_addresses.values()), None)
            )
        else:
            legacy_addr = request.wallet_address

        # Persist payment fields to Agent entity and save to PG
        agent.accepts_payment = request.accepts_payment
        agent.wallet_address = legacy_addr
        agent.wallet_addresses = wallet_addresses
        agent.token_pricing = request.token_pricing
        if request.supported_methods:
            agent.payment_methods = [m.value for m in request.supported_methods]
        await registry.save(agent)

        # Build TokenPricing for Redis discovery index
        token_pricing_obj = None
        if request.token_pricing:
            try:
                token_pricing_obj = TokenPricing(**request.token_pricing)
            except Exception:
                pass

        capability = PaymentCapability(
            agent_id=agent_id,
            accepts_payment=request.accepts_payment,
            supported_methods=request.supported_methods,
            supported_networks=request.supported_networks,
            wallet_address=legacy_addr,
            wallet_addresses=wallet_addresses,
            token_pricing=token_pricing_obj,
            api_endpoint=request.api_endpoint,
            webhook_url=request.webhook_url,
        )

        await payment_discovery.register_capability(capability)
        return {"status": "registered", "agent_id": agent_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "set_payment_capability_failed", agent_id=agent_id, error=str(e), exc_info=True
        )
        raise HTTPException(status_code=500, detail="Failed to register payment capability") from e


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
    agent_info: AgentApiKeyDep,
    payment_tasks: PaymentTasksDep = None,
):
    """Create a payment task (requires Agent API Key)

    The authenticated agent must match the `from_agent` field to prevent spoofing.
    """
    if agent_info["agent_id"] != request.from_agent:
        raise HTTPException(
            status_code=403,
            detail="Authenticated agent does not match from_agent field",
        )
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
        logger.error("create_payment_task_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create payment task") from e


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
    agent_info: AgentApiKeyDep,
    status: PaymentTaskStatus | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    payment_tasks: PaymentTasksDep = None,
):
    """Get payment tasks for agent (requires Agent API Key matching agent_id)"""
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")
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
    agent_info: AgentApiKeyDep,
    payment_tasks: PaymentTasksDep = None,
):
    """Get payment statistics for agent (requires Agent API Key matching agent_id)"""
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")
    stats = await payment_tasks.get_agent_stats(agent_id)
    return stats


# =============================================================================
# Token Billing Endpoints
# =============================================================================


@router.get("/billing/config")
async def get_billing_config():
    """Get current billing configuration"""
    return {
        "network_fee_rate": NETWORK_FEE_RATE,
        "credits_per_usd": CREDITS_PER_USD,
        "supported_currencies": ["USD"],
        "pricing_models": ["token_based", "fixed_price"],
    }


@router.post("/{agent_id}/token-pricing")
async def set_token_pricing(
    agent_id: str,
    request: TokenPricingRequest,
    agent_info: AgentApiKeyDep,
    registry: RegistryDep = None,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """
    Set token-based pricing for an agent (requires Agent API Key).

    The authenticated agent must match the path `agent_id`.
    This enables OpenAI-style per-token billing for the agent.
    """
    if agent_info["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not match agent_id")
    agent = await registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        # Create token pricing
        token_pricing = TokenPricing(
            input_price_per_million=request.input_price_per_million,
            output_price_per_million=request.output_price_per_million,
        )

        # Get existing capability or create new one
        existing = await payment_discovery.get_agent_payment_capability(agent_id)
        if existing:
            existing.token_pricing = token_pricing
            capability = existing
        else:
            capability = PaymentCapability(
                accepts_payment=True,
                payment_methods=[SupportedPaymentMethod.PLATFORM_CREDITS],
                token_pricing=token_pricing,
            )

        await payment_discovery.index_payment_capability(agent_id, capability)

        return {
            "status": "configured",
            "agent_id": agent_id,
            "token_pricing": {
                "input_price_per_million": request.input_price_per_million,
                "output_price_per_million": request.output_price_per_million,
                "currency": "USD",
            },
            "network_fee_rate": NETWORK_FEE_RATE,
        }

    except Exception as e:
        logger.error("set_token_pricing_failed", agent_id=agent_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to set token pricing") from e


@router.get("/{agent_id}/token-pricing")
async def get_token_pricing(
    agent_id: str,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """Get token-based pricing for an agent"""
    capability = await payment_discovery.get_agent_payment_capability(agent_id)
    if not capability or not capability.token_pricing:
        raise HTTPException(status_code=404, detail="Token pricing not configured for this agent")

    return {
        "agent_id": agent_id,
        "token_pricing": {
            "input_price_per_million": capability.token_pricing.input_price_per_million,
            "output_price_per_million": capability.token_pricing.output_price_per_million,
            "currency": capability.token_pricing.currency,
        },
        "network_fee_rate": NETWORK_FEE_RATE,
        "pricing_type": capability.get_pricing_type(),
    }


@router.post("/billing/estimate")
async def estimate_cost(
    request: EstimateCostRequest,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """
    Estimate cost before calling an agent.

    Returns cost breakdown including network fee.
    """
    capability = await payment_discovery.get_agent_payment_capability(request.agent_id)
    if not capability or not capability.token_pricing:
        raise HTTPException(status_code=404, detail="Token pricing not configured for this agent")

    # Calculate cost breakdown
    breakdown = capability.token_pricing.calculate_cost_with_network_fee(
        request.estimated_input_tokens,
        request.estimated_output_tokens,
    )

    return {
        "agent_id": request.agent_id,
        "estimate": breakdown,
        "note": "Actual cost may vary based on actual token usage",
    }


@router.post("/billing/charge")
async def bill_usage(
    request: BillUsageRequest,
    _: InternalTokenDep,
    payment_discovery: PaymentDiscoveryDep = None,
    billing_service: BillingServiceDep = None,
    registry: RegistryDep = None,
):
    """
    Bill token usage after a service call (requires X-Internal-Token).

    Restricted to ACN backend — triggered after actual service call completes.
    This creates a billing transaction and returns the cost breakdown.
    The actual credit deduction is handled by the backend wallet system.
    """
    # Get agent's token pricing
    capability = await payment_discovery.get_agent_payment_capability(request.agent_id)
    if not capability or not capability.token_pricing:
        raise HTTPException(status_code=404, detail="Token pricing not configured for this agent")

    # Get agent owner
    agent = await registry.get_agent(request.agent_id)
    agent_owner_id = agent.owner if agent else None

    # Calculate cost
    cost = billing_service.calculate_cost(
        request.input_tokens,
        request.output_tokens,
        capability.token_pricing,
    )

    # Create transaction
    transaction = await billing_service.create_transaction(
        user_id=request.user_id,
        agent_id=request.agent_id,
        agent_owner_id=agent_owner_id,
        cost=cost,
        task_id=request.task_id,
    )

    return {
        "transaction_id": transaction.transaction_id,
        "status": transaction.status.value,
        "cost": {
            "input_tokens": cost.input_tokens,
            "output_tokens": cost.output_tokens,
            "total_usd": cost.total_usd,
            "total_credits": cost.total_credits,
            "network_fee_credits": cost.network_fee_credits,
            "agent_income_credits": cost.agent_income_credits,
        },
        "note": "Transaction created. Use /billing/process to complete payment.",
    }


@router.get("/billing/transactions/{transaction_id}")
async def get_billing_transaction(
    transaction_id: str,
    _: InternalTokenDep,
    billing_service: BillingServiceDep = None,
):
    """Get a billing transaction by ID (requires X-Internal-Token)"""
    transaction = await billing_service.get_transaction(transaction_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")

    return transaction


@router.get("/billing/user/{user_id}/transactions")
async def get_user_billing_transactions(
    user_id: str,
    _: InternalTokenDep,
    status: BillingTransactionStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    billing_service: BillingServiceDep = None,
):
    """Get billing transactions for a user (requires X-Internal-Token)"""
    transactions = await billing_service.get_user_transactions(
        user_id=user_id,
        limit=limit,
        status=status,
    )
    return {
        "user_id": user_id,
        "transactions": [t.model_dump() for t in transactions],
        "count": len(transactions),
    }


@router.get("/billing/user/{user_id}/stats")
async def get_user_billing_stats(
    user_id: str,
    _: InternalTokenDep,
    billing_service: BillingServiceDep = None,
):
    """Get billing statistics for a user (requires X-Internal-Token)"""
    stats = await billing_service.get_user_billing_stats(user_id)
    return {
        "user_id": user_id,
        "stats": stats,
    }


@router.get("/billing/network-fees")
async def get_network_fee_stats(
    _: InternalTokenDep,
    billing_service: BillingServiceDep = None,
):
    """Get network fee statistics (requires X-Internal-Token)"""
    stats = await billing_service.get_network_fee_stats()
    return stats
