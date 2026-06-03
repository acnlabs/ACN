"""Regression tests for A2A discovery vs direct delivery URL semantics."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from acn.models import AgentRegisterRequest
from acn.routes.registry import (
    AgentJoinRequest,
    _extract_jsonrpc_endpoint_from_agent_card,
    _resolve_registration_endpoint,
)


def test_register_request_accepts_explicit_a2a_endpoint_without_legacy_endpoint():
    request = AgentRegisterRequest(
        owner="user-1",
        name="Agent",
        a2a_endpoint="https://agent.example.com/a2a",
    )

    assert request.endpoint is None
    assert request.get_direct_a2a_endpoint() == "https://agent.example.com/a2a"


def test_join_request_accepts_agent_card_url_as_discovery_only_input():
    request = AgentJoinRequest(
        name="Discovery Agent",
        description="An agent that publishes an Agent Card",
        agent_card_url="https://agent.example.com/.well-known/agent-card.json",
    )

    assert request.get_direct_a2a_endpoint() is None
    assert request.agent_card_url == "https://agent.example.com/.well-known/agent-card.json"


def test_register_request_still_requires_delivery_or_discovery_url():
    with pytest.raises(ValidationError):
        AgentRegisterRequest(owner="user-1", name="Agent")


def test_register_request_relay_delivery_waives_url_in_push_mode():
    # ADR-0012 Mode B: delivery="relay" lets an open-mode agent register with
    # no public delivery URL (it is reached over its outbound WebSocket).
    request = AgentRegisterRequest(
        owner="user-1",
        name="Relay Agent",
        delivery="relay",
        communication_policy={"mode": "open"},
    )

    assert request.delivery == "relay"
    assert request.get_direct_a2a_endpoint() is None


def test_register_request_open_mode_without_relay_still_requires_url():
    with pytest.raises(ValidationError):
        AgentRegisterRequest(
            owner="user-1",
            name="Pushy Agent",
            communication_policy={"mode": "open"},
        )


def test_register_request_rejects_unknown_delivery_value():
    with pytest.raises(ValidationError):
        AgentRegisterRequest(
            owner="user-1",
            name="Agent",
            a2a_endpoint="https://agent.example.com/a2a",
            delivery="carrier-pigeon",
        )


def test_join_request_relay_delivery_waives_url_in_push_mode():
    request = AgentJoinRequest(
        name="Relay Agent",
        description="Reached over an outbound WebSocket",
        delivery="relay",
        communication_policy={"mode": "open"},
    )

    assert request.delivery == "relay"
    assert request.get_direct_a2a_endpoint() is None


def test_join_request_open_mode_without_relay_still_requires_url():
    with pytest.raises(ValidationError):
        AgentJoinRequest(
            name="Pushy Agent",
            description="Open mode but no endpoint and no relay opt-in",
            communication_policy={"mode": "open"},
        )


def test_register_request_relay_with_url_is_rejected():
    # delivery="relay" + a direct URL is contradictory: route() would dial
    # over HTTP and ignore the relay intent. Must be rejected at validation.
    with pytest.raises(ValidationError, match="mutually exclusive"):
        AgentRegisterRequest(
            owner="user-1",
            name="Confused Agent",
            delivery="relay",
            a2a_endpoint="https://agent.example.com/a2a",
            communication_policy={"mode": "open"},
        )


def test_join_request_relay_with_url_is_rejected():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        AgentJoinRequest(
            name="Confused Agent",
            description="Relay opt-in but also supplies a direct URL",
            delivery="relay",
            a2a_endpoint="https://agent.example.com/a2a",
            communication_policy={"mode": "open"},
        )


def test_extract_jsonrpc_endpoint_prefers_supported_interfaces():
    card = {
        "name": "Agent",
        "url": "https://legacy.example.com/a2a",
        "supportedInterfaces": [
            {
                "protocolBinding": "JSONRPC",
                "protocolVersion": "0.3.0",
                "url": "https://agent.example.com/a2a/jsonrpc",
            }
        ],
    }

    assert (
        _extract_jsonrpc_endpoint_from_agent_card(card)
        == "https://agent.example.com/a2a/jsonrpc"
    )


def test_extract_jsonrpc_endpoint_falls_back_to_legacy_card_url():
    assert (
        _extract_jsonrpc_endpoint_from_agent_card({"url": "https://agent.example.com/a2a"})
        == "https://agent.example.com/a2a"
    )


@pytest.mark.asyncio
async def test_resolved_agent_card_endpoint_is_registration_validated():
    with pytest.raises(HTTPException) as exc_info:
        await _resolve_registration_endpoint(
            direct_endpoint=None,
            agent_card_url=None,
            agent_card={"url": "ftp://agent.example.com/a2a"},
        )

    assert exc_info.value.status_code == 400
