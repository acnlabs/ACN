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

The close-frame reason is always the compact JSON payload
``{"c":"<error_code>","r":"<request_id>"}`` (sprint #11b RFC §4.1,
bake window removed).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from acn.core.errors import ErrorCode
from acn.routes.websocket import _safe_close


@pytest.fixture
def fake_ws() -> SimpleNamespace:
    """A minimal WebSocket-shaped object with a settable ``close`` mock."""
    ws = SimpleNamespace()
    ws.close = AsyncMock()
    return ws


class TestSafeCloseHappyPath:
    @pytest.mark.asyncio
    async def test_normal_close_passes_compact_reason(self, fake_ws):
        """``_safe_close`` always emits the compact JSON reason ``{c, r}``."""
        await _safe_close(
            fake_ws,
            code=4401,
            error_code=ErrorCode.AUTHENTICATION_REQUIRED,
            request_id="req-id-001",
        )
        expected_reason = '{"c":"authentication_required","r":"req-id-001"}'
        fake_ws.close.assert_awaited_once_with(code=4401, reason=expected_reason)


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
        await _safe_close(
            fake_ws,
            code=4401,
            error_code=ErrorCode.AUTHENTICATION_REQUIRED,
            request_id="req-id-002",
        )
        # And we did try exactly once — no retry storm.
        fake_ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_retry_on_failure(self, fake_ws):
        """A retry would be wrong: if close() failed because the socket is
        gone, hammering it can mask real bugs and amplify GC pressure."""
        fake_ws.close.side_effect = RuntimeError("gone")
        await _safe_close(
            fake_ws,
            code=4401,
            error_code=ErrorCode.AUTHENTICATION_REQUIRED,
            request_id="req-id-003",
        )
        assert fake_ws.close.await_count == 1

    @pytest.mark.asyncio
    async def test_passes_code_and_compact_reason_through(self, fake_ws):
        """Even when the underlying close raises, the *intent* (code,
        compact reason) is recorded by the call site for log/debug correlation."""
        fake_ws.close.side_effect = RuntimeError("gone")
        await _safe_close(
            fake_ws,
            code=4401,
            error_code=ErrorCode.AUTHENTICATION_REQUIRED,
            request_id="req-id-004",
        )
        expected_reason = '{"c":"authentication_required","r":"req-id-004"}'
        fake_ws.close.assert_awaited_once_with(
            code=4401, reason=expected_reason
        )
