"""ADR-0004 Slice 2.3 — Python SDK subnet-admission surface tests.

Pin:

- ``SubnetCreateRequest`` round-trips the new ``join_policy`` field
  with default-omit semantics (back-compat for legacy callers).
- All 13 admission methods issue the right verb + path + params
  + body shape on the wire (canonical paths, no body for the
  no-arg verbs, optional ``note`` is only included when set).
- The optional-body discipline matches the server contract: body
  argument to ``_request`` is ``None`` when no fields are set, so
  httpx doesn't send a ``Content-Type: application/json`` header
  on bodyless ``DELETE``.
- ``subnet_invitation_send`` returns the raw server payload —
  the merge-vs-normal branch shape is the caller's responsibility,
  matching the ``rotate_api_key`` raw-dict precedent.
- ``subnet_join_request_list`` defaults ``kind="join_request"``
  to match the server's default + the ADR-0004 §"Application-side
  endpoints" rule that ``kind=invitation`` is only valid on the
  invitation list endpoint.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from acn_client.client import ACNClient
from acn_client.models import SubnetCreateRequest


def _make_client_with_stub(request_mock: AsyncMock) -> ACNClient:
    """ACNClient with ``_request`` replaced by an awaitable stub."""
    client = ACNClient(base_url="http://acn.test")
    client._request = request_mock  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------------------
# SubnetCreateRequest.join_policy round-trip
# ---------------------------------------------------------------------------


class TestSubnetCreateRequestJoinPolicy:
    def test_serialises_join_policy_when_set(self):
        req = SubnetCreateRequest(name="Gated", join_policy="approval")
        dumped = req.model_dump(exclude_none=True)
        assert dumped["join_policy"] == "approval"

    def test_join_policy_omitted_when_none(self):
        req = SubnetCreateRequest(name="Open")
        dumped = req.model_dump(exclude_none=True)
        # ``exclude_none=True`` drops the field. Server defaults to
        # ``"open"`` server-side, so the wire payload stays minimal
        # and back-compat with legacy callers who never set the
        # field.
        assert "join_policy" not in dumped

    @pytest.mark.asyncio
    async def test_create_subnet_wires_join_policy(self):
        request_mock = AsyncMock(return_value={"status": "created"})
        client = _make_client_with_stub(request_mock)

        await client.create_subnet(
            SubnetCreateRequest(name="Gated", join_policy="approval")
        )

        body = request_mock.await_args.kwargs["json"]
        assert body["join_policy"] == "approval"


# ---------------------------------------------------------------------------
# Allowlist (3 verbs)
# ---------------------------------------------------------------------------


class TestAllowlistVerbs:
    @pytest.mark.asyncio
    async def test_subnet_allowlist_add_posts_canonical_path(self):
        request_mock = AsyncMock(
            return_value={
                "agent_id": "alice",
                "added_by": "owner",
                "added_at": "2026-05-19T00:00:00Z",
            }
        )
        client = _make_client_with_stub(request_mock)

        result = await client.subnet_allowlist_add("squad-1", "alice")

        assert result["agent_id"] == "alice"
        request_mock.assert_awaited_once_with(
            "POST",
            "/api/v1/subnets/squad-1/allowlist",
            json={"agent_id": "alice"},
        )

    @pytest.mark.asyncio
    async def test_subnet_allowlist_remove_uses_delete_and_returns_none(self):
        request_mock = AsyncMock(return_value=None)
        client = _make_client_with_stub(request_mock)

        result = await client.subnet_allowlist_remove("squad-1", "alice")

        assert result is None
        request_mock.assert_awaited_once_with(
            "DELETE", "/api/v1/subnets/squad-1/allowlist/alice"
        )

    @pytest.mark.asyncio
    async def test_subnet_allowlist_list_passes_pagination_params(self):
        request_mock = AsyncMock(
            return_value={"slug": "squad-1", "entries": []}
        )
        client = _make_client_with_stub(request_mock)

        result = await client.subnet_allowlist_list(
            "squad-1", limit=50, offset=10
        )

        assert result == {"slug": "squad-1", "entries": []}
        request_mock.assert_awaited_once_with(
            "GET",
            "/api/v1/subnets/squad-1/allowlist",
            params={"limit": 50, "offset": 10},
        )


# ---------------------------------------------------------------------------
# Join requests (4 verbs)
# ---------------------------------------------------------------------------


class TestJoinRequestVerbs:
    @pytest.mark.asyncio
    async def test_approve_omits_body_when_no_note(self):
        request_mock = AsyncMock(return_value={"status": "approved"})
        client = _make_client_with_stub(request_mock)

        await client.subnet_join_request_approve("squad-1", "req-42")

        request_mock.assert_awaited_once_with(
            "POST",
            "/api/v1/subnets/squad-1/join-requests/req-42/approve",
            json=None,
        )

    @pytest.mark.asyncio
    async def test_approve_includes_note_when_set(self):
        request_mock = AsyncMock(return_value={"status": "approved"})
        client = _make_client_with_stub(request_mock)

        await client.subnet_join_request_approve(
            "squad-1", "req-42", note="welcome aboard"
        )

        request_mock.assert_awaited_once_with(
            "POST",
            "/api/v1/subnets/squad-1/join-requests/req-42/approve",
            json={"note": "welcome aboard"},
        )

    @pytest.mark.asyncio
    async def test_reject_uses_reject_path(self):
        request_mock = AsyncMock(return_value={"status": "rejected"})
        client = _make_client_with_stub(request_mock)

        await client.subnet_join_request_reject(
            "squad-1", "req-42", note="not a fit"
        )

        request_mock.assert_awaited_once_with(
            "POST",
            "/api/v1/subnets/squad-1/join-requests/req-42/reject",
            json={"note": "not a fit"},
        )

    @pytest.mark.asyncio
    async def test_withdraw_uses_delete_on_join_requests_path(self):
        request_mock = AsyncMock(return_value={"status": "withdrawn"})
        client = _make_client_with_stub(request_mock)

        await client.subnet_join_request_withdraw("squad-1", "req-42")

        request_mock.assert_awaited_once_with(
            "DELETE",
            "/api/v1/subnets/squad-1/join-requests/req-42",
            json=None,
        )

    @pytest.mark.asyncio
    async def test_list_defaults_kind_to_join_request(self):
        request_mock = AsyncMock(
            return_value={"slug": "squad-1", "items": []}
        )
        client = _make_client_with_stub(request_mock)

        await client.subnet_join_request_list("squad-1")

        request_mock.assert_awaited_once_with(
            "GET",
            "/api/v1/subnets/squad-1/join-requests",
            params={"kind": "join_request", "limit": 100, "offset": 0},
        )

    @pytest.mark.asyncio
    async def test_list_passes_status_and_kind_filters(self):
        request_mock = AsyncMock(
            return_value={"slug": "squad-1", "items": []}
        )
        client = _make_client_with_stub(request_mock)

        await client.subnet_join_request_list(
            "squad-1",
            kind="allowlist_auto",
            status="approved",
            limit=25,
            offset=5,
        )

        request_mock.assert_awaited_once_with(
            "GET",
            "/api/v1/subnets/squad-1/join-requests",
            params={
                "kind": "allowlist_auto",
                "limit": 25,
                "offset": 5,
                "status": "approved",
            },
        )


# ---------------------------------------------------------------------------
# Invitations (5 + 1 verbs)
# ---------------------------------------------------------------------------


class TestInvitationVerbs:
    @pytest.mark.asyncio
    async def test_send_normal_path_returns_pending_payload(self):
        # 202 Accepted — normal "no overlap" send. Server returns
        # the new invitation row.
        normal_response: dict[str, Any] = {
            "invitation_id": "inv-42",
            "status": "pending",
        }
        request_mock = AsyncMock(return_value=normal_response)
        client = _make_client_with_stub(request_mock)

        result = await client.subnet_invitation_send("squad-1", "bob")

        assert result == normal_response
        request_mock.assert_awaited_once_with(
            "POST",
            "/api/v1/subnets/squad-1/invitations",
            json={"agent_id": "bob"},
        )

    @pytest.mark.asyncio
    async def test_send_merge_path_returns_auto_resolved_payload(self):
        # Target had a pending join_request — server returns 200
        # with the auto-resolution shape. SDK forwards verbatim;
        # branch dispatch is the caller's responsibility.
        merge_response: dict[str, Any] = {
            "auto_resolved": True,
            "resolved_kind": "join_request",
            "request_id": "req-7",
        }
        request_mock = AsyncMock(return_value=merge_response)
        client = _make_client_with_stub(request_mock)

        result = await client.subnet_invitation_send(
            "squad-1", "bob", note="merging"
        )

        assert result == merge_response
        request_mock.assert_awaited_once_with(
            "POST",
            "/api/v1/subnets/squad-1/invitations",
            json={"agent_id": "bob", "note": "merging"},
        )

    @pytest.mark.asyncio
    async def test_accept_uses_accept_path(self):
        request_mock = AsyncMock(return_value={"status": "approved"})
        client = _make_client_with_stub(request_mock)

        await client.subnet_invitation_accept("squad-1", "inv-42")

        request_mock.assert_awaited_once_with(
            "POST",
            "/api/v1/subnets/squad-1/invitations/inv-42/accept",
            json=None,
        )

    @pytest.mark.asyncio
    async def test_reject_uses_reject_path_with_note(self):
        request_mock = AsyncMock(return_value={"status": "rejected"})
        client = _make_client_with_stub(request_mock)

        await client.subnet_invitation_reject(
            "squad-1", "inv-42", note="too busy"
        )

        request_mock.assert_awaited_once_with(
            "POST",
            "/api/v1/subnets/squad-1/invitations/inv-42/reject",
            json={"note": "too busy"},
        )

    @pytest.mark.asyncio
    async def test_cancel_uses_delete_on_invitations_path(self):
        request_mock = AsyncMock(return_value={"status": "withdrawn"})
        client = _make_client_with_stub(request_mock)

        await client.subnet_invitation_cancel("squad-1", "inv-42")

        request_mock.assert_awaited_once_with(
            "DELETE",
            "/api/v1/subnets/squad-1/invitations/inv-42",
            json=None,
        )

    @pytest.mark.asyncio
    async def test_list_omits_status_when_none(self):
        request_mock = AsyncMock(
            return_value={"slug": "squad-1", "items": []}
        )
        client = _make_client_with_stub(request_mock)

        await client.subnet_invitation_list("squad-1")

        request_mock.assert_awaited_once_with(
            "GET",
            "/api/v1/subnets/squad-1/invitations",
            params={"limit": 100, "offset": 0},
        )

    @pytest.mark.asyncio
    async def test_list_includes_status_filter(self):
        request_mock = AsyncMock(
            return_value={"slug": "squad-1", "items": []}
        )
        client = _make_client_with_stub(request_mock)

        await client.subnet_invitation_list("squad-1", status="pending")

        request_mock.assert_awaited_once_with(
            "GET",
            "/api/v1/subnets/squad-1/invitations",
            params={"limit": 100, "offset": 0, "status": "pending"},
        )

    @pytest.mark.asyncio
    async def test_agent_subnet_invitations_uses_agent_path(self):
        request_mock = AsyncMock(
            return_value={"agent_id": "bob", "items": []}
        )
        client = _make_client_with_stub(request_mock)

        result = await client.agent_subnet_invitations("bob")

        assert result == {"agent_id": "bob", "items": []}
        request_mock.assert_awaited_once_with(
            "GET", "/api/v1/agents/bob/subnet-invitations"
        )
