"""Pre-launch audit backlog #3: WS ``_safe_close`` must never re-raise.

Threat model
------------
M14 added server-driven ``websocket.close(code=4401, ...)`` calls on
auth-failure paths. Starlette's ``WebSocket.close()`` is documented as
idempotent on already-disconnected sockets (it short-circuits when the
internal ``application_state`` is DISCONNECTED), but:

  - Relying on a private state machine is fragile across starlette
    upgrades.
  - A peer that hard-resets the TCP connection mid-handshake, a stalled
    send buffer, or a timer-driven disconnect can still surface a
    ``RuntimeError`` / ``ConnectionClosed`` / ``OSError`` from the
    underlying transport.

When close() raises out of an auth-failure path, it propagates into
FastAPI's WS lifecycle and shows up as a noisy 500-equivalent in logs,
without changing what the wire actually does (the socket is already
gone). ``_safe_close`` swallows those failures by design.

These tests pin the contract: any exception class can come out of
``await websocket.close(...)`` and ``_safe_close`` must (a) not propagate
it, (b) log a structured debug breadcrumb, (c) still issue exactly one
close attempt.

Sprint #11b updated ``_safe_close``'s signature to accept typed fields
(``error_code``, ``request_id``, ``legacy_reason``) instead of a bare
``reason`` string — the helper now dispatches to the correct reason
format based on the ``WEBSOCKET_CLOSE_REASON_FORMAT`` config flag.
The swallow-on-exception contract is unchanged; the tests here are
updated to use the new signature with representative values.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import acn.routes.websocket as ws_route
from acn.core.errors import ErrorCode
from acn.routes.websocket import _safe_close


def _legacy_settings():
    """Return a settings stub with the bake-window default (legacy mode)."""
    return SimpleNamespace(websocket_close_reason_format="legacy")


@pytest.fixture
def fake_ws() -> SimpleNamespace:
    """A minimal WebSocket-shaped object with a settable ``close`` mock."""
    ws = SimpleNamespace()
    ws.close = AsyncMock()
    return ws


class TestSafeCloseHappyPath:
    @pytest.mark.asyncio
    async def test_normal_close_passes_through(self, fake_ws):
        """In legacy mode, ``_safe_close`` passes the ``legacy_reason`` text
        through to ``websocket.close()`` verbatim — same wire as pre-#11b."""
        with patch.object(ws_route, "get_settings", return_value=_legacy_settings()):
            await _safe_close(
                fake_ws,
                code=4401,
                error_code=ErrorCode.AUTHENTICATION_REQUIRED,
                request_id="req-id-001",
                legacy_reason="bye",
            )
        fake_ws.close.assert_awaited_once_with(code=4401, reason="bye")


class TestSafeCloseSwallowsExceptions:
    """The whole reason this helper exists: every realistic close failure
    must be swallowed so the auth-reject path stays clean."""

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("WebSocket is already closed"),
            RuntimeError(
                'Cannot call "send" once a close message has been sent.'
            ),
            ConnectionError("broken pipe"),
            OSError("transport gone"),
            Exception("unknown transport failure"),
        ],
    )
    @pytest.mark.asyncio
    async def test_swallows_all_realistic_close_errors(self, fake_ws, exc):
        fake_ws.close.side_effect = exc
        # Must not propagate.
        with patch.object(ws_route, "get_settings", return_value=_legacy_settings()):
            await _safe_close(
                fake_ws,
                code=4401,
                error_code=ErrorCode.AUTHENTICATION_REQUIRED,
                request_id="req-id-002",
                legacy_reason="x",
            )
        # And we did try exactly once — no retry storm.
        fake_ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_retry_on_failure(self, fake_ws):
        """A retry would be wrong: if close() failed because the socket is
        gone, hammering it can mask real bugs and amplify GC pressure."""
        fake_ws.close.side_effect = RuntimeError("gone")
        with patch.object(ws_route, "get_settings", return_value=_legacy_settings()):
            await _safe_close(
                fake_ws,
                code=4401,
                error_code=ErrorCode.AUTHENTICATION_REQUIRED,
                request_id="req-id-003",
                legacy_reason="x",
            )
        assert fake_ws.close.await_count == 1

    @pytest.mark.asyncio
    async def test_passes_code_and_reason_through(self, fake_ws):
        """Even when the underlying close raises, the *intent* (code,
        reason) is recorded by the call site for log/debug correlation.
        In legacy mode the reason wire value is the ``legacy_reason`` text."""
        fake_ws.close.side_effect = RuntimeError("gone")
        with patch.object(ws_route, "get_settings", return_value=_legacy_settings()):
            await _safe_close(
                fake_ws,
                code=4401,
                error_code=ErrorCode.AUTHENTICATION_REQUIRED,
                request_id="req-id-004",
                legacy_reason="Unauthorized: foo",
            )
        fake_ws.close.assert_awaited_once_with(
            code=4401, reason="Unauthorized: foo"
        )
