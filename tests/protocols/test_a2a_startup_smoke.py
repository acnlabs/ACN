"""Startup smoke tests for the A2A integration.

These intentionally exercise import and route construction rather than
handler behavior. Railway failures from dependency drift happen before
the app can serve requests, so this file pins those startup-time edges.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from acn.protocols.a2a.server import create_a2a_app


def test_acn_api_imports_with_a2a_sdk_1_x():
    """Importing the main API must not depend on removed a2a-sdk 0.3 symbols."""
    module = importlib.import_module("acn.api")

    assert hasattr(module, "app")


def test_create_a2a_app_mounts_agent_card_and_jsonrpc_routes():
    app = create_a2a_app(
        registry=MagicMock(),
        router=MagicMock(),
        broadcast=MagicMock(),
        subnet_manager=MagicMock(),
        redis=MagicMock(),
        metrics=None,
    )

    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert "/.well-known/agent-card.json" in route_paths
    assert "/jsonrpc" in route_paths

    response = TestClient(app).get("/.well-known/agent-card.json")

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "ACN Infrastructure Agent"
    assert card["skills"]
