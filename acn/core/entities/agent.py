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


@dataclass
class Agent:
    """
    Agent Domain Entity

    Represents a registered AI agent in the ACN network.
    Contains business logic and invariants.
    """

    agent_id: str
    owner: str
    name: str
    endpoint: str
    status: AgentStatus = AgentStatus.ONLINE
    description: str | None = None
    skills: list[str] = field(default_factory=list)
    subnet_ids: list[str] = field(default_factory=lambda: ["public"])
    metadata: dict = field(default_factory=dict)
    registered_at: datetime = field(default_factory=datetime.now)
    last_heartbeat: datetime | None = None

    # Payment capabilities
    wallet_address: str | None = None
    accepts_payment: bool = False
    payment_methods: list[str] = field(default_factory=list)

    # Token-based pricing (OpenAI-style, per million tokens)
    # Format: {"input_price_per_million": 3.0, "output_price_per_million": 15.0, "currency": "USD"}
    token_pricing: dict | None = None

    def __post_init__(self):
        """Validate invariants"""
        if not self.agent_id:
            raise ValueError("agent_id cannot be empty")
        if not self.owner:
            raise ValueError("owner cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if not self.endpoint:
            raise ValueError("endpoint cannot be empty")
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

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            "agent_id": self.agent_id,
            "owner": self.owner,
            "name": self.name,
            "endpoint": self.endpoint,
            "status": self.status.value,
            "description": self.description,
            "skills": self.skills,
            "subnet_ids": self.subnet_ids,
            "metadata": self.metadata,
            "registered_at": self.registered_at.isoformat(),
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "wallet_address": self.wallet_address,
            "accepts_payment": self.accepts_payment,
            "payment_methods": self.payment_methods,
            "token_pricing": self.token_pricing,
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
        # Parse status enum
        if isinstance(data.get("status"), str):
            data["status"] = AgentStatus(data["status"])
        return cls(**data)
