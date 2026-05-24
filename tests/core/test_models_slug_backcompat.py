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

    def test_search_agents_signature_uses_subnet_id_kwarg(self):
        import inspect

        from acn.services.agent_service import AgentService

        sig = inspect.signature(AgentService.search_agents)
        params = sig.parameters
        assert "subnet_id" in params, (
            "AgentService.search_agents must accept ``subnet_id=`` "
            "until Step 2 migrates Agent.subnet_ids; routes/subnets.py "
            "calls it with this kwarg."
        )
        # And it must NOT accept the new name yet — that would mask the
        # very inconsistency this test pins. Step 2 migrates this; this
        # assertion is expected to be updated alongside that migration.
        assert "slug" not in params

    @pytest.mark.asyncio
    async def test_route_calls_with_subnet_id_kwarg(self):
        # Real-call regression: mock the service with a strict spec so
        # an unexpected kwarg raises (the loose ``AsyncMock(return_value=[])``
        # used elsewhere swallows the error). Imports kept local because
        # ``agent_service`` constructs are heavy to import.
        from unittest.mock import AsyncMock

        from acn.services.agent_service import AgentService

        service = AsyncMock(spec=AgentService)
        service.search_agents.return_value = []

        # This is the call shape from routes/subnets.py::get_subnet_agents.
        await service.search_agents(subnet_id="some-slug")

        service.search_agents.assert_awaited_once_with(subnet_id="some-slug")
