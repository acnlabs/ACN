"""Agent Domain Entity

Pure business logic for Agent, independent of infrastructure.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AgentStatus(str, Enum):
    """Agent operational status"""

    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


class ClaimStatus(str, Enum):
    """Agent claim status"""

    UNCLAIMED = "unclaimed"  # No owner yet
    CLAIMED = "claimed"  # Has owner


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

    status: AgentStatus = AgentStatus.ONLINE
    description: str | None = None
    skills: list[str] = field(default_factory=list)
    subnet_ids: list[str] = field(default_factory=lambda: ["public"])
    metadata: dict = field(default_factory=dict)
    registered_at: datetime = field(default_factory=datetime.now)
    last_heartbeat: datetime | None = None

    # Authentication (for autonomous agents)
    api_key: str | None = None

    # Auth0 M2M 凭证（Agent 自主身份认证）
    auth0_client_id: str | None = None
    auth0_client_secret: str | None = None  # 仅在内存中使用，不持久化到 Redis
    auth0_token_endpoint: str | None = None

    # Claim status (for autonomous agents)
    claim_status: ClaimStatus | None = None
    verification_code: str | None = None  # Short code for human verification

    # Referral tracking
    referrer_id: str | None = None  # Agent who referred this agent

    # Owner change tracking
    owner_changed_at: datetime | None = None

    # A2A Agent Card (stored as raw dict; provided by registrant or auto-generated on demand)
    agent_card: dict | None = None

    # Payment capabilities
    wallet_address: str | None = None
    accepts_payment: bool = False
    payment_methods: list[str] = field(default_factory=list)

    # Token-based pricing (OpenAI-style, per million tokens)
    # Format: {"input_price_per_million": 3.0, "output_price_per_million": 15.0, "currency": "USD"}
    token_pricing: dict | None = None

    # Agent Wallet - 钱包数据由 Backend 管理，不在 ACN 存储
    # [REMOVED] balance, total_earned, total_spent, owner_share - 全部迁移到 Backend Wallet

    def __post_init__(self):
        """Validate invariants"""
        if not self.agent_id:
            raise ValueError("agent_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        # Note: owner and endpoint are now optional
        if not self.subnet_ids:
            self.subnet_ids = ["public"]

    @property
    def primary_subnet(self) -> str:
        """Get primary subnet (for backward compatibility)"""
        return self.subnet_ids[0] if self.subnet_ids else "public"

    def is_online(self) -> bool:
        """Check if agent is online"""
        return self.status == AgentStatus.ONLINE

    def is_in_subnet(self, subnet_id: str) -> bool:
        """Check if agent belongs to a subnet"""
        return subnet_id in self.subnet_ids

    def add_to_subnet(self, subnet_id: str) -> None:
        """Add agent to a subnet"""
        if subnet_id not in self.subnet_ids:
            self.subnet_ids.append(subnet_id)

    def remove_from_subnet(self, subnet_id: str) -> None:
        """Remove agent from a subnet"""
        if subnet_id in self.subnet_ids:
            self.subnet_ids.remove(subnet_id)
        # Ensure at least one subnet
        if not self.subnet_ids:
            self.subnet_ids = ["public"]

    def update_heartbeat(self) -> None:
        """Update last heartbeat timestamp"""
        self.last_heartbeat = datetime.now()

    def mark_offline(self) -> None:
        """Mark agent as offline"""
        self.status = AgentStatus.OFFLINE

    def mark_online(self) -> None:
        """Mark agent as online"""
        self.status = AgentStatus.ONLINE

    def has_skill(self, skill_id: str) -> bool:
        """Check if agent has a specific skill"""
        return skill_id in self.skills

    def has_all_skills(self, skill_ids: list[str]) -> bool:
        """Check if agent has all specified skills"""
        return all(skill in self.skills for skill in skill_ids)

    def can_accept_payment(self) -> bool:
        """Check if agent can accept payments"""
        return self.accepts_payment and bool(self.wallet_address)

    # ========== Ownership Methods ==========

    def is_owned(self) -> bool:
        """Check if agent has an owner"""
        return self.owner is not None

    def is_claimed(self) -> bool:
        """Check if agent has been claimed"""
        return self.claim_status == ClaimStatus.CLAIMED

    def can_be_claimed(self) -> bool:
        """Check if agent can be claimed"""
        return self.claim_status == ClaimStatus.UNCLAIMED

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

        self.owner = owner
        self.claim_status = ClaimStatus.CLAIMED
        self.owner_changed_at = datetime.now()

    def transfer(self, new_owner: str) -> None:
        """
        Transfer ownership to another user

        Args:
            new_owner: New owner identifier
        """
        self.owner = new_owner
        self.owner_changed_at = datetime.now()

    def release(self) -> None:
        """Release ownership (make agent unowned)"""
        self.owner = None
        self.claim_status = ClaimStatus.UNCLAIMED
        self.owner_changed_at = datetime.now()

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
            "status": self.status.value,
            "description": self.description,
            "skills": self.skills,
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
            # Payment
            "wallet_address": self.wallet_address,
            "accepts_payment": self.accepts_payment,
            "payment_methods": self.payment_methods,
            "token_pricing": self.token_pricing,
            # Auth0 M2M 凭证（client_secret 不序列化）
            "auth0_client_id": self.auth0_client_id,
            "auth0_token_endpoint": self.auth0_token_endpoint,
            # [REMOVED] Agent Wallet fields - 由 Backend 管理
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
        # Parse status enum
        if isinstance(data.get("status"), str):
            data["status"] = AgentStatus(data["status"])
        # Parse claim_status enum
        if isinstance(data.get("claim_status"), str):
            data["claim_status"] = ClaimStatus(data["claim_status"])
        return cls(**data)
