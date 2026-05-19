"""Agent Service

Business logic for agent registration, discovery, and management.
"""

import hashlib
import secrets
from uuid import uuid4

import structlog  # type: ignore[import-untyped]

from ..config import Settings
from ..core.entities import Agent, ClaimStatus
from ..core.exceptions import AgentNotFoundException
from ..core.interfaces import IAgentRepository
from ..protocols.ap2.core import (
    PaymentCapability,
    PaymentDiscoveryService,
    SupportedNetwork,
    SupportedPaymentMethod,
    TokenPricing,
)
from .auth0_client import Auth0CredentialClient

# Heartbeat TTL policy (seconds)
ALIVE_GRACE_TTL = 1800  # 30 min — grace period after join, no heartbeat yet
ALIVE_RENEW_TTL = 3600  # 60 min — renewed on each heartbeat call

logger = structlog.get_logger()


def generate_api_key() -> str:
    """Generate a secure API key"""
    return f"acn_{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    """Return the SHA-256 hex digest of an API key.

    API keys are high-entropy (~256-bit) so plain SHA-256 (no salt) is
    sufficient — rainbow-table attacks are infeasible.  The hash is what
    gets stored in Redis/Postgres; the plaintext is only held in memory
    for the duration of a single request.
    """
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_verification_code() -> str:
    """Generate a cryptographically secure one-time claim token (256-bit entropy)"""
    return secrets.token_urlsafe(32)


class AgentService:
    """
    Agent Service

    Orchestrates agent-related business operations.
    Uses Repository pattern for persistence.
    """

    def __init__(
        self,
        agent_repository: IAgentRepository,
        auth0_client: Auth0CredentialClient | None = None,
        payment_discovery: PaymentDiscoveryService | None = None,
    ):
        """
        Initialize Agent Service

        Args:
            agent_repository: Agent repository implementation
            auth0_client: Auth0 credential client for creating Agent M2M credentials
            payment_discovery: Payment discovery service for auto-indexing payment capabilities
        """
        self.repository = agent_repository
        self.auth0_client = auth0_client
        self.payment_discovery = payment_discovery
        # Optional FollowService — wired post-construction in lifespan
        # because the follow subsystem depends on this service (would
        # otherwise create a circular dependency).  When present,
        # ``unregister_agent`` will additionally drop the deleted
        # agent's follow indexes so dangling pointers cannot accumulate.
        self.follow_service: object | None = None

    async def register_agent(
        self,
        owner: str,
        name: str,
        endpoint: str,
        a2a_endpoint: str | None = None,
        tags: list[str] | None = None,
        subnet_ids: list[str] | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        agent_card: dict | None = None,
        wallet_address: str | None = None,
        accepts_payment: bool = False,
        payment_methods: list[str] | None = None,
        communication_policy: dict | None = None,
        agent_card_url: str | None = None,
        social_card_url: str | None = None,
    ) -> Agent:
        """
        Register a new agent or update existing one

        Uses natural key idempotency: if owner + endpoint already exists,
        updates that agent; otherwise creates new one.

        Args:
            owner: Agent owner identifier
            name: Agent name
            endpoint: Direct Agent A2A JSON-RPC endpoint URL
            a2a_endpoint: Explicit direct Agent A2A JSON-RPC endpoint URL
            tags: List of capability tag IDs
            subnet_ids: Subnets to join
            description: Agent description
            metadata: Additional metadata
            wallet_address: Wallet address for payments
            accepts_payment: Whether agent accepts payments
            payment_methods: Accepted payment methods

        Returns:
            Registered agent entity
        """
        # Check for existing agent by owner + endpoint
        existing_agent = await self.repository.find_by_owner_and_endpoint(owner, endpoint)

        if existing_agent:
            # Update existing agent
            logger.info("update_existing_agent", agent_id=existing_agent.agent_id)
            existing_agent.name = name
            existing_agent.a2a_endpoint = a2a_endpoint or endpoint
            existing_agent.description = description
            existing_agent.tags = tags or []
            existing_agent.metadata = metadata or {}
            if agent_card is not None:
                existing_agent.agent_card = agent_card
            if agent_card_url is not None:
                existing_agent.agent_card_url = agent_card_url

            # Update subnets if provided
            if subnet_ids:
                existing_agent.subnet_ids = subnet_ids

            # Update payment info
            if wallet_address:
                existing_agent.wallet_address = wallet_address
                # Keep wallet_addresses["ethereum"] in sync with the legacy single-address field
                existing_agent.wallet_addresses = {
                    **existing_agent.wallet_addresses,
                    "ethereum": wallet_address,
                }
            existing_agent.accepts_payment = accepts_payment
            if payment_methods:
                existing_agent.payment_methods = payment_methods

            # Update communication policy only when the caller explicitly
            # provides one. A heartbeat-style re-register that omits the
            # field must NOT overwrite a previously configured policy.
            if communication_policy is not None:
                existing_agent.communication_policy = communication_policy

            # Same explicit-only semantics for the SOCIAL.md pointer:
            # a heartbeat re-register that omits the field must not blow
            # away a previously published URL. To clear the URL, callers
            # use the dedicated PATCH /agents/{id}/social-card-url
            # endpoint.
            if social_card_url is not None:
                existing_agent.social_card_url = social_card_url

            # The ``set_alive`` call below is the single source of truth
            # for online-ness (see ``touch_alive`` / ``search_agents``
            # for the read side). ``update_heartbeat`` is kept because
            # it stamps ``last_heartbeat`` which is a distinct, useful
            # audit field — but no DB ``status`` is written, that
            # column no longer exists.
            existing_agent.update_heartbeat()

            await self.repository.save(existing_agent)
            await self.repository.set_alive(existing_agent.agent_id, ALIVE_RENEW_TTL)
            await self._sync_payment_discovery(existing_agent)
            return existing_agent

        # Create new agent
        agent_id = str(uuid4())
        agent = Agent(
            agent_id=agent_id,
            owner=owner,
            name=name,
            endpoint=endpoint,
            a2a_endpoint=a2a_endpoint or endpoint,
            description=description,
            tags=tags or [],
            subnet_ids=subnet_ids or ["public"],
            metadata=metadata or {},
            agent_card=agent_card,
            wallet_address=wallet_address,
            accepts_payment=accepts_payment,
            payment_methods=payment_methods or [],
            communication_policy=communication_policy,
            agent_card_url=agent_card_url,
            social_card_url=social_card_url,
        )

        logger.info("register_new_agent", agent_id=agent_id, name=name)
        await self.repository.save(agent)
        await self.repository.set_alive(agent_id, ALIVE_GRACE_TTL)
        await self._sync_payment_discovery(agent)

        # 创建 Auth0 M2M 凭证（异步，不阻塞注册）
        if self.auth0_client:
            try:
                cred_result = await self.auth0_client.create_credentials(
                    agent_id=agent_id,
                    agent_name=name,
                )
                if cred_result.success:
                    agent.auth0_client_id = cred_result.client_id
                    agent.auth0_client_secret = cred_result.client_secret
                    agent.auth0_token_endpoint = cred_result.token_endpoint
                    await self.repository.save(agent)
                    logger.info(
                        "agent_auth0_credentials_assigned",
                        agent_id=agent_id,
                        client_id=cred_result.client_id,
                    )
                else:
                    logger.warning(
                        "agent_auth0_credentials_failed",
                        agent_id=agent_id,
                        error=cred_result.error,
                    )
            except Exception as e:
                logger.warning(
                    "agent_auth0_credentials_error",
                    agent_id=agent_id,
                    error=str(e),
                )

        return agent

    async def get_agent(self, agent_id: str) -> Agent:
        """
        Get agent by ID

        Args:
            agent_id: Agent identifier

        Returns:
            Agent entity

        Raises:
            AgentNotFoundException: If agent not found
        """
        agent = await self.repository.find_by_id(agent_id)
        if not agent:
            raise AgentNotFoundException(f"Agent {agent_id} not found")
        return agent

    async def update_social_card_url(
        self,
        agent_id: str,
        social_card_url: str | None,
    ) -> Agent:
        """Update an agent's ``social_card_url`` pointer.

        Mirrors ``update_communication_policy`` semantics: explicit
        ``None`` clears the URL (agent publishes nothing), any other
        value replaces the current pointer. Schema validation
        (``https://`` prefix, length cap) lives in ``AgentInfo`` /
        ``AgentRegisterRequest`` field validators and in
        ``Agent.__post_init__`` — by the time we get here, the value
        is either ``None`` or a vetted URL string.

        We deliberately do NOT fetch the URL or validate the body.
        That responsibility lives with consumers per the consumption
        model at https://agentsocial.one/consumption-model. ACN's
        contract is "we hold the pointer; the agent owns the body".

        Args:
            agent_id: Agent identifier.
            social_card_url: New URL, or ``None`` to clear.

        Raises:
            AgentNotFoundException: If agent not found.
        """
        agent = await self.get_agent(agent_id)
        agent.social_card_url = social_card_url or None
        await self.repository.save(agent)
        return agent

    async def update_communication_policy(
        self,
        agent_id: str,
        communication_policy: dict | None,
    ) -> Agent:
        """Update an agent's ``communication_policy``.

        Phase 1 L410-B: this is the user-facing path agents/owners
        use to flip themselves into ``closed`` mode (or back to
        ``open``). Schema validation happens at the route layer
        (``validate_policy_dict``), so by the time the value lands
        here it's either ``None`` (reset to default) or a vetted
        ``{"mode": ..., ["reject_reason": ...]}`` dict.

        Why we shallow-copy the input dict: the route-layer
        validator already hands us a fresh dict, but a future
        non-route caller could pass a shared reference. A shallow
        ``dict(...)`` is enough because the validated shape only
        contains primitive values (``mode`` str, optional
        ``reject_reason`` str) — no nested mutable structures to
        worry about. Skipping ``deepcopy`` keeps the hot path
        cheap; if Phase 2/3 introduces nested config (e.g.
        ``manifest`` thresholds with sub-dicts), revisit.

        Args:
            agent_id: Agent identifier.
            communication_policy: New policy dict, or ``None`` to
                reset to the default open policy.

        Returns:
            The updated Agent entity (post-save, so any
            entity-level normalization is reflected).

        Raises:
            AgentNotFoundException: If the agent does not exist.
        """
        agent = await self.get_agent(agent_id)
        # Always assign through the field — assigning ``None`` lets
        # the entity decide the default in ``__post_init__`` on
        # subsequent rehydration; assigning a dict stores it
        # verbatim. We normalize the in-memory ``agent`` here so
        # the returned entity already shows what's persisted.
        if communication_policy is None:
            agent.communication_policy = {"mode": "open"}
        else:
            agent.communication_policy = dict(communication_policy)
        await self.repository.save(agent)
        return agent

    async def search_agents(
        self,
        tags: list[str] | None = None,
        status: str = "online",
        subnet_id: str | None = None,
    ) -> list[Agent]:
        """
        Search for agents.

        Args:
            tags: Required tags (filters agents that have ALL tags)
            status: Agent status filter ("online" | "offline" | "all")
            subnet_id: Subnet filter

        Returns:
            List of matching agents

        Online semantics: "online" is defined as
        "Redis alive key present" — see ``_filter_by_status``. The
        legacy DB column ``agent.status`` is no longer consulted on
        the read path; this is the fix for the dual-source drift that
        used to make an agent stay invisible in listings after its
        ``alive`` TTL was refreshed by implicit-heartbeat but before
        an explicit ``POST /heartbeat`` re-stamped ``status='online'``
        in the database.
        """
        if subnet_id:
            candidates = await self.repository.find_by_subnet(subnet_id)
            if tags:
                candidates = [a for a in candidates if a.has_all_tags(tags)]
        elif tags:
            # Pass ``status="all"`` so the repository does NOT pre-filter by
            # the now-legacy DB column. The single source of truth for
            # online-ness is the Redis alive key, applied uniformly by
            # ``_filter_by_status`` below — pre-filtering at the repo would
            # remove agents that ``_filter_by_status`` would otherwise
            # include (the same dual-source drift as the dropped status
            # filters in this method).
            candidates = await self.repository.find_by_tags(tags)
        else:
            candidates = await self.repository.find_all()

        return await self._filter_by_status(candidates, status)

    async def _filter_by_status(
        self,
        candidates: list[Agent],
        status: str,
    ) -> list[Agent]:
        """Single source of truth: keep candidates whose alive key matches *status*.

        - ``"online"`` → ``agent_id`` is in the alive set
        - ``"offline"`` → ``agent_id`` is NOT in the alive set
        - ``"all"`` / falsy → no filter
        - anything else → no-op (route-layer validation owns rejection)

        Reads only Redis (via ``filter_alive``); the DB column
        ``agent.status`` is intentionally never consulted here. That column
        is a legacy field that Phase 2 will drop along with its partial
        index and the now-defunct heartbeat watchdog.
        """
        if not status or status == "all":
            return candidates
        if not candidates:
            return []
        alive_ids = await self.repository.filter_alive(
            [a.agent_id for a in candidates]
        )
        if status == "online":
            return [a for a in candidates if a.agent_id in alive_ids]
        if status == "offline":
            return [a for a in candidates if a.agent_id not in alive_ids]
        return candidates

    async def is_alive(self, agent_id: str) -> bool:
        """Whether *agent_id* currently holds an unexpired alive key.

        Single-source-of-truth check for "online". Prefer this over reading
        ``Agent.status`` for any read-time gate (listing serialization,
        delivery routing, broadcast eligibility, …) so the value never
        drifts from the Redis TTL behind it.

        For batch contexts (loops over many agents), use
        :py:meth:`batch_alive` to collapse to a single Redis round-trip.
        """
        alive_ids = await self.repository.filter_alive([agent_id])
        return agent_id in alive_ids

    async def batch_alive(self, agent_ids: list[str]) -> set[str]:
        """Batched ``is_alive`` for listing/serialization paths.

        Returns the subset of *agent_ids* whose alive key is currently
        present. Use this once per request alongside a listing query
        instead of looping over :py:meth:`is_alive` (which would issue one
        Redis EXISTS per agent).
        """
        if not agent_ids:
            return set()
        return await self.repository.filter_alive(agent_ids)

    async def unregister_agent(self, agent_id: str, owner: str) -> bool:
        """
        Unregister an agent

        Args:
            agent_id: Agent identifier
            owner: Owner identifier (for authorization check)

        Returns:
            True if unregistered successfully

        Raises:
            AgentNotFoundException: If agent not found
            PermissionError: If owner doesn't match
        """
        agent = await self.get_agent(agent_id)

        # Authorization check
        if agent.owner != owner:
            raise PermissionError(f"Owner mismatch: {owner} != {agent.owner}")

        logger.info("unregister_agent", agent_id=agent_id)
        deleted = await self.repository.delete(agent_id)
        # Best-effort follow-graph cleanup AFTER successful deletion so a
        # cleanup failure cannot leave the agent itself half-deleted.
        # Stale follow entries are cosmetic (lists ignore dangling ids
        # via the existence check in ``_resolve_agents_with_counts``)
        # so we do not unwind the agent delete on a cleanup error.
        if deleted and self.follow_service is not None:
            try:
                await self.follow_service.cleanup_agent(agent_id)  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning(
                    "follow_cleanup_failed_on_unregister",
                    agent_id=agent_id,
                    error=str(e),
                )
        return deleted

    async def update_heartbeat(self, agent_id: str) -> Agent:
        """
        Update agent heartbeat

        Args:
            agent_id: Agent identifier

        Returns:
            Updated agent entity
        """
        agent = await self.get_agent(agent_id)
        # ``set_alive`` is the single source of truth for online-ness;
        # ``update_heartbeat`` stamps the audit-only ``last_heartbeat``
        # field. The legacy DB ``status`` column is gone.
        agent.update_heartbeat()
        await self.repository.save(agent)
        await self.repository.set_alive(agent_id, ALIVE_RENEW_TTL)
        return agent

    async def touch_alive(self, agent_id: str) -> None:
        """Renew the agent's alive TTL without loading the Agent row.

        Called as a fire-and-forget background task whenever an authenticated
        agent request reaches ACN (any HTTP route via the agent-API-key
        dependencies, or a WebSocket HEARTBEAT frame). It refreshes only the
        Redis ``acn:agents:{id}:alive`` key so that an agent producing
        business traffic does not need a separate explicit ``/heartbeat`` cron
        to avoid being marked offline.

        Exceptions are swallowed: this is background work and must never
        affect the user-facing request that scheduled it. A transient Redis
        blip means the alive key is missing for one TTL window and the agent
        appears offline until the next refresh succeeds — which is the same
        behaviour the read side already shows for any agent that genuinely
        stopped heart-beating, so no special handling is required.
        """
        try:
            await self.repository.set_alive(agent_id, ALIVE_RENEW_TTL)
        except Exception as e:  # noqa: BLE001 — best-effort background renewal
            logger.warning(
                "touch_alive_failed",
                agent_id=agent_id,
                error=str(e),
            )

    async def get_agents_by_owner(self, owner: str) -> list[Agent]:
        """
        Get all agents owned by a user

        Args:
            owner: Owner identifier

        Returns:
            List of owned agents
        """
        return await self.repository.find_by_owner(owner)

    async def join_subnet(self, agent_id: str, subnet_id: str) -> Agent:
        """
        Add agent to a subnet

        Args:
            agent_id: Agent identifier
            subnet_id: Subnet identifier

        Returns:
            Updated agent entity
        """
        agent = await self.get_agent(agent_id)
        agent.add_to_subnet(subnet_id)
        await self.repository.save(agent)
        logger.info("agent_joined_subnet", agent_id=agent_id, subnet_id=subnet_id)
        return agent

    async def leave_subnet(self, agent_id: str, subnet_id: str) -> Agent:
        """
        Remove agent from a subnet

        Args:
            agent_id: Agent identifier
            subnet_id: Subnet identifier

        Returns:
            Updated agent entity
        """
        agent = await self.get_agent(agent_id)
        agent.remove_from_subnet(subnet_id)
        await self.repository.save(agent)
        logger.info("agent_left_subnet", agent_id=agent_id, subnet_id=subnet_id)
        return agent

    # ========== Autonomous Agent Methods ==========

    async def join_agent(
        self,
        name: str,
        description: str,
        tags: list[str],
        endpoint: str,
        a2a_endpoint: str | None = None,
        referrer_id: str | None = None,
        metadata: dict | None = None,
        agent_card: dict | None = None,
        wallet_addresses: dict[str, str] | None = None,
        accepts_payment: bool = False,
        payment_methods: list[str] | None = None,
        token_pricing: dict | None = None,
        communication_policy: dict | None = None,
        agent_card_url: str | None = None,
        social_card_url: str | None = None,
    ) -> tuple[Agent, str]:
        """
        Autonomous agent joins ACN (self-registration)

        Unlike register_agent (platform-managed), this allows agents
        to self-register without an owner. Returns an API key for auth.

        If payment info is provided at join time, the PaymentDiscovery index
        is automatically populated (有则即时同步). Payment info can also be
        set or updated later via POST /payments/{id}/payment-capability.

        Args:
            name: Agent name
            description: Agent description
            tags: List of capability tag IDs
            endpoint: Direct A2A JSON-RPC endpoint URL
            a2a_endpoint: Explicit direct A2A JSON-RPC endpoint URL
            referrer_id: ID of agent who referred this one
            metadata: Additional metadata
            agent_card: A2A Agent Card (v0.3.0) to store at registration time
            wallet_addresses: Per-network wallet addresses (e.g. {"ethereum": "0x..."})
            accepts_payment: Whether agent accepts payments
            payment_methods: Accepted payment methods
            token_pricing: Token-based pricing config

        Returns:
            Tuple of (Agent entity, API key)
        """
        agent_id = str(uuid4())
        api_key = generate_api_key()
        verification_code = generate_verification_code()

        agent = Agent(
            agent_id=agent_id,
            name=name,
            owner=None,  # No owner initially
            endpoint=endpoint,
            a2a_endpoint=a2a_endpoint or endpoint,
            description=description,
            tags=tags or [],
            subnet_ids=["public"],
            metadata=metadata or {},
            api_key=hash_api_key(api_key),  # store hash only; plaintext returned once below
            claim_status=ClaimStatus.UNCLAIMED,
            verification_code=verification_code,
            referrer_id=referrer_id,
            agent_card=agent_card,
            wallet_addresses=wallet_addresses or {},
            accepts_payment=accepts_payment,
            payment_methods=payment_methods or [],
            token_pricing=token_pricing,
            communication_policy=communication_policy,
            agent_card_url=agent_card_url,
            social_card_url=social_card_url,
        )

        logger.info("agent_joined", agent_id=agent_id, name=name, referrer_id=referrer_id)
        await self.repository.save(agent)
        await self.repository.set_alive(agent_id, ALIVE_GRACE_TTL)
        # Auto-sync payment discovery if payment info provided at join time
        await self._sync_payment_discovery(agent)
        return agent, api_key

    async def _sync_payment_discovery(self, agent: Agent) -> None:
        """Auto-sync agent payment capability to PaymentDiscovery index after save."""
        if not self.payment_discovery or not agent.accepts_payment:
            return
        try:
            valid_methods = []
            for m in agent.payment_methods:
                try:
                    valid_methods.append(SupportedPaymentMethod(m))
                except ValueError:
                    pass
            # Derive supported_networks from wallet_addresses keys
            valid_networks = []
            for net in agent.wallet_addresses:
                try:
                    valid_networks.append(SupportedNetwork(net))
                except ValueError:
                    pass
            token_pricing = None
            if agent.token_pricing:
                try:
                    token_pricing = TokenPricing(**agent.token_pricing)
                except Exception:
                    pass
            capability = PaymentCapability(
                accepts_payment=True,
                payment_methods=valid_methods,
                wallet_address=agent.wallet_address,
                wallet_addresses=agent.wallet_addresses,
                supported_networks=valid_networks,
                token_pricing=token_pricing,
            )
            await self.payment_discovery.index_payment_capability(agent.agent_id, capability)
            logger.info("payment_discovery_synced", agent_id=agent.agent_id)
        except Exception:
            logger.warning("payment_discovery_sync_failed", agent_id=agent.agent_id, exc_info=True)

    async def get_agent_by_api_key(self, api_key: str) -> Agent | None:
        """Find agent by API key (for authentication).

        Looks up by SHA-256 hash. There used to be a legacy plaintext
        fallback (``find_by_api_key_legacy`` + auto-migrate on first
        use) for agents registered before H1 hashed all keys. By
        v0.12.0 every live ``by_api_key`` index entry was already a
        SHA-256 hex (verified against the prod-shaped dev cluster:
        58/58 indexes were 64-char hex, 0 were ``acn_*`` plaintext),
        so the fallback became dead code and was removed alongside
        ``rotate_api_key`` — keeping it would have left a perpetually
        cold branch that future refactors could resurrect or break
        without anyone noticing.
        """
        return await self.repository.find_by_api_key(hash_api_key(api_key))

    async def claim_agent(
        self,
        agent_id: str,
        owner: str,
        verification_code: str | None = None,
    ) -> Agent:
        """
        Claim ownership of an unclaimed agent

        Args:
            agent_id: Agent identifier
            owner: New owner identifier
            verification_code: Optional verification code

        Returns:
            Claimed agent entity

        Raises:
            AgentNotFoundException: If agent not found
            ValueError: If agent is already claimed or code doesn't match
        """
        agent = await self.get_agent(agent_id)

        if agent.claim_status == ClaimStatus.CLAIMED:
            raise ValueError(f"Agent {agent_id} is already claimed")

        # Claim token is always required — prevents unauthorized claim of any unclaimed agent
        if not verification_code:
            raise ValueError("Claim token is required")
        if not secrets.compare_digest(agent.verification_code or "", verification_code):
            raise ValueError("Invalid claim token")

        agent.claim(owner)
        agent.verification_code = None  # One-time use: invalidate after claim
        await self.repository.save(agent)

        logger.info("agent_claimed", agent_id=agent_id, owner=owner)
        return agent

    async def transfer_agent(
        self,
        agent_id: str,
        current_owner: str,
        new_owner: str,
    ) -> Agent:
        """
        Transfer agent ownership to another user

        Args:
            agent_id: Agent identifier
            current_owner: Current owner (for authorization)
            new_owner: New owner identifier

        Returns:
            Updated agent entity

        Raises:
            AgentNotFoundException: If agent not found
            PermissionError: If current_owner doesn't match
        """
        agent = await self.get_agent(agent_id)

        if agent.owner != current_owner:
            raise PermissionError("Only owner can transfer agent")

        agent.transfer(new_owner)
        await self.repository.save(agent)

        logger.info(
            "agent_transferred",
            agent_id=agent_id,
            from_owner=current_owner,
            to_owner=new_owner,
        )
        return agent

    async def release_agent(self, agent_id: str, owner: str) -> Agent:
        """
        Release ownership of an agent (make it unowned)

        Args:
            agent_id: Agent identifier
            owner: Current owner (for authorization)

        Returns:
            Updated agent entity

        Raises:
            AgentNotFoundException: If agent not found
            PermissionError: If owner doesn't match
        """
        agent = await self.get_agent(agent_id)

        if agent.owner != owner:
            raise PermissionError("Only owner can release agent")

        agent.release()
        await self.repository.save(agent)

        logger.info("agent_released", agent_id=agent_id, previous_owner=owner)
        return agent

    async def get_unclaimed_agents(self, limit: int = 100) -> list[Agent]:
        """
        Get all unclaimed agents

        Args:
            limit: Maximum number of agents to return

        Returns:
            List of unclaimed agents
        """
        return await self.repository.find_unclaimed(limit)

    async def rotate_api_key(self, agent_id: str) -> str:
        """Rotate an agent's API key.

        Generates a fresh high-entropy plaintext key, stores only its
        SHA-256 hash on the agent record, and returns the plaintext to
        the caller exactly once. The previous key's hash is overwritten,
        so any subsequent ``get_agent_by_api_key(old_key)`` returns
        ``None`` and the old credential cannot authenticate.

        Authorization is enforced at the route layer (owner or agent
        self) — this method assumes the caller has already passed that
        check. Cache invalidation is also the caller's responsibility
        because the in-memory cache lives in the routes layer.

        H1 (audit): completes the API-key hashing story. Previously
        callers had no way to replace a leaked key without re-registering
        the agent (which would also burn its agent_id, reputation, and
        on-chain ERC-8004 binding).
        """
        agent = await self.get_agent(agent_id)
        new_plaintext = generate_api_key()
        agent.api_key = hash_api_key(new_plaintext)
        await self.repository.save(agent)
        logger.info("agent_api_key_rotated", agent_id=agent_id)
        return new_plaintext


def build_erc8004_registration_file(
    agent: Agent,
    settings: Settings,
    *,
    is_online: bool,
) -> dict:
    """Build an ERC-8004 compliant agent registration file for the given agent.

    This JSON is served at /{agent_id}/.well-known/agent-registration.json
    and used as the on-chain agentURI when the agent registers on ERC-8004.

    ``is_online`` is **required** and feeds the ``active`` field — it must
    come from ``AgentService.is_alive`` (the single source of truth for
    online-ness). The legacy ``Agent.status`` column is no longer
    consulted; that column is a leftover that Phase 2 will drop.

    Spec: https://eips.ethereum.org/EIPS/eip-8004#registration-v1
    """
    base_url = settings.gateway_base_url
    file: dict = {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": agent.name,
        "description": agent.description or "",
        "services": [
            {
                "name": "A2A",
                "endpoint": (
                    f"{base_url}/api/v1/agents/{agent.agent_id}"
                    "/.well-known/agent-card.json"
                ),
                "version": settings.a2a_protocol_version,
            }
        ],
        "x402Support": agent.accepts_payment,
        "active": is_online,
    }

    # Top-level agentWallet field per ERC-8004 spec (plain Ethereum address)
    if agent.wallet_address:
        file["agentWallet"] = agent.wallet_address

    # Include on-chain registration reference once the agent has bound a token ID.
    # Pre-launch audit backlog #1: tolerate non-integer ``erc8004_agent_id``
    # (manually-edited rows / very old data) by skipping the ``registrations``
    # field instead of 5xx-ing the .well-known endpoint. The file is still
    # spec-valid without ``registrations`` (the field is optional).
    if agent.erc8004_agent_id:
        try:
            token_id = int(agent.erc8004_agent_id)
        except (TypeError, ValueError):
            logger.warning(
                "erc8004_corrupt_token_id_skipped",
                agent_id=agent.agent_id,
                stored_value=agent.erc8004_agent_id,
                action="omitted_registrations_field",
            )
        else:
            file["registrations"] = [
                {
                    "agentId": token_id,
                    "agentRegistry": (
                        f"eip155:{settings.erc8004_chain_id}"
                        f":{settings.erc8004_identity_contract}"
                    ),
                }
            ]

    return file
