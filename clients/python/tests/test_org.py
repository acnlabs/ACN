"""Org Harness Work Port — Python SDK surface (parity with TS 0.15)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from acn_client.client import ACNClient, ACNError, _build_acn_error
from acn_client.models import (
    Org,
    OrgCreateRequest,
    OrgWorkCreateRequest,
    OrgWorkUpdateRequest,
    org_subnet_id,
)


def _make_client_with_stub(request_mock: AsyncMock) -> ACNClient:
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_create_org_and_get_org_paths():
    org_payload: dict[str, Any] = {
        "org_id": "org_abc",
        "display_name": "Acme",
        "subnet_id": "sub-1",
        "fencing": {"subnet_id": "sub-1"},
    }
    request_mock = AsyncMock(return_value=org_payload)
    client = _make_client_with_stub(request_mock)

    created = await client.create_org(
        OrgCreateRequest(
            display_name="Acme",
            subnet_id="sub-1",
            join_policy="open",
        )
    )
    assert created.org_id == "org_abc"
    request_mock.assert_awaited_with(
        "POST",
        "/api/v1/orgs",
        json={
            "display_name": "Acme",
            "subnet_id": "sub-1",
            "join_policy": "open",
        },
    )

    await client.get_org("org_abc")
    request_mock.assert_awaited_with("GET", "/api/v1/orgs/org_abc")


@pytest.mark.asyncio
async def test_create_update_list_work_and_tick():
    work_payload = {
        "work_id": "work_1",
        "org_id": "org_abc",
        "title": "Ship SDK",
        "status": "todo",
    }
    request_mock = AsyncMock(
        side_effect=[
            work_payload,
            {**work_payload, "status": "done"},
            {"work": [work_payload]},
            {"open_count": 1, "work_ids": ["work_1"]},
        ]
    )
    client = _make_client_with_stub(request_mock)

    await client.create_work("org_abc", OrgWorkCreateRequest(title="Ship SDK"))
    assert request_mock.await_args_list[0].args == (
        "POST",
        "/api/v1/orgs/org_abc/work",
    )
    assert request_mock.await_args_list[0].kwargs["json"] == {"title": "Ship SDK"}

    await client.update_work(
        "org_abc", "work_1", OrgWorkUpdateRequest(status="done")
    )
    assert request_mock.await_args_list[1].args == (
        "PATCH",
        "/api/v1/orgs/org_abc/work/work_1",
    )
    assert request_mock.await_args_list[1].kwargs["json"] == {"status": "done"}

    listed = await client.list_work("org_abc", open_only=True)
    assert len(listed.work) == 1
    assert request_mock.await_args_list[2].kwargs["params"] == {"open_only": True}

    tick = await client.tick_org_loop("org_abc")
    assert tick.open_count == 1
    assert request_mock.await_args_list[3].args == (
        "POST",
        "/api/v1/orgs/org_abc/loop/tick",
    )


def test_org_subnet_id_prefers_fencing():
    assert (
        org_subnet_id(
            Org(
                org_id="org_x",
                display_name="X",
                subnet_id="top",
                fencing={"subnet_id": "fence"},
            )
        )
        == "fence"
    )
    assert (
        org_subnet_id(Org(org_id="org_x", display_name="X", subnet_id="top"))
        == "top"
    )


def test_acn_error_reason_and_bound_org_id_hint():
    response = httpx.Response(
        409,
        content=(
            b'{"error_code":"conflict","message":"subnet already bound to '
            b'org_deadbeef01234567","details":{"reason":"subnet_bound",'
            b'"bound_org_id":"org_deadbeef01234567"}}'
        ),
        headers={"content-type": "application/json"},
    )
    err = _build_acn_error(response)
    assert err.status_code == 409
    assert err.reason == "subnet_bound"
    assert err.bound_org_id_hint == "org_deadbeef01234567"
    assert err.body is not None
    assert err.body["details"]["bound_org_id"] == "org_deadbeef01234567"

    prose = ACNError(
        409,
        "conflict prose mentions org_aabbccddeeff0011",
        body={"message": "x"},
    )
    assert prose.bound_org_id_hint == "org_aabbccddeeff0011"
