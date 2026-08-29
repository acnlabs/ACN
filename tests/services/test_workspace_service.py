"""WorkspaceService unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from acn.core.entities.agent import Agent
from acn.core.entities.org import Org, OrgMembership, OrgOwner, OrgPrincipal
from acn.core.entities.task import Task, TaskStatus
from acn.core.entities.workspace import Workspace, WorkspaceAttestation
from acn.core.exceptions import WorkspaceAlreadyActiveError
from acn.core.interfaces.workspace_repository import IWorkspaceRepository
from acn.services.org_service import OrgPermissionError, OrgService
from acn.services.workspace_service import (
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspacePermissionError,
    WorkspaceService,
)


@pytest.fixture
def mock_ws_repo():
    repo = AsyncMock(spec=IWorkspaceRepository)
    repo.find_active_by_task_id = AsyncMock(return_value=None)
    repo.find_active_by_org_id = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_agent_service():
    svc = AsyncMock()

    async def _get_agent(agent_id: str) -> Agent:
        return Agent(
            agent_id=agent_id,
            name=agent_id,
            endpoint="https://example.com",
            owner=None,
        )

    svc.get_agent = AsyncMock(side_effect=_get_agent)
    return svc


def _org() -> Org:
    return Org(
        org_id="org_test",
        display_name="Test",
        created_by=OrgPrincipal(kind="agent", subject="agt_owner"),
        subnet_id="fence",
        steward_agent_id="agt_owner",
        owner=OrgOwner(kind="none"),
    )


@pytest.fixture
def workspace_service(mock_ws_repo, mock_agent_service):
    org_svc = AsyncMock(spec=OrgService)
    org_svc.get_org = AsyncMock(return_value=_org())
    org_svc._require_governance = lambda *args, **kwargs: None
    org_svc.repository = AsyncMock()
    org_svc.repository.find_membership = AsyncMock(return_value=None)
    return WorkspaceService(
        workspace_repository=mock_ws_repo,
        agent_service=mock_agent_service,
        org_service=org_svc,
    )


class TestCreateWorkspace:
    async def test_agent_creates_allowlist(
        self, workspace_service, mock_ws_repo
    ):
        ws = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="allowlist",
            allowlist=["agt_worker"],
        )
        assert ws.owner_agent_id == "agt_owner"
        assert ws.workspace_id.startswith("ws_")
        mock_ws_repo.save_workspace.assert_awaited()

    async def test_human_cannot_create(self, workspace_service):
        with pytest.raises(WorkspacePermissionError, match="agent"):
            await workspace_service.create_workspace(
                caller_type="human",
                caller_sub="auth0|u",
                display_name="Squad",
                execution_env={
                    "kind": "git",
                    "uri": "https://github.com/acme/squad.git",
                },
                admit="allowlist",
            )


class TestGetAndAttest:
    async def test_outsider_404(self, workspace_service, mock_ws_repo):
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="allowlist",
        )
        mock_ws_repo.find_workspace.return_value = created
        with pytest.raises(WorkspaceNotFoundError):
            await workspace_service.get_workspace(
                created.workspace_id,
                caller_type="agent",
                caller_sub="agt_stranger",
            )

    async def test_owner_get_and_attest_git_rejects_usage(
        self, workspace_service, mock_ws_repo
    ):
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="allowlist",
        )
        mock_ws_repo.find_workspace.return_value = created
        got = await workspace_service.get_workspace(
            created.workspace_id,
            caller_type="agent",
            caller_sub="agt_owner",
        )
        assert got.workspace_id == created.workspace_id
        att = await workspace_service.create_attestation(
            created.workspace_id,
            caller_type="agent",
            caller_sub="agt_owner",
            agent_id="agt_worker",
            run_id="r1",
            artifact={"git_sha": "abc"},
        )
        assert att.kind == "workspace_owner"
        with pytest.raises(ValueError, match="usage"):
            await workspace_service.create_attestation(
                created.workspace_id,
                caller_type="agent",
                caller_sub="agt_owner",
                agent_id="agt_worker",
                run_id="r1",
                usage={"input_tokens": 1, "output_tokens": 1},
            )

    async def test_url_attestation_ok(self, workspace_service, mock_ws_repo):
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Runner",
            execution_env={
                "kind": "url",
                "uri": "https://runner.example/v1",
            },
            admit="allowlist",
        )
        mock_ws_repo.find_workspace.return_value = created
        att = await workspace_service.create_attestation(
            created.workspace_id,
            caller_type="agent",
            caller_sub="agt_owner",
            agent_id="agt_worker",
            run_id="r1",
            usage={"input_tokens": 3, "output_tokens": 4},
        )
        assert att.kind == "workspace_owner"
        mock_ws_repo.save_attestation.assert_awaited()

    async def test_non_owner_cannot_attest(self, workspace_service, mock_ws_repo):
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="allowlist",
        )
        mock_ws_repo.find_workspace.return_value = created
        with pytest.raises(WorkspaceNotFoundError):
            await workspace_service.create_attestation(
                created.workspace_id,
                caller_type="agent",
                caller_sub="agt_stranger",
                agent_id="agt_stranger",
                run_id="r1",
            )

    async def test_allowlist_member_cannot_attest(
        self, workspace_service, mock_ws_repo
    ):
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="allowlist",
            allowlist=["agt_worker"],
        )
        mock_ws_repo.find_workspace.return_value = created
        with pytest.raises(WorkspacePermissionError, match="owner"):
            await workspace_service.create_attestation(
                created.workspace_id,
                caller_type="agent",
                caller_sub="agt_worker",
                agent_id="agt_worker",
                run_id="r1",
            )

    async def test_org_member_can_read(self, workspace_service, mock_ws_repo):
        org_svc = workspace_service.org_service
        org_svc.repository.find_membership = AsyncMock(
            return_value=OrgMembership(
                org_id="org_test", agent_id="agt_member", role="worker"
            )
        )
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="org",
            org_id="org_test",
        )
        mock_ws_repo.find_workspace.return_value = created
        got = await workspace_service.get_workspace(
            created.workspace_id,
            caller_type="agent",
            caller_sub="agt_member",
        )
        assert got.admit == "org"


def _task(
    *,
    creator_id: str = "agt_owner",
    creator_type: str = "agent",
    invited_agent_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> Task:
    return Task(
        task_id="task-001",
        creator_type=creator_type,
        creator_id=creator_id,
        creator_name=creator_id,
        title="Bind me",
        description="A task that can bind a workspace",
        reward="0",
        invited_agent_ids=list(invited_agent_ids or []),
        metadata=dict(metadata or {}),
    )


class TestAuthzAndAttestation:
    async def test_task_create_requires_creator(
        self, workspace_service, mock_ws_repo
    ):
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(return_value=_task(creator_id="agt_publisher"))
        workspace_service.task_repository = task_repo
        with pytest.raises(WorkspacePermissionError, match="publisher"):
            await workspace_service.create_workspace(
                caller_type="agent",
                caller_sub="agt_stranger",
                display_name="Task yard",
                execution_env={
                    "kind": "git",
                    "uri": "https://github.com/acme/squad.git",
                },
                admit="task",
                task_id="task-001",
            )

    async def test_task_creator_can_bind(self, workspace_service, mock_ws_repo):
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(return_value=_task())
        workspace_service.task_repository = task_repo
        ws = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Task yard",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="task",
            task_id="task-001",
        )
        assert ws.admit == "task"
        assert ws.task_id == "task-001"

    async def test_human_task_owned_steward_can_bind(
        self, workspace_service, mock_ws_repo, mock_agent_service
    ):
        async def _get_agent(agent_id: str) -> Agent:
            return Agent(
                agent_id=agent_id,
                name=agent_id,
                endpoint="https://example.com",
                owner="auth0|boss",
            )

        mock_agent_service.get_agent = AsyncMock(side_effect=_get_agent)
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(
            return_value=_task(
                creator_type="human",
                creator_id="auth0|boss",
                invited_agent_ids=["agt_worker"],
            )
        )
        workspace_service.task_repository = task_repo
        ws = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_steward",
            display_name="Task yard",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="task",
            task_id="task-001",
        )
        assert ws.owner_agent_id == "agt_steward"

    async def test_human_task_invitee_cannot_bind(
        self, workspace_service, mock_ws_repo
    ):
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(
            return_value=_task(
                creator_type="human",
                creator_id="auth0|boss",
                invited_agent_ids=["agt_worker"],
            )
        )
        workspace_service.task_repository = task_repo
        with pytest.raises(WorkspacePermissionError, match="publisher"):
            await workspace_service.create_workspace(
                caller_type="agent",
                caller_sub="agt_worker",
                display_name="Task yard",
                execution_env={
                    "kind": "git",
                    "uri": "https://github.com/acme/squad.git",
                },
                admit="task",
                task_id="task-001",
            )

    async def test_org_paid_task_steward_can_bind(
        self, workspace_service, mock_ws_repo
    ):
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(
            return_value=_task(creator_type="org", creator_id="org_test")
        )
        workspace_service.task_repository = task_repo
        ws = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Task yard",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="task",
            task_id="task-001",
        )
        assert ws.admit == "task"

    async def test_spoofed_metadata_org_id_does_not_grant_bind(
        self, workspace_service, mock_ws_repo
    ):
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(
            return_value=_task(
                creator_type="human",
                creator_id="auth0|stranger",
                metadata={"org_id": "org_test"},
            )
        )
        workspace_service.task_repository = task_repo
        with pytest.raises(WorkspacePermissionError, match="publisher"):
            await workspace_service.create_workspace(
                caller_type="agent",
                caller_sub="agt_owner",
                display_name="Task yard",
                execution_env={
                    "kind": "git",
                    "uri": "https://github.com/acme/squad.git",
                },
                admit="task",
                task_id="task-001",
            )

    async def test_human_owned_org_steward_can_create(
        self, workspace_service, mock_ws_repo
    ):
        org = Org(
            org_id="org_test",
            display_name="Test",
            created_by=OrgPrincipal(kind="human", subject="auth0|boss"),
            subnet_id="fence",
            steward_agent_id="agt_steward",
            owner=OrgOwner(kind="human", subject="auth0|boss"),
        )
        workspace_service.org_service.get_org = AsyncMock(return_value=org)
        workspace_service.org_service._require_governance = MagicMock(
            side_effect=OrgPermissionError(
                "ownership_mismatch", "Caller is not Org owner"
            )
        )
        ws = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_steward",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="org",
            org_id="org_test",
        )
        assert ws.owner_agent_id == "agt_steward"
        workspace_service.org_service._require_governance.assert_not_called()

    async def test_human_owned_org_other_claimed_agent_cannot_create(
        self, workspace_service, mock_ws_repo, mock_agent_service
    ):
        org = Org(
            org_id="org_test",
            display_name="Test",
            created_by=OrgPrincipal(kind="human", subject="auth0|boss"),
            subnet_id="fence",
            steward_agent_id="agt_steward",
            owner=OrgOwner(kind="human", subject="auth0|boss"),
        )
        workspace_service.org_service.get_org = AsyncMock(return_value=org)
        workspace_service.org_service._require_governance = MagicMock(
            side_effect=OrgPermissionError(
                "ownership_mismatch", "Caller is not Org owner"
            )
        )

        async def _get_agent(agent_id: str) -> Agent:
            return Agent(
                agent_id=agent_id,
                name=agent_id,
                endpoint="https://example.com",
                owner="auth0|boss",
            )

        mock_agent_service.get_agent = AsyncMock(side_effect=_get_agent)
        with pytest.raises(OrgPermissionError, match="not Org owner"):
            await workspace_service.create_workspace(
                caller_type="agent",
                caller_sub="agt_other",
                display_name="Squad",
                execution_env={
                    "kind": "git",
                    "uri": "https://github.com/acme/squad.git",
                },
                admit="org",
                org_id="org_test",
            )

    async def test_org_create_auto_binds(self, workspace_service, mock_ws_repo):
        org = _org()
        workspace_service.org_service.get_org = AsyncMock(return_value=org)
        ws = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="org",
            org_id="org_test",
        )
        assert org.execution_env["workspace_id"] == ws.workspace_id
        workspace_service.org_service.repository.save_org.assert_awaited()

    async def test_org_bind_failure_closes_workspace(
        self, workspace_service, mock_ws_repo
    ):
        org = _org()
        workspace_service.org_service.get_org = AsyncMock(return_value=org)
        workspace_service.org_service.repository.save_org = AsyncMock(
            side_effect=RuntimeError("org save failed")
        )
        with pytest.raises(RuntimeError, match="org save failed"):
            await workspace_service.create_workspace(
                caller_type="agent",
                caller_sub="agt_owner",
                display_name="Squad",
                execution_env={
                    "kind": "git",
                    "uri": "https://github.com/acme/squad.git",
                },
                admit="org",
                org_id="org_test",
            )
        assert mock_ws_repo.save_workspace.await_count == 2
        rolled = mock_ws_repo.save_workspace.await_args_list[-1].args[0]
        assert rolled.status == "closed"
        assert org.execution_env is None or "workspace_id" not in (
            org.execution_env or {}
        )

    async def test_close_clears_org_workspace_pointer(
        self, workspace_service, mock_ws_repo
    ):
        org = _org()
        workspace_service.org_service.get_org = AsyncMock(return_value=org)
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="org",
            org_id="org_test",
        )
        assert org.execution_env["workspace_id"] == created.workspace_id
        mock_ws_repo.find_workspace.return_value = created
        closed = await workspace_service.close_workspace(
            created.workspace_id,
            caller_type="agent",
            caller_sub="agt_owner",
        )
        assert closed.status == "closed"
        assert "workspace_id" not in (org.execution_env or {})
        assert org.execution_env["kind"] == "git"
        assert org.execution_env["uri"] == "https://github.com/acme/squad.git"

    async def test_get_attestation_same_admit(
        self, workspace_service, mock_ws_repo
    ):
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="allowlist",
        )
        mock_ws_repo.find_workspace.return_value = created
        att = WorkspaceAttestation(
            attestation_id="att_1",
            workspace_id=created.workspace_id,
            agent_id="agt_worker",
            run_id="r1",
        )
        mock_ws_repo.find_attestation.return_value = att
        got = await workspace_service.get_attestation(
            created.workspace_id,
            "att_1",
            caller_type="agent",
            caller_sub="agt_owner",
        )
        assert got.attestation_id == "att_1"
        with pytest.raises(WorkspaceNotFoundError):
            await workspace_service.get_attestation(
                created.workspace_id,
                "att_1",
                caller_type="agent",
                caller_sub="agt_stranger",
            )

    async def test_human_org_owner_can_read(
        self, workspace_service, mock_ws_repo
    ):
        org = Org(
            org_id="org_test",
            display_name="Test",
            created_by=OrgPrincipal(kind="human", subject="auth0|boss"),
            subnet_id="fence",
            steward_agent_id="agt_owner",
            owner=OrgOwner(kind="human", subject="auth0|boss"),
        )
        workspace_service.org_service.get_org = AsyncMock(return_value=org)
        workspace_service.org_service._is_owner = (
            lambda o, t, s: t == "human" and s == "auth0|boss"
        )
        workspace_service.org_service._is_created_by = lambda o, t, s: False
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="org",
            org_id="org_test",
        )
        mock_ws_repo.find_workspace.return_value = created
        got = await workspace_service.get_workspace(
            created.workspace_id,
            caller_type="human",
            caller_sub="auth0|boss",
        )
        assert got.workspace_id == created.workspace_id

    async def test_human_task_publisher_can_read(
        self, workspace_service, mock_ws_repo, mock_agent_service
    ):
        async def _get_agent(agent_id: str) -> Agent:
            return Agent(
                agent_id=agent_id,
                name=agent_id,
                endpoint="https://example.com",
                owner="auth0|boss",
            )

        mock_agent_service.get_agent = AsyncMock(side_effect=_get_agent)
        human_task = _task(
            creator_type="human",
            creator_id="auth0|boss",
        )
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(return_value=human_task)
        workspace_service.task_repository = task_repo
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_steward",
            display_name="Task yard",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="task",
            task_id="task-001",
        )
        mock_ws_repo.find_workspace.return_value = created
        got = await workspace_service.get_workspace(
            created.workspace_id,
            caller_type="human",
            caller_sub="auth0|boss",
        )
        assert got.workspace_id == created.workspace_id
        with pytest.raises(WorkspaceNotFoundError):
            await workspace_service.get_workspace(
                created.workspace_id,
                caller_type="human",
                caller_sub="auth0|other",
            )

    async def test_task_assignee_can_read_without_participation(
        self, workspace_service, mock_ws_repo
    ):
        """Single-participant accept sets assignee_id, not a Participation row."""
        bound = _task()
        bound.assignee_id = "agt_worker"
        bound.status = TaskStatus.IN_PROGRESS
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(return_value=bound)
        task_repo.find_participation_by_user_and_task = AsyncMock(return_value=None)
        workspace_service.task_repository = task_repo
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Task yard",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="task",
            task_id="task-001",
        )
        mock_ws_repo.find_workspace.return_value = created
        got = await workspace_service.get_workspace(
            created.workspace_id,
            caller_type="agent",
            caller_sub="agt_worker",
        )
        assert got.workspace_id == created.workspace_id
        with pytest.raises(WorkspaceNotFoundError):
            await workspace_service.get_workspace(
                created.workspace_id,
                caller_type="agent",
                caller_sub="agt_other",
            )

    async def test_spoofed_metadata_org_id_does_not_grant_read(
        self, workspace_service, mock_ws_repo, mock_agent_service
    ):
        async def _get_agent(agent_id: str) -> Agent:
            return Agent(
                agent_id=agent_id,
                name=agent_id,
                endpoint="https://example.com",
                owner="auth0|stranger",
            )

        mock_agent_service.get_agent = AsyncMock(side_effect=_get_agent)
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(
            return_value=_task(
                creator_type="human",
                creator_id="auth0|stranger",
                metadata={"org_id": "org_test"},
            )
        )
        workspace_service.task_repository = task_repo
        org = Org(
            org_id="org_test",
            display_name="Test",
            created_by=OrgPrincipal(kind="human", subject="auth0|boss"),
            subnet_id="fence",
            steward_agent_id="agt_owner",
            owner=OrgOwner(kind="human", subject="auth0|boss"),
        )
        workspace_service.org_service.get_org = AsyncMock(return_value=org)
        workspace_service.org_service._is_owner = (
            lambda o, t, s: t == "human" and s == "auth0|boss"
        )
        workspace_service.org_service._is_created_by = lambda o, t, s: False
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_stranger",
            display_name="Task yard",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="task",
            task_id="task-001",
        )
        mock_ws_repo.find_workspace.return_value = created
        with pytest.raises(WorkspaceNotFoundError):
            await workspace_service.get_workspace(
                created.workspace_id,
                caller_type="human",
                caller_sub="auth0|boss",
            )

    async def test_close_owner_only(self, workspace_service, mock_ws_repo):
        created = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="Squad",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/squad.git",
            },
            admit="allowlist",
            allowlist=["agt_worker"],
        )
        mock_ws_repo.find_workspace.return_value = created
        with pytest.raises(WorkspacePermissionError, match="owner"):
            await workspace_service.close_workspace(
                created.workspace_id,
                caller_type="agent",
                caller_sub="agt_worker",
            )
        closed = await workspace_service.close_workspace(
            created.workspace_id,
            caller_type="agent",
            caller_sub="agt_owner",
        )
        assert closed.status == "closed"
        with pytest.raises(WorkspacePermissionError, match="closed"):
            await workspace_service.create_attestation(
                created.workspace_id,
                caller_type="agent",
                caller_sub="agt_owner",
                agent_id="agt_worker",
                run_id="r1",
            )


def _active_ws(
    *,
    workspace_id: str = "ws_existing",
    admit: str = "task",
    task_id: str | None = "task-001",
    org_id: str | None = None,
) -> Workspace:
    return Workspace(
        workspace_id=workspace_id,
        owner_agent_id="agt_owner",
        display_name="Existing",
        execution_env={
            "kind": "git",
            "uri": "https://github.com/acme/squad.git",
        },
        admit=admit,  # type: ignore[arg-type]
        org_id=org_id,
        task_id=task_id,
        status="active",
    )


class TestCreateUniqueness:
    async def test_second_task_workspace_conflicts(
        self, workspace_service, mock_ws_repo
    ):
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(return_value=_task())
        workspace_service.task_repository = task_repo
        mock_ws_repo.find_active_by_task_id = AsyncMock(
            return_value=_active_ws()
        )
        with pytest.raises(WorkspaceConflictError, match="ws_existing") as ei:
            await workspace_service.create_workspace(
                caller_type="agent",
                caller_sub="agt_owner",
                display_name="Task yard 2",
                execution_env={
                    "kind": "git",
                    "uri": "https://github.com/acme/squad.git",
                },
                admit="task",
                task_id="task-001",
            )
        assert ei.value.reason == "task_workspace_active"
        mock_ws_repo.save_workspace.assert_not_awaited()

    async def test_second_org_workspace_conflicts(
        self, workspace_service, mock_ws_repo
    ):
        mock_ws_repo.find_active_by_org_id = AsyncMock(
            return_value=_active_ws(
                admit="org", org_id="org_test", task_id=None
            )
        )
        with pytest.raises(WorkspaceConflictError, match="ws_existing") as ei:
            await workspace_service.create_workspace(
                caller_type="agent",
                caller_sub="agt_owner",
                display_name="Squad 2",
                execution_env={
                    "kind": "git",
                    "uri": "https://github.com/acme/squad.git",
                },
                admit="org",
                org_id="org_test",
            )
        assert ei.value.reason == "org_workspace_active"
        mock_ws_repo.save_workspace.assert_not_awaited()

    async def test_save_race_maps_to_conflict(
        self, workspace_service, mock_ws_repo
    ):
        task_repo = AsyncMock()
        task_repo.find_by_id = AsyncMock(return_value=_task())
        workspace_service.task_repository = task_repo
        mock_ws_repo.save_workspace = AsyncMock(
            side_effect=WorkspaceAlreadyActiveError(
                "task", "task-001", "ws_winner"
            )
        )
        with pytest.raises(WorkspaceConflictError, match="ws_winner") as ei:
            await workspace_service.create_workspace(
                caller_type="agent",
                caller_sub="agt_owner",
                display_name="Task yard",
                execution_env={
                    "kind": "git",
                    "uri": "https://github.com/acme/squad.git",
                },
                admit="task",
                task_id="task-001",
            )
        assert ei.value.reason == "task_workspace_active"

    async def test_allowlist_not_unique(self, workspace_service, mock_ws_repo):
        first = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="A",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/a.git",
            },
            admit="allowlist",
        )
        second = await workspace_service.create_workspace(
            caller_type="agent",
            caller_sub="agt_owner",
            display_name="B",
            execution_env={
                "kind": "git",
                "uri": "https://github.com/acme/b.git",
            },
            admit="allowlist",
        )
        assert first.workspace_id != second.workspace_id
        assert mock_ws_repo.save_workspace.await_count == 2
        mock_ws_repo.find_active_by_task_id.assert_not_awaited()
        mock_ws_repo.find_active_by_org_id.assert_not_awaited()
