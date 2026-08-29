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
_EXECUTION_ENV_KINDS = frozenset({"none", "git", "url"})
_EXECUTION_ENV_URI_PREFIXES = ("https://", "http://", "git@", "ssh://", "git://")


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
            "work": "builtin_work",
            "loop": "heartbeat",
            "memory": "noop",
            "knowledge": "noop",
        }
    )
    roles: list[str] = field(default_factory=lambda: list(_DEFAULT_ROLES))
    status: OrgStatus = "active"
    steward_agent_id: str = ""
    execution_env: dict[str, Any] | None = None
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
        self.execution_env = normalize_execution_env(self.execution_env)

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
            "execution_env": self.execution_env or {"kind": "none"},
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
            or {
                "work": "builtin_work",
                "loop": "heartbeat",
                "memory": "noop",
                "knowledge": "noop",
            },
            roles=list(data.get("roles") or _DEFAULT_ROLES),
            status=data.get("status") or "active",
            steward_agent_id=data.get("steward_agent_id") or "",
            execution_env=data.get("execution_env"),
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


def normalize_execution_env(raw: Any) -> dict[str, Any] | None:
    """Org-level shared workplace pointer. Kernel stores; does not run it.

    ``None`` / ``{kind: none}`` means members use their own L1. ``git`` and
    ``url`` are pointers the member follows (clone a repo, call a runner).
    ACN does not provision the environment.
    """
    from ..validators import check_dict_size_64k

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("execution_env must be a JSON object or null")
    check_dict_size_64k("execution_env", raw)
    kind = raw.get("kind") or "none"
    if kind not in _EXECUTION_ENV_KINDS:
        raise ValueError(f"invalid execution_env.kind: {kind!r}")
    if kind == "none":
        return None
    uri = raw.get("uri")
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("execution_env.uri is required when kind is not none")
    uri = uri.strip()
    if len(uri) > 2048:
        raise ValueError("execution_env.uri is too long")
    if not any(uri.startswith(prefix) for prefix in _EXECUTION_ENV_URI_PREFIXES):
        raise ValueError("execution_env.uri must be http(s), git, or ssh")
    out: dict[str, Any] = {"kind": kind, "uri": uri}
    hint = raw.get("hint")
    if hint is not None:
        if not isinstance(hint, str):
            raise ValueError("execution_env.hint must be a string")
        hint = hint.strip()
        if len(hint) > 500:
            raise ValueError("execution_env.hint is too long")
        if hint:
            out["hint"] = hint
    workspace_id = raw.get("workspace_id")
    if workspace_id is not None:
        if not isinstance(workspace_id, str) or not workspace_id.startswith("ws_"):
            raise ValueError("execution_env.workspace_id must start with ws_")
        workspace_id = workspace_id.strip()
        if not workspace_id or len(workspace_id) > 128:
            raise ValueError("execution_env.workspace_id is invalid")
        out["workspace_id"] = workspace_id
    return out


def normalize_work_metadata(raw: Any) -> dict[str, Any] | None:
    """Validate optional work metadata (Kernel stores; does not interpret).

    ``None`` clears / means absent. Non-object JSON (list/str/…) is rejected.
    Serialised size capped at 64 KiB (same budget as other metadata fields).
    """
    from ..validators import check_dict_size_64k

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("work metadata must be a JSON object or null")
    check_dict_size_64k("metadata", raw)
    # Shallow copy — callers own nested mutation; Kernel does not deep-parse.
    return dict(raw)


@dataclass
class OrgWorkItem:
    """Minimal work queue item (Phase 1; not full Task Pool)."""

    work_id: str
    org_id: str
    title: str
    status: WorkStatus = "todo"
    assignee_agent_id: str | None = None
    metadata: dict[str, Any] | None = None
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
        self.metadata = normalize_work_metadata(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "org_id": self.org_id,
            "title": self.title,
            "status": self.status,
            "assignee_agent_id": self.assignee_agent_id,
            "metadata": self.metadata,
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
            metadata=data.get("metadata"),
            created_at=created_at or datetime.now(UTC),
            updated_at=updated_at or datetime.now(UTC),
        )
