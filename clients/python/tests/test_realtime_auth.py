"""Tests for ACNRealtime auth handshake (P0-3 of pre-launch hardening).

Background:
    Pre-fix, ``ACNRealtime`` connected to ACN with no authentication and a
    URL of ``/ws/{channel}`` — but ACN's endpoint is actually
    ``/ws/{agent_id}`` and (as of the M14 hardening) requires either an
    ``Authorization: Bearer`` header, a ``?token=...`` query (gated by
    feature flag), or a first-message ``{"type":"auth","token":"..."}``.

    These tests pin the new behaviour:
      * Constructor accepts ``agent_id`` / ``api_key`` / ``auth_mode``.
      * Header mode passes ``Authorization: Bearer`` to ``websockets.connect``.
      * Query mode appends ``?token=...`` to the URL.
      * First-message mode sends an auth frame and waits for ``auth_ok``.
      * Legacy ``connect(channel="...")`` still works but warns.
"""

from __future__ import annotations

import json
import warnings
from unittest.mock import AsyncMock, patch

import pytest

from acn_client.realtime import ACNRealtime, AuthMode, WSState

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_mock_ws() -> AsyncMock:
    """Build a minimal mock that satisfies the bits of ``websockets``
    we exercise: ``send``, ``recv``, ``close``, plus a no-op default for
    anything else."""
    ws = AsyncMock()
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.fixture(autouse=True)
def _stop_background_tasks(monkeypatch: pytest.MonkeyPatch):
    """Neutralise the heartbeat / receive loops so connect() doesn't
    spawn tasks that try to use our mock past the test boundary.

    We replace them with coroutines that immediately return. This keeps
    each test's ``connect()`` call synchronous in spirit and lets the
    test inspect ``self._ws`` / ``self.state`` deterministically.
    """

    async def _noop(*args, **kwargs):  # noqa: ANN001
        return None

    monkeypatch.setattr(ACNRealtime, "_heartbeat_loop", _noop)
    monkeypatch.setattr(ACNRealtime, "_receive_loop", _noop)


# --------------------------------------------------------------------------- #
# Constructor
# --------------------------------------------------------------------------- #


class TestConstructor:
    def test_defaults_no_auth(self) -> None:
        """No api_key + no agent_id is allowed — back-compat with the
        zero-arg style — but caller is on the hook for 4401s on connect."""
        rt = ACNRealtime("ws://localhost:9000")
        assert rt.agent_id is None
        assert rt.api_key is None
        assert rt.auth_mode == AuthMode.HEADER

    def test_accepts_string_auth_mode(self) -> None:
        """Many users won't import AuthMode; raw strings should work."""
        rt = ACNRealtime("ws://x", auth_mode="first_message")
        assert rt.auth_mode == AuthMode.FIRST_MESSAGE

    def test_rejects_unknown_auth_mode(self) -> None:
        with pytest.raises(ValueError):
            ACNRealtime("ws://x", auth_mode="hmac_signed")

    def test_http_url_rewritten_to_ws(self) -> None:
        """SDK contract: pass either ws:// or http:// — we rewrite."""
        rt = ACNRealtime("https://acn.example.com")
        assert rt.base_url == "wss://acn.example.com"


# --------------------------------------------------------------------------- #
# Header auth (default, recommended)
# --------------------------------------------------------------------------- #


class TestHeaderAuth:
    @pytest.mark.asyncio
    async def test_passes_bearer_header(self) -> None:
        """The whole point of HEADER mode: secret never hits the URL."""
        ws = _make_mock_ws()
        with patch(
            "acn_client.realtime.websockets.connect",
            AsyncMock(return_value=ws),
        ) as mock_connect:
            rt = ACNRealtime(
                "ws://localhost:9000",
                agent_id="agent_abc",
                api_key="ak_test_xyz",
            )
            await rt.connect()

            mock_connect.assert_awaited_once()
            args, kwargs = mock_connect.call_args
            # URL hits the right path…
            assert args[0] == "ws://localhost:9000/ws/agent_abc"
            # …with no token leaked into it.
            assert "token=" not in args[0]
            # And the Bearer header is delivered via the modern
            # ``additional_headers`` kwarg.
            assert kwargs.get("additional_headers") == [
                ("Authorization", "Bearer ak_test_xyz")
            ]
            assert rt.state == WSState.CONNECTED

    @pytest.mark.asyncio
    async def test_no_api_key_no_header(self) -> None:
        """If the caller didn't pass api_key, we don't fabricate one —
        and we don't emit an empty Authorization header."""
        ws = _make_mock_ws()
        with patch(
            "acn_client.realtime.websockets.connect",
            AsyncMock(return_value=ws),
        ) as mock_connect:
            rt = ACNRealtime("ws://localhost:9000", agent_id="agent_abc")
            await rt.connect()

            _, kwargs = mock_connect.call_args
            assert "additional_headers" not in kwargs


# --------------------------------------------------------------------------- #
# Query auth (deprecated)
# --------------------------------------------------------------------------- #


class TestQueryAuth:
    @pytest.mark.asyncio
    async def test_appends_token_query_param(self) -> None:
        ws = _make_mock_ws()
        with patch(
            "acn_client.realtime.websockets.connect",
            AsyncMock(return_value=ws),
        ) as mock_connect:
            rt = ACNRealtime(
                "ws://localhost:9000",
                agent_id="agent_abc",
                api_key="ak_test_xyz",
                auth_mode=AuthMode.QUERY,
            )
            await rt.connect()

            url = mock_connect.call_args.args[0]
            assert url == "ws://localhost:9000/ws/agent_abc?token=ak_test_xyz"
            # Importantly, no header — query mode is mutually exclusive.
            assert "additional_headers" not in mock_connect.call_args.kwargs

    @pytest.mark.asyncio
    async def test_url_encodes_special_chars(self) -> None:
        """API keys *should* be opaque ASCII, but we still defend against
        a key with reserved characters slipping in."""
        ws = _make_mock_ws()
        with patch(
            "acn_client.realtime.websockets.connect",
            AsyncMock(return_value=ws),
        ) as mock_connect:
            rt = ACNRealtime(
                "ws://localhost:9000",
                agent_id="agent_abc",
                api_key="key with/slash&amp",
                auth_mode=AuthMode.QUERY,
            )
            await rt.connect()

            url = mock_connect.call_args.args[0]
            # Spaces and reserved characters must be percent-encoded so
            # they don't terminate the query string or be eaten by proxies.
            assert "key%20with" in url
            assert "%2Fslash" in url
            assert "%26amp" in url


# --------------------------------------------------------------------------- #
# First-message auth (browser path)
# --------------------------------------------------------------------------- #


class TestFirstMessageAuth:
    @pytest.mark.asyncio
    async def test_sends_auth_frame_and_waits_for_ack(self) -> None:
        """The browser auth path: server expects
        ``{"type":"auth","token":"..."}`` then replies ``{"type":"auth_ok"}``
        before any application message flows."""
        ws = _make_mock_ws()
        ws.recv = AsyncMock(return_value=json.dumps({"type": "auth_ok"}))

        with patch(
            "acn_client.realtime.websockets.connect",
            AsyncMock(return_value=ws),
        ):
            rt = ACNRealtime(
                "ws://localhost:9000",
                agent_id="agent_abc",
                api_key="ak_test_xyz",
                auth_mode=AuthMode.FIRST_MESSAGE,
            )
            await rt.connect()

            # First (and only) send is the auth frame.
            ws.send.assert_awaited_once()
            sent_payload = json.loads(ws.send.call_args.args[0])
            assert sent_payload == {"type": "auth", "token": "ak_test_xyz"}
            assert rt.state == WSState.CONNECTED

    @pytest.mark.asyncio
    async def test_raises_on_auth_fail_response(self) -> None:
        """Server replied with anything that's not ``auth_ok`` — typically
        because it 4401'd us. We must surface this as a ConnectionError
        and *not* land in CONNECTED state."""
        ws = _make_mock_ws()
        ws.recv = AsyncMock(
            return_value=json.dumps({"type": "auth_fail", "reason": "bad key"})
        )

        with patch(
            "acn_client.realtime.websockets.connect",
            AsyncMock(return_value=ws),
        ):
            rt = ACNRealtime(
                "ws://localhost:9000",
                agent_id="agent_abc",
                api_key="ak_test_xyz",
                auth_mode=AuthMode.FIRST_MESSAGE,
            )
            with pytest.raises(ConnectionError, match="auth handshake failed"):
                await rt.connect()
            assert rt.state == WSState.DISCONNECTED
            ws.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_raises_on_non_json_ack(self) -> None:
        """A misbehaving server / proxy returning HTML or junk should
        produce a clean error, not silent success."""
        ws = _make_mock_ws()
        ws.recv = AsyncMock(return_value="<html>500 internal</html>")

        with patch(
            "acn_client.realtime.websockets.connect",
            AsyncMock(return_value=ws),
        ):
            rt = ACNRealtime(
                "ws://localhost:9000",
                agent_id="agent_abc",
                api_key="ak_test_xyz",
                auth_mode=AuthMode.FIRST_MESSAGE,
            )
            with pytest.raises(ConnectionError, match="non-JSON ack"):
                await rt.connect()
            assert rt.state == WSState.DISCONNECTED


# --------------------------------------------------------------------------- #
# Legacy channel arg back-compat
# --------------------------------------------------------------------------- #


class TestLegacyChannelArg:
    @pytest.mark.asyncio
    async def test_channel_arg_still_works_with_warning(self) -> None:
        """Pre-fix users called ``rt.connect("agents")`` thinking it was a
        channel name. Keep them working but warn loudly so the migration
        is visible in CI logs."""
        ws = _make_mock_ws()
        with patch(
            "acn_client.realtime.websockets.connect",
            AsyncMock(return_value=ws),
        ) as mock_connect:
            rt = ACNRealtime("ws://localhost:9000")
            with pytest.warns(DeprecationWarning, match="agent_id"):
                await rt.connect(channel="agent_xyz")

            assert mock_connect.call_args.args[0] == "ws://localhost:9000/ws/agent_xyz"

    @pytest.mark.asyncio
    async def test_constructor_agent_id_overrides_channel_arg(self) -> None:
        """When both are present, ctor wins. This is the migration path:
        the user adds ``agent_id`` to the ctor first, can leave the old
        ``connect("...")`` call site in place, sees no behaviour change.

        Critically, this case must NOT emit ``DeprecationWarning`` — the
        user has already migrated to the new style by passing ``agent_id``
        to the constructor; warning them again would be noisy and would
        push them to delete the channel arg even though removing it is
        purely cosmetic at this point. We promote any DeprecationWarning
        to an error so a regression is impossible to miss.
        """
        ws = _make_mock_ws()
        with patch(
            "acn_client.realtime.websockets.connect",
            AsyncMock(return_value=ws),
        ) as mock_connect:
            rt = ACNRealtime("ws://localhost:9000", agent_id="from_ctor")
            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                await rt.connect(channel="from_arg")

            assert mock_connect.call_args.args[0] == "ws://localhost:9000/ws/from_ctor"
