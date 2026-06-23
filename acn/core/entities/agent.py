"""Agent Domain Entity

Pure business logic for Agent, independent of infrastructure.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

# ``AgentStatus`` deliberately removed from the entity layer.
#
# Online-ness is no longer modeled as a stored attribute of an
# ``Agent`` — it is derived at read time from the Redis
# ``acn:agents:{id}:alive`` TTL key via
# ``AgentService.is_alive`` / ``batch_alive``. The earlier dual-source
# drift (DB column vs Redis alive set) is gone for good.
#
# The API-layer enum ``acn.models.AgentStatus`` (online/offline/busy)
# is unchanged and remains the public contract used by ``AgentInfo``
# and the SDKs.


class ClaimStatus(StrEnum):
    """Agent claim status"""

    UNCLAIMED = "unclaimed"  # No owner yet
    CLAIMED = "claimed"  # Has owner
    PENDING_TRANSFER = "pending_transfer"  # Owner issued a one-time transfer invite


@dataclass
class Agent:
    """
    Agent Domain Entity

    Represents a registered AI agent in the ACN network.
    Contains business logic and invariants.

    Supports two registration modes:
    1. Platform Registration (managed): owner required, no api_key
    2. Autonomous Join: owner optional, api_key generated for auth
    """

    agent_id: str
    name: str

    # Owner is optional and mutable (supports claim, transfer, release)
    owner: str | None = None

    # Endpoint is optional for pull-mode agents
    endpoint: str | None = None
    # Explicit direct A2A JSON-RPC delivery URL. During the transition this
    # mirrors endpoint; keeping the named field makes the semantics visible.
    a2a_endpoint: str | None = None

    # ``status`` field deliberately removed in the alive-as-single-source
    # phase 2 refactor. See the module-level comment for ``AgentStatus``.
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    subnet_ids: list[str] = field(default_factory=lambda: ["public"])
    metadata: dict = field(default_factory=dict)
    registered_at: datetime = field(default_factory=datetime.now)
    last_heartbeat: datetime | None = None

    # Authentication (for autonomous agents)
    api_key: str | None = None

    # Transient (NOT persisted, NOT serialized): carries a freshly rotated
    # plaintext API key out to the route layer for one-time delivery to the
    # new owner. Set by AgentService.claim_agent / transfer_agent when an
    # ownership change rotates the key; never read back from storage.
    rotated_api_key: str | None = field(default=None, repr=False, compare=False)

    # Claim status (for autonomous agents)
    claim_status: ClaimStatus | None = None
    verification_code: str | None = None  # Short code for human verification

    # Referral tracking
    referrer_id: str | None = None  # Agent who referred this agent

    # Owner change tracking
    owner_changed_at: datetime | None = None

    # A2A Agent Card (stored as raw dict; provided by registrant or auto-generated on demand)
    agent_card: dict | None = None
    # Optional A2A Agent Card discovery URL. Persisted separately from the
    # direct delivery URL so ACN does not have to guess endpoint semantics.
    agent_card_url: str | None = None

    # Communication policy (gateway-level access control)
    # Default {"mode": "open"} backfilled in __post_init__ — keeps existing
    # agents on open behavior. See docs/features/acn-communication-economic-model.md.
    # Phase 1 supports modes: "open" | "closed".
    # Phase 2+ will extend with "manifest" | "allowlist" + rate_limit / allowlist fields.
    communication_policy: dict | None = None

    # Payment capabilities
    wallet_address: str | None = None  # Legacy single-address field (backward compat)
    wallet_addresses: dict[str, str] = field(default_factory=dict)  # Multi-chain: {network: address}
    accepts_payment: bool = False
    payment_methods: list[str] = field(default_factory=list)

    # Token-based pricing (OpenAI-style, per million tokens)
    # Format: {"input_price_per_million": 3.0, "output_price_per_million": 15.0, "currency": "USD"}
    token_pricing: dict | None = None

    # Agent Wallet - 钱包数据由 Backend 管理，不在 ACN 存储
    # [REMOVED] balance, total_earned, total_spent, owner_share - 全部迁移到 Backend Wallet

    # ERC-8004 On-Chain Identity (optional, self-registered by agent)
    erc8004_agent_id: str | None = None       # NFT token ID after on-chain registration
    erc8004_chain: str | None = None          # e.g. "eip155:8453"
    erc8004_tx_hash: str | None = None        # registration tx hash (informational)
    erc8004_registered_at: datetime | None = None

    # SOCIAL.md pointer (https://agentsocial.one spec).
    # Body is NEVER stored here — we only persist the URL and let consumers
    # fetch + cache per the consumption model defined at
    # https://agentsocial.one/consumption-model. Clients are expected to honor
    # Cache-Control / ETag from the source server.
    social_card_url: str | None = None

    def __post_init__(self):
        """Validate invariants"""
        if not self.agent_id:
            raise ValueError("agent_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        # Note: owner and endpoint are now optional
        if self.a2a_endpoint and not self.endpoint:
            self.endpoint = self.a2a_endpoint
        elif self.endpoint and not self.a2a_endpoint:
            self.a2a_endpoint = self.endpoint
        if not self.subnet_ids:
            self.subnet_ids = ["public"]
        # Backward compat: if legacy wallet_address is set but wallet_addresses is empty,
        # auto-populate ethereum entry so multi-chain lookup works transparently
        if self.wallet_address and not self.wallet_addresses:
            self.wallet_addresses = {"ethereum": self.wallet_address}
        # Keep wallet_address in sync with the primary address from wallet_addresses
        if self.wallet_addresses and not self.wallet_address:
            self.wallet_address = (
                self.wallet_addresses.get("ethereum")
                or self.wallet_addresses.get("base")
                or next(iter(self.wallet_addresses.values()), None)
            )
        # Normalize communication_policy so the gateway can always read
        # `agent.communication_policy["mode"]` without guarding for None,
        # empty dict, or a partial payload that forgot to set ``mode``.
        # ``open`` is the legacy default and keeps existing agents on the
        # original push-to-inbox behavior with no migration required.
        # See docs/features/acn-communication-economic-model.md.
        if not self.communication_policy:
            self.communication_policy = {"mode": "open"}
        elif "mode" not in self.communication_policy:
            # Caller passed a partial policy (e.g. just a reject_reason);
            # preserve their fields and fill in the missing mode.
            self.communication_policy = {
                "mode": "open",
                **self.communication_policy,
            }

        # Light validation for social_card_url. We accept None or any
        # https URL; we deliberately don't fetch it here (that's the
        # consumer's job) and we don't validate beyond the scheme to
        # avoid accidentally rejecting legitimate IDN / port forms.
        if self.social_card_url is not None:
            if not isinstance(self.social_card_url, str):
                raise ValueError("social_card_url must be a string or None")
            url = self.social_card_url.strip()
            if url and not url.lower().startswith(("https://", "http://")):
                raise ValueError("social_card_url must start with https:// or http://")
            self.social_card_url = url or None

    @property
    def primary_subnet(self) -> str:
        """Get primary subnet (for backward compatibility)"""
        return self.subnet_ids[0] if self.subnet_ids else "public"

    # ``is_online()`` deliberately removed.
    #
    # "Online" is no longer a property of the entity — it is a function
    # over the Redis ``alive`` TTL key, exposed by
    # ``AgentService.is_alive`` / ``batch_alive``. Reading
    # ``Agent.status`` to answer this question reintroduces the
    # dual-source drift the alive-as-single-source refactor eliminates.
    # Callers should ``await agent_service.is_alive(agent.agent_id)``.

    def is_in_subnet(self, slug: str) -> bool:
        """Check if agent belongs to a subnet"""
        return slug in self.subnet_ids

    def add_to_subnet(self, slug: str) -> None:
        """Add agent to a subnet"""
        if slug not in self.subnet_ids:
            self.subnet_ids.append(slug)

    def remove_from_subnet(self, slug: str) -> None:
        """Remove agent from a subnet"""
        if slug in self.subnet_ids:
            self.subnet_ids.remove(slug)
        # Ensure at least one subnet
        if not self.subnet_ids:
            self.subnet_ids = ["public"]

    def update_heartbeat(self) -> None:
        """Update last heartbeat timestamp"""
        self.last_heartbeat = datetime.now(UTC)

    # ``mark_offline()`` / ``mark_online()`` deliberately removed alongside
    # the ``status`` field — see the module-level ``AgentStatus`` comment.
    # Callers must let ``AgentService.set_alive`` /
    # ``AgentService.touch_alive`` write the Redis alive key instead; that
    # key is the single source of truth for online-ness.

    def has_tag(self, tag_id: str) -> bool:
        """Check if agent has a specific tag"""
        return tag_id in self.tags

    def has_all_tags(self, tag_ids: list[str]) -> bool:
        """Check if agent has all specified tags"""
        return all(tag in self.tags for tag in tag_ids)

    def can_accept_payment(self) -> bool:
        """Check if agent can accept payments"""
        return self.accepts_payment and bool(self.wallet_addresses or self.wallet_address)

    # ========== Ownership Methods ==========

    def is_owned(self) -> bool:
        """Check if agent has an owner"""
        return self.owner is not None

    def is_claimed(self) -> bool:
        """Check if agent has been claimed (includes pending transfer)."""
        return self.claim_status in (ClaimStatus.CLAIMED, ClaimStatus.PENDING_TRANSFER)

    def is_pending_transfer(self) -> bool:
        return self.claim_status == ClaimStatus.PENDING_TRANSFER

    def can_be_claimed(self) -> bool:
        """Check if agent can be claimed"""
        return self.claim_status in (ClaimStatus.UNCLAIMED, ClaimStatus.PENDING_TRANSFER)

    def transfer_invite_expires_at(self) -> datetime | None:
        raw = (self.metadata or {}).get("transfer_invite_expires_at")
        if not raw:
            return None
        return datetime.fromisoformat(raw)

    def begin_transfer_invite(self, verification_code: str, expires_at: datetime) -> None:
        """Issue a one-time transfer invite; owner anchor stays on current owner."""
        if self.claim_status != ClaimStatus.CLAIMED:
            raise ValueError("Agent must be claimed to create a transfer invite")
        self.verification_code = verification_code
        self.claim_status = ClaimStatus.PENDING_TRANSFER
        meta = dict(self.metadata or {})
        meta["transfer_invite_expires_at"] = expires_at.isoformat()
        self.metadata = meta

    def cancel_transfer_invite(self) -> None:
        """Revoke a pending transfer invite and restore claimed state."""
        if self.claim_status != ClaimStatus.PENDING_TRANSFER:
            raise ValueError("No pending transfer invite")
        self.verification_code = None
        self.claim_status = ClaimStatus.CLAIMED
        meta = dict(self.metadata or {})
        meta.pop("transfer_invite_expires_at", None)
        self.metadata = meta

    def claim(self, owner: str) -> None:
        """
        Claim ownership of this agent

        Args:
            owner: New owner identifier

        Raises:
            ValueError: If agent is already claimed
        """
        if self.claim_status == ClaimStatus.CLAIMED:
            raise ValueError("Agent is already claimed")
        if self.claim_status not in (ClaimStatus.UNCLAIMED, ClaimStatus.PENDING_TRANSFER):
            raise ValueError("Agent cannot be claimed in its current state")

        self.owner = owner
        self.claim_status = ClaimStatus.CLAIMED
        self.owner_changed_at = datetime.now(UTC)
        meta = dict(self.metadata or {})
        meta.pop("transfer_invite_expires_at", None)
        self.metadata = meta

    def transfer(self, new_owner: str) -> None:
        """
        Transfer ownership to another user

        Args:
            new_owner: New owner identifier
        """
        self.owner = new_owner
        self.owner_changed_at = datetime.now(UTC)

    def release(self) -> None:
        """Release ownership (make agent unowned)"""
        self.owner = None
        self.claim_status = ClaimStatus.UNCLAIMED
        self.owner_changed_at = datetime.now(UTC)

    # [REMOVED] Wallet Methods (add_earnings, spend, receive)
    # 钱包操作全部通过 Backend Wallet API (wallet_client) 进行

    # [DELETED] withdraw() - 使用 spend() 或 transfer_balance 代替

    # [DELETED] set_owner_share() - 不再支持自动分成

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "owner": self.owner,
            "endpoint": self.endpoint,
            "a2a_endpoint": self.a2a_endpoint,
            # ``status`` deliberately not emitted — the legacy DB column
            # is being dropped this PR, and the API response field
            # ``AgentInfo.status`` is computed from the Redis alive key
            # by the route-layer serializers (``_agent_entity_to_info``).
            "description": self.description,
            "tags": self.tags,
            "subnet_ids": self.subnet_ids,
            "metadata": self.metadata,
            "registered_at": self.registered_at.isoformat(),
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            # Authentication
            "api_key": self.api_key,
            # Claim
            "claim_status": self.claim_status.value if self.claim_status else None,
            "verification_code": self.verification_code,
            # Referral
            "referrer_id": self.referrer_id,
            # Owner tracking
            "owner_changed_at": self.owner_changed_at.isoformat()
            if self.owner_changed_at
            else None,
            # Agent Card
            "agent_card": self.agent_card,
            "agent_card_url": self.agent_card_url,
            # Communication policy (Phase 1: open|closed)
            "communication_policy": self.communication_policy,
            # Payment
            "wallet_address": self.wallet_address,
            "wallet_addresses": self.wallet_addresses,
            "accepts_payment": self.accepts_payment,
            "payment_methods": self.payment_methods,
            "token_pricing": self.token_pricing,
            # [REMOVED] Agent Wallet fields - 由 Backend 管理
            # ERC-8004 On-Chain Identity
            "erc8004_agent_id": self.erc8004_agent_id,
            "erc8004_chain": self.erc8004_chain,
            "erc8004_tx_hash": self.erc8004_tx_hash,
            "erc8004_registered_at": (
                self.erc8004_registered_at.isoformat() if self.erc8004_registered_at else None
            ),
            # SOCIAL.md pointer (https://agentsocial.one)
            "social_card_url": self.social_card_url,
        }

    def has_token_pricing(self) -> bool:
        """Check if agent has token-based pricing configured"""
        return self.token_pricing is not None and bool(self.token_pricing)

    def get_pricing_type(self) -> str:
        """Get the pricing type for this agent"""
        if self.has_token_pricing():
            return "token_based"
        return "none"

    @classmethod
    def from_dict(cls, data: dict) -> "Agent":
        """Create Agent from dictionary"""
        # Parse datetime strings
        data = data.copy()
        if isinstance(data.get("registered_at"), str):
            data["registered_at"] = datetime.fromisoformat(data["registered_at"])
        if data.get("last_heartbeat") and isinstance(data["last_heartbeat"], str):
            data["last_heartbeat"] = datetime.fromisoformat(data["last_heartbeat"])
        if data.get("owner_changed_at") and isinstance(data["owner_changed_at"], str):
            data["owner_changed_at"] = datetime.fromisoformat(data["owner_changed_at"])
        if data.get("erc8004_registered_at") and isinstance(data["erc8004_registered_at"], str):
            data["erc8004_registered_at"] = datetime.fromisoformat(data["erc8004_registered_at"])
        # Drop the legacy ``status`` field if a caller hands us an old
        # serialized dict (Redis hashes written before the alive-as-
        # single-source phase 2 refactor still carry it). Silently
        # ignoring the value is correct: the field no longer exists on
        # the entity and ``cls(**data)`` would otherwise raise
        # ``TypeError: unexpected keyword argument 'status'``.
        data.pop("status", None)
        # Parse claim_status enum
        if isinstance(data.get("claim_status"), str):
            data["claim_status"] = ClaimStatus(data["claim_status"])
        return cls(**data)
