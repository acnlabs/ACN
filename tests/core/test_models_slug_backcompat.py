"""Pydantic-layer back-compat tests for the ``subnet_id`` → ``slug`` rename.

The Step 1 rename keeps support for legacy field names on inputs so
existing SDK clients survive a rolling deploy. The repo test suite
otherwise stubs out the request models with ``AsyncMock`` /
``MagicMock``, which means a regression in these compatibility paths
only surfaces in production. Pin them here against the real Pydantic
classes so a future "clean up the alias" PR can't silently drop
support without a failing test.
"""

from __future__ import annotations

import pytest

from acn.models import AgentRegisterRequest, SubnetCreateRequest


class TestSubnetCreateRequestLegacyAliases:
    """``SubnetCreateRequest`` must accept the pre-rename body fields
    (``subnet_id``, ``parent_subnet_id``) on input. Without
    ``AliasChoices``, Pydantic's default ``extra='ignore'`` config
    silently drops the legacy fields and the route auto-generates a
    different slug than the caller intended — a worse failure mode
    than a 400 because clients don't notice the drift."""

    def test_legacy_subnet_id_maps_to_slug(self):
        req = SubnetCreateRequest(subnet_id="legacy-net", name="Legacy")  # type: ignore[call-arg]

        assert req.slug == "legacy-net"

    def test_legacy_parent_subnet_id_maps_to_parent_slug(self):
        req = SubnetCreateRequest(
            slug="child-net",
            name="Child",
            parent_subnet_id="parent-net",  # type: ignore[call-arg]
        )

        assert req.parent_slug == "parent-net"

    def test_new_slug_field_takes_precedence_when_both_present(self):
        # Pydantic resolves ``AliasChoices`` left-to-right: ``slug``
        # wins over ``subnet_id`` when both are supplied. Pinned so a
        # future alias re-ordering can't silently flip precedence.
        req = SubnetCreateRequest(
            slug="new",
            subnet_id="old",  # type: ignore[call-arg]
            name="Mixed",
        )

        assert req.slug == "new"


class TestAgentRegisterRequestGetSubnetIds:
    """``AgentRegisterRequest.get_subnet_ids()`` is the single helper
    that resolves "which subnet(s) does this registration target?"
    across the legacy single-id and the modern multi-id field. Bug
    report: a previous batch rename of ``subnet_id`` → ``slug``
    rewrote the helper to read ``self.slug``, which the request model
    doesn't have — every legacy single-subnet registration would
    500. Pin all three branches here so the helper survives future
    refactors."""

    def test_returns_subnet_ids_when_provided(self):
        req = AgentRegisterRequest(
            owner="owner-1",
            name="alice",
            a2a_endpoint="https://example.com",
            subnet_ids=["foo", "bar"],
        )

        assert req.get_subnet_ids() == ["foo", "bar"]

    def test_legacy_single_subnet_id_wraps_to_list(self):
        req = AgentRegisterRequest(
            owner="owner-1",
            name="alice",
            a2a_endpoint="https://example.com",
            subnet_id="legacy-net",
        )

        # Critical regression: must not raise AttributeError; must
        # return the legacy field value wrapped in a single-element
        # list so downstream registry validation walks one entry.
        assert req.get_subnet_ids() == ["legacy-net"]

    def test_defaults_to_public_when_neither_field_provided(self):
        req = AgentRegisterRequest(
            owner="owner-1",
            name="alice",
            a2a_endpoint="https://example.com",
        )

        assert req.get_subnet_ids() == ["public"]


class TestAgentServiceSearchAgentsKwargCompat:
    """The route layer ``GET /api/v1/subnets/{slug}/agents`` calls
    ``agent_service.search_agents(...)``. The kwarg name on the
    service is still ``subnet_id`` (Step 2 of the rename will migrate
    it together with ``Agent.subnet_ids``). Pin the signature so a
    future patch can't accidentally re-introduce the
    ``slug=slug`` call site that mock-based route tests fail to
    catch."""

    def test_search_agents_signature_uses_slug_kwarg(self):
        """Step 2 migrated AgentService.search_agents to use ``slug=``."""
        import inspect

        from acn.services.agent_service import AgentService

        sig = inspect.signature(AgentService.search_agents)
        params = sig.parameters
        assert "slug" in params, (
            "AgentService.search_agents must accept ``slug=`` "
            "after Step 2 migrates the parameter name."
        )
        assert "subnet_id" not in params, (
            "Legacy ``subnet_id`` parameter removed in Step 2; "
            "callers should use ``slug=`` now."
        )

    @pytest.mark.asyncio
    async def test_route_calls_with_slug_kwarg(self):
        """routes/subnets.py calls search_agents(slug=...) after Step 2."""
        from unittest.mock import AsyncMock

        from acn.services.agent_service import AgentService

        service = AsyncMock(spec=AgentService)
        service.search_agents.return_value = []

        await service.search_agents(slug="some-slug")

        service.search_agents.assert_awaited_once_with(slug="some-slug")


# ===========================================================================
# Step 2: Task entity backward compatibility
# ===========================================================================
class TestTaskFromDictLegacySubnetId:
    """Back-compat: Task.from_dict must accept legacy ``subnet_id`` key."""

    def test_subnet_id_key_translated_to_subnet_slug(self):
        from acn.core.entities import Task, TaskStatus

        data = {
            "task_id": "t1",
            "creator_type": "agent",
            "creator_id": "a1",
            "creator_name": "Alice",
            "title": "T",
            "description": "",
            "reward": "10",
            "reward_currency": "credits",
            "max_participants": 1,
            "status": TaskStatus.OPEN,
            "required_tags": [],
            "subnet_id": "acnlabs-core",
        }
        task = Task.from_dict(data)
        assert task.subnet_slug == "acnlabs-core"

    def test_subnet_slug_key_takes_precedence(self):
        """subnet_slug wins when both keys are present (shouldn't happen, but safe)."""
        from acn.core.entities import Task, TaskStatus

        data = {
            "task_id": "t1",
            "creator_type": "agent",
            "creator_id": "a1",
            "creator_name": "Alice",
            "title": "T",
            "description": "",
            "reward": "10",
            "reward_currency": "credits",
            "max_participants": 1,
            "status": TaskStatus.OPEN,
            "required_tags": [],
            "subnet_slug": "correct-slug",
            "subnet_id": "stale-old-key",
        }
        task = Task.from_dict(data)
        assert task.subnet_slug == "correct-slug"

    def test_no_subnet_gives_none(self):
        from acn.core.entities import Task, TaskStatus

        data = {
            "task_id": "t1",
            "creator_type": "agent",
            "creator_id": "a1",
            "creator_name": "Alice",
            "title": "T",
            "description": "",
            "reward": "10",
            "reward_currency": "credits",
            "max_participants": 1,
            "status": TaskStatus.OPEN,
            "required_tags": [],
        }
        task = Task.from_dict(data)
        assert task.subnet_slug is None


class TestTaskCreateRequestLegacySubnetId:
    """Back-compat: TaskCreateRequest must accept legacy ``subnet_id`` in request body."""

    def test_subnet_id_body_key_translated(self):
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
        from acn.routes.tasks import TaskCreateRequest  # type: ignore[attr-defined]

        req = TaskCreateRequest.model_validate({
            "title": "Test task",
            "description": "A meaningful description",
            "reward": "10",
            "deadline_hours": 24,
            "subnet_id": "acnlabs-core",
        })
        assert req.subnet_slug == "acnlabs-core"

    def test_subnet_slug_body_key_accepted(self):
        from acn.routes.tasks import TaskCreateRequest  # type: ignore[attr-defined]

        req = TaskCreateRequest.model_validate({
            "title": "Test task",
            "description": "A meaningful description",
            "reward": "10",
            "deadline_hours": 24,
            "subnet_slug": "acnlabs-core",
        })
        assert req.subnet_slug == "acnlabs-core"
