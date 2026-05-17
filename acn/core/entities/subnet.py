"""Subnet Domain Entity

Pure business logic for Subnet.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

# Valid values for the ``Subnet.lifecycle`` field. ``Literal`` type hints are
# not validated at runtime on a dataclass, so we mirror the set here and
# enforce it in ``__post_init__``.
_LIFECYCLE_VALUES: frozenset[str] = frozenset({"persistent", "task_scoped"})
_RESERVED_SUBNET_IDS: frozenset[str] = frozenset({"public", "system"})


@dataclass
class Subnet:
    """
    Subnet Domain Entity

    Represents a logical network segment for agent grouping. Subnets can
    optionally nest one level deep — see ADR-0003. The three nesting
    fields default to "top-level persistent subnet" so legacy callers
    that don't touch them behave exactly as before.
    """

    subnet_id: str
    name: str
    owner: str
    description: str | None = None
    is_private: bool = False
    security_config: dict = field(default_factory=dict)
    member_agent_ids: set[str] = field(default_factory=set)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = field(default_factory=dict)
    harness_url: str | None = None
    harness_secret: str | None = None
    # Nesting fields (ADR-0003). All optional; defaults preserve legacy
    # "flat top-level subnet" semantics.
    parent_subnet_id: str | None = None
    lifecycle: Literal["persistent", "task_scoped"] = "persistent"
    linked_task_id: str | None = None

    def __post_init__(self):
        """Validate invariants"""
        if not self.subnet_id:
            raise ValueError("subnet_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if not self.owner:
            raise ValueError("owner cannot be empty")
        # Reserved subnet IDs
        if self.subnet_id in _RESERVED_SUBNET_IDS:
            if self.owner != "system":
                raise ValueError(f"Subnet '{self.subnet_id}' is reserved for system use")
            # Reserved subnets can never participate in nesting — neither
            # as a child (would let attackers slot platform-owned IDs into
            # a hierarchy) nor with a task-bound lifecycle (would let a
            # task termination auto-dissolve a platform-level subnet,
            # breaking the implicit "always-on" guarantee that callers
            # depend on for `public`).
            if self.parent_subnet_id is not None:
                raise ValueError(
                    f"Reserved subnet '{self.subnet_id}' cannot have a parent_subnet_id"
                )
            if self.lifecycle == "task_scoped":
                raise ValueError(
                    f"Reserved subnet '{self.subnet_id}' cannot be task_scoped"
                )

        # ADR-0003 entity-layer invariants.
        if self.lifecycle not in _LIFECYCLE_VALUES:
            raise ValueError(
                f"lifecycle must be one of {sorted(_LIFECYCLE_VALUES)}, got {self.lifecycle!r}"
            )
        # ``task_scoped`` ⇔ ``linked_task_id is not None`` is enforced both
        # directions so callers can't construct a half-state.
        if self.lifecycle == "task_scoped" and self.linked_task_id is None:
            raise ValueError("lifecycle='task_scoped' requires linked_task_id to be set")
        if self.lifecycle == "persistent" and self.linked_task_id is not None:
            raise ValueError(
                "lifecycle='persistent' must not carry a linked_task_id; "
                "use lifecycle='task_scoped' or clear linked_task_id"
            )

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

    def to_dict(self, include_secret: bool = False) -> dict:
        """Convert to dictionary for serialization.

        By default ``harness_secret`` is omitted from the output to avoid
        accidentally exposing it in API responses. Set ``include_secret=True``
        for internal use (e.g. snapshotting onto Task.metadata).
        """
        out = {
            "subnet_id": self.subnet_id,
            "name": self.name,
            "owner": self.owner,
            "description": self.description,
            "is_private": self.is_private,
            "security_config": self.security_config,
            "member_agent_ids": list(self.member_agent_ids),
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
            "harness_url": self.harness_url,
            "parent_subnet_id": self.parent_subnet_id,
            "lifecycle": self.lifecycle,
            "linked_task_id": self.linked_task_id,
        }
        if include_secret:
            out["harness_secret"] = self.harness_secret
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Subnet":
        """Create Subnet from dictionary.

        Legacy stored rows that predate ADR-0003 don't carry the nesting
        fields. ``**data`` would then pass nothing for them and the
        dataclass defaults take over (top-level persistent subnet).
        That's the intended migration path — no backfill needed.
        """
        data = data.copy()
        if isinstance(data.get("created_at"), str):
            try:
                data["created_at"] = datetime.fromisoformat(data["created_at"])
            except (ValueError, TypeError):
                data["created_at"] = datetime.now(UTC)
        if isinstance(data.get("member_agent_ids"), list):
            data["member_agent_ids"] = set(data["member_agent_ids"])
        return cls(**data)
