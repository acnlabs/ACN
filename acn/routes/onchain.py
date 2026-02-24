"""ERC-8004 On-Chain Identity API Routes

Endpoints for agents to bind their on-chain ERC-8004 identity to ACN and
for external parties to query on-chain identity and reputation data.

All write operations require the agent's API key.
All read operations are public (no auth required).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..core.exceptions import AgentNotFoundException
from ..services.agent_service import AgentService
from ..services.erc8004_client import ERC8004Client
from .dependencies import AgentApiKeyDep, AgentServiceDep

router = APIRouter(prefix="/api/v1/onchain", tags=["onchain"])
logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Shared ERC-8004 client (lazy singleton, created on first use)
# ---------------------------------------------------------------------------

_erc8004_client: ERC8004Client | None = None


def get_erc8004_client(settings: Settings = Depends(get_settings)) -> ERC8004Client:
    global _erc8004_client
    if _erc8004_client is None:
        _erc8004_client = ERC8004Client(
            rpc_url=settings.erc8004_rpc_url,
            identity_contract=settings.erc8004_identity_contract,
            reputation_contract=settings.erc8004_reputation_contract,
            validation_contract=settings.erc8004_validation_contract,
        )
    return _erc8004_client


ERC8004ClientDep = ERC8004Client


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class BindRequest(BaseModel):
    """Request to bind an on-chain ERC-8004 token ID to an ACN agent."""

    token_id: int = Field(..., description="ERC-8004 NFT token ID (agentId on-chain)")
    chain: str = Field(
        default="eip155:8453",
        description='Chain namespace, e.g. "eip155:8453" (Base mainnet)',
    )
    tx_hash: str | None = Field(None, description="Registration transaction hash (informational)")


class BindResponse(BaseModel):
    status: str
    agent_id: str
    token_id: int
    chain: str
    wallet_address: str | None = None
    message: str


class OnchainIdentityResponse(BaseModel):
    agent_id: str
    token_id: str | None
    chain: str | None
    tx_hash: str | None
    registered_at: datetime | None
    wallet_address: str | None


class ReputationResponse(BaseModel):
    token_id: int
    count: int
    avg_value: float | None
    by_tag: dict


class ValidationSummaryResponse(BaseModel):
    token_id: int
    available: bool
    total: int
    approved: int
    rejected: int
    pending: int
    by_tag: dict


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/agents/{agent_id}/bind", response_model=BindResponse)
async def bind_onchain_identity(
    agent_id: str,
    body: BindRequest,
    caller: AgentApiKeyDep = None,
    agent_service: AgentServiceDep = None,
    settings: Settings = Depends(get_settings),
    erc8004: ERC8004Client = Depends(get_erc8004_client),
):
    """Bind an on-chain ERC-8004 token ID to this ACN agent.

    The agent must already be registered in ACN and authenticated with its API
    key. ACN verifies on-chain that the tokenURI matches the agent's
    agent-registration.json URL before storing the binding.
    """
    if caller["agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="API key does not belong to this agent")

    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e

    # Check for duplicate binding (another agent already bound this token)
    existing_binding = await _check_duplicate_token(agent_service, body.token_id, agent_id)
    if existing_binding:
        raise HTTPException(
            status_code=409,
            detail=f"Token ID {body.token_id} is already bound to agent {existing_binding}",
        )

    # Verify on-chain: tokenURI must point to this agent's registration file
    expected_url = (
        f"{settings.gateway_base_url}/api/v1/agents/{agent_id}"
        "/.well-known/agent-registration.json"
    )
    verified = await erc8004.verify_registration(body.token_id, expected_url)
    if not verified:
        raise HTTPException(
            status_code=422,
            detail=(
                f"On-chain tokenURI does not match expected URL: {expected_url}. "
                "Please register on-chain with the correct agentURI first."
            ),
        )

    # Read wallet address from chain (more trustworthy than agent self-report)
    on_chain_wallet = await erc8004.get_agent_wallet(body.token_id)

    # Persist binding
    agent.erc8004_agent_id = str(body.token_id)
    agent.erc8004_chain = body.chain
    agent.erc8004_tx_hash = body.tx_hash
    agent.erc8004_registered_at = datetime.now(UTC)
    if on_chain_wallet:
        agent.wallet_address = on_chain_wallet

    await agent_service.repository.save(agent)

    logger.info(
        "erc8004_bound",
        agent_id=agent_id,
        token_id=body.token_id,
        chain=body.chain,
        wallet=on_chain_wallet,
    )

    return BindResponse(
        status="bound",
        agent_id=agent_id,
        token_id=body.token_id,
        chain=body.chain,
        wallet_address=on_chain_wallet,
        message=(
            f"ERC-8004 token {body.token_id} successfully bound to agent {agent_id}"
        ),
    )


@router.get("/agents/{agent_id}", response_model=OnchainIdentityResponse)
async def get_onchain_identity(agent_id: str, agent_service: AgentServiceDep = None):
    """Query the on-chain identity of an ACN agent."""
    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e

    return OnchainIdentityResponse(
        agent_id=agent_id,
        token_id=agent.erc8004_agent_id,
        chain=agent.erc8004_chain,
        tx_hash=agent.erc8004_tx_hash,
        registered_at=agent.erc8004_registered_at,
        wallet_address=agent.wallet_address,
    )


@router.get("/agents/{agent_id}/reputation", response_model=ReputationResponse)
async def get_agent_reputation(
    agent_id: str,
    agent_service: AgentServiceDep = None,
    erc8004: ERC8004Client = Depends(get_erc8004_client),
):
    """Fetch on-chain reputation for an ACN agent.

    Uses readAllFeedback (empty clientAddresses = no filter) and aggregates
    at the application layer, because getSummary() requires non-empty
    clientAddresses to mitigate Sybil attacks.
    """
    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e

    if not agent.erc8004_agent_id:
        raise HTTPException(
            status_code=404,
            detail="Agent has not bound an ERC-8004 token ID yet",
        )

    summary = await erc8004.get_reputation_summary(int(agent.erc8004_agent_id))
    return ReputationResponse(**summary)


@router.get("/agents/{agent_id}/validation", response_model=ValidationSummaryResponse)
async def get_agent_validation(
    agent_id: str,
    agent_service: AgentServiceDep = None,
    erc8004: ERC8004Client = Depends(get_erc8004_client),
):
    """Fetch on-chain validation summary for an ACN agent.

    Queries the ERC-8004 Validation Registry for all validation records linked
    to this agent's token ID and returns a summary grouped by tag and status.

    Returns 503 if the Validation Registry contract address is not configured
    (the registry is still experimental — addresses not yet publicly published).
    """
    if not erc8004.validation_available:
        raise HTTPException(
            status_code=503,
            detail=(
                "Validation Registry is not configured. "
                "Set ERC8004_VALIDATION_CONTRACT env var when the address is available."
            ),
        )

    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise HTTPException(status_code=404, detail="Agent not found") from e

    if not agent.erc8004_agent_id:
        raise HTTPException(
            status_code=404,
            detail="Agent has not bound an ERC-8004 token ID yet",
        )

    summary = await erc8004.get_validation_summary(int(agent.erc8004_agent_id))
    return ValidationSummaryResponse(**summary)


@router.get("/discover")
async def discover_onchain_agents(
    limit: int = 50,
    agent_service: AgentServiceDep = None,
    erc8004: ERC8004Client = Depends(get_erc8004_client),
    settings: Settings = Depends(get_settings),
):
    """Discover agents registered on the ERC-8004 Identity Registry.

    Scans Transfer(from=0x0) mint events on-chain. Results are cached in
    Redis for 5 minutes to avoid repeated expensive event scans.
    """
    cache_key = f"acn:erc8004:discover:limit:{limit}"

    # Try cache first
    cached = await _get_discover_cache(agent_service, cache_key)
    if cached is not None:
        return {"source": "cache", "agents": cached}

    # Fetch from chain
    agents = await erc8004.discover_agents(limit=limit)

    # Cache for 5 minutes (300 seconds)
    await _set_discover_cache(agent_service, cache_key, agents, ttl=300)

    return {"source": "chain", "agents": agents}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _check_duplicate_token(
    agent_service: AgentService,
    token_id: int,
    requesting_agent_id: str,
) -> str | None:
    """Return the existing bound agent_id if this token is already bound to a
    different ACN agent. Uses the Redis reverse-index written by save().
    """
    try:
        key = f"acn:agents:by_erc8004_id:{token_id}"
        existing = await agent_service.repository.redis.get(key)  # type: ignore[attr-defined]
        if existing and existing != requesting_agent_id:
            return existing
    except Exception:
        pass
    return None


async def _get_discover_cache(agent_service: AgentService, key: str) -> list | None:
    try:
        raw = await agent_service.repository.redis.get(key)  # type: ignore[attr-defined]
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


async def _set_discover_cache(
    agent_service: AgentService, key: str, data: list, ttl: int
) -> None:
    try:
        await agent_service.repository.redis.setex(  # type: ignore[attr-defined]
            key, ttl, json.dumps(data)
        )
    except Exception:
        pass
