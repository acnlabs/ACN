"""Unit tests for TaskService.invite_agent A2A push (best-effort)."""

from unittest.mock import AsyncMock

import pytest

from acn.core.entities.task import Task, TaskStatus
from acn.protocols.ap2 import WebhookEventType
from acn.services.task_service import TaskService


def _make_task(**overrides) -> Task:
    defaults = {
        "task_id": "task-invite-001",
        "creator_type": "agent",
        "creator_id": "creator-001",
        "creator_name": "Creator",
        "title": "Fix the invite push",
        "description": "Invitee should receive A2A",
        "reward": "10",
        "reward_currency": "credits",
        "status": TaskStatus.OPEN,
        "subnet_slug": "demo-subnet",
        "invited_agent_ids": [],
    }
    defaults.update(overrides)
    return Task(**defaults)


@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_message_service():
    svc = AsyncMock()
    svc.send_message = AsyncMock(return_value={"status": "delivered"})
    return svc


@pytest.fixture
def mock_webhook():
    wh = AsyncMock()
    wh.send_event = AsyncMock()
    wh.send_to = AsyncMock()
    return wh


class TestInviteAgentA2APush:
    async def test_invite_sends_a2a_with_task_id_metadata(
        self, mock_repo, mock_message_service, mock_webhook
    ):
        task = _make_task()
        mock_repo.find_by_id = AsyncMock(return_value=task)
        mock_repo.save = AsyncMock()

        service = TaskService(
            repository=mock_repo,
            message_service=mock_message_service,
            webhook_service=mock_webhook,
        )

        result = await service.invite_agent(
            task_id=task.task_id,
            inviter_id="creator-001",
            invitee_id="worker-001",
            invitee_name="Worker",
        )

        assert "worker-001" in result.invited_agent_ids
        mock_repo.save.assert_awaited_once()
        mock_message_service.send_message.assert_awaited_once()

        call = mock_message_service.send_message.await_args
        assert call.kwargs["from_agent_id"] == "creator-001"
        assert call.kwargs["to_agent_id"] == "worker-001"
        assert call.kwargs["message_type"] == "task_request"
        message = call.kwargs["message"]
        assert message.metadata["task_id"] == "task-invite-001"
        assert message.metadata["acn_task_id"] == "task-invite-001"
        assert message.metadata["type"] == "task_request"
        assert message.metadata["message_type"] == "task_request"
        assert message.metadata["title"] == "Fix the invite push"

        mock_webhook.send_event.assert_awaited_once()
        wh_call = mock_webhook.send_event.await_args
        assert wh_call.kwargs["event"] == WebhookEventType.TASK_INVITED
        assert wh_call.kwargs["task_id"] == "task-invite-001"
        assert wh_call.kwargs["data"]["invitee_id"] == "worker-001"

    async def test_invite_push_failure_does_not_rollback(
        self, mock_repo, mock_message_service
    ):
        task = _make_task()
        mock_repo.find_by_id = AsyncMock(return_value=task)
        mock_repo.save = AsyncMock()
        mock_message_service.send_message = AsyncMock(
            side_effect=RuntimeError("router down")
        )

        service = TaskService(
            repository=mock_repo,
            message_service=mock_message_service,
        )

        result = await service.invite_agent(
            task_id=task.task_id,
            inviter_id="creator-001",
            invitee_id="worker-001",
        )

        assert "worker-001" in result.invited_agent_ids
        mock_repo.save.assert_awaited_once()
        mock_message_service.send_message.assert_awaited_once()

    async def test_invite_without_message_service_only_writes_whitelist(self, mock_repo):
        task = _make_task()
        mock_repo.find_by_id = AsyncMock(return_value=task)
        mock_repo.save = AsyncMock()

        service = TaskService(repository=mock_repo, message_service=None)

        result = await service.invite_agent(
            task_id=task.task_id,
            inviter_id="creator-001",
            invitee_id="worker-001",
        )

        assert "worker-001" in result.invited_agent_ids
        mock_repo.save.assert_awaited_once()
        assert service.message_service is None

    async def test_human_inviter_sends_as_system_task_invite(
        self, mock_repo, mock_message_service
    ):
        """Human creators are not in the agent table — use system: sender."""
        task = _make_task(creator_type="human", creator_id="human-user-1")
        mock_repo.find_by_id = AsyncMock(return_value=task)
        mock_repo.save = AsyncMock()

        agent_repo = AsyncMock()
        agent_repo.find_by_id = AsyncMock(return_value=None)

        service = TaskService(
            repository=mock_repo,
            message_service=mock_message_service,
            agent_repository=agent_repo,
        )

        result = await service.invite_agent(
            task_id=task.task_id,
            inviter_id="human-user-1",
            invitee_id="worker-001",
        )

        assert "worker-001" in result.invited_agent_ids
        call = mock_message_service.send_message.await_args
        assert call.kwargs["from_agent_id"] == "system:task-invite"
        assert call.kwargs["message"].metadata["from_agent"] == "human-user-1"
        agent_repo.find_by_id.assert_awaited_once_with("human-user-1")
