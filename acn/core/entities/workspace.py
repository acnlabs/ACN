"""ACN Execution Workspace — Network Core object (exec-workspace-v0).

Kernel stores a pointer + admit + owner attestations. It does not run a
sandbox or a harness. Collaboration does not require a workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .org import normalize_execution_env

AdmitKind = Literal["org", "task", "allowlist"]
WorkspaceStatus = Literal["active", "closed"]

_ADMIT_KINDS = frozenset({"org", "task", "allowlist"})
_WORKSPACE_STATUSES = frozenset({"active", "closed"})
_MAX_ALLOWLIST = 64
_MAX_WORKSPACE_ID_LEN = 128


def normalize_workspace_execution_env(raw: Any) -> dict[str, Any]:
    """Workspace env face: ``git`` or ``url`` only (not ``none``)."""
    env = normalize_execution_env(raw)
    if env is None:
        raise ValueError("workspace execution_env.kind must be git or url")
    env.pop("workspace_id", None)
    return env


def normalize_workspace_id(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.startswith("ws_"):
        raise ValueError("workspace_id must start with ws_")
    ws_id = raw.strip()
    if not ws_id or len(ws_id) > _MAX_WORKSPACE_ID_LEN:
        raise ValueError("invalid workspace_id")
    return ws_id


@dataclass
class Workspace:
    workspace_id: str
    owner_agent_id: str
    display_name: str
    execution_env: dict[str, Any]
    admit: AdmitKind
    org_id: str | None = None
    task_id: str | None = None
    allowlist: list[str] = field(default_factory=list)
    status: WorkspaceStatus = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise ValueError("workspace_id cannot be empty")
        if not self.owner_agent_id:
            raise ValueError("owner_agent_id cannot be empty")
        if not self.display_name or not self.display_name.strip():
            raise ValueError("display_name cannot be empty")
        self.display_name = self.display_name.strip()
        if len(self.display_name) > 200:
            raise ValueError("display_name is too long")
        if self.admit not in _ADMIT_KINDS:
            raise ValueError(f"invalid admit: {self.admit!r}")
        if self.status not in _WORKSPACE_STATUSES:
            raise ValueError(f"invalid workspace status: {self.status!r}")
        self.execution_env = normalize_workspace_execution_env(self.execution_env)
        if self.admit == "org":
            if not self.org_id:
                raise ValueError("admit=org requires org_id")
            self.task_id = None
        elif self.admit == "task":
            if not self.task_id:
                raise ValueError("admit=task requires task_id")
            self.org_id = None
        else:
            self.org_id = None
            self.task_id = None
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in self.allowlist:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("allowlist entries must be agent ids")
            aid = item.strip()
            if aid in seen:
                continue
            seen.add(aid)
            cleaned.append(aid)
        if len(cleaned) > _MAX_ALLOWLIST:
            raise ValueError("allowlist is too long")
        self.allowlist = cleaned

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "owner_agent_id": self.owner_agent_id,
            "display_name": self.display_name,
            "execution_env": dict(self.execution_env),
            "admit": self.admit,
            "org_id": self.org_id,
            "task_id": self.task_id,
            "allowlist": list(self.allowlist),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Workspace:
        created_at = data.get("created_at")
        updated_at = data.get("updated_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        return cls(
            workspace_id=data["workspace_id"],
            owner_agent_id=data["owner_agent_id"],
            display_name=data["display_name"],
            execution_env=data.get("execution_env"),
            admit=data.get("admit") or "allowlist",
            org_id=data.get("org_id"),
            task_id=data.get("task_id"),
            allowlist=list(data.get("allowlist") or []),
            status=data.get("status") or "active",
            created_at=created_at or datetime.now(UTC),
            updated_at=updated_at or datetime.now(UTC),
        )


@dataclass
class WorkspaceAttestation:
    attestation_id: str
    workspace_id: str
    agent_id: str
    run_id: str
    kind: str = "workspace_owner"
    work_id: str | None = None
    task_id: str | None = None
    hop_id: str | None = None
    artifact: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    issued_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.attestation_id:
            raise ValueError("attestation_id cannot be empty")
        if self.kind != "workspace_owner":
            raise ValueError("attestation.kind must be workspace_owner")
        if not self.workspace_id or not self.agent_id or not self.run_id:
            raise ValueError("workspace_id, agent_id, and run_id are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            "kind": self.kind,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "work_id": self.work_id,
            "task_id": self.task_id,
            "hop_id": self.hop_id,
            "artifact": self.artifact,
            "usage": self.usage,
            "issued_at": self.issued_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceAttestation:
        issued_at = data.get("issued_at")
        if isinstance(issued_at, str):
            issued_at = datetime.fromisoformat(issued_at)
        return cls(
            attestation_id=data["attestation_id"],
            workspace_id=data["workspace_id"],
            agent_id=data["agent_id"],
            run_id=data["run_id"],
            kind=data.get("kind") or "workspace_owner",
            work_id=data.get("work_id"),
            task_id=data.get("task_id"),
            hop_id=data.get("hop_id"),
            artifact=data.get("artifact"),
            usage=data.get("usage"),
            issued_at=issued_at or datetime.now(UTC),
        )
