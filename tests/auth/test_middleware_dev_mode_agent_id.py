"""Regression tests: ``dev_mode`` must resolve ``acn_*`` API keys to the
owning agent's UUID (not return the raw token as ``sub``).

The shared dev-mode short-circuit in ``acn.auth.middleware`` used to set
``sub = credentials.credentials`` unconditionally. That made downstream
ACL checks — every one of which compares against the agent's UUID
(subnet membership, task ownership, ...) — trivially fail with the
literal ``acn_…`` string treated as the agent id. This was the same
class of bug fixed in ``acn/routes/tasks.py::require_task_write_auth``
during PR #41; this test file pins the matching fix for the three
middleware helpers (``verify_token``, ``require_internal_or_permission``,
``get_subject``) so future refactors can't silently regress them.

We don't go through the HTTP layer here: dev_mode is short-circuit
synchronous, the helpers expose Python-native signatures, and the only
external dependency (``get_agent_service``) is monkeypatched. This keeps
the tests fast and isolated from FastAPI routing/Auth0 wiring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from acn.auth import middleware as mw
from acn.core.entities import Agent, AgentStatus


def _make_agent(agent_id: str = "agent-uuid-abc") -> Agent:
    """Minimal agent for ``get_agent_by_api_key`` to return."""
    return Agent(
        agent_id=agent_id,
        owner="owner-1",
        name="Test Agent",
        endpoint="https://agent.example.com",
        description="x",
        tags=[],
        subnet_ids=[],
        status=AgentStatus.ONLINE,
        metadata={},
    )


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _stub_request() -> MagicMock:
    """The dev_mode path doesn't touch ``request`` — just give it
    something so the signature is satisfied."""
    return MagicMock(name="Request")


@pytest.fixture(autouse=True)
def _force_dev_mode(monkeypatch):
    """Tests in this file are *about* the dev_mode branch; pin it on
    regardless of env state."""
    fake_settings = MagicMock()
    fake_settings.dev_mode = True
    fake_settings.internal_api_token = "x" * 32  # placate prod path checks
    monkeypatch.setattr(mw, "_get_settings", lambda: fake_settings)
    return fake_settings


def _patch_agent_service(monkeypatch, *, returns: Agent | None, raises: Exception | None = None):
    """Install a fake ``get_agent_service`` in the same module the
    middleware lazy-imports from."""
    service = MagicMock()
    if raises is not None:
        service.get_agent_by_api_key = AsyncMock(side_effect=raises)
    else:
        service.get_agent_by_api_key = AsyncMock(return_value=returns)

    from acn.routes import dependencies as deps

    monkeypatch.setattr(deps, "get_agent_service", lambda: service)
    return service


# ---------------------------------------------------------------------------
# Helper: _resolve_agent_id_from_api_key
# ---------------------------------------------------------------------------


class TestResolveAgentIdFromApiKey:
    @pytest.mark.asyncio
    async def test_returns_none_for_none_token(self):
        assert await mw._resolve_agent_id_from_api_key(None) is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_token(self):
        assert await mw._resolve_agent_id_from_api_key("") is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_api_key(self):
        # JWT-shaped tokens, dev stubs, etc. must short-circuit *before*
        # we hit the agent service — agent keys are uniquely prefixed.
        assert await mw._resolve_agent_id_from_api_key("eyJhbGciOi…") is None

    @pytest.mark.asyncio
    async def test_returns_agent_id_for_valid_api_key(self, monkeypatch):
        service = _patch_agent_service(monkeypatch, returns=_make_agent("agent-xyz"))

        resolved = await mw._resolve_agent_id_from_api_key("acn_real-key")

        assert resolved == "agent-xyz"
        service.get_agent_by_api_key.assert_awaited_once_with("acn_real-key")

    @pytest.mark.asyncio
    async def test_returns_none_when_agent_not_found(self, monkeypatch):
        _patch_agent_service(monkeypatch, returns=None)
        assert await mw._resolve_agent_id_from_api_key("acn_unknown") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_service_uninitialised(self, monkeypatch):
        # ``get_agent_service`` raises ``RuntimeError`` at app boot before
        # ``initialize_services`` runs. The resolver must swallow it —
        # the dev-mode caller falls back to legacy behaviour.
        from acn.routes import dependencies as deps

        def _raise():
            raise RuntimeError("AgentService not initialized")

        monkeypatch.setattr(deps, "get_agent_service", _raise)
        assert await mw._resolve_agent_id_from_api_key("acn_anything") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_lookup_raises(self, monkeypatch):
        _patch_agent_service(monkeypatch, returns=None, raises=RuntimeError("db down"))
        assert await mw._resolve_agent_id_from_api_key("acn_anything") is None


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------


class TestVerifyTokenDevMode:
    @pytest.mark.asyncio
    async def test_acn_key_resolves_to_agent_id(self, monkeypatch):
        _patch_agent_service(monkeypatch, returns=_make_agent("agent-1"))

        payload = await mw.verify_token(_stub_request(), _bearer("acn_valid"))

        assert payload["sub"] == "agent-1"
        assert payload.get("type") == "agent"
        assert set(payload["permissions"]) >= {"acn:read", "acn:write", "acn:admin"}

    @pytest.mark.asyncio
    async def test_acn_key_unknown_agent_falls_back_to_raw_token(self, monkeypatch):
        # Preserves the pre-fix legacy behaviour for orphaned tokens —
        # we don't want to start 401'ing dev_mode callers who used to
        # work. The downstream ACL still fails, but explicitly with a
        # missing-member error, not silently against a UUID-shaped sub.
        _patch_agent_service(monkeypatch, returns=None)

        payload = await mw.verify_token(_stub_request(), _bearer("acn_orphan"))

        assert payload["sub"] == "acn_orphan"

    @pytest.mark.asyncio
    async def test_non_api_key_falls_back_to_raw_token(self, monkeypatch):
        # Doesn't need agent service patched — but install one that
        # would fail the test if called, to pin the short-circuit.
        service = _patch_agent_service(monkeypatch, returns=_make_agent("should-not-be-returned"))

        payload = await mw.verify_token(_stub_request(), _bearer("dev-stub-token"))

        assert payload["sub"] == "dev-stub-token"
        service.get_agent_by_api_key.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_credentials_returns_dev_clients(self):
        payload = await mw.verify_token(_stub_request(), None)
        assert payload["sub"] == "dev@clients"

    @pytest.mark.asyncio
    async def test_service_unavailable_falls_back_to_raw_token(self, monkeypatch):
        from acn.routes import dependencies as deps

        def _raise():
            raise RuntimeError("AgentService not initialized")

        monkeypatch.setattr(deps, "get_agent_service", _raise)

        payload = await mw.verify_token(_stub_request(), _bearer("acn_pre_boot"))

        # Without an initialised service we can't resolve — legacy
        # fallback so callers don't 500 during early boot.
        assert payload["sub"] == "acn_pre_boot"


# ---------------------------------------------------------------------------
# require_internal_or_permission
# ---------------------------------------------------------------------------


class TestRequireInternalOrPermissionDevMode:
    @pytest.mark.asyncio
    async def test_acn_key_resolves_to_agent_id(self, monkeypatch):
        _patch_agent_service(monkeypatch, returns=_make_agent("agent-rip-1"))
        checker = mw.require_internal_or_permission("acn:write")

        payload = await checker(_stub_request(), _bearer("acn_xx"), None)

        assert payload["sub"] == "agent-rip-1"
        assert payload.get("type") == "agent"

    @pytest.mark.asyncio
    async def test_acn_key_unknown_agent_falls_back(self, monkeypatch):
        _patch_agent_service(monkeypatch, returns=None)
        checker = mw.require_internal_or_permission("acn:write")

        payload = await checker(_stub_request(), _bearer("acn_unknown"), None)

        assert payload["sub"] == "acn_unknown"

    @pytest.mark.asyncio
    async def test_no_credentials_returns_dev_clients(self):
        checker = mw.require_internal_or_permission("acn:write")

        payload = await checker(_stub_request(), None, None)

        assert payload["sub"] == "dev@clients"

    @pytest.mark.asyncio
    async def test_non_api_key_falls_back_to_raw_token(self, monkeypatch):
        service = _patch_agent_service(monkeypatch, returns=_make_agent("nope"))
        checker = mw.require_internal_or_permission("acn:write")

        payload = await checker(_stub_request(), _bearer("dev-stub"), None)

        assert payload["sub"] == "dev-stub"
        service.get_agent_by_api_key.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_subject
# ---------------------------------------------------------------------------


class TestGetSubjectDevMode:
    @pytest.mark.asyncio
    async def test_acn_key_returns_agent_id(self, monkeypatch):
        _patch_agent_service(monkeypatch, returns=_make_agent("agent-gs-1"))

        sub = await mw.get_subject(_stub_request(), _bearer("acn_xx"))

        assert sub == "agent-gs-1"

    @pytest.mark.asyncio
    async def test_acn_key_unknown_agent_falls_back(self, monkeypatch):
        _patch_agent_service(monkeypatch, returns=None)

        sub = await mw.get_subject(_stub_request(), _bearer("acn_unknown"))

        assert sub == "acn_unknown"

    @pytest.mark.asyncio
    async def test_no_credentials_returns_dev_clients(self):
        sub = await mw.get_subject(_stub_request(), None)
        assert sub == "dev@clients"

    @pytest.mark.asyncio
    async def test_non_api_key_returns_raw_token(self, monkeypatch):
        service = _patch_agent_service(monkeypatch, returns=_make_agent("nope"))

        sub = await mw.get_subject(_stub_request(), _bearer("jwt-like-token"))

        assert sub == "jwt-like-token"
        service.get_agent_by_api_key.assert_not_awaited()
