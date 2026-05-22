"""Payment System API Routes"""

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.validators import check_dict_size_64k
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
    AgentServiceDep,
    BillingServiceDep,
    InternalTokenDep,
    PaymentDiscoveryDep,
    PaymentTasksDep,
    limiter,
)

router = APIRouter(
    prefix="/api/v1/payments",
    tags=["payments"],
    responses=ACN_DEFAULT_RESPONSES,
)
logger = structlog.get_logger()


class PaymentCapabilityRequest(BaseModel):
    supported_methods: list[SupportedPaymentMethod] = Field(..., max_length=20)
    supported_networks: list[SupportedNetwork] = Field(..., max_length=20)
    wallet_address: str | None = Field(default=None, max_length=128)
    wallet_addresses: dict[str, str] = Field(
        default_factory=dict,
        description="Per-network wallet addresses, e.g. {'ethereum': '0x...', 'base': '0x...'}",
    )
    accepts_payment: bool = True
    token_pricing: dict | None = Field(
        default=None,
        description="Token-based pricing config, e.g. {'input_price_per_million': 2.5, 'output_price_per_million': 10.0, 'currency': 'USD'}",
    )
    api_endpoint: str | None = Field(default=None, max_length=500)
    webhook_url: str | None = Field(default=None, max_length=500)

    @field_validator("token_pricing")
    @classmethod
    def _token_pricing_size(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        return check_dict_size_64k("token_pricing", v)


class CreatePaymentTaskRequest(BaseModel):
    from_agent: str = Field(..., max_length=128)
    to_agent: str = Field(..., max_length=128)
    amount: float = Field(..., gt=0, description="Payment amount (must be positive)")
    currency: str = Field(..., max_length=32)
    payment_method: SupportedPaymentMethod
    network: SupportedNetwork
    description: str | None = Field(default=None, max_length=2_000)
    metadata: dict | None = None

    @field_validator("metadata")
    @classmethod
    def _metadata_size(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        return check_dict_size_64k("metadata", v)


# =============================================================================
# Token Billing Models
# =============================================================================


class TokenPricingRequest(BaseModel):
    """Request to set token-based pricing for an agent"""

    input_price_per_million: float = Field(..., ge=0, description="USD per 1M input tokens")
    output_price_per_million: float = Field(..., ge=0, description="USD per 1M output tokens")


class EstimateCostRequest(BaseModel):
    """Request to estimate cost for a service call"""

    agent_id: str = Field(..., max_length=128)
    estimated_input_tokens: int = Field(default=0, ge=0)
    estimated_output_tokens: int = Field(default=0, ge=0)


class BillUsageRequest(BaseModel):
    """Request to bill token usage after a service call"""

    user_id: str = Field(..., max_length=128, description="User being charged")
    agent_id: str = Field(..., max_length=128, description="Agent that provided service")
    task_id: str | None = Field(None, max_length=128, description="Associated task ID")
    input_tokens: int = Field(..., ge=0, description="Actual input tokens used")
    output_tokens: int = Field(..., ge=0, description="Actual output tokens used")


@router.post("/{agent_id}/payment-capability")
async def set_payment_capability(
    agent_id: str,
    request: PaymentCapabilityRequest,
    agent_info: AgentApiKeyDep,
    agent_service: AgentServiceDep = None,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """Set payment capability for agent (requires Agent API Key)

    The authenticated agent must match the path `agent_id`.
    Persists wallet_addresses and token_pricing to PostgreSQL and indexes in Redis.
    """
    if agent_info["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            status_code=403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )

    from ..core.exceptions import AgentNotFoundException

    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id},
        ) from None

    try:
        # Build wallet_addresses: merge request.wallet_addresses with legacy wallet_address
        wallet_addresses = dict(request.wallet_addresses)
        if request.wallet_address and "ethereum" not in wallet_addresses:
            wallet_addresses["ethereum"] = request.wallet_address

        # Derive legacy single-address field
        if not request.wallet_address and wallet_addresses:
            legacy_addr = (
                wallet_addresses.get("ethereum")
                or wallet_addresses.get("base")
                or next(iter(wallet_addresses.values()), None)
            )
        else:
            legacy_addr = request.wallet_address

        # Persist payment fields to Agent entity and save to PG (critical path)
        agent.accepts_payment = request.accepts_payment
        agent.wallet_address = legacy_addr
        agent.wallet_addresses = wallet_addresses
        agent.token_pricing = request.token_pricing
        if request.supported_methods:
            agent.payment_methods = [m.value for m in request.supported_methods]
        await agent_service.repository.save(agent)

    except ACNHTTPError:
        # P3 cross-module catch-all defence: ``ACNHTTPError`` is
        # ``Exception``-typed (not ``HTTPException``-typed); without
        # this re-raise, any caller-actionable 4xx raised inside the
        # try body would be silently rewritten as a sanitised 500.
        raise
    except HTTPException:
        # Mirror defence for legacy ``HTTPException`` raises — same
        # swallow risk via the catch-all below.
        raise
    except Exception as e:
        logger.error(
            "set_payment_capability_failed", agent_id=agent_id, error=str(e), exc_info=True
        )
        raise HTTPException(status_code=500, detail="Failed to register payment capability") from e

    # Index into Redis discovery service (best-effort: PG is source of truth)
    token_pricing_obj = None
    if request.token_pricing:
        try:
            token_pricing_obj = TokenPricing(**request.token_pricing)
        except Exception:
            pass

    capability = PaymentCapability(
        agent_id=agent_id,
        accepts_payment=request.accepts_payment,
        payment_methods=request.supported_methods,
        supported_networks=request.supported_networks,
        wallet_address=legacy_addr,
        wallet_addresses=wallet_addresses,
        token_pricing=token_pricing_obj,
        api_endpoint=request.api_endpoint,
        webhook_url=request.webhook_url,
    )
    try:
        await payment_discovery.index_payment_capability(agent_id, capability)
    except Exception:
        logger.warning("payment_discovery_index_failed", agent_id=agent_id, exc_info=True)

    return {"status": "registered", "agent_id": agent_id}


@router.get("/{agent_id}/payment-capability")
async def get_payment_capability(
    agent_id: str,
    _caller: AgentApiKeyDep,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """Get payment capability for agent"""
    capability = await payment_discovery.get_agent_payment_capability(agent_id)
    if not capability:
        raise ACNHTTPError(
            ErrorCode.PAYMENT_CAPABILITY_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id},
        )
    return capability


@router.get("/discover")
async def discover_payment_agents(
    method: SupportedPaymentMethod | None = None,
    network: SupportedNetwork | None = None,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """Discover agents with payment capabilities"""
    agents = await payment_discovery.find_agents_accepting_payment(
        payment_method=method,
        network=network,
    )
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
        raise ACNHTTPError(
            ErrorCode.FROM_AGENT_MISMATCH,
            status_code=403,
            details={
                "authenticated_as": agent_info["agent_id"],
                "from_agent": request.from_agent,
            },
        )
    try:
        task_metadata = dict(request.metadata or {})
        task_metadata["network"] = request.network.value if request.network else None

        task = await payment_tasks.create_payment_task(
            buyer_agent=request.from_agent,
            seller_agent=request.to_agent,
            task_description=request.description or f"Payment task: {request.from_agent} -> {request.to_agent}",
            amount=str(request.amount),
            currency=request.currency,
            payment_method=request.payment_method,
            network=request.network,
            task_type="payment",
            metadata=task_metadata,
        )

        return {"task_id": task.task_id, "status": "created"}

    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except ValueError as e:
        # The seller does not accept this method/network, or the seller has
        # no payment capability registered. These are caller-correctable.
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={"reason": str(e)},
        ) from e
    except Exception as e:
        logger.error("create_payment_task_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create payment task") from e


class ConfirmPaymentRequest(BaseModel):
    tx_hash: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="On-chain transaction hash (or any external payment reference)",
    )


@router.post("/tasks/{task_id}/confirm")
async def confirm_payment_task(
    task_id: str,
    body: ConfirmPaymentRequest,
    agent_info: AgentApiKeyDep,
    payment_tasks: PaymentTasksDep = None,
):
    """Confirm that an external payment has been made (requires Agent API Key).

    The authenticated agent must be the buyer of the payment task.
    Transitions the task to ``payment_confirmed`` and stores the
    ``tx_hash`` so the seller can verify on-chain or in the external
    payment system.

    Typical flow::

        1. Buyer calls ``POST /payments/tasks`` → gets ``task_id`` + seller wallet address
        2. Buyer executes the actual payment (on-chain transfer, Stripe, etc.)
        3. Buyer calls ``POST /payments/tasks/{task_id}/confirm`` with the ``tx_hash``
        4. Seller receives ``payment_task.payment_confirmed`` webhook → releases goods/service
    """
    task = await payment_tasks.get_task(task_id)
    if not task:
        raise ACNHTTPError(
            ErrorCode.PAYMENT_TASK_NOT_FOUND,
            status_code=404,
            details={"task_id": task_id},
        )

    if agent_info["agent_id"] != task.buyer_agent:
        raise ACNHTTPError(
            ErrorCode.FROM_AGENT_MISMATCH,
            status_code=403,
            details={
                "authenticated_as": agent_info["agent_id"],
                "from_agent": task.buyer_agent,
            },
        )

    try:
        updated = await payment_tasks.update_task_status(
            task_id=task_id,
            status=PaymentTaskStatus.PAYMENT_CONFIRMED,
            tx_hash=body.tx_hash,
        )
        return {
            "task_id": updated.task_id,
            "status": updated.status.value,
            "tx_hash": updated.tx_hash,
        }
    except Exception as e:
        logger.error("confirm_payment_task_failed", task_id=task_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to confirm payment task") from e


@router.get("/tasks/{task_id}")
async def get_payment_task(task_id: str, _: InternalTokenDep, payment_tasks: PaymentTasksDep = None):
    """Get payment task status (internal only)"""
    task = await payment_tasks.get_task(task_id)
    if not task:
        raise ACNHTTPError(
            ErrorCode.PAYMENT_TASK_NOT_FOUND,
            status_code=404,
            details={"task_id": task_id},
        )
    return task


@router.get("/tasks/agent/{agent_id}")
async def get_agent_payment_tasks(
    agent_id: str,
    agent_info: AgentApiKeyDep,
    status: PaymentTaskStatus | None = None,
    limit: int = Query(default=50, ge=1, le=1000),
    payment_tasks: PaymentTasksDep = None,
):
    """Get payment tasks for agent (requires Agent API Key matching agent_id)"""
    if agent_info["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            status_code=403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )
    tasks = await payment_tasks.get_tasks_by_agent(
        agent_id=agent_id,
        status=status,
        limit=limit,
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
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            status_code=403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )
    stats = await payment_tasks.get_payment_stats(agent_id)
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
    agent_service: AgentServiceDep = None,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """
    Set token-based pricing for an agent (requires Agent API Key).

    The authenticated agent must match the path `agent_id`.
    This enables OpenAI-style per-token billing for the agent.
    """
    if agent_info["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            status_code=403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )
    # ``find_agent`` (non-throwing) matches the legacy
    # ``AgentRegistry.get_agent`` contract this call site was wired
    # against — absence is a normal 404 branch, not an exception.
    agent = await agent_service.find_agent(agent_id)
    if not agent:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id},
        )

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

    except ACNHTTPError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("set_token_pricing_failed", agent_id=agent_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to set token pricing") from e


@router.get("/{agent_id}/token-pricing")
async def get_token_pricing(
    agent_id: str,
    _caller: AgentApiKeyDep,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """Get token-based pricing for an agent"""
    capability = await payment_discovery.get_agent_payment_capability(agent_id)
    if not capability or not capability.token_pricing:
        raise ACNHTTPError(
            ErrorCode.TOKEN_PRICING_NOT_CONFIGURED,
            status_code=404,
            details={"agent_id": agent_id},
        )

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
@limiter.limit("30/minute")
async def estimate_cost(
    request: Request,
    body: EstimateCostRequest,
    _caller: AgentApiKeyDep,
    payment_discovery: PaymentDiscoveryDep = None,
):
    """
    Estimate cost before calling an agent.

    Returns cost breakdown including network fee.
    """
    capability = await payment_discovery.get_agent_payment_capability(body.agent_id)
    if not capability or not capability.token_pricing:
        raise ACNHTTPError(
            ErrorCode.TOKEN_PRICING_NOT_CONFIGURED,
            status_code=404,
            details={"agent_id": body.agent_id},
        )

    # Calculate cost breakdown
    breakdown = capability.token_pricing.calculate_cost_with_network_fee(
        body.estimated_input_tokens,
        body.estimated_output_tokens,
    )

    return {
        "agent_id": body.agent_id,
        "estimate": breakdown,
        "note": "Actual cost may vary based on actual token usage",
    }


@router.post("/billing/charge")
async def bill_usage(
    request: BillUsageRequest,
    _: InternalTokenDep,
    payment_discovery: PaymentDiscoveryDep = None,
    billing_service: BillingServiceDep = None,
    agent_service: AgentServiceDep = None,
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
        raise ACNHTTPError(
            ErrorCode.TOKEN_PRICING_NOT_CONFIGURED,
            status_code=404,
            details={"agent_id": request.agent_id},
        )

    # Get agent owner — None is a legitimate state (autonomous /
    # unclaimed agent), so the billing record stays owner-less rather
    # than erroring out.
    agent = await agent_service.find_agent(request.agent_id)
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
        raise ACNHTTPError(
            ErrorCode.BILLING_TRANSACTION_NOT_FOUND,
            status_code=404,
            details={"transaction_id": transaction_id},
        )

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
