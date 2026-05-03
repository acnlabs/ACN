"""ERC-8004 On-Chain Identity API Routes

Endpoints for agents to bind their on-chain ERC-8004 identity to ACN and
for external parties to query on-chain identity and reputation data.

All write operations require the agent's API key.
All read operations are public (no auth required).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..core.errors import ACN_DEFAULT_RESPONSES, ACNHTTPError, ErrorCode
from ..core.exceptions import AgentNotFoundException
from ..services.agent_service import AgentService
from ..services.erc8004_client import ERC8004Client
from .dependencies import AgentApiKeyDep, AgentServiceDep, limiter

router = APIRouter(
    prefix="/api/v1/onchain",
    tags=["onchain"],
    responses=ACN_DEFAULT_RESPONSES,
)
logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Shared ERC-8004 client (lazy singleton, created on first use)
# ---------------------------------------------------------------------------

_erc8004_client: ERC8004Client | None = None


def set_erc8004_client(client: ERC8004Client | None) -> None:
    """Lifespan hook for installing a pre-warmed ERC-8004 client.

    Used by ``acn.api`` startup so the chain_id RPC roundtrip is paid
    once at boot (fail-fast on RPC mismatch), instead of on the first
    bind request.  Subsequent ``get_erc8004_client`` calls return the
    same instance so cache state is preserved.
    """
    global _erc8004_client
    _erc8004_client = client


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


def _parse_token_id_or_422(value: str | None, agent_id: str) -> int:
    """Parse a stored ``erc8004_agent_id`` to int, or raise 422.

    Token IDs are persisted as ``str`` for forward-compat (huge IDs, future
    string-namespaced schemes), so callers must coerce. Pre-launch audit
    backlog #1: a manually-edited or extremely-old DB row could hold a
    non-numeric value and cause an unhandled ``ValueError`` → 500.

    Returns 422 (not 500) so the client can distinguish "this agent's
    on-chain binding is corrupted" from "ACN is broken". Logs the offender
    so an operator can clean it up.
    """
    if value is None:
        raise ACNHTTPError(
            ErrorCode.ERC8004_TOKEN_ID_MISSING,
            status_code=422,
            details={"agent_id": agent_id},
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.error(
            "erc8004_corrupt_token_id",
            agent_id=agent_id,
            stored_value=value,
        )
        raise ACNHTTPError(
            ErrorCode.ERC8004_TOKEN_ID_CORRUPT,
            status_code=422,
            details={"agent_id": agent_id},
        ) from None


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class BindRequest(BaseModel):
    """Request to bind an on-chain ERC-8004 token ID to an ACN agent.

    The ``chain`` field is **deprecated for client use** (security audit
    H-erc8004): ACN derives the canonical chain string from its own
    ``erc8004_chain_id`` setting and ignores any divergent value from the
    client. Sending it is still allowed for backward-compat, but if it
    disagrees with the server-derived value the bind is rejected with 422.

    Why: the previous implementation stored ``chain`` verbatim and then
    served it back as ground truth via ``GET /onchain/agents/{id}``,
    letting an attacker on a cheap testnet bind a token while claiming
    "Ethereum mainnet" to anyone who queried the agent.
    """

    token_id: int = Field(..., description="ERC-8004 NFT token ID (agentId on-chain)")
    chain: str | None = Field(
        default=None,
        max_length=64,
        description=(
            'Optional chain namespace, e.g. "eip155:8453" (Base mainnet). '
            "Server-derived from ERC8004_CHAIN_ID; if provided must match."
        ),
    )
    tx_hash: str | None = Field(None, max_length=128, description="Registration transaction hash (informational)")


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
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            status_code=403,
            details={
                "path_agent": agent_id,
                "key_agent": caller["agent_id"],
            },
        )

    # ── H-erc8004 ──────────────────────────────────────────────────────────
    # 1. Server-derive the canonical chain string from settings. Any
    #    client-supplied value is treated as a hint that must match —
    #    never as data we trust to persist.
    server_chain = f"eip155:{settings.erc8004_chain_id}"
    if body.chain is not None and body.chain != server_chain:
        raise ACNHTTPError(
            ErrorCode.ERC8004_CHAIN_MISMATCH,
            status_code=422,
            details={
                "server_chain": server_chain,
                "client_chain": body.chain,
            },
        )

    # 2. Confirm the RPC endpoint is actually on the configured chain.
    #    Without this an operator who swaps ERC8004_RPC_URL to a different
    #    network (or an attacker who controls the RPC node) could trick
    #    ACN into accepting binds backed by tokens on the wrong chain.
    matches, actual = await erc8004.verify_chain_id(settings.erc8004_chain_id)
    if not matches:
        logger.error(
            "erc8004_rpc_chain_mismatch",
            expected=settings.erc8004_chain_id,
            actual=actual,
        )
        # 503 not 422 — this is a server-side misconfiguration / outage,
        # not a client error. ``HTTPException`` (not ``ACNHTTPError``)
        # because ``ACNHTTPError`` rejects 5xx at construction time so
        # the central 5xx sanitisation handler stays in charge. The
        # client cannot fix this by retrying — it surfaces as the flat
        # sanitised 5xx body via the existing handler chain.
        raise HTTPException(
            status_code=503,
            detail=(
                "ERC-8004 RPC endpoint is not on the configured chain "
                f"(expected chain_id={settings.erc8004_chain_id})."
            ),
        )

    try:
        agent = await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id},
        ) from e

    # Check for duplicate binding (another agent already bound this token)
    existing_binding = await _check_duplicate_token(agent_service, body.token_id, agent_id)
    if existing_binding:
        # ``bound_agent_id`` IS leaked here on purpose. It already
        # leaked pre-migration via the legacy ``detail`` string, and
        # the on-chain reverse-index that powers
        # ``_check_duplicate_token`` is *publicly* readable on the
        # ERC-8004 contract — anyone can call ``ownerOf(token_id)``
        # against the chain and resolve the same mapping client-side.
        # Hiding it from the response body would not actually keep
        # the binding private; it would only force SDK clients to
        # round-trip through the public chain to get a piece of
        # data the route already knows. Echoing it back keeps the
        # SDK contract honest and avoids a false sense of privacy.
        raise ACNHTTPError(
            ErrorCode.ERC8004_TOKEN_ALREADY_BOUND,
            status_code=409,
            details={
                "token_id": body.token_id,
                "bound_agent_id": existing_binding,
                "requesting_agent_id": agent_id,
            },
        )

    # Verify on-chain: tokenURI must point to this agent's registration file
    expected_url = (
        f"{settings.gateway_base_url}/api/v1/agents/{agent_id}"
        "/.well-known/agent-registration.json"
    )
    verified = await erc8004.verify_registration(body.token_id, expected_url)
    if not verified:
        # ``expected_url`` is preserved in full because the caller
        # needs it verbatim to know what URL to set as the on-chain
        # ``tokenURI``. Truncating or omitting it would force the
        # caller to reconstruct it from gateway_base_url and agent_id,
        # which is fragile (gateway URL changes, future path edits)
        # and provides zero diagnostic value over echoing the canonical
        # string ACN actually expects.
        raise ACNHTTPError(
            ErrorCode.ERC8004_REGISTRATION_MISMATCH,
            status_code=422,
            details={
                "token_id": body.token_id,
                "expected_url": expected_url,
            },
        )

    # Read wallet address from chain (more trustworthy than agent self-report)
    on_chain_wallet = await erc8004.get_agent_wallet(body.token_id)

    # Persist binding — chain is the *server*-derived value, never the
    # client-supplied one (even when they happen to match the client value
    # is still discarded; we re-derive on every bind).
    agent.erc8004_agent_id = str(body.token_id)
    agent.erc8004_chain = server_chain
    agent.erc8004_tx_hash = body.tx_hash
    agent.erc8004_registered_at = datetime.now(UTC)
    if on_chain_wallet:
        agent.wallet_address = on_chain_wallet

    await agent_service.repository.save(agent)

    logger.info(
        "erc8004_bound",
        agent_id=agent_id,
        token_id=body.token_id,
        chain=server_chain,
        wallet=on_chain_wallet,
    )

    return BindResponse(
        status="bound",
        agent_id=agent_id,
        token_id=body.token_id,
        chain=server_chain,
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
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id},
        ) from e

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
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id},
        ) from e

    if not agent.erc8004_agent_id:
        raise ACNHTTPError(
            ErrorCode.ERC8004_NOT_BOUND,
            status_code=404,
            details={"agent_id": agent_id},
        )

    token_id = _parse_token_id_or_422(agent.erc8004_agent_id, agent_id)
    summary = await erc8004.get_reputation_summary(token_id)
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
        # 5xx — Validation Registry contract address not configured
        # is operator-side misconfig, not caller-actionable. ``ACNHTTPError``
        # rejects 5xx at construction time so the central 5xx
        # sanitisation handler stays in charge here.
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
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id},
        ) from e

    if not agent.erc8004_agent_id:
        raise ACNHTTPError(
            ErrorCode.ERC8004_NOT_BOUND,
            status_code=404,
            details={"agent_id": agent_id},
        )

    token_id = _parse_token_id_or_422(agent.erc8004_agent_id, agent_id)
    summary = await erc8004.get_validation_summary(token_id)
    return ValidationSummaryResponse(**summary)


@router.get("/discover")
@limiter.limit("20/minute")
async def discover_onchain_agents(
    request: Request,
    limit: int = 50,
    agent_service: AgentServiceDep = None,
    erc8004: ERC8004Client = Depends(get_erc8004_client),
    settings: Settings = Depends(get_settings),
):
    """Discover agents registered on the ERC-8004 Identity Registry.

    Primary path: calls totalSupply() and iterates from the newest token
    backward — pure eth_call, no event scanning.
    Fallback: if totalSupply() is unavailable, scans recent Transfer mint
    events via getLogs() in 2000-block batches (compatible with public RPCs).
    Results are cached in Redis for 5 minutes.
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
