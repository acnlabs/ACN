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
