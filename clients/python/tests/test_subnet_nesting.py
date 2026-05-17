"""ADR-0003 Phase 2 — Python SDK subnet-nesting surface tests.

Pin:
- ``SubnetInfo`` / ``SubnetCreateRequest`` round-trip the three new
  nesting fields (parent_subnet_id, lifecycle, linked_task_id).
- ``Client.list_subnets(parent_subnet_id=...)`` issues the
  ``?parent=...`` filter on the wire.
- ``Client.list_children(...)`` hits the dedicated
  ``/api/v1/subnets/{id}/children`` endpoint.
- ``Client.promote_subnet(...)`` issues ``POST
  /api/v1/subnets/{id}/promote`` and parses the returned
  ``SubnetInfo``.
- Back-compat: SDKs talking to an older server that omits the new
  fields still parse cleanly (defaults preserved).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from acn_client.client import ACNClient
from acn_client.models import SubnetCreateRequest, SubnetInfo


def _server_subnet_payload(**overrides: Any) -> dict[str, Any]:
    """Minimal subnet response payload as ACN would emit it."""
    base = {
        "subnet_id": "squad-1",
        "name": "Squad 1",
        "owner": "alice",
        "is_private": False,
        "parent_subnet_id": "parent-1",
        "lifecycle": "task_scoped",
        "linked_task_id": "task-42",
    }
    base.update(overrides)
    return base


class TestSubnetInfoNestingFields:
    def test_parses_new_nesting_fields_from_server_payload(self):
        payload = _server_subnet_payload()
        info = SubnetInfo.model_validate(payload)
        assert info.parent_subnet_id == "parent-1"
        assert info.lifecycle == "task_scoped"
        assert info.linked_task_id == "task-42"

    def test_back_compat_with_legacy_server_omitting_fields(self):
        """SDK talking to an older server: the three new fields are
        absent from the JSON. Defaults must preserve a top-level
        persistent subnet shape so legacy consumers don't crash."""
        info = SubnetInfo.model_validate(
            {"subnet_id": "legacy", "name": "Legacy"}
        )
        assert info.parent_subnet_id is None
        assert info.lifecycle == "persistent"
        assert info.linked_task_id is None


class TestSubnetCreateRequestNestingFields:
    def test_serialises_nesting_fields(self):
        req = SubnetCreateRequest(
            name="Bug Squad",
            parent_subnet_id="parent-1",
            lifecycle="task_scoped",
            linked_task_id="task-42",
        )
        dumped = req.model_dump(exclude_none=True)
        assert dumped["parent_subnet_id"] == "parent-1"
        assert dumped["lifecycle"] == "task_scoped"
        assert dumped["linked_task_id"] == "task-42"

    def test_back_compat_defaults_when_omitted(self):
        req = SubnetCreateRequest(name="Plain")
        dumped = req.model_dump(exclude_none=True)
        # ``exclude_none`` drops None fields but keeps ``lifecycle``
        # since it defaults to a non-None string. That's intentional —
        # the server-side default is also ``persistent`` so the value
        # is correct + makes the wire payload self-describing.
        assert dumped.get("parent_subnet_id") is None
        assert dumped["lifecycle"] == "persistent"
        assert dumped.get("linked_task_id") is None


def _make_client_with_stub(request_mock: AsyncMock) -> ACNClient:
    """ACNClient with ``_request`` replaced by an awaitable stub."""
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]
    return client


class TestSubnetClientMethods:
    @pytest.mark.asyncio
    async def test_list_subnets_with_parent_filter_passes_param(self):
        request_mock = AsyncMock(
            return_value={
                "subnets": [_server_subnet_payload()],
                "count": 1,
            }
        )
        client = _make_client_with_stub(request_mock)

        result = await client.list_subnets(parent_subnet_id="parent-1")

        assert len(result) == 1
        assert result[0].parent_subnet_id == "parent-1"
        request_mock.assert_awaited_once_with(
            "GET", "/api/v1/subnets", params={"parent": "parent-1"}
        )

    @pytest.mark.asyncio
    async def test_list_subnets_without_filter_passes_no_params(self):
        request_mock = AsyncMock(return_value={"subnets": [], "count": 0})
        client = _make_client_with_stub(request_mock)

        await client.list_subnets()

        # ``params=None`` is required so the call signature matches
        # legacy invocations (didn't pass ``params``) — older servers
        # may reject unexpected query strings.
        request_mock.assert_awaited_once_with(
            "GET", "/api/v1/subnets", params=None
        )

    @pytest.mark.asyncio
    async def test_list_children_uses_dedicated_endpoint(self):
        request_mock = AsyncMock(
            return_value={
                "count": 1,
                "subnets": [_server_subnet_payload(subnet_id="child-1")],
            }
        )
        client = _make_client_with_stub(request_mock)

        children = await client.list_children("parent-1")

        assert len(children) == 1
        assert children[0].id == "child-1"
        request_mock.assert_awaited_once_with(
            "GET", "/api/v1/subnets/parent-1/children"
        )

    @pytest.mark.asyncio
    async def test_promote_subnet_returns_updated_info(self):
        promoted_payload = _server_subnet_payload(
            lifecycle="persistent",
            linked_task_id=None,
        )
        request_mock = AsyncMock(return_value=promoted_payload)
        client = _make_client_with_stub(request_mock)

        info = await client.promote_subnet("squad-1")

        assert info.lifecycle == "persistent"
        assert info.linked_task_id is None
        request_mock.assert_awaited_once_with(
            "POST", "/api/v1/subnets/squad-1/promote"
        )


class TestCreateSubnetWiresNestingFields:
    """``create_subnet`` already accepts a ``SubnetCreateRequest`` —
    the new field surface is end-to-end testable by inspecting the
    JSON body the client emits."""

    @pytest.mark.asyncio
    async def test_create_subnet_body_carries_nesting_fields(self):
        request_mock = AsyncMock(return_value={"status": "created"})
        client = _make_client_with_stub(request_mock)

        await client.create_subnet(
            SubnetCreateRequest(
                name="Squad",
                parent_subnet_id="parent-1",
                lifecycle="task_scoped",
                linked_task_id="task-42",
            )
        )

        request_mock.assert_awaited_once()
        call_kwargs = request_mock.await_args.kwargs
        body = call_kwargs["json"]
        assert body["parent_subnet_id"] == "parent-1"
        assert body["lifecycle"] == "task_scoped"
        assert body["linked_task_id"] == "task-42"
