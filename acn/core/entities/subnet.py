"""Subnet Domain Entity

Pure business logic for Subnet.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

# Valid values for the ``Subnet.lifecycle`` field. ``Literal`` type hints are
# not validated at runtime on a dataclass, so we mirror the set here and
# enforce it in ``__post_init__``.
_LIFECYCLE_VALUES: frozenset[str] = frozenset({"persistent", "task_scoped"})
# Same pattern for ``Subnet.join_policy`` (ADR-0004 §Data model). The two
# legal values mirror the ADR's "open" vs "approval" admission split — any
# future third mode (e.g. "invite_only", "paid") extends this set and the
# ``__post_init__`` validator picks it up automatically.
_JOIN_POLICY_VALUES: frozenset[str] = frozenset({"open", "approval"})
_RESERVED_SLUGS: frozenset[str] = frozenset({"public", "system"})


@dataclass
class Subnet:
    """
    Subnet Domain Entity

    Represents a logical network segment for agent grouping. Subnets can
    optionally nest one level deep — see ADR-0003. The three nesting
    fields default to "top-level persistent subnet" so legacy callers
    that don't touch them behave exactly as before.

    Identifiers
    -----------
    ``slug`` — the URL-safe, human-readable identifier. Stable primary key
        in Postgres (column ``subnets.slug``), used in every API path,
        Redis index, CLI, and SDK. Examples: ``"public"``,
        ``"acnlabs-core"``, ``"team-alpha-7f3a9c"``.

        Cross-entity columns that *carry* a slug value but whose Python
        attribute / DB column are still named ``subnet_id`` (Step 2 of
        the rename migration): ``Agent.subnet_ids``, ``Task.subnet_id``,
        ``SubnetJoinRequest`` / ``SubnetAllowlist`` ORM rows
        (Python attribute is ``slug`` since Step 1, DB column stays
        ``subnet_id`` until Step 2's column-rename migration ships).
    ``id`` — opaque UUID, server-generated. Surfaced only in
        ``SubnetInfo.id`` and ``SubnetStub.id`` so anonymous callers
        receive a non-revealing identifier. NOT used for lookups, foreign
        keys, or membership arrays — those continue to key off ``slug``.

    Display name
    ------------
    ``name`` — free-text display name shown in UIs. May contain spaces,
        uppercase letters, and Unicode. Defaults to ``slug`` at creation
        time so legacy callers that never supply a separate display name
        keep the old behaviour.
    """

    slug: str
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
    # Opaque UUID identifier — see class docstring §Identifiers.
    # Default ``uuid4()`` covers entity-only construction in tests; the
    # ORM layer's ``server_default=gen_random_uuid()`` covers DB inserts.
    id: str = field(default_factory=lambda: str(uuid4()))
    # Nesting fields (ADR-0003). All optional; defaults preserve legacy
    # "flat top-level subnet" semantics.
    parent_slug: str | None = None
    lifecycle: Literal["persistent", "task_scoped"] = "persistent"
    linked_task_id: str | None = None
    # Join admission policy (ADR-0004). Decouples admission control from
    # discoverability (``is_private``). ``"open"`` keeps today's "any
    # registered agent may join" behaviour; ``"approval"`` requires an
    # owner-side decision via the request / invitation / allowlist
    # state machine implemented in ``acn/services/join_flow_service.py``
    # and surfaced through ``acn/routes/subnet_admission.py``. Defaults to
    # ``"open"`` so legacy callers that never set the field behave
    # exactly as before; the ``__post_init__`` invariant below rejects
    # the ``is_private=True + join_policy="open"`` combination that the
    # ADR identifies as the existing "private but joinable by anyone"
    # bug, so storage that arrives via the entity is always self-
    # consistent.
    join_policy: Literal["open", "approval"] = "open"

    def __post_init__(self):
        """Validate invariants"""
        if not self.slug:
            raise ValueError("slug cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if not self.owner:
            raise ValueError("owner cannot be empty")
        # Reserved slugs
        if self.slug in _RESERVED_SLUGS:
            if self.owner != "system":
                raise ValueError(f"Subnet '{self.slug}' is reserved for system use")
            # Reserved subnets can never participate in nesting — neither
            # as a child (would let attackers slot platform-owned slugs into
            # a hierarchy) nor with a task-bound lifecycle (would let a
            # task termination auto-dissolve a platform-level subnet,
            # breaking the implicit "always-on" guarantee that callers
            # depend on for `public`).
            if self.parent_slug is not None:
                raise ValueError(
                    f"Reserved subnet '{self.slug}' cannot have a parent_slug"
                )
            if self.lifecycle == "task_scoped":
                raise ValueError(
                    f"Reserved subnet '{self.slug}' cannot be task_scoped"
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

        # ADR-0004 entity-layer invariants.
        if self.join_policy not in _JOIN_POLICY_VALUES:
            raise ValueError(
                f"join_policy must be one of {sorted(_JOIN_POLICY_VALUES)}, "
                f"got {self.join_policy!r}"
            )
        # ``is_private=True`` implies ``join_policy="approval"`` — the
        # combination ``private + open`` is the status-quo "private but
        # joinable by anyone who knows the slug" bug that ADR-0004 closes.
        # Rejecting at construction time means no service / route caller
        # can produce or load a row in that state; the same combination
        # received over the wire (``POST /api/v1/subnets`` body) is
        # translated by the route layer into ``INVALID_REQUEST`` with
        # ``details.reason="visibility_policy_conflict"``.
        if self.is_private and self.join_policy == "open":
            raise ValueError(
                f"subnet '{self.slug}' configuration invalid: "
                f"is_private=True requires join_policy='approval' "
                f"(reason=visibility_policy_conflict)"
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
        """Check if subnet requires authentication.

        DEPRECATED (ADR-0004). Historically used to gate the join
        path on ``is_private + security_config``, but ADR-0004
        decoupled join admission from discoverability: the canonical
        signal is now ``self.join_policy``, and the join path no
        longer consults this helper. Left in place to avoid breaking
        existing callers (manifest service, harness-bound metadata
        readers) that read ``is_private`` for non-admission reasons;
        a follow-up cleanup PR will either repurpose ``security_config``
        or drop the helper entirely (ADR-0004 §"Out of scope" item).
        New code MUST gate on ``subnet.join_policy`` instead.
        """
        return self.is_private and bool(self.security_config)

    def to_dict(self, include_secret: bool = False) -> dict:
        """Convert to dictionary for serialization.

        By default ``harness_secret`` is omitted from the output to avoid
        accidentally exposing it in API responses. Set ``include_secret=True``
        for internal use (e.g. snapshotting onto Task.metadata).
        """
        out = {
            "slug": self.slug,
            "id": self.id,
            "name": self.name,
            "owner": self.owner,
            "description": self.description,
            "is_private": self.is_private,
            "security_config": self.security_config,
            "member_agent_ids": list(self.member_agent_ids),
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
            "harness_url": self.harness_url,
            "parent_slug": self.parent_slug,
            "lifecycle": self.lifecycle,
            "linked_task_id": self.linked_task_id,
            "join_policy": self.join_policy,
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

        ADR-0004 legacy compatibility
        -----------------------------
        Rows that predate ADR-0004 don't carry ``join_policy`` at all.
        Two cases:

        - ``is_private == False`` → default ``join_policy="open"`` matches
          the legacy behaviour and the ``__post_init__`` invariant
          accepts it.
        - ``is_private == True`` → defaulting to ``"open"`` would trip
          the ``private + open`` invariant and refuse to reconstruct the
          entity, breaking every Redis read of a legacy private subnet
          during the migration window. We therefore **automatically
          upgrade** missing ``join_policy`` to ``"approval"`` whenever
          ``is_private`` is true, exactly matching the Alembic
          backfill's ``UPDATE subnets SET join_policy='approval' WHERE
          is_private=true`` semantic. This keeps reads safe even when
          ``scripts/backfill_subnet_join_policy.py`` has not yet run
          on a Redis-fallback deployment.

        The auto-upgrade only fires when the key is **absent**. If the
        caller explicitly passes ``join_policy=...`` we honour it as-is
        and let ``__post_init__`` validate.
        """
        data = data.copy()
        if isinstance(data.get("created_at"), str):
            try:
                data["created_at"] = datetime.fromisoformat(data["created_at"])
            except (ValueError, TypeError):
                data["created_at"] = datetime.now(UTC)
        if isinstance(data.get("member_agent_ids"), list):
            data["member_agent_ids"] = set(data["member_agent_ids"])
        # ADR-0004 legacy auto-upgrade — see docstring above.
        if "join_policy" not in data and data.get("is_private") is True:
            data["join_policy"] = "approval"
        # Back-compat: old serialised dicts used ``subnet_id`` and
        # ``parent_subnet_id`` before the slug rename. Translate on
        # read so Redis-cached rows survive the migration without a
        # backfill. ``data.pop`` (not ``setdefault``) so the legacy
        # key never reaches ``cls(**data)`` — the dataclass would
        # otherwise reject it as an unexpected keyword argument.
        if "subnet_id" in data and "slug" not in data:
            data["slug"] = data.pop("subnet_id")
        elif "subnet_id" in data:
            data.pop("subnet_id")
        if "parent_subnet_id" in data and "parent_slug" not in data:
            data["parent_slug"] = data.pop("parent_subnet_id")
        elif "parent_subnet_id" in data:
            data.pop("parent_subnet_id")
        return cls(**data)
