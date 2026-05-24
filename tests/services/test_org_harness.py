"""Unit tests for the pluggable Org Harness interface.

Covers:
- ``SubnetService.update_harness`` — owner authorisation & set / clear
- ``WebhookService.send_to`` — per-target delivery, HMAC signing, no-secret path
- ``TaskService.create_task`` — snapshot of ``subnet.harness_url`` /
  ``harness_secret`` onto ``task.metadata``
- ``TaskService._notify_webhook`` — dual delivery (platform default + per-task
  harness) and the guarantee that a harness delivery failure does NOT break
  the platform-level webhook

These tests use mocks only, no Redis / no network.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from acn.core.entities.subnet import Subnet
from acn.core.entities.task import Task, TaskStatus
from acn.core.exceptions import SubnetNotFoundException
from acn.core.interfaces.subnet_repository import ISubnetRepository
from acn.core.interfaces.task_repository import ITaskRepository
from acn.infrastructure.task_pool import TaskPool
from acn.protocols.ap2.webhook import (
    WebhookEventType,
    WebhookService,
)
from acn.services.subnet_service import SubnetService
from acn.services.task_service import TaskService

# ============================================================================
# SubnetService.update_harness
# ============================================================================


@pytest.fixture
def mock_subnet_repo():
    return AsyncMock(spec=ISubnetRepository)


@pytest.fixture
def subnet_service(mock_subnet_repo):
    return SubnetService(subnet_repository=mock_subnet_repo)


def _make_subnet(**overrides) -> Subnet:
    defaults = {
        "slug": "sn-paperclip",
        "name": "Paperclip Subnet",
        "owner": "agent-owner",
    }
    defaults.update(overrides)
    return Subnet(**defaults)


class TestSubnetServiceUpdateHarness:
    async def test_owner_can_register_harness(self, subnet_service, mock_subnet_repo):
        sn = _make_subnet()
        mock_subnet_repo.find_by_id.return_value = sn

        result = await subnet_service.update_harness(
            slug="sn-paperclip",
            owner="agent-owner",
            harness_url="https://paperclip.example.com/acn/webhook",
            harness_secret="s3cr3t",
        )

        assert result.harness_url == "https://paperclip.example.com/acn/webhook"
        assert result.harness_secret == "s3cr3t"
        mock_subnet_repo.save.assert_awaited_once_with(sn)

    async def test_owner_can_clear_harness(self, subnet_service, mock_subnet_repo):
        sn = _make_subnet(harness_url="https://old.example", harness_secret="old")
        mock_subnet_repo.find_by_id.return_value = sn

        result = await subnet_service.update_harness(
            slug="sn-paperclip",
            owner="agent-owner",
            harness_url=None,
            harness_secret=None,
        )

        assert result.harness_url is None
        assert result.harness_secret is None
        mock_subnet_repo.save.assert_awaited_once()

    async def test_non_owner_raises_permission_error(self, subnet_service, mock_subnet_repo):
        sn = _make_subnet()
        mock_subnet_repo.find_by_id.return_value = sn

        with pytest.raises(PermissionError, match="Owner mismatch"):
            await subnet_service.update_harness(
                slug="sn-paperclip",
                owner="agent-hacker",
                harness_url="https://evil.example",
                harness_secret="pwn",
            )
        mock_subnet_repo.save.assert_not_awaited()

    async def test_system_can_override(self, subnet_service, mock_subnet_repo):
        sn = _make_subnet()
        mock_subnet_repo.find_by_id.return_value = sn

        await subnet_service.update_harness(
            slug="sn-paperclip",
            owner="system",
            harness_url="https://platform-managed.example",
            harness_secret=None,
        )
        mock_subnet_repo.save.assert_awaited_once()

    async def test_unknown_subnet_raises_not_found(self, subnet_service, mock_subnet_repo):
        mock_subnet_repo.find_by_id.return_value = None
        with pytest.raises(SubnetNotFoundException):
            await subnet_service.update_harness(
                slug="nope",
                owner="agent-owner",
                harness_url="https://x",
                harness_secret=None,
            )


# ============================================================================
# WebhookService.send_to
# ============================================================================


class _StubResponse:
    def __init__(self, status_code: int = 200, text: str = "ok"):
        self.status_code = status_code
        self.text = text

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _StubAsyncClient:
    """Captures the last POST and returns a configurable response."""

    def __init__(self, response: _StubResponse | None = None, raises: Exception | None = None):
        self._response = response or _StubResponse()
        self._raises = raises
        self.calls: list[dict] = []

    async def post(self, url, content=None, headers=None, timeout=None):
        self.calls.append({"url": url, "content": content, "headers": headers, "timeout": timeout})
        if self._raises:
            raise self._raises
        return self._response

    async def aclose(self):
        pass


@pytest.fixture
def webhook_service():
    redis = AsyncMock()
    # _save_delivery uses set / lpush / ltrim / expire. AsyncMock auto-handles.
    return WebhookService(redis=redis, default_config=None)


class TestWebhookServiceSendTo:
    async def test_signs_payload_when_secret_provided(self, webhook_service):
        stub = _StubAsyncClient()
        webhook_service._http_client = stub

        ok = await webhook_service.send_to(
            url="https://harness.example/hook",
            secret="topsecret",
            event=WebhookEventType.AGENT_JOINED_SUBNET,
            task_id="sn-1",
            data={"slug": "sn-1", "agent_id": "agent-007"},
            retry_count=1,
            retry_delay=0,
        )

        assert ok is True
        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert call["url"] == "https://harness.example/hook"
        assert call["headers"]["X-ACN-Event"] == "agent.joined_subnet"
        # HMAC signature header present and prefixed
        sig = call["headers"]["X-ACN-Signature"]
        assert sig.startswith("sha256=")
        assert len(sig) > len("sha256=")

    async def test_no_signature_when_secret_is_none(self, webhook_service):
        stub = _StubAsyncClient()
        webhook_service._http_client = stub

        await webhook_service.send_to(
            url="https://harness.example/hook",
            secret=None,
            event=WebhookEventType.AGENT_LEFT_SUBNET,
            task_id="sn-1",
            data={"slug": "sn-1", "agent_id": "agent-007"},
            retry_count=1,
            retry_delay=0,
        )

        headers = stub.calls[0]["headers"]
        assert "X-ACN-Signature" not in headers
        assert headers["X-ACN-Event"] == "agent.left_subnet"

    async def test_empty_url_is_noop_success(self, webhook_service):
        stub = _StubAsyncClient()
        webhook_service._http_client = stub

        ok = await webhook_service.send_to(
            url="",
            secret=None,
            event=WebhookEventType.TASK_ACCEPTED,
            task_id="t-1",
            data={},
        )

        assert ok is True
        assert stub.calls == []

    async def test_returns_false_after_retries_exhausted(self, webhook_service):
        stub = _StubAsyncClient(response=_StubResponse(status_code=500, text="boom"))
        webhook_service._http_client = stub

        ok = await webhook_service.send_to(
            url="https://harness.example/hook",
            secret=None,
            event=WebhookEventType.TASK_COMPLETED,
            task_id="t-1",
            data={"status": "completed"},
            retry_count=2,
            retry_delay=0,
        )

        assert ok is False
        assert len(stub.calls) == 2


# ============================================================================
# TaskService.create_task — harness snapshot
# ============================================================================


@pytest.fixture
def mock_task_repo():
    return AsyncMock(spec=ITaskRepository)


@pytest.fixture
def mock_task_pool(mock_task_repo):
    pool = AsyncMock(spec=TaskPool)
    pool.repository = mock_task_repo
    return pool


@pytest.fixture
def task_service(mock_task_repo, mock_task_pool, mock_subnet_repo):
    return TaskService(
        repository=mock_task_repo,
        task_pool=mock_task_pool,
        payment_manager=None,
        webhook_service=None,
        activity_service=None,
        escrow_client=None,
        agent_repository=None,
        subnet_repository=mock_subnet_repo,
    )


class TestCreateTaskHarnessSnapshot:
    async def test_snapshots_harness_url_and_secret_when_set(
        self, task_service, mock_task_pool, mock_subnet_repo
    ):
        mock_subnet_repo.find_by_id.return_value = _make_subnet(
            harness_url="https://paperclip.example/acn",
            harness_secret="harness-key",
        )

        task = await task_service.create_task(
            creator_type="agent",
            creator_id="agent-001",
            creator_name="Bot-1",
            title="Wire a paperclip",
            description="Do the thing",
            task_type="general",
            required_tags=[],
            reward="0",
            reward_currency="credits",
            max_participants=1,
            subnet_id="sn-paperclip",
        )

        assert task.metadata.get("harness_url") == "https://paperclip.example/acn"
        assert task.metadata.get("harness_secret") == "harness-key"
        mock_task_pool.add.assert_awaited_once()

    async def test_no_snapshot_when_subnet_has_no_harness(
        self, task_service, mock_subnet_repo
    ):
        mock_subnet_repo.find_by_id.return_value = _make_subnet()  # no harness

        task = await task_service.create_task(
            creator_type="agent",
            creator_id="agent-001",
            creator_name="Bot-1",
            title="t",
            description="d",
            task_type="general",
            required_tags=[],
            reward="0",
            reward_currency="credits",
            max_participants=1,
            subnet_id="sn-plain",
        )

        assert "harness_url" not in task.metadata
        assert "harness_secret" not in task.metadata

    async def test_no_snapshot_when_subnet_id_absent(self, task_service, mock_subnet_repo):
        task = await task_service.create_task(
            creator_type="agent",
            creator_id="agent-001",
            creator_name="Bot-1",
            title="t",
            description="d",
            task_type="general",
            required_tags=[],
            reward="0",
            reward_currency="credits",
            max_participants=1,
            subnet_id=None,
        )

        assert "harness_url" not in task.metadata
        # No subnet lookup attempted
        mock_subnet_repo.find_by_id.assert_not_called()

    async def test_subnet_lookup_failure_is_swallowed(
        self, task_service, mock_subnet_repo
    ):
        mock_subnet_repo.find_by_id.side_effect = RuntimeError("redis is down")

        task = await task_service.create_task(
            creator_type="agent",
            creator_id="agent-001",
            creator_name="Bot-1",
            title="t",
            description="d",
            task_type="general",
            required_tags=[],
            reward="0",
            reward_currency="credits",
            max_participants=1,
            subnet_id="sn-paperclip",
        )
        # Task still created, harness simply not snapshotted
        assert "harness_url" not in task.metadata

    async def test_does_not_mutate_caller_metadata(
        self, task_service, mock_subnet_repo
    ):
        """Caller-supplied metadata dict must not be mutated in place."""
        mock_subnet_repo.find_by_id.return_value = _make_subnet(
            harness_url="https://h.example",
            harness_secret="sec",
        )
        caller_metadata = {"foo": "bar"}

        await task_service.create_task(
            creator_type="agent",
            creator_id="agent-001",
            creator_name="Bot-1",
            title="t",
            description="d",
            task_type="general",
            required_tags=[],
            reward="0",
            reward_currency="credits",
            max_participants=1,
            metadata=caller_metadata,
            subnet_id="sn-paperclip",
        )

        assert caller_metadata == {"foo": "bar"}  # unchanged


# ============================================================================
# TaskService._notify_webhook — dual delivery
# ============================================================================


def _make_task_with_harness(**overrides) -> Task:
    _SENTINEL = object()
    metadata = overrides.pop("metadata", _SENTINEL)
    if metadata is _SENTINEL:
        metadata = {
            "harness_url": "https://paperclip.example/acn",
            "harness_secret": "harness-key",
        }
    defaults = {
        "task_id": "t-harness-1",
        "creator_type": "agent",
        "creator_id": "agent-creator",
        "creator_name": "Creator",
        "title": "T",
        "description": "D",
        "reward": "0",
        "reward_currency": "credits",
        "max_participants": 1,
        "subnet_id": "sn-paperclip",
        "metadata": metadata,
        "status": TaskStatus.IN_PROGRESS,
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestNotifyWebhookDualDelivery:
    async def test_delivers_to_both_platform_and_harness(self, task_service):
        webhook = AsyncMock()
        task_service.webhook = webhook
        task = _make_task_with_harness()

        await task_service._notify_webhook(WebhookEventType.TASK_ACCEPTED, task)

        webhook.send_event.assert_awaited_once()
        send_event_kwargs = webhook.send_event.await_args.kwargs
        assert send_event_kwargs["event"] == WebhookEventType.TASK_ACCEPTED
        assert send_event_kwargs["task_id"] == "t-harness-1"
        # payload includes slug so receivers can correlate
        assert send_event_kwargs["data"]["slug"] == "sn-paperclip"

        webhook.send_to.assert_awaited_once()
        send_to_kwargs = webhook.send_to.await_args.kwargs
        assert send_to_kwargs["url"] == "https://paperclip.example/acn"
        assert send_to_kwargs["secret"] == "harness-key"
        assert send_to_kwargs["event"] == WebhookEventType.TASK_ACCEPTED

    async def test_no_harness_url_skips_send_to(self, task_service):
        webhook = AsyncMock()
        task_service.webhook = webhook
        task = _make_task_with_harness(metadata={})  # snapshot absent

        await task_service._notify_webhook(WebhookEventType.TASK_SUBMITTED, task)

        webhook.send_event.assert_awaited_once()
        webhook.send_to.assert_not_awaited()

    async def test_harness_delivery_failure_does_not_break_platform_delivery(
        self, task_service
    ):
        """Even if Paperclip's harness URL is dead, ACN must not stop firing
        the platform-level webhook (which feeds billing, reputation, etc.)."""
        webhook = AsyncMock()
        webhook.send_event = AsyncMock(return_value=True)
        webhook.send_to = AsyncMock(side_effect=httpx.ConnectError("conn refused"))
        task_service.webhook = webhook

        task = _make_task_with_harness()

        # No exception should escape
        await task_service._notify_webhook(WebhookEventType.TASK_COMPLETED, task)

        webhook.send_event.assert_awaited_once()
        webhook.send_to.assert_awaited_once()

    async def test_platform_webhook_failure_does_not_break_harness(self, task_service):
        """Symmetric: if the platform webhook explodes, we still try to call
        the per-subnet harness."""
        webhook = AsyncMock()
        webhook.send_event = AsyncMock(side_effect=RuntimeError("platform webhook bug"))
        webhook.send_to = AsyncMock(return_value=True)
        task_service.webhook = webhook

        task = _make_task_with_harness()

        await task_service._notify_webhook(WebhookEventType.TASK_COMPLETED, task)

        webhook.send_event.assert_awaited_once()
        webhook.send_to.assert_awaited_once()

    async def test_no_webhook_service_is_noop(self, task_service):
        task_service.webhook = None
        task = _make_task_with_harness()
        # Should not raise
        await task_service._notify_webhook(WebhookEventType.TASK_ACCEPTED, task)


# ============================================================================
# Participation resubmit cap  (max_resubmit_attempts + resubmit_count)
# ============================================================================


def _make_participation(**overrides):
    from acn.core.entities.task import Participation, ParticipationStatus

    defaults = {
        "participation_id": "p-001",
        "task_id": "t-001",
        "participant_id": "agent-worker",
        "participant_name": "Worker",
        "status": ParticipationStatus.REJECTED,
        "resubmit_count": 0,
    }
    defaults.update(overrides)
    return Participation(**defaults)


class TestResubmitCap:
    """Verify that max_resubmit_attempts is enforced in submit_task and
    that resubmit_count increments correctly on the entity."""

    def test_resubmit_increments_count(self):
        """Participation.resubmit() bumps resubmit_count by 1."""
        p = _make_participation(resubmit_count=2)
        p.resubmit("second try")
        assert p.resubmit_count == 3

    def test_resubmit_resets_rejection_fields(self):
        """After resubmit, rejection fields are cleared."""
        from datetime import UTC, datetime

        p = _make_participation(
            rejection_reason="not good",
            rejected_at=datetime.now(UTC),
            review_notes="try harder",
        )
        p.resubmit("better attempt")
        assert p.rejection_reason is None
        assert p.rejected_at is None
        assert p.review_notes is None

    async def test_submit_task_blocks_when_cap_reached(
        self, task_service, mock_task_repo, mock_task_pool
    ):
        """submit_task raises ValueError once resubmit_count >= max_resubmit_attempts."""
        from acn.core.entities.task import ParticipationStatus, TaskStatus

        task = _make_task_with_harness(
            max_participants=3,
            status=TaskStatus.OPEN,
            max_resubmit_attempts=2,
        )
        mock_task_repo.find_by_id.return_value = task

        p = _make_participation(
            task_id=task.task_id,
            status=ParticipationStatus.REJECTED,
            resubmit_count=2,  # already at cap
        )
        # _resolve_participation uses task_pool.get_user_participation
        mock_task_pool.get_user_participation.return_value = p

        with pytest.raises(ValueError, match="Max resubmit attempts"):
            await task_service.submit_task(
                task_id=task.task_id,
                agent_id="agent-worker",
                submission="third try — should be blocked",
            )

    async def test_submit_task_allows_when_under_cap(
        self, task_service, mock_task_repo, mock_task_pool
    ):
        """submit_task succeeds when resubmit_count < max_resubmit_attempts."""
        from acn.core.entities.task import ParticipationStatus, TaskStatus

        task = _make_task_with_harness(
            max_participants=3,
            status=TaskStatus.OPEN,
            max_resubmit_attempts=3,
        )
        mock_task_repo.find_by_id.return_value = task

        p = _make_participation(
            task_id=task.task_id,
            status=ParticipationStatus.REJECTED,
            resubmit_count=2,  # one slot remaining
        )
        mock_task_pool.get_user_participation.return_value = p
        mock_task_repo.save_participation = AsyncMock()

        returned_task = await task_service.submit_task(
            task_id=task.task_id,
            agent_id="agent-worker",
            submission="third try — should pass",
        )
        assert returned_task is task
        assert p.resubmit_count == 3

    async def test_submit_task_no_cap_when_max_resubmit_none(
        self, task_service, mock_task_repo, mock_task_pool
    ):
        """max_resubmit_attempts=None means unlimited resubmits."""
        from acn.core.entities.task import ParticipationStatus, TaskStatus

        task = _make_task_with_harness(
            max_participants=3,
            status=TaskStatus.OPEN,
            max_resubmit_attempts=None,
        )
        mock_task_repo.find_by_id.return_value = task

        p = _make_participation(
            task_id=task.task_id,
            status=ParticipationStatus.REJECTED,
            resubmit_count=999,
        )
        mock_task_pool.get_user_participation.return_value = p
        mock_task_repo.save_participation = AsyncMock()

        # Should not raise
        await task_service.submit_task(
            task_id=task.task_id,
            agent_id="agent-worker",
            submission="attempt 1000",
        )
        assert p.resubmit_count == 1000


# ============================================================================
# PARTICIPATION_REJECTED webhook
# ============================================================================


class TestParticipationRejectedWebhook:
    """_notify_participation_webhook sends PARTICIPATION_REJECTED to both
    platform webhook and per-subnet harness, with participation-level fields."""

    async def test_delivers_participation_fields_to_harness(self, task_service):
        from acn.core.entities.task import ParticipationStatus

        webhook = AsyncMock()
        task_service.webhook = webhook
        task = _make_task_with_harness()
        p = _make_participation(
            task_id=task.task_id,
            status=ParticipationStatus.REJECTED,
            rejection_reason="output too short",
            resubmit_count=1,
        )

        await task_service._notify_participation_webhook(
            WebhookEventType.PARTICIPATION_REJECTED, task, p
        )

        # Platform webhook receives participation context
        webhook.send_event.assert_awaited_once()
        data = webhook.send_event.await_args.kwargs["data"]
        assert data["participant_id"] == "agent-worker"
        assert data["participation_id"] == "p-001"
        assert data["rejection_reason"] == "output too short"
        assert data["resubmit_count"] == 1
        assert data["max_resubmit_attempts"] == task.max_resubmit_attempts

        # Harness also receives the same payload
        webhook.send_to.assert_awaited_once()
        assert webhook.send_to.await_args.kwargs["event"] == WebhookEventType.PARTICIPATION_REJECTED

    async def test_no_webhook_service_is_noop(self, task_service):
        from acn.core.entities.task import ParticipationStatus

        task_service.webhook = None
        p = _make_participation(status=ParticipationStatus.REJECTED)
        task = _make_task_with_harness()
        # Should not raise
        await task_service._notify_participation_webhook(
            WebhookEventType.PARTICIPATION_REJECTED, task, p
        )

    async def test_harness_failure_does_not_surface(self, task_service):
        """A harness delivery failure must not propagate to the caller."""
        from acn.core.entities.task import ParticipationStatus

        webhook = AsyncMock()
        webhook.send_event = AsyncMock(return_value=True)
        webhook.send_to = AsyncMock(side_effect=RuntimeError("network error"))
        task_service.webhook = webhook

        p = _make_participation(status=ParticipationStatus.REJECTED)
        task = _make_task_with_harness()

        # Should not raise
        await task_service._notify_participation_webhook(
            WebhookEventType.PARTICIPATION_REJECTED, task, p
        )
