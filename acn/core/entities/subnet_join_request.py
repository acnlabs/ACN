"""SubnetJoinRequest Domain Entity (ADR-0004 Phase 2 Slice 2.1).

A single row in the three-in-one ``subnet_join_requests`` table.
Implements ADR-0004 §"Three approval entry paths, unified table
model": one schema covers join_request (pull / applicant-initiated),
invitation (push / owner-initiated), and allowlist_auto (system-
materialised on allowlist hit). The ``kind`` discriminator is the
only field that meaningfully differs across the three flows;
everything else — status machine, audit fields, decision actor
semantics — is uniform.

State machine summary (full table in ADR §State transition table):

* ``join_request``:   pending → approved (owner approve) / rejected
                       (owner reject) / withdrawn (applicant withdraw)
* ``invitation``:     pending → approved (invitee accept) / rejected
                       (invitee reject) / withdrawn (owner cancel)
* ``allowlist_auto``: born approved (no pending lifecycle); no edges
                       out of ``approved``

The entity enforces the **construction-time invariants** the table
schema can't express — primarily the (kind, status, initiated_by,
decided_by) coherence checks. State transitions themselves are
service-layer concerns (CAS on ``status='pending'`` against the
database); the entity refuses to materialise structurally
impossible rows, but doesn't gate transitions on its own.

Why a dataclass, not a Pydantic model: same reasoning as ``Subnet``.
Pure domain object, no framework dep, framework wrapping happens at
``acn/models.py`` (response shape) and the SQLAlchemy ORM
(persistence shape).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

# Valid ``kind`` values (the discriminator). ``Literal`` type hints
# are not runtime-enforced on a dataclass; we mirror the set here so
# ``__post_init__`` can reject typos at construction time before any
# repository write reaches storage.
_KIND_VALUES: frozenset[str] = frozenset(
    {"join_request", "invitation", "allowlist_auto"}
)

# Valid ``status`` values. ``withdrawn`` is distinguished from
# ``rejected`` so audit consumers can tell "withdrawn by the side
# that asked" from "rejected by the side that decided" — see ADR
# §State transition table for the per-kind decided_by mapping.
_STATUS_VALUES: frozenset[str] = frozenset(
    {"pending", "approved", "rejected", "withdrawn"}
)

# Reserved synthetic actor token for ``allowlist_auto`` rows. The
# token shape ``system:<reason>`` mirrors the convention ADR-0003
# established for system-generated audit actors (``system:cascade``,
# ``system:reaper``). Reserved here so the entity-layer invariant
# check can pin both ``initiated_by`` and ``decided_by`` against the
# same literal — a typo in either field is caught at construction.
SYSTEM_ALLOWLIST_ACTOR: str = "system:allowlist"

# Note length cap matches ADR §SubnetJoinRequest schema (note ≤500
# chars). Enforced at the entity layer so persistence-layer
# truncation can never surprise a downstream reader.
_NOTE_MAX_LEN: int = 500


@dataclass
class SubnetJoinRequest:
    """A single row in ``subnet_join_requests``.

    The ``agent_id`` semantics are uniform across all three kinds:
    it is always **the agent who would (or did) become a member**.
    Directionality (who initiated, who decided) lives in
    ``initiated_by`` / ``decided_by`` — never in ``agent_id``.
    """

    request_id: str
    subnet_id: str
    agent_id: str
    kind: Literal["join_request", "invitation", "allowlist_auto"]
    status: Literal["pending", "approved", "rejected", "withdrawn"]
    initiated_by: str
    decided_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        """Enforce construction-time invariants.

        The five invariants below are the ones the persistence
        schema cannot express (or could only express via partial
        indexes that don't compose cleanly with our dual-store
        Postgres+Redis layout). Service-layer state transitions
        re-derive these on every CAS, but rejecting structurally
        impossible rows at the entity boundary means a single
        ``service.repo.save(...)`` is the only place that can write
        garbage — useful when the implementation eventually grows
        an admin-side replay tool that constructs entities from
        raw dicts.
        """
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.subnet_id:
            raise ValueError("subnet_id cannot be empty")
        if not self.agent_id:
            raise ValueError("agent_id cannot be empty")
        if not self.initiated_by:
            raise ValueError("initiated_by cannot be empty")

        if self.kind not in _KIND_VALUES:
            raise ValueError(
                f"kind must be one of {sorted(_KIND_VALUES)}, got {self.kind!r}"
            )
        if self.status not in _STATUS_VALUES:
            raise ValueError(
                f"status must be one of {sorted(_STATUS_VALUES)}, "
                f"got {self.status!r}"
            )

        # ``pending`` rows must NOT carry a decision; non-``pending``
        # rows MUST. The bidirectional check catches both "service
        # forgot to clear decided_by on rollback" and "service
        # forgot to set decided_by on transition out of pending".
        if self.status == "pending":
            if self.decided_by is not None or self.decided_at is not None:
                raise ValueError(
                    "pending rows must have decided_by=None and "
                    "decided_at=None"
                )
        else:
            if self.decided_by is None or self.decided_at is None:
                raise ValueError(
                    f"status={self.status!r} requires both decided_by and "
                    f"decided_at to be set"
                )

        # ``allowlist_auto`` is born approved by the system. Per ADR
        # §State transition table the only legal shape is
        # (status=approved, initiated_by=decided_by=SYSTEM_ALLOWLIST_ACTOR).
        # No pending lifecycle, no other status values — anything
        # else means a route or service layer is mis-using the
        # discriminator.
        if self.kind == "allowlist_auto":
            if self.status != "approved":
                raise ValueError(
                    "allowlist_auto rows must be born approved "
                    f"(got status={self.status!r})"
                )
            if self.initiated_by != SYSTEM_ALLOWLIST_ACTOR:
                raise ValueError(
                    "allowlist_auto rows must have "
                    f"initiated_by={SYSTEM_ALLOWLIST_ACTOR!r} "
                    f"(got {self.initiated_by!r})"
                )
            if self.decided_by != SYSTEM_ALLOWLIST_ACTOR:
                raise ValueError(
                    "allowlist_auto rows must have "
                    f"decided_by={SYSTEM_ALLOWLIST_ACTOR!r} "
                    f"(got {self.decided_by!r})"
                )

        if self.note is not None and len(self.note) > _NOTE_MAX_LEN:
            raise ValueError(
                f"note exceeds {_NOTE_MAX_LEN}-char limit "
                f"(got {len(self.note)} chars)"
            )

    @property
    def is_pending(self) -> bool:
        """True iff this row blocks future ``(subnet_id, agent_id)``
        request creation under the unique partial index
        ``WHERE status='pending'``."""
        return self.status == "pending"

    @property
    def is_terminal(self) -> bool:
        """True iff no further state transitions are legal. Both
        ``approved`` and ``rejected``/``withdrawn`` are terminal —
        the service layer's re-apply path (ADR §State machine edges)
        creates a *new* request_id rather than re-opening the old."""
        return self.status != "pending"

    def to_dict(self) -> dict:
        """Serialise to a flat dict for Redis HASH storage.

        ``datetime`` fields are emitted as ISO 8601 strings so the
        Redis HASH stays string-only (matches the convention
        ``Subnet.to_dict`` established for harness URLs etc.).
        Empty values are emitted as empty strings, not omitted —
        ``from_dict`` reconstitutes ``None`` from empty strings via
        ``or None`` so callers don't have to special-case
        per-field "missing vs falsy".
        """
        return {
            "request_id": self.request_id,
            "subnet_id": self.subnet_id,
            "agent_id": self.agent_id,
            "kind": self.kind,
            "status": self.status,
            "initiated_by": self.initiated_by,
            "decided_by": self.decided_by or "",
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else "",
            "note": self.note or "",
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SubnetJoinRequest":
        """Reconstitute from a Redis HASH dict.

        Inverse of :meth:`to_dict`. Empty strings in the
        ``decided_by`` / ``decided_at`` / ``note`` slots collapse
        back to ``None`` — these three are the only nullable fields
        in the schema. ``created_at`` is always present (set at
        creation, never nullable)."""
        decided_at_raw = data.get("decided_at") or None
        return cls(
            request_id=data["request_id"],
            subnet_id=data["subnet_id"],
            agent_id=data["agent_id"],
            kind=data["kind"],
            status=data["status"],
            initiated_by=data["initiated_by"],
            decided_by=data.get("decided_by") or None,
            created_at=datetime.fromisoformat(data["created_at"]),
            decided_at=(
                datetime.fromisoformat(decided_at_raw)
                if decided_at_raw
                else None
            ),
            note=data.get("note") or None,
        )
