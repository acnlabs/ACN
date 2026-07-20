"""Org Harness domain entities (ADR-0014 / org-model-v0).

Org is a first-class ACN object. Members are agents. Owner is optional
(``none`` | ``human`` | ``agent``). Hard fencing binds one subnet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

OwnerKind = Literal["none", "human", "agent"]
PrincipalKind = Literal["human", "agent"]
MembershipStatus = Literal["active", "inactive"]
OrgStatus = Literal["active", "dissolved", "fence_missing", "frozen"]
WorkStatus = Literal["todo", "in_progress", "done", "cancelled"]

_OWNER_KINDS = frozenset({"none", "human", "agent"})
_PRINCIPAL_KINDS = frozenset({"human", "agent"})
_MEMBERSHIP_STATUSES = frozenset({"active", "inactive"})
_ORG_STATUSES = frozenset({"active", "dissolved", "fence_missing", "frozen"})
_WORK_STATUSES = frozenset({"todo", "in_progress", "done", "cancelled"})
_DEFAULT_ROLES = ("manager", "worker", "reviewer")


@dataclass
class OrgPrincipal:
    """Identity principal for ``created_by`` / owner subjects."""

    kind: PrincipalKind
    subject: str

    def __post_init__(self) -> None:
        if self.kind not in _PRINCIPAL_KINDS:
            raise ValueError(f"invalid principal kind: {self.kind!r}")
        if not self.subject:
            raise ValueError("principal subject cannot be empty")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "subject": self.subject}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrgPrincipal:
        return cls(kind=data["kind"], subject=data["subject"])


@dataclass
class OrgOwner:
    """Optional Org owner — isomorphic to agent claim semantics."""

    kind: OwnerKind
    subject: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _OWNER_KINDS:
            raise ValueError(f"invalid owner kind: {self.kind!r}")
        if self.kind == "none":
            self.subject = None
        elif not self.subject:
            raise ValueError(f"owner.kind={self.kind!r} requires subject")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.subject is not None:
            out["subject"] = self.subject
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OrgOwner:
        if not data:
            return cls(kind="none")
        return cls(kind=data.get("kind", "none"), subject=data.get("subject"))


@dataclass
class Org:
    """Organisation — Kernel object of the Org Harness module."""

    org_id: str
    display_name: str
    created_by: OrgPrincipal
    subnet_id: str
    owner: OrgOwner = field(default_factory=lambda: OrgOwner(kind="none"))
    charter: dict[str, Any] = field(default_factory=dict)
    plugins: dict[str, str] = field(
        default_factory=lambda: {
            "work": "minimal",
            "loop": "thin",
            "memory": "noop",
        }
    )
    roles: list[str] = field(default_factory=lambda: list(_DEFAULT_ROLES))
    status: OrgStatus = "active"
    steward_agent_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.org_id:
            raise ValueError("org_id cannot be empty")
        if not self.display_name:
            raise ValueError("display_name cannot be empty")
        if not self.subnet_id:
            raise ValueError("subnet_id cannot be empty")
        if self.status not in _ORG_STATUSES:
            raise ValueError(f"invalid org status: {self.status!r}")
        if not self.steward_agent_id:
            raise ValueError("steward_agent_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "display_name": self.display_name,
            "charter": self.charter,
            "owner": self.owner.to_dict(),
            "created_by": self.created_by.to_dict(),
            "fencing": {"subnet_id": self.subnet_id},
            "subnet_id": self.subnet_id,
            "plugins": self.plugins,
            "roles": list(self.roles),
            "status": self.status,
            "steward_agent_id": self.steward_agent_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Org:
        data = dict(data)
        fencing = data.get("fencing") or {}
        subnet_id = data.get("subnet_id") or fencing.get("subnet_id", "")
        created_at = data.get("created_at")
        updated_at = data.get("updated_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        return cls(
            org_id=data["org_id"],
            display_name=data["display_name"],
            created_by=OrgPrincipal.from_dict(data["created_by"]),
            subnet_id=subnet_id,
            owner=OrgOwner.from_dict(data.get("owner")),
            charter=data.get("charter") or {},
            plugins=data.get("plugins")
            or {"work": "minimal", "loop": "thin", "memory": "noop"},
            roles=list(data.get("roles") or _DEFAULT_ROLES),
            status=data.get("status") or "active",
            steward_agent_id=data.get("steward_agent_id") or "",
            created_at=created_at or datetime.now(UTC),
            updated_at=updated_at or datetime.now(UTC),
        )


@dataclass
class OrgMembership:
    """Agent membership in an Org (roles live here; fence truth is subnet)."""

    org_id: str
    agent_id: str
    role: str = "worker"
    reports_to: str | None = None
    status: MembershipStatus = "active"
    joined_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.org_id:
            raise ValueError("org_id cannot be empty")
        if not self.agent_id:
            raise ValueError("agent_id cannot be empty")
        if not self.role:
            raise ValueError("role cannot be empty")
        if self.status not in _MEMBERSHIP_STATUSES:
            raise ValueError(f"invalid membership status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "agent_id": self.agent_id,
            "role": self.role,
            "reports_to": self.reports_to,
            "status": self.status,
            "joined_at": self.joined_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrgMembership:
        joined_at = data.get("joined_at")
        if isinstance(joined_at, str):
            joined_at = datetime.fromisoformat(joined_at)
        return cls(
            org_id=data["org_id"],
            agent_id=data["agent_id"],
            role=data.get("role") or "worker",
            reports_to=data.get("reports_to"),
            status=data.get("status") or "active",
            joined_at=joined_at or datetime.now(UTC),
        )


@dataclass
class OrgWorkItem:
    """Minimal work queue item (Phase 1; not full Task Pool)."""

    work_id: str
    org_id: str
    title: str
    status: WorkStatus = "todo"
    assignee_agent_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.work_id:
            raise ValueError("work_id cannot be empty")
        if not self.org_id:
            raise ValueError("org_id cannot be empty")
        if not self.title:
            raise ValueError("title cannot be empty")
        if self.status not in _WORK_STATUSES:
            raise ValueError(f"invalid work status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "org_id": self.org_id,
            "title": self.title,
            "status": self.status,
            "assignee_agent_id": self.assignee_agent_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrgWorkItem:
        created_at = data.get("created_at")
        updated_at = data.get("updated_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        return cls(
            work_id=data["work_id"],
            org_id=data["org_id"],
            title=data["title"],
            status=data.get("status") or "todo",
            assignee_agent_id=data.get("assignee_agent_id"),
            created_at=created_at or datetime.now(UTC),
            updated_at=updated_at or datetime.now(UTC),
        )
