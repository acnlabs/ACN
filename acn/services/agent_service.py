"""Agent Service

Business logic for agent registration, discovery, and management.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
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

# Heartbeat TTL policy (seconds)
ALIVE_GRACE_TTL = 1800  # 30 min — grace period after join, no heartbeat yet
ALIVE_RENEW_TTL = 3600  # 60 min — renewed on each heartbeat call

# Cap for heartbeat-declared model lists (Interfaze composer dropdown).
_SUPPORTED_MODELS_MAX = 50


def _normalize_supported_model_ids(models: list[str] | None) -> list[str]:
    """Dedupe + trim Host Catalog model ids from heartbeat self-report."""
    if not models:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in models:
        if not isinstance(raw, str):
            continue
        mid = raw.strip()[:200]
        if not mid:
            continue
        key = mid.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(mid)
        if len(out) >= _SUPPORTED_MODELS_MAX:
            break
    return out


# Inbound reachability policy (decoupled from ``alive``). These describe how a
# real direct-push history is summarized into a tri-state ``reachable``:
#   - retain the per-agent record for a week so an abandoned endpoint's last
#     known state stays inspectable without growing unbounded;
#   - treat a success as "reachable" only while it is fresher than one hour
#     (matches ALIVE_RENEW_TTL — same staleness horizon as online-ness);
#   - declare "unreachable" once N consecutive pushes have failed, so a single
#     blip never flips the verdict.
INBOUND_HEALTH_TTL = 7 * 24 * 3600  # 7 days — record retention
INBOUND_REACHABLE_WINDOW = 3600  # 60 min — a success older than this is stale
INBOUND_UNREACHABLE_FAILS = 3  # consecutive failures → reachable=False

# How long an agent-initiated deletion request stays open for the human
# owner to confirm before it expires. 72h covers a long weekend so a
# request raised Friday isn't silently dead by Monday.
DELETION_REQUEST_TTL_SECONDS = 72 * 3600

logger = structlog.get_logger()


def _within_window(iso_ts: str, window_seconds: int) -> bool:
    """True if ISO-8601 ``iso_ts`` is within ``window_seconds`` of now (UTC).

    Tolerant of malformed/empty timestamps (returns ``False``) so a corrupt
    Redis value can never raise on the read path.
    """
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() <= window_seconds


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
        payment_discovery: PaymentDiscoveryService | None = None,
    ):
        """
        Initialize Agent Service

        Args:
            agent_repository: Agent repository implementation
            payment_discovery: Payment discovery service for auto-indexing payment capabilities
        """
        self.repository = agent_repository
        self.payment_discovery = payment_discovery
        # Optional WebhookService — wired post-construction in lifespan
        # (see api.py). When present, ownership changes (claim / transfer /
        # release) emit ``agent.owner_changed`` to the platform Backend so
        # it can re-point the agent wallet's owner. Left None in unit tests
        # and webhook-disabled deployments (emit is then a no-op).
        self.webhook_service: object | None = None
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

        # ADR-0007 Phase 3: agents no longer get an Auth0 M2M credential at
        # registration. Identity is the acn_* API key (minted into short-lived
        # JWTs at /oauth/token), so there is no backend provisioning call here.

        # Notify platform Backend for cultivator growth G2 (create+register with owner).
        if owner:
            await self._emit_owner_changed(agent, None, "register")

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

    async def find_agent(self, agent_id: str) -> Agent | None:
        """Get agent by ID, returning ``None`` when absent.

        Sibling of :py:meth:`get_agent`. Use this in code paths that
        treat "missing agent" as a normal control-flow branch rather
        than an exceptional condition — typically routes that map
        absence to a 404 response (``payments.py``, message routers,
        gateway re-fetches). Keeps callers off the
        ``try/except AgentNotFoundException`` ladder, which was
        introduced when ``AgentService.get_agent`` replaced
        ``AgentRegistry.get_agent`` (the latter returned ``None``
        directly).
        """
        return await self.repository.find_by_id(agent_id)

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

    async def update_profile(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        invoke_slots: list[dict] | None = None,
        chat_invitees: list[str] | None = None,
    ) -> Agent:
        """Partial update of an agent's editable metadata.

        Only the fields passed as non-``None`` are changed; the rest keep
        their stored values. This is a PATCH (partial) update, not a PUT
        (replace) — omitting a field must never blank it out.

        Schema validation (name format, length caps, tag count) lives at
        the route layer (``ProfilePatchRequest``), so by the time we get
        here the values are already vetted.

        ``tags`` is replaced wholesale when provided (a list is the unit
        of update — there is no per-tag add/remove here); pass the full
        desired list. An empty list clears all tags.

        ``invoke_slots`` is stored on ``metadata.invoke_slots`` (AgentRouter
        P2). An empty list clears the declaration. Route layer already
        normalized unknown ids away.

        Args:
            agent_id: Agent identifier.
            name: New display name, or ``None`` to leave unchanged.
            description: New description, or ``None`` to leave unchanged.
            tags: New full tag list, or ``None`` to leave unchanged.
            invoke_slots: Platform-owned slot contracts, or ``None`` to
                leave unchanged.
            chat_invitees: Human user ids allowed to invoke (AgentRouter
                P9). Empty list clears. ``None`` leaves unchanged.

        Raises:
            AgentNotFoundException: If the agent does not exist.
        """
        agent = await self.get_agent(agent_id)
        if name is not None:
            agent.name = name
        if description is not None:
            agent.description = description
        if tags is not None:
            agent.tags = list(tags)
        if invoke_slots is not None or chat_invitees is not None:
            meta = dict(agent.metadata or {})
            if invoke_slots is not None:
                if invoke_slots:
                    meta["invoke_slots"] = list(invoke_slots)
                else:
                    meta.pop("invoke_slots", None)
            if chat_invitees is not None:
                if chat_invitees:
                    meta["chat_invitees"] = list(chat_invitees)
                else:
                    meta.pop("chat_invitees", None)
            agent.metadata = meta
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

    async def update_endpoint(
        self,
        agent_id: str,
        endpoint: str | None,
    ) -> Agent:
        """Set or clear an agent's delivery endpoint after registration.

        This is the pull→push upgrade path: a manifest-mode agent that
        later deploys an HTTPS server can register the endpoint here
        without re-joining (which would mint a new ``agent_id`` and lose
        its identity, reputation, and subnet memberships).

        ``endpoint`` is mirrored onto both ``endpoint`` and
        ``a2a_endpoint`` so the two stay in sync (the entity's
        ``__post_init__`` only back-fills one from the other when exactly
        one is set; assigning both explicitly avoids relying on that).
        Passing ``None`` clears both URL fields. Combined with the agent's
        current reception policy that means:

        - push mode (``open`` / ``allowlist``) + cleared URL → Mode B
          **relay** (ADR-0012; prefer :meth:`switch_to_relay`);
        - ``manifest`` / ``closed`` + cleared URL → pull-only / reject
          (no real-time push transport).

        URL validation (scheme, ACN-gateway-host rejection, SSRF) and any
        reachability probe live at the route layer — by the time we get
        here the value is either ``None`` or a vetted URL string.

        The bare ``PATCH /endpoint`` route still rejects clear-in-push so
        operators cannot accidentally leave an open agent with nowhere to
        deliver; intentional Mode A → Mode B goes through
        ``PATCH /agents/{id}/delivery`` → :meth:`switch_to_relay`.

        Args:
            agent_id: Agent identifier.
            endpoint: New delivery URL, or ``None`` to clear.

        Raises:
            AgentNotFoundException: If the agent does not exist.
        """
        agent = await self.get_agent(agent_id)
        agent.endpoint = endpoint or None
        agent.a2a_endpoint = endpoint or None
        await self.repository.save(agent)
        return agent

    async def switch_to_relay(self, agent_id: str) -> Agent:
        """Clear the direct delivery URL so inbound push uses Mode B relay.

        ADR-0012: a push-mode agent (``open`` / ``allowlist``) with no
        direct endpoint is reached over its outbound WebSocket
        (``acn listen``). This is the intentional A→B migration path;
        policy mode is left unchanged. Reception-policy / push-mode
        checks live at the route layer
        (``PATCH /agents/{id}/delivery``).

        Args:
            agent_id: Agent identifier.

        Raises:
            AgentNotFoundException: If the agent does not exist.
        """
        return await self.update_endpoint(agent_id, None)

    async def set_direct_delivery(self, agent_id: str, endpoint: str) -> Agent:
        """Set a direct A2A URL so inbound push uses Mode A HTTP dial.

        Counterpart to :meth:`switch_to_relay` (B→A). Reachability /
        handshake probes run at the route layer before this is called.

        Args:
            agent_id: Agent identifier.
            endpoint: Non-empty direct A2A delivery URL.

        Raises:
            AgentNotFoundException: If the agent does not exist.
            ValueError: If ``endpoint`` is empty.
        """
        if not endpoint:
            raise ValueError("endpoint is required for direct delivery")
        return await self.update_endpoint(agent_id, endpoint)

    async def search_agents(
        self,
        tags: list[str] | None = None,
        status: str = "online",
        slug: str | None = None,
    ) -> list[Agent]:
        """
        Search for agents.

        Args:
            tags: Required tags (filters agents that have ALL tags)
            status: Agent status filter ("online" | "offline" | "all")
            slug: Subnet slug filter (renamed from ``subnet_id`` in Step 2)

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
        if slug:
            candidates = await self.repository.find_by_subnet(slug)
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
        if deleted:
            # Best-effort follow-graph cleanup; stale follow entries are
            # cosmetic so we do not unwind the delete on cleanup error.
            if self.follow_service is not None:
                try:
                    await self.follow_service.cleanup_agent(agent_id)  # type: ignore[union-attr]
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "follow_cleanup_failed_on_unregister",
                        agent_id=agent_id,
                        error=str(e),
                    )
            # Remove from payment discovery index so the agent no longer
            # appears in /payments/discover after deletion.
            if self.payment_discovery is not None:
                try:
                    await self.payment_discovery.remove_payment_capability(agent_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "payment_discovery_cleanup_failed_on_unregister",
                        agent_id=agent_id,
                        error=str(e),
                    )
        return deleted

    async def request_deletion(self, agent_id: str) -> tuple[Agent, str]:
        """Open a pending deletion request on a **claimed** agent.

        This is the agent-initiated path for an agent that has a human
        owner: the agent (via its API key) asks to be deleted, but the
        actual deletion requires the owner to confirm — mirroring the
        claim flow in reverse.

        The pending request lives in ``metadata["deletion_request"]`` as
        ``{token_hash, requested_at, expires_at}``. Only the SHA-256 hash
        of the one-time token is stored; the plaintext token is returned
        once (for the confirmation URL) and never persisted. The public
        serializer (``_agent_entity_to_info``) strips ``token_hash`` and
        surfaces a ``pending_deletion`` marker instead.

        Returns ``(agent, plaintext_token)``.

        Raises:
            AgentNotFoundException: If the agent does not exist.
            ValueError: If the agent is unclaimed (those delete directly,
                with no human to confirm).
        """
        agent = await self.get_agent(agent_id)
        if agent.owner is None:
            raise ValueError(
                "Agent is unclaimed; delete it directly instead of requesting "
                "owner confirmation."
            )
        token = generate_verification_code()
        now = datetime.now(UTC)
        metadata = dict(agent.metadata or {})
        metadata["deletion_request"] = {
            "token_hash": hash_api_key(token),
            "requested_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=DELETION_REQUEST_TTL_SECONDS)).isoformat(),
        }
        agent.metadata = metadata
        await self.repository.save(agent)
        logger.info("agent_deletion_requested", agent_id=agent_id)
        return agent, token

    async def confirm_deletion(self, agent_id: str, owner: str, token: str) -> bool:
        """Confirm and execute a pending deletion as the agent's owner.

        Validates that (a) a pending request exists, (b) the caller is the
        agent's owner, (c) the request hasn't expired, and (d) the token
        matches — then delegates to ``unregister_agent`` so the same
        follow-graph / payment-discovery cleanup runs as any other delete.

        Raises:
            AgentNotFoundException: If the agent does not exist.
            PermissionError: If ``owner`` is not the agent's owner.
            ValueError: If there is no pending request, it has expired, or
                the token is invalid.
        """
        agent = await self.get_agent(agent_id)
        req = (agent.metadata or {}).get("deletion_request")
        if not req:
            raise ValueError("No pending deletion request for this agent.")
        if agent.owner != owner:
            raise PermissionError(f"Owner mismatch: {owner} != {agent.owner}")

        expires_at_raw = req.get("expires_at", "")
        try:
            expired = datetime.fromisoformat(expires_at_raw) < datetime.now(UTC)
        except ValueError:
            expired = True
        if expired:
            # Clear the stale request so the agent returns to a clean state.
            await self._clear_deletion_request(agent)
            raise ValueError("Deletion request has expired; request deletion again.")

        if not secrets.compare_digest(hash_api_key(token), req.get("token_hash", "")):
            raise ValueError("Invalid deletion token.")

        # Owner matches → reuse the canonical delete path (cleanups included).
        return await self.unregister_agent(agent_id, owner)

    async def cancel_deletion(self, agent_id: str) -> Agent:
        """Cancel a pending deletion request, returning the agent to normal.

        Idempotent: clearing a non-existent request is a no-op.

        Raises:
            AgentNotFoundException: If the agent does not exist.
        """
        agent = await self.get_agent(agent_id)
        await self._clear_deletion_request(agent)
        return agent

    async def _clear_deletion_request(self, agent: Agent) -> None:
        """Drop the ``deletion_request`` marker from an agent and persist."""
        if (agent.metadata or {}).get("deletion_request") is None:
            return
        metadata = dict(agent.metadata or {})
        metadata.pop("deletion_request", None)
        agent.metadata = metadata
        await self.repository.save(agent)

    async def update_heartbeat(
        self,
        agent_id: str,
        *,
        preferred_model: str | None = None,
        supported_models: list[str] | None = None,
    ) -> Agent:
        """
        Update agent heartbeat

        Args:
            agent_id: Agent identifier
            preferred_model: Optional Host Catalog model id the runtime
                currently uses. Stored on ``metadata.preferred_model`` for
                Host Pricing prefill — **self-reported, not verified**.
            supported_models: Optional list of Host Catalog model ids this
                runtime can run (Interfaze composer dropdown). Stored on
                ``metadata.supported_models`` — **self-reported**. ``None``
                leaves prior value unchanged; ``[]`` clears it.

        Returns:
            Updated agent entity
        """
        agent = await self.get_agent(agent_id)
        # ``set_alive`` is the single source of truth for online-ness;
        # ``update_heartbeat`` stamps the audit-only ``last_heartbeat``
        # field. The legacy DB ``status`` column is gone.
        agent.update_heartbeat()
        meta_dirty = False
        meta = dict(agent.metadata or {})
        if preferred_model is not None:
            mid = preferred_model.strip()[:200]
            if mid:
                meta["preferred_model"] = mid
                desired = str(meta.get("desired_preferred_model") or "").strip()
                if desired and desired.lower() == mid.lower():
                    meta.pop("desired_preferred_model", None)
            else:
                meta.pop("preferred_model", None)
            meta_dirty = True
        if supported_models is not None:
            normalized = _normalize_supported_model_ids(supported_models)
            if normalized:
                meta["supported_models"] = normalized
            else:
                meta.pop("supported_models", None)
            meta_dirty = True
        if meta_dirty:
            agent.metadata = meta
        await self.repository.save(agent)
        await self.repository.set_alive(agent_id, ALIVE_RENEW_TTL)
        return agent

    async def set_desired_preferred_model(self, agent_id: str, model_id: str) -> Agent:
        """Owner-requested default. Cleared when heartbeat preferred matches."""
        agent = await self.get_agent(agent_id)
        mid = (model_id or "").strip()[:200]
        if not mid:
            raise ValueError("preferred_model_required")
        meta = dict(agent.metadata or {})
        meta["desired_preferred_model"] = mid
        agent.metadata = meta
        await self.repository.save(agent)
        return agent

    async def clear_desired_preferred_model(self, agent_id: str) -> Agent:
        """Drop a pending Owner default after apply failed (offline / timeout / reject)."""
        agent = await self.get_agent(agent_id)
        meta = dict(agent.metadata or {})
        if "desired_preferred_model" not in meta:
            return agent
        meta.pop("desired_preferred_model", None)
        agent.metadata = meta
        await self.repository.save(agent)
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

    async def record_inbound_delivery(
        self,
        agent_id: str,
        *,
        ok: bool,
        probe_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        """Record a real inbound direct-push outcome.

        Source of truth for **inbound reachability** — distinct from ``alive``,
        which conflates outbound liveness (heartbeats / authenticated calls)
        with inbound deliverability. Only ``MessageRouter.route()``'s Mode-A
        push result writes here. Best-effort: a Redis blip must never turn a
        delivered (or correctly-parked) message into a failure, so all
        exceptions are swallowed.
        """
        try:
            await self.repository.record_inbound_delivery(
                agent_id,
                ok=ok,
                probe_ms=probe_ms,
                error=error,
                ttl=INBOUND_HEALTH_TTL,
            )
        except Exception as e:  # noqa: BLE001 — best-effort observability write
            logger.warning(
                "record_inbound_delivery_failed",
                agent_id=agent_id,
                error=str(e),
            )

    async def get_inbound_health(self, agent_id: str) -> dict[str, object]:
        """Inbound reachability for *agent_id*, driven by real push outcomes.

        Returns a dict always carrying a tri-state ``reachable``:
          - ``True``  — a recent successful push (``last_ok_at`` within
            ``INBOUND_REACHABLE_WINDOW``) and not currently in a failure streak
          - ``False`` — ``consec_fail`` has reached
            ``INBOUND_UNREACHABLE_FAILS`` (the endpoint is consistently failing)
          - ``None``  — unknown: no push has been attempted, or the last
            success has aged out without a failure streak

        ``reachable`` intentionally answers a different question from
        ``is_alive``: "can ACN actually push to this agent right now?" rather
        than "has this agent done anything recently?".
        """
        record = await self.repository.get_inbound_health(agent_id)
        if not record:
            return {"reachable": None}

        raw_fail = record.get("consec_fail", 0)
        consec_fail = int(raw_fail) if isinstance(raw_fail, (int, str, bytes)) else 0
        last_ok_at = record.get("last_ok_at")

        reachable: bool | None
        if consec_fail >= INBOUND_UNREACHABLE_FAILS:
            reachable = False
        elif last_ok_at and _within_window(str(last_ok_at), INBOUND_REACHABLE_WINDOW):
            reachable = True
        else:
            reachable = None

        return {"reachable": reachable, **record}

    async def get_agents_by_owner(self, owner: str) -> list[Agent]:
        """
        Get all agents owned by a user

        Args:
            owner: Owner identifier

        Returns:
            List of owned agents
        """
        return await self.repository.find_by_owner(owner)

    async def join_subnet(self, agent_id: str, slug: str) -> Agent:
        """
        Add agent to a subnet

        Args:
            agent_id: Agent identifier
            slug: Subnet slug

        Returns:
            Updated agent entity
        """
        agent = await self.get_agent(agent_id)
        agent.add_to_subnet(slug)
        await self.repository.save(agent)
        logger.info("agent_joined_subnet", agent_id=agent_id, slug=slug)
        return agent

    async def leave_subnet(self, agent_id: str, slug: str) -> Agent:
        """
        Remove agent from a subnet

        Args:
            agent_id: Agent identifier
            slug: Subnet slug

        Returns:
            Updated agent entity
        """
        agent = await self.get_agent(agent_id)
        agent.remove_from_subnet(slug)
        await self.repository.save(agent)
        logger.info("agent_left_subnet", agent_id=agent_id, slug=slug)
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

    @staticmethod
    def _is_self_hosted(agent: Agent) -> bool:
        """True when the owner operates the agent directly (holds the key).

        Marked at ``/join`` via ``self_hosted=true`` and stored in metadata.
        Gates key rotation on ownership change: self-hosted agents always rotate
        and the new plaintext is returned to the claimer. Managed agents rotate
        only when ``managed_rotate_on_transfer`` is enabled, and then the key is
        invalidated without surfacing plaintext (re-keyed out-of-band).
        """
        return bool((agent.metadata or {}).get("self_hosted"))

    @staticmethod
    def _managed_rotate_enabled() -> bool:
        """True when managed agents should also rotate (invalidate) on transfer.

        Off by default: invalidating a managed key drops the running instance
        until the platform re-keys it, so this is gated until the new-key
        delivery path (L0 AM rekey queue / L1 WS hot-swap) is live. See
        ``Settings.managed_rotate_on_transfer`` and P3 §15.7.
        """
        from ..config import get_settings

        return bool(getattr(get_settings(), "managed_rotate_on_transfer", False))

    async def _emit_owner_changed(
        self,
        agent: Agent,
        previous_owner: str | None,
        change_type: str,
    ) -> None:
        """Notify the platform Backend that an agent's owner changed.

        Best-effort: a webhook failure must never roll back the ownership
        mutation (the DB is the source of truth; the durable outbox retries
        delivery). No-op when no WebhookService is wired (unit tests /
        webhook-disabled deployments).
        """
        if self.webhook_service is None:
            return
        from acn.protocols.ap2.webhook import WebhookEventType

        try:
            await self.webhook_service.send_event(
                event=WebhookEventType.AGENT_OWNER_CHANGED,
                task_id=agent.agent_id,
                data={
                    "agent_id": agent.agent_id,
                    "previous_owner": previous_owner,
                    "new_owner": agent.owner,
                    "change_type": change_type,
                    "owner_changed_at": (
                        agent.owner_changed_at.isoformat() if agent.owner_changed_at else None
                    ),
                    # Re-key signal for the platform Backend (P3 §15.5). When a
                    # platform-managed agent's key is rotated on transfer, ACN
                    # burns the old key to a fresh hash it cannot surface as
                    # plaintext (``key_invalidated``). The managed instance can
                    # no longer authenticate and must be re-keyed out-of-band by
                    # the hosting operator (AgentMother): the Backend uses this
                    # flag to enqueue a re-key work order. Self-hosted agents
                    # (``self_hosted``) instead mint a working key via
                    # /rotate-key themselves, so no work order is created.
                    "key_invalidated": bool(agent.key_invalidated),
                    "self_hosted": self._is_self_hosted(agent),
                },
                outbox=True,
            )
        except Exception as e:  # noqa: BLE001 — webhook must not break ownership ops
            logger.warning(
                "agent_owner_changed_webhook_failed",
                agent_id=agent.agent_id,
                change_type=change_type,
                error=str(e),
            )

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

        # Claim token is always required — prevents unauthorized claim
        if not verification_code:
            raise ValueError("Claim token is required")
        if not secrets.compare_digest(agent.verification_code or "", verification_code):
            raise ValueError("Invalid claim token")

        if agent.claim_status == ClaimStatus.PENDING_TRANSFER:
            expires_at = agent.transfer_invite_expires_at()
            if expires_at and datetime.now(UTC) > expires_at:
                raise ValueError("Transfer invite expired")

        previous_owner = agent.owner  # giver during PENDING_TRANSFER; None on first claim
        agent.claim(owner)
        agent.verification_code = None  # One-time use: invalidate after claim
        # P3 gift hand-off: when ownership actually moves between two humans
        # (previous_owner set → transfer-invite claim) for a SELF-HOSTED agent
        # (owner holds the key), rotate the API key so the giver's still-running
        # instance — which kept the old key — can no longer operate the agent or
        # move its wallet assets. The fresh plaintext rides out on the transient
        # ``rotated_api_key`` for one-time delivery to the claimer (the caller).
        # Skipped when: first claim (previous_owner None, nobody to lock out and
        # the deployer's key must keep working), no api_key (platform-managed),
        # or the agent is operator-managed (e.g. AgentMother) — there the
        # operator re-keys on the owner_changed event instead, so rotating here
        # would break the running instance with no security gain.
        if previous_owner is not None and agent.api_key:
            if self._is_self_hosted(agent):
                new_plaintext = generate_api_key()
                agent.api_key = hash_api_key(new_plaintext)
                agent.rotated_api_key = new_plaintext
            elif self._managed_rotate_enabled():
                # Managed: invalidate the old key (defense-in-depth against a
                # leak during the giver's tenure) but DO NOT surface a plaintext
                # to the claimer — the platform re-keys the running instance
                # out-of-band. Burn the key to a fresh random hash nobody holds.
                agent.api_key = hash_api_key(generate_api_key())
                agent.key_invalidated = True
        await self.repository.save(agent)

        logger.info(
            "agent_claimed",
            agent_id=agent_id,
            owner=owner,
            key_rotated=agent.rotated_api_key is not None,
            key_invalidated=agent.key_invalidated,
        )
        await self._emit_owner_changed(agent, previous_owner, "claim")
        return agent

    async def create_transfer_invite(
        self,
        agent_id: str,
        owner: str,
        ttl_seconds: int | None = None,
        *,
        settings: Settings | None = None,
    ) -> Agent:
        """Issue a one-time transfer invite for a claimed agent (owner unchanged until claim)."""
        from ..config import get_settings

        cfg = settings or get_settings()
        ttl = ttl_seconds if ttl_seconds is not None else cfg.transfer_invite_default_ttl_seconds
        ttl = min(max(ttl, 60), cfg.transfer_invite_max_ttl_seconds)

        agent = await self.get_agent(agent_id)

        if agent.owner != owner:
            raise PermissionError("Only owner can create transfer invite")
        if agent.claim_status == ClaimStatus.PENDING_TRANSFER:
            raise ValueError("Agent already has a pending transfer invite")
        if agent.claim_status != ClaimStatus.CLAIMED:
            raise ValueError("Agent must be claimed to create a transfer invite")

        code = generate_verification_code()
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        agent.begin_transfer_invite(code, expires_at)
        await self.repository.save(agent)

        logger.info(
            "transfer_invite_created",
            agent_id=agent_id,
            owner=owner,
            expires_at=expires_at.isoformat(),
        )
        return agent

    async def cancel_transfer_invite(self, agent_id: str, owner: str) -> Agent:
        """Revoke a pending transfer invite; restore claimed state."""
        agent = await self.get_agent(agent_id)

        if agent.owner != owner:
            raise PermissionError("Only owner can cancel transfer invite")
        if agent.claim_status != ClaimStatus.PENDING_TRANSFER:
            raise ValueError("No pending transfer invite")

        agent.cancel_transfer_invite()
        await self.repository.save(agent)

        logger.info("transfer_invite_cancelled", agent_id=agent_id, owner=owner)
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
        if agent.claim_status == ClaimStatus.PENDING_TRANSFER:
            # An invite is outstanding; transferring directly would race the
            # pending claim (recipient's verification_code is still live).
            # Force the owner to cancel the invite first.
            raise ValueError("Cancel the pending transfer invite before transferring")

        previous_owner = agent.owner
        agent.transfer(new_owner)
        # A direct transfer moves the agent to a different principal. For a
        # self-hosted agent (owner holds the key), rotate to lock out the
        # previous owner's deployed instance. The new plaintext is NOT surfaced
        # to the caller here (the caller is the *giver*, whom we are locking
        # out) — the new owner mints a working key via /rotate-key. Managed
        # agents (no marker) keep their key; the operator re-keys on the
        # owner_changed event.
        if agent.api_key:
            if self._is_self_hosted(agent):
                new_plaintext = generate_api_key()
                agent.api_key = hash_api_key(new_plaintext)
                agent.rotated_api_key = new_plaintext
            elif self._managed_rotate_enabled():
                # Managed: invalidate without surfacing plaintext (platform
                # re-keys the instance out-of-band). See claim_agent.
                agent.api_key = hash_api_key(generate_api_key())
                agent.key_invalidated = True
        await self.repository.save(agent)

        logger.info(
            "agent_transferred",
            agent_id=agent_id,
            from_owner=current_owner,
            to_owner=new_owner,
            key_rotated=agent.rotated_api_key is not None,
            key_invalidated=agent.key_invalidated,
        )
        await self._emit_owner_changed(agent, previous_owner, "transfer")
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
        if agent.claim_status == ClaimStatus.PENDING_TRANSFER:
            # release() would drop to public UNCLAIMED while leaving the
            # invite's verification_code live — cancel the invite first so
            # the one-time token is invalidated deterministically.
            raise ValueError("Cancel the pending transfer invite before releasing")

        previous_owner = agent.owner
        agent.release()
        await self.repository.save(agent)

        logger.info("agent_released", agent_id=agent_id, previous_owner=owner)
        await self._emit_owner_changed(agent, previous_owner, "release")
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

    async def upsert_performance(
        self,
        agent_id: str,
        performance: dict,
    ) -> Agent:
        """Replace ``metadata["performance"]`` with ``performance`` (wholesale).

        Other metadata keys are preserved; the performance block itself is
        not deep-merged. Clients cannot PATCH arbitrary metadata; this is
        the sole write path for denormalized completion / response signals
        used by auto-collab matching. Callers pass a full aggregate.
        """
        agent = await self.get_agent(agent_id)
        metadata = dict(agent.metadata or {})
        # Drop stale rate when aggregate omits it (cold sample window).
        metadata["performance"] = dict(performance)
        agent.metadata = metadata
        await self.repository.save(agent)
        logger.info(
            "agent_performance_upserted",
            agent_id=agent_id,
            settled=performance.get("settled"),
            has_rate="completion_rate" in performance,
        )
        return agent

    async def refresh_performance_from_history(
        self,
        agent_id: str,
        history_items: list[dict],
        *,
        min_samples: int = 3,
    ) -> dict:
        """Aggregate history items and persist ``metadata.performance``.

        ``history_items`` must already be fetched by the caller (typically
        ``TaskService.get_agent_task_history`` with
        ``limit=DEFAULT_HISTORY_LIMIT`` / last 50) to avoid a circular
        import between AgentService and TaskService.
        """
        from .agent_performance import aggregate_performance_from_history

        performance = aggregate_performance_from_history(
            history_items,
            min_samples=min_samples,
        )
        await self.upsert_performance(agent_id, performance)
        return performance


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
