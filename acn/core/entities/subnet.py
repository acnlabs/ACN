"""Subnet Domain Entity

Pure business logic for Subnet.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Subnet:
    """
    Subnet Domain Entity

    Represents a logical network segment for agent grouping.
    """

    subnet_id: str
    name: str
    owner: str
    description: str | None = None
    is_private: bool = False
    security_config: dict = field(default_factory=dict)
    member_agent_ids: set[str] = field(default_factory=set)
    created_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate invariants"""
        if not self.subnet_id:
            raise ValueError("subnet_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if not self.owner:
            raise ValueError("owner cannot be empty")
        # Reserved subnet IDs
        if self.subnet_id in ["public", "system"]:
            if self.owner != "system":
                raise ValueError(f"Subnet '{self.subnet_id}' is reserved for system use")

    def add_member(self, agent_id: str) -> None:
        """Add an agent to this subnet"""
        self.member_agent_ids.add(agent_id)

    def remove_member(self, agent_id: str) -> None:
        """Remove an agent from this subnet"""
        self.member_agent_ids.discard(agent_id)

    def has_member(self, agent_id: str) -> bool:
        """Check if agent is a member"""
        return agent_id in self.member_agent_ids

    def get_member_count(self) -> int:
        """Get number of members"""
        return len(self.member_agent_ids)

    def is_public(self) -> bool:
        """Check if subnet is public"""
        return not self.is_private

    def requires_authentication(self) -> bool:
        """Check if subnet requires authentication"""
        return self.is_private and bool(self.security_config)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            "subnet_id": self.subnet_id,
            "name": self.name,
            "owner": self.owner,
            "description": self.description,
            "is_private": self.is_private,
            "security_config": self.security_config,
            "member_agent_ids": list(self.member_agent_ids),
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Subnet":
        """Create Subnet from dictionary"""
        data = data.copy()
        if isinstance(data.get("created_at"), str):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        if isinstance(data.get("member_agent_ids"), list):
            data["member_agent_ids"] = set(data["member_agent_ids"])
        return cls(**data)
