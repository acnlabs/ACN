"""Sprint #11b — WebSocket protocol error contract tests.

Phase 2 review v2 P1 #11 sprint #11b RFC §6:
``acn/docs/features/acn-error-schema-websocket.md``.

These tests pin the wire shape: every handshake-phase error emits an
application error-frame (mirroring ``ACNErrorResponse`` 1:1) followed
by a close with the RFC-mapped code and a compact close-reason
``{c, r}`` fallback.

Coverage map (one test per *raise site* — the same precedent established
by sprints #9 and #7):

* Site #1 — query-token disabled when flag off
  → close 4401 + ``authentication_required`` + reason
  ``ws_query_token_disabled``.
* Site #2 — first-message JSON parsed but shape wrong
  → close 4400 + ``authentication_required`` + reason
  ``ws_invalid_auth_message``.
* Site #3 — first-message JSON parse failure / disconnect / timeout
  → close 4400 + ``authentication_required`` + reason
  ``ws_invalid_auth_message_format``.
* Site #4a — API key did not resolve
  → close 4401 + ``authentication_required`` + reason
  ``invalid_api_key``.
* Site #4b — API key resolved, agent_id mismatch
  → close 4403 + ``api_key_agent_mismatch`` + ``{path_agent, key_agent}``.

Plus one cross-cutting invariant test: every ``ErrorCode`` the helper
might emit produces a compact close-reason ≤ 123 bytes (RFC 6455
budget). This is the static-analysis backstop that catches a future
contributor adding a long ErrorCode name without realising it'd silently
truncate on the wire.

The tests deliberately consume **two** wire artefacts per failure:

1. ``ws.receive_json()`` — the application error-frame, which carries
   the full ``ACNErrorResponse``-shaped payload (``type`` discriminator
   + four canonical fields).
2. The subsequent ``WebSocketDisconnect`` — its ``.code`` and
   ``.reason`` (the compact ``{c, r}`` JSON) carry the close-channel
   fallback for close-only SDK clients that miss the frame.

Both channels are asserted independently because SDK 0.6.0 expects to
parse either shape and converge on the same typed ``error_code``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import acn.routes.websocket as ws_route
from acn.core.errors import _DEFAULT_MESSAGES, ErrorCode


def _make_app(
    *,
    valid_token: str = "good-token",
    matching_agent_id: str = "agent-1",
    allow_query_token: bool = False,
):
    """Build a FastAPI app mounting the WS router with stubbed deps."""
    app = FastAPI()
    app.include_router(ws_route.router)

    settings_stub = SimpleNamespace(
        websocket_allow_query_token=allow_query_token,
    )

    async def _get_agent_by_api_key(token: str):
        if token == valid_token:
            return SimpleNamespace(agent_id=matching_agent_id)
        return None

    agent_service_stub = SimpleNamespace(get_agent_by_api_key=_get_agent_by_api_key)

    ws_manager_stub = MagicMock()
    ws_manager_stub.connect = AsyncMock(return_value="conn-1")
    ws_manager_stub.disconnect = AsyncMock()

    return app, ws_manager_stub, agent_service_stub, settings_stub


def _patches(ws_manager, agent_service, settings):
    return [
        patch.object(ws_route, "get_ws_manager", return_value=ws_manager),
        patch.object(ws_route, "get_agent_service", return_value=agent_service),
        patch.object(ws_route, "get_settings", return_value=settings),
    ]


def _enter(ps):
    return [p.__enter__() for p in ps]


def _exit(ps):
    for p in reversed(ps):
        p.__exit__(None, None, None)


def _assert_error_frame_shape(frame: dict) -> None:
    """Pin the ACNErrorResponse-mirroring application error-frame shape.

    The frame is sent on the WS application channel BEFORE close, so
    SDK 0.6.0+ parsers can branch on ``error_code`` without consulting
    the close-reason. The shape mirrors ``ACNErrorResponse`` 1:1 with
    one extra field — ``type: "error"`` — that lets the SDK
    discriminate this frame from regular application messages on the
    same channel (echo loop, auth_ok ack, future server-pushed
    notifications, etc.).
    """
    assert isinstance(frame, dict), frame
    assert set(frame.keys()) == {
        "type",
        "error_code",
        "message",
        "details",
        "request_id",
    }, frame
    assert frame["type"] == "error"
    assert isinstance(frame["error_code"], str)
    assert isinstance(frame["message"], str)
    assert isinstance(frame["details"], dict)
    assert isinstance(frame["request_id"], str)
    # request_id should be UUID-shaped (36 chars: 8-4-4-4-12 hex). Same
    # invariant the HTTP middleware enforces; #11b assigns this at
    # connection accept time.
    assert len(frame["request_id"]) == 36
    assert frame["request_id"].count("-") == 4


def _assert_compact_close_reason(reason: str, error_code: str, request_id: str) -> None:
    """Pin the close-reason fallback shape ``{c, r}``.

    The close-reason is RFC 6455-bounded (≤123 UTF-8 bytes); details
    are NOT included here — they live exclusively on the application
    error-frame (see ``_send_error_and_close`` docstring). Close-only
    clients (browsers without a ``message`` listener, or libraries
    that drain on close) still get a typed ``error_code`` and a
    correlation id from this fallback.
    """
    payload = json.loads(reason)
    assert set(payload.keys()) == {"c", "r"}, payload
    assert payload["c"] == error_code
    assert payload["r"] == request_id
    assert "d" not in payload  # details NOT in close-reason
    assert len(reason.encode("utf-8")) <= 123, len(reason.encode("utf-8"))


# ─────────────────────────────────────────────
# Site #1 — query token disabled
# ─────────────────────────────────────────────


class TestSite1QueryTokenDisabled:
    """``allow_query_token=False`` + a ``?token=...`` rejection."""

    def test_query_token_disabled_emits_compact_contract(self):
        app, ws_mgr, svc, settings = _make_app(allow_query_token=False)
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect("/ws/agent-1?token=any") as ws:
                        frame = ws.receive_json()
                        _assert_error_frame_shape(frame)
                        assert frame["error_code"] == "authentication_required"
                        assert frame["details"] == {"reason": "ws_query_token_disabled"}
                        assert frame["message"] == _DEFAULT_MESSAGES[
                            ErrorCode.AUTHENTICATION_REQUIRED
                        ]
                        # Triggering the close: receive_text raises.
                        ws.receive_text()
                assert exc_info.value.code == 4401
                _assert_compact_close_reason(
                    exc_info.value.reason,
                    error_code="authentication_required",
                    request_id=frame["request_id"],
                )
            ws_mgr.connect.assert_not_called()
        finally:
            _exit(ps)


# ─────────────────────────────────────────────
# Site #2 — first-message JSON shape wrong
# ─────────────────────────────────────────────


class TestSite2InvalidAuthMessage:
    def test_first_message_wrong_type_emits_compact_contract(self):
        app, ws_mgr, svc, settings = _make_app()
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect("/ws/agent-1") as ws:
                        # Valid JSON but ``type`` is not "auth".
                        ws.send_json({"type": "ping"})
                        frame = ws.receive_json()
                        _assert_error_frame_shape(frame)
                        assert frame["error_code"] == "authentication_required"
                        assert frame["details"] == {"reason": "ws_invalid_auth_message"}
                        ws.receive_text()
                # 4400 = bad-request class (RFC §3-D2b). The previous
                # implementation collapsed this to 4401; the migration
                # disambiguates "your auth message was malformed" from
                # "your credentials were bad".
                assert exc_info.value.code == 4400
                _assert_compact_close_reason(
                    exc_info.value.reason,
                    error_code="authentication_required",
                    request_id=frame["request_id"],
                )
            ws_mgr.connect.assert_not_called()
        finally:
            _exit(ps)


# ─────────────────────────────────────────────
# Site #3 — JSON parse error / disconnect
# ─────────────────────────────────────────────


class TestSite3InvalidAuthMessageFormat:
    def test_first_message_invalid_json_emits_compact_contract(self):
        app, ws_mgr, svc, settings = _make_app()
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect("/ws/agent-1") as ws:
                        # Not valid JSON at all — triggers the broad
                        # ``except`` in the first-message branch.
                        ws.send_text("not-json {oops")
                        frame = ws.receive_json()
                        _assert_error_frame_shape(frame)
                        assert frame["error_code"] == "authentication_required"
                        assert frame["details"] == {
                            "reason": "ws_invalid_auth_message_format"
                        }
                        ws.receive_text()
                assert exc_info.value.code == 4400
                _assert_compact_close_reason(
                    exc_info.value.reason,
                    error_code="authentication_required",
                    request_id=frame["request_id"],
                )
            ws_mgr.connect.assert_not_called()
        finally:
            _exit(ps)


# ─────────────────────────────────────────────
# Site #4a — API key did not resolve
# ─────────────────────────────────────────────


class TestSite4aInvalidApiKey:
    def test_unknown_api_key_emits_compact_contract(self):
        app, ws_mgr, svc, settings = _make_app(valid_token="good-token")
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect(
                        "/ws/agent-1",
                        headers={"Authorization": "Bearer wrong-token"},
                    ) as ws:
                        frame = ws.receive_json()
                        _assert_error_frame_shape(frame)
                        assert frame["error_code"] == "authentication_required"
                        assert frame["details"] == {"reason": "invalid_api_key"}
                        ws.receive_text()
                # 4401 = auth class — credentials were checked and rejected.
                assert exc_info.value.code == 4401
                _assert_compact_close_reason(
                    exc_info.value.reason,
                    error_code="authentication_required",
                    request_id=frame["request_id"],
                )
            ws_mgr.connect.assert_not_called()
        finally:
            _exit(ps)


# ─────────────────────────────────────────────
# Site #4b — API key resolved, agent_id mismatch
# ─────────────────────────────────────────────


class TestSite4bApiKeyAgentMismatch:
    """Sprint #11b RFC Q3 — split this from site #4a so the close code
    matches the HTTP route #11a precedent (HTTP 403 + ``api_key_agent_mismatch``)."""

    def test_path_key_mismatch_emits_compact_contract(self):
        # Agent service resolves "good-token" to "agent-A"; we connect
        # to /ws/agent-OTHER. Mismatch.
        app, ws_mgr, svc, settings = _make_app(matching_agent_id="agent-A")
        ps = _patches(ws_mgr, svc, settings)
        _enter(ps)
        try:
            with TestClient(app) as client:
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with client.websocket_connect(
                        "/ws/agent-OTHER",
                        headers={"Authorization": "Bearer good-token"},
                    ) as ws:
                        frame = ws.receive_json()
                        _assert_error_frame_shape(frame)
                        assert frame["error_code"] == "api_key_agent_mismatch"
                        # Strict shape — same key set as HTTP route #11a
                        # and the 16 other ``api_key_agent_mismatch``
                        # emitters audited under sprints #1–#10.
                        assert frame["details"] == {
                            "path_agent": "agent-OTHER",
                            "key_agent": "agent-A",
                        }
                        ws.receive_text()
                # 4403 = forbidden class — distinct from #4a's 4401 to
                # match HTTP semantics and prevent transport-switch
                # side-channel oracles.
                assert exc_info.value.code == 4403
                _assert_compact_close_reason(
                    exc_info.value.reason,
                    error_code="api_key_agent_mismatch",
                    request_id=frame["request_id"],
                )
            ws_mgr.connect.assert_not_called()
        finally:
            _exit(ps)


# ─────────────────────────────────────────────
# Cross-cutting — close-reason byte budget
# ─────────────────────────────────────────────


class TestCloseReasonByteBudget:
    """Static guard: every ErrorCode the helper might emit produces a
    compact close-reason that fits inside the RFC 6455 123-byte budget.

    Catches a future contributor renaming an ErrorCode to something
    longer (we currently have a 32-char ceiling — at 36-char request_id
    + envelope, the cap is ~85 chars on the ErrorCode value to stay
    under 123 bytes — there's plenty of headroom but no static guard
    today, so this test fails LOUDLY in CI rather than silently
    truncating on the wire in production).
    """

    def test_every_error_code_value_fits(self):
        # UUID v4 is the longest request_id we ever emit (36 chars).
        request_id = "00000000-0000-0000-0000-000000000000"
        for ec in ErrorCode:
            reason = ws_route._build_compact_close_reason(
                error_code=ec,
                request_id=request_id,
            )
            assert len(reason.encode("utf-8")) <= 123, (
                f"ErrorCode {ec.name} value={ec.value!r} produces "
                f"close-reason {len(reason.encode('utf-8'))} bytes — "
                f"exceeds RFC 6455 123-byte budget"
            )
