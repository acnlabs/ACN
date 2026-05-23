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
from ..core.interfaces.reputation_repository import ReputationEvent
from ..services.agent_service import AgentService
from ..services.erc8004_client import ERC8004Client
from ..services.reputation_query_service import (
    OffChainReputationSummary,
    ReputationSummary,
)
from .dependencies import (
    AgentApiKeyDep,
    AgentServiceDep,
    ReputationQueryServiceDep,
    ReputationServiceDep,
    limiter,
)
from .tasks import TaskServiceDep

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


# ---- Reputation write models (Saga v0.1, off-chain) -----------------------
#
# Shape matches ERC-8004 semantics (target_agent_id, task_id, score,
# evidence_uri, attestation) so the v1 chain-write upgrade is a backend
# swap with no API surface change. v0.1 persists rows in
# ``reputation_events`` and ignores the chain entirely; clients receive
# ``note: "off-chain v0.1"`` so they can detect the chain-write phase.


class FeedbackRequest(BaseModel):
    """ERC-8004 feedback submission. Caller is the feedback issuer (signer).

    The ``signer`` is intentionally NOT in the body: routes derive it
    from the authenticated API key. Letting the body carry ``signer``
    would let any API-key holder forge feedback on behalf of arbitrary
    agents — exactly the spoofing vector ``API_KEY_AGENT_MISMATCH``
    guards against elsewhere.
    """

    task_id: str = Field(
        ..., min_length=1, max_length=64, description="Task this feedback is about."
    )
    score: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Optional 0-100 score. v0.1 persists but doesn't surface — "
            "reserved for v0.2 aggregate ranking."
        ),
    )
    evidence_uri: str | None = Field(
        default=None,
        max_length=512,
        description="Optional pointer to off-chain evidence (IPFS, signed JSON URL).",
    )


class ValidationRequest(BaseModel):
    """ERC-8004 validation submission. Caller is the validator (signer).

    ``attestation`` is required and carries the validator's signed
    proof. v0.1 doesn't verify the signature server-side — the
    validator's signing key custody is out of scope until v0.2 — but
    the field is mandatory so v0.2 can verify retroactively without a
    backfill migration.
    """

    task_id: str = Field(..., min_length=1, max_length=64)
    attestation: dict = Field(
        ...,
        description=(
            "Signed JSON attestation. Required — empty / null is rejected. "
            "Shape is open for v0.1 (validator-defined); v0.2 will pin "
            "{tag, status, signature, key_id}."
        ),
    )
    score: int | None = Field(default=None, ge=0, le=100)
    evidence_uri: str | None = Field(default=None, max_length=512)


class ReputationEventResponse(BaseModel):
    """Response shape for both POST endpoints. ``note`` advertises the
    v0.1 phase so clients building on top can detect when chain-write
    ships (v1 will return e.g. ``"chain_pending"`` + ``chain_tx_hash``).
    """

    id: int
    agent_id: str
    task_id: str
    kind: str
    score: int | None
    evidence_uri: str | None
    signer: str
    created_at: datetime
    smoke_test: bool = False
    note: str = "off-chain v0.1; chain-write reserved for v1"


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


# ---------------------------------------------------------------------------
# Reputation write endpoints (Saga v0.1, off-chain)
# ---------------------------------------------------------------------------


def _require_reputation_service(
    service: object | None,
) -> None:
    """Raise 503 if the reputation service is not wired (Redis-only deploy).

    Same rationale as ``validation_available`` checks above: missing PG
    is an operator-side configuration issue, not caller-actionable.
    ``HTTPException`` (not ``ACNHTTPError``) because the 5xx
    sanitisation handler chain owns 5xx responses.
    """
    if service is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Off-chain reputation is unavailable — the deployment is "
                "not configured with a PostgreSQL backend. Set DATABASE_URL "
                "to enable v0.1 reputation."
            ),
        )


async def _fetch_agent_or_404(
    agent_service: AgentService, agent_id: str
):
    """Fetch an agent or raise 404. Single source of truth for
    "agent must exist" checks on this route file — review fix R2
    folded a pre-check + later re-fetch in ``get_agent_reputation_summary``
    into one call.
    """
    try:
        return await agent_service.get_agent(agent_id)
    except AgentNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            status_code=404,
            details={"agent_id": agent_id},
        ) from e


def _enforce_not_self(caller_id: str, target_id: str) -> None:
    """Self-feedback / self-validation is forbidden at the route layer.

    ``ReputationService`` also enforces this (defense-in-depth) but
    surfacing the error here gives a more specific error code than the
    service-raised ``ValueError`` (which the central 4xx handler would
    flatten to ``invalid_request``).
    """
    if caller_id == target_id:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={
                "reason": "self_feedback_forbidden",
                "agent_id": target_id,
            },
        )


# Reputation write authorisation matrix (review fix R1)
# ----------------------------------------------------
# Without these checks, any agent holding a valid API key could write
# arbitrary feedback / validation rows against any other agent — by
# creating throw-away tasks and looping POST. The
# ``UNIQUE(agent_id, task_id, kind)`` constraint only deduplicates
# repeated submissions for the same task; it cannot stop "10 000
# fresh task_ids, 10 000 rows".
#
# Authorisation rules (v0.1):
#
#   feedback:    caller MUST be the task's ``creator_id``.
#                target MUST be the task's ``assignee_id`` (no None).
#                task.status MUST be ``COMPLETED`` (approved & released).
#
#   validation:  target MUST be the task's ``assignee_id``.
#                task.status MUST be ``COMPLETED``.
#                caller MUST NOT be ``creator_id`` (creator goes via
#                  ``feedback`` — validation is a third-party voice).
#                caller MUST NOT be ``assignee_id`` (already enforced
#                  by ``_enforce_not_self`` upstream, but re-checked
#                  here for completeness).
#
# COMPLETED is currently the only terminal status where reputation makes
# sense — REJECTED tasks shouldn't produce positive feedback, and
# CANCELLED ones have nothing to evaluate. If we later want negative
# feedback ("you delivered something bad"), it'd flow through a separate
# dispute endpoint, not POST /feedback.


async def _fetch_task_for_reputation(
    task_service, task_id: str
):
    """Fetch the task or raise 404. Wraps ``TaskNotFoundException``
    into the standard 404 response shape so route handlers don't have
    to duplicate the catch.
    """
    from ..services.task_service import TaskNotFoundException

    try:
        return await task_service.get_task(task_id)
    except TaskNotFoundException as e:
        raise ACNHTTPError(
            ErrorCode.TASK_NOT_FOUND,
            status_code=404,
            details={"task_id": task_id},
        ) from e


def _enforce_feedback_authorisation(
    task,
    caller_id: str,
    target_agent_id: str,
) -> None:
    """Enforce: caller is task creator, target is task assignee, task
    is in COMPLETED state. Each failure surfaces a distinct error code
    so the SDK can show actionable messages."""
    from ..core.entities.task import TaskStatus

    if task.status != TaskStatus.COMPLETED:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={
                "reason": "task_not_completed",
                "task_id": task.task_id,
                "current_status": task.status,
            },
        )
    if task.creator_id != caller_id:
        # Use OWNERSHIP_MISMATCH (not API_KEY_AGENT_MISMATCH) because
        # the API key IS valid for the caller — they're just not the
        # right party for this resource.
        raise ACNHTTPError(
            ErrorCode.OWNERSHIP_MISMATCH,
            status_code=403,
            details={
                "reason": "caller_is_not_task_creator",
                "task_id": task.task_id,
                "task_creator_id": task.creator_id,
                "caller_id": caller_id,
            },
        )
    if task.assignee_id != target_agent_id:
        # 400 not 403: this is a route-shape error (the caller asked
        # to write feedback against the wrong agent for this task),
        # not an auth failure.
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={
                "reason": "target_is_not_task_assignee",
                "task_id": task.task_id,
                "task_assignee_id": task.assignee_id,
                "target_agent_id": target_agent_id,
            },
        )


def _enforce_validation_authorisation(
    task,
    caller_id: str,
    target_agent_id: str,
) -> None:
    """Enforce: target is task assignee, task is COMPLETED, caller is
    NEITHER creator NOR assignee (validation is a third-party voice).
    """
    from ..core.entities.task import TaskStatus

    if task.status != TaskStatus.COMPLETED:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={
                "reason": "task_not_completed",
                "task_id": task.task_id,
                "current_status": task.status,
            },
        )
    if task.assignee_id != target_agent_id:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={
                "reason": "target_is_not_task_assignee",
                "task_id": task.task_id,
                "task_assignee_id": task.assignee_id,
                "target_agent_id": target_agent_id,
            },
        )
    if task.creator_id == caller_id:
        # Creator should write feedback, not validation. Surface a
        # distinct reason so the SDK can suggest the correct endpoint.
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={
                "reason": "creator_must_use_feedback_endpoint",
                "task_id": task.task_id,
            },
        )


def _event_to_response(event: ReputationEvent) -> ReputationEventResponse:
    """Map a ``ReputationEvent`` DTO to the route response.

    ``smoke_test`` is hoisted from ``event_metadata`` so clients have
    one direct boolean rather than a nested JSON path. Other metadata
    keys are intentionally NOT exposed — v0.1 only carries
    ``smoke_test`` and we don't want to commit to a wider response
    shape before the schema is stable.
    """
    # ``id`` / ``created_at`` are non-None for any persisted event;
    # asserting here would be defensive (the DTO marks them Optional
    # for the pre-persist case) but the route only ever returns rows
    # the repository just persisted.
    assert event.id is not None, "Persisted event must have an id"
    assert event.created_at is not None, "Persisted event must have created_at"
    return ReputationEventResponse(
        id=event.id,
        agent_id=event.agent_id,
        task_id=event.task_id,
        kind=event.kind,
        score=event.score,
        evidence_uri=event.evidence_uri,
        signer=event.signer,
        created_at=event.created_at,
        smoke_test=bool(event.event_metadata.get("smoke_test", False)),
    )


@router.post(
    "/agents/{agent_id}/feedback",
    response_model=ReputationEventResponse,
    status_code=201,
)
@limiter.limit("60/minute")
async def post_agent_feedback(
    request: Request,
    agent_id: str,
    body: FeedbackRequest,
    caller: AgentApiKeyDep = None,
    agent_service: AgentServiceDep = None,
    task_service: TaskServiceDep = None,
    reputation_service: ReputationServiceDep = None,
):
    """Submit one ERC-8004 feedback event against ``agent_id``.

    v0.1 (this commit) stores the event in the off-chain
    ``reputation_events`` table. Chain write (calling the ERC-8004
    Reputation Registry) is reserved for v1; the response field
    ``note`` advertises the current phase so SDKs can detect when the
    chain write goes live.

    Idempotency: ``(agent_id, task_id, 'feedback')`` is unique. A
    second POST with the same triplet returns the existing event
    (HTTP 201 still, with the original ``id`` / ``created_at``) —
    callers can safely retry on transient network errors. 201 (vs.
    200) is correct even on idempotent re-hit because the operation
    "ensure this fact is recorded" is conceptually a creation, and
    the response carries the canonical resource id.

    Auth (review fix R1):
        * Caller must hold a valid agent API key (401 otherwise).
        * Caller MUST be the task's ``creator_id`` (403 otherwise).
        * Target MUST be the task's ``assignee_id`` (400 otherwise).
        * Task MUST be in ``COMPLETED`` state (400 otherwise).
        * Self-feedback is rejected (400).

    Direct POST submissions are ALWAYS treated as non-smoke
    (``smoke_test=false`` in the response) because the smoke flag
    propagates exclusively through the worker's review_pass payload.
    Direct API integrators that want smoke-tagged feedback should
    invoke the saga path (create + accept + complete a smoke task)
    rather than POST against this endpoint.
    """
    _require_reputation_service(reputation_service)
    _enforce_not_self(caller["agent_id"], agent_id)
    await _fetch_agent_or_404(agent_service, agent_id)
    task = await _fetch_task_for_reputation(task_service, body.task_id)
    _enforce_feedback_authorisation(task, caller["agent_id"], agent_id)

    try:
        event = await reputation_service.record_feedback(  # type: ignore[union-attr]
            agent_id=agent_id,
            task_id=body.task_id,
            signer=caller["agent_id"],
            score=body.score,
            evidence_uri=body.evidence_uri,
            # Smoke flag propagation is for the worker path only —
            # the route always submits ``task_metadata=None``. See
            # docstring above.
            task_metadata=None,
        )
    except ValueError as e:
        # Service-layer validation (defense in depth — route-layer
        # checks above should catch the same things, but service
        # might catch corner cases like very-long evidence_uri).
        # ``reason`` is a static token; raw ``str(e)`` may include
        # internal field hints we don't want to surface externally
        # (the full message is preserved in the chained ``from e``
        # logger output for operators).
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={"reason": "invalid_request"},
        ) from e

    return _event_to_response(event)


@router.post(
    "/agents/{agent_id}/validations",
    response_model=ReputationEventResponse,
    status_code=201,
)
@limiter.limit("60/minute")
async def post_agent_validation(
    request: Request,
    agent_id: str,
    body: ValidationRequest,
    caller: AgentApiKeyDep = None,
    agent_service: AgentServiceDep = None,
    task_service: TaskServiceDep = None,
    reputation_service: ReputationServiceDep = None,
):
    """Submit one ERC-8004 validation event against ``agent_id``.

    Same idempotency and auth-shape as :func:`post_agent_feedback`,
    with these differences:

    * Caller is a **third party** (NOT the task creator and NOT the
      assignee — creator should use ``/feedback`` instead, assignee
      writing about themselves is self-validation).
    * ``attestation`` is required (validator's signed proof). v0.1
      does NOT verify the signature — that requires a validator
      registry which is v0.2 — but the field shape is fixed so v0.2
      can verify retroactively without backfill.
    * Unique key is ``(agent_id, task_id, 'validation')`` so the same
      task can receive both feedback and validation (different ``kind``
      means no UNIQUE collision).
    """
    _require_reputation_service(reputation_service)
    # Empty attestation is a malformed validation payload — pydantic
    # accepts ``{}`` as a valid dict, so we enforce the non-empty
    # invariant at the route boundary. ``ReputationService.record_validation``
    # also catches this (defense in depth), but checking here surfaces
    # a route-shaped 400 before any of the more expensive fetches run.
    if not body.attestation:
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={"reason": "attestation_required"},
        )
    _enforce_not_self(caller["agent_id"], agent_id)
    await _fetch_agent_or_404(agent_service, agent_id)
    task = await _fetch_task_for_reputation(task_service, body.task_id)
    _enforce_validation_authorisation(task, caller["agent_id"], agent_id)

    try:
        event = await reputation_service.record_validation(  # type: ignore[union-attr]
            agent_id=agent_id,
            task_id=body.task_id,
            signer=caller["agent_id"],
            attestation=body.attestation,
            score=body.score,
            evidence_uri=body.evidence_uri,
            task_metadata=None,
        )
    except ValueError as e:
        # ``reason`` is a static token (matches ``record_feedback``
        # error shape); the full message stays on the server side
        # via the chained ``from e`` for operator triage.
        raise ACNHTTPError(
            ErrorCode.INVALID_REQUEST,
            status_code=400,
            details={"reason": "invalid_request"},
        ) from e

    return _event_to_response(event)


@router.get(
    "/agents/{agent_id}/reputation/summary",
    response_model=ReputationSummary,
)
async def get_agent_reputation_summary(
    agent_id: str,
    include_smoke_test: bool = False,
    recent_limit: int = 20,
    agent_service: AgentServiceDep = None,
    reputation_query_service: ReputationQueryServiceDep = None,
):
    """Return a merged off-chain + on-chain reputation summary.

    Why a new GET sibling rather than extending GET
    ``/agents/{id}/reputation``: the existing endpoint returns the raw
    chain projection (``token_id``, ``count``, ``avg_value``,
    ``by_tag``) and is consumed by SDKs that expect that exact shape.
    Adding off-chain fields to it would be a breaking change for those
    SDKs. The new ``/reputation/summary`` endpoint is opt-in for
    callers (UI, ops dashboards) that want the full v0.1 picture.

    Behaviour matrix:

    * No PG → ``off_chain`` zeroed, ``on_chain`` from chain if the
      agent has a bound token, else ``None``.
    * No chain binding → ``off_chain`` populated, ``on_chain=None``,
      ``source='off_chain'``.
    * Both wired and present → ``source='merged'``, both fields filled.

    Note: in Redis-only deployments ``reputation_query_service`` is
    None; the dependency-layer ``get_reputation_query_service`` falls
    back to a chain-only singleton constructed at lifespan time so we
    don't reconstruct one per request.
    """
    # If reputation service isn't wired at all (Redis-only AND chain
    # disabled) the route still returns a zero-filled summary so SDKs
    # don't have to special-case the response shape — the contract
    # promises an always-populated ``off_chain`` block.
    agent = await _fetch_agent_or_404(agent_service, agent_id)
    on_chain_token_id: int | None = None
    if agent.erc8004_agent_id:
        on_chain_token_id = _parse_token_id_or_422(
            agent.erc8004_agent_id, agent_id
        )

    if reputation_query_service is None:
        # Dependency layer didn't construct one — operate in fully
        # degraded mode (no off-chain, no chain merge).
        return ReputationSummary(
            agent_id=agent_id,
            off_chain=OffChainReputationSummary(
                feedback_count=0,
                validation_count=0,
                recent_events=[],
            ),
            on_chain=None,
            source="off_chain",
        )

    return await reputation_query_service.get_summary(
        agent_id=agent_id,
        on_chain_token_id=on_chain_token_id,
        include_smoke_test=include_smoke_test,
        recent_limit=recent_limit,
    )


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
