"""SubnetAllowlist Domain Entity (ADR-0004 Phase 2 Slice 2.1).

A single ``(slug, agent_id)`` entry in the subnet's
admission-allowlist. Distinct from ``IAllowlistRepository`` (which
governs **agent-to-agent communication** under
``communication_policy.mode=allowlist``) — this entity governs
**agent-to-subnet admission** under ``join_policy='approval'``. The
namespace ambiguity is acknowledged but ADR-0004 keeps the term
"allowlist" because both flows share the same semantic shape
("preauthorised members of a trust set"); the prefix discipline
(``SubnetAllowlist`` vs the agent-comm ``AllowlistEntry``) keeps
them distinguishable in code and in error messages.

Flat configuration set, no state machine. The ``join`` flow
(ADR-0004 §join branch 4) checks for an entry, and on hit
materialises a ``SubnetJoinRequest(kind='allowlist_auto',
status='approved')`` audit row. Removal from the allowlist does
NOT evict an already-joined member (ADR §State machine edges
"Allowlist removal does not evict members") — it only changes the
path future re-joins take.

Why a dataclass, not a Pydantic model: same reasoning as ``Subnet``
/ ``SubnetJoinRequest``. Pure domain object; framework wrapping
happens at the API and ORM layers.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class SubnetAllowlist:
    """A single preauthorised ``(slug, agent_id)`` admission entry."""

    slug: str
    agent_id: str
    added_by: str
    added_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Enforce non-empty identity fields.

        Schema-level constraints (composite PK on
        ``(slug, agent_id)``, FK against ``agents.agent_id``,
        existence-check on ``slug``) live at the persistence
        layer; the entity only rejects the structurally impossible
        shapes (empty strings) that the schema's NOT NULL doesn't
        catch.

        ``added_by`` is required because every allowlist mutation
        is owner-audited — an empty actor makes the row
        irreversibly anonymous, which defeats the whole audit
        trail. The service layer is expected to set this to the
        authenticated owner agent_id; allowlist add through an
        admin path that lacks an owner identity should synthesise
        a ``system:<reason>`` actor (matches the convention
        ``SubnetJoinRequest.SYSTEM_ALLOWLIST_ACTOR`` uses).
        """
        if not self.slug:
            raise ValueError("slug cannot be empty")
        if not self.agent_id:
            raise ValueError("agent_id cannot be empty")
        if not self.added_by:
            raise ValueError("added_by cannot be empty")

    def to_dict(self) -> dict:
        """Serialise to a flat dict for Redis HASH storage.

        Stored in the parallel-meta HASH
        ``acn:subnets:{subnet_id}:allowlist_meta:{agent_id}`` — the
        primary membership SET ``acn:subnets:{subnet_id}:allowlist``
        only carries the agent_ids; this meta HASH carries the
        audit fields. Two-key layout instead of single-HASH because
        ``SISMEMBER`` on the SET is the hot ``is_member`` check
        and a HASH-only layout would force ``HEXISTS`` instead
        (same cost, but loses the future ability to SDIFF /
        SINTERSTORE the membership for batch operations).
        """
        return {
            "slug": self.slug,
            "agent_id": self.agent_id,
            "added_by": self.added_by,
            "added_at": self.added_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SubnetAllowlist":
        """Reconstitute from a Redis HASH dict.

        Inverse of :meth:`to_dict`. All four fields are required —
        unlike ``SubnetJoinRequest`` there are no nullable fields
        and no legacy-row tolerance: an allowlist entry that's
        missing ``added_by`` or ``added_at`` is a corrupt record,
        not a backward-compat case."""
        return cls(
            slug=data.get("slug") or data.get("subnet_id", ""),
            agent_id=data["agent_id"],
            added_by=data["added_by"],
            added_at=datetime.fromisoformat(data["added_at"]),
        )
