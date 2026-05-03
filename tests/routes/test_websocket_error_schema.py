"""WebSocket *HTTP* routes — flat ACN error schema contract tests.

Phase 2 review v2 P1 #11 sprint row #11a — pin the single 4xx site in
the HTTP-mounted endpoints of ``acn/routes/websocket.py`` to the
canonical ``ACNHTTPError`` flat schema after migration from raw
``HTTPException``.

Scope (HTTP routes only — *not* the WebSocket protocol)
-------------------------------------------------------
``websocket.py`` registers three endpoints on its ``APIRouter``:

1. ``GET /api/v1/websocket/connections`` — gated by ``InternalTokenDep``;
   no file-local 4xx site (auth-fail comes from ``dependencies.py``,
   already migrated under sprint #10).
2. ``GET /api/v1/websocket/agent/{agent_id}/status`` — gated by
   ``AgentApiKeyDep`` and additionally enforces "agent may only query
   *its own* connection status". The `path != key` mismatch is the
   one new 4xx site this contract file pins.
3. ``WEBSOCKET /ws/{agent_id}`` — *not* an HTTP route. Its error
   contract uses RFC 6455 close codes (4401 today) and is governed by
   sprint #11b. Out of scope for this file.

Coverage matrix (1 raise site)
------------------------------
* ``API_KEY_AGENT_MISMATCH`` (×1) — caller authenticates with API
  key for agent A but requests connection status of agent B.
  ``details = {path_agent, key_agent}`` — the strict cross-sprint
  schema, identical to the same-named raise sites in sprint rows
  #5/#6/#7/#9 (registry, payments, follows, onchain, analytics).

Schema-bucket invariant
-----------------------
``API_KEY_AGENT_MISMATCH`` is a *strict* cross-module code: every
emitter must produce the same ``{path_agent, key_agent}`` dict.
``tests/test_error_code_details_consistency.py`` enforces this by
AST-walking every ``raise ACNHTTPError`` site in ``acn/routes/*.py``;
this contract file does NOT introduce any new keys for the code,
it only exercises the existing strict shape from a new entry point.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import (
    _api_key_cache,
    get_ws_manager,
    limiter,
    verify_agent_api_key,
)
from tests.routes.conftest import _assert_flat_shape


@pytest.fixture(autouse=True)
def _reset_state():
    """Override the conftest.py fixture so ws_manager / api-key cache
    are scrubbed between tests (the ``InternalTokenDep`` cache used
    elsewhere is irrelevant here, but symmetric reset keeps test
    isolation cheap to reason about)."""
    limiter.enabled = False
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_ws_manager() -> MagicMock:
    """Stub ``WebSocketManager`` — never reached on the error path,
    but must be wired so the dependency-resolution machinery succeeds
    BEFORE the path-vs-key check at L221 fires.

    We pin ``is_user_connected`` to a sentinel that would be returned
    on the success path so a regression that *skips* the mismatch
    check (and somehow returns 200) surfaces a wrong-shape body
    immediately — the test would fail with "missing canonical fields"
    rather than passing on a vacuous match.
    """
    mgr = MagicMock()
    mgr.is_user_connected = MagicMock(return_value=True)
    return mgr


def _wire(mgr: MagicMock, *, key_agent_id: str) -> None:
    """Inject a fake ``AgentApiKeyDep`` resolution that always
    returns ``key_agent_id`` regardless of header content, plus a
    stub WebSocket manager.

    Why override ``verify_agent_api_key`` instead of stubbing
    ``agent_service.get_agent_by_api_key``: ``verify_agent_api_key``
    *also* writes ``request.state.agent_id`` and
    ``request.state.rate_limit_key`` for the rate limiter. Bypassing
    it via a no-op override avoids needing to hand-construct that
    state (and avoids the ``_api_key_cache`` interaction). This is
    the same pattern used by ``test_analytics_error_schema.py``'s
    ``_wire`` for ``get_agent_service``.
    """
    app.dependency_overrides[verify_agent_api_key] = lambda: {
        "agent_id": key_agent_id,
        "name": f"agent-{key_agent_id}",
    }
    app.dependency_overrides[get_ws_manager] = lambda: mgr


# ---------------------------------------------------------------------------
# API_KEY_AGENT_MISMATCH (×1)
# ---------------------------------------------------------------------------


class TestApiKeyAgentMismatchFlatShape:
    """``GET /api/v1/websocket/agent/{path_agent_id}/status`` —
    Bearer key resolves to ``key_agent_id`` ≠ ``path_agent_id``.

    The 403 must carry the strict cross-module
    ``{path_agent, key_agent}`` shape — same as registry / payments /
    follows / onchain / analytics emit. Any drift here would fail the
    AST consistency test in
    ``tests/test_error_code_details_consistency.py``.
    """

    def test_path_key_mismatch_403_flat_shape(self, stub_ws_manager):
        _wire(stub_ws_manager, key_agent_id="agent-A")
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/websocket/agent/agent-B/status",
                headers={"Authorization": "Bearer fake-key"},
            )
        assert r.status_code == 403, r.text
        body = r.json()
        _assert_flat_shape(body)
        assert body["error_code"] == "api_key_agent_mismatch"
        assert body["details"] == {
            "path_agent": "agent-B",
            "key_agent": "agent-A",
        }
        assert r.headers.get("X-Request-ID") == body["request_id"]
        # Sanity: the manager's ``is_user_connected`` must NOT have
        # been called — the mismatch check is supposed to short-circuit
        # BEFORE any state is consulted. A regression that swaps the
        # check order would let a stranger probe whether arbitrary
        # agents are online.
        stub_ws_manager.is_user_connected.assert_not_called()
