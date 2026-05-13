"""WebSocket API Routes"""

import json
from typing import Any
from uuid import uuid4

import structlog  # type: ignore[import-untyped]
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..config import get_settings
from ..core.errors import (
    _DEFAULT_MESSAGES,
    ACN_DEFAULT_RESPONSES,
    ACNHTTPError,
    ErrorCode,
)
from .dependencies import (  # type: ignore[import-untyped]
    AgentApiKeyDep,
    InternalTokenDep,
    WsManagerDep,
    get_agent_service,
    get_ws_manager,
)

# Phase 2 review v2 P1 #11 sprint #11b — RFC 6455 close-frame ``reason`` field
# carries at most 123 bytes of UTF-8 (the frame header reserves 2 bytes for
# the close code, leaving 125 for the application-data payload of a 125-byte
# control frame). The compact JSON helper below MUST stay below this
# threshold; we assert it at runtime so a future contributor adding a key to
# ``details`` cannot silently truncate-on-the-wire.
_CLOSE_REASON_MAX_BYTES = 123

# M4 — inbound frame size caps.
#
# Starlette's ``receive_text()`` buffers the entire frame payload before
# returning. Without an application-level cap an attacker can send a
# single multi-GB frame and exhaust process memory before we parse a
# single byte.
#
# Two distinct limits because the auth handshake payload is structurally
# tiny ({"type":"auth","token":"<key>"} < 200 bytes in practice) while
# application messages may carry legitimate large content.
#
#   _MAX_WS_AUTH_FRAME_BYTES  — covers first-message auth and any future
#       handshake sub-frames.  4 KB is several times the maximum API key
#       length (128 hex chars) plus JSON envelope overhead; anything
#       larger is unambiguously malicious.
#
#   _MAX_WS_MESSAGE_FRAME_BYTES — covers application messages in the
#       main message loop.  1 MiB matches the HTTP body cap (H6) so
#       every transport tier enforces the same policy.  Operators who
#       need larger payloads (e.g. base64-encoded files) should use
#       the REST upload API instead.
_MAX_WS_AUTH_FRAME_BYTES: int = 4_096  # 4 KB
_MAX_WS_MESSAGE_FRAME_BYTES: int = 1_048_576  # 1 MiB

# ``responses=`` only affects HTTP routes registered on this APIRouter
# (see ``get_active_connections`` and ``get_agent_websocket_status``
# below). The ``@router.websocket(...)`` endpoint at L65 is not an HTTP
# route — its error contract is governed by RFC 6455 close codes and is
# tracked separately under sprint #11b.
router = APIRouter(tags=["websocket"], responses=ACN_DEFAULT_RESPONSES)
logger = structlog.get_logger()


def _extract_bearer_token(websocket: WebSocket) -> str | None:
    """Pull a Bearer token out of the WebSocket handshake's Authorization header.

    Browser ``new WebSocket()`` cannot set arbitrary headers, but proxies,
    server-side clients (httpx_ws, websockets, aiohttp), and SDKs all
    can. Supporting it gives every non-browser caller a path that keeps
    the API key out of access logs and Referer headers.
    """
    auth = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
    if not auth:
        return None
    auth = auth.strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _build_compact_close_reason(
    *,
    error_code: ErrorCode,
    request_id: str,
) -> str:
    """Serialise the typed close-frame ``reason`` fallback payload.

    Schema (sprint #11b RFC §4.1, revised post-implementation):

    ::

        {"c":"<error_code>","r":"<request_id>"}

    * ``c`` — ``error_code`` value (ASCII snake_case, same vocabulary the
      HTTP routes emit). Single-letter key chosen for byte economy.
    * ``r`` — ``request_id`` (UUID v4 string). Operators correlate this
      with the ``websocket_*`` audit log lines on the server side.

    **No** ``d`` (details) — RFC §4 originally proposed including details
    inline, but ``api_key_agent_mismatch``'s ``{path_agent, key_agent}``
    payload (two UUIDs) overflows the 123-byte RFC 6455 close-reason
    budget by ~60 bytes. Rather than amputate or compress details
    asymmetrically per-code (which would re-introduce the cross-channel
    drift that union-schema codes already cause), the implementation
    moves ``details`` exclusively onto the application error-frame
    channel (see ``_send_error_and_close`` below). Close-reason becomes
    a fixed-shape fallback that lets close-only SDK clients (browsers
    that don't read pending frames before ``onclose``) still get a
    typed ``error_code`` and a correlation id — but they MUST consult
    the application frame to see ``details``.

    The 64-byte upper bound (``c`` ≤ 32 chars + ``r`` = 36 chars + 12
    bytes envelope) is well within the 123-byte budget; the assertion
    here is a defensive guard against future ErrorCode names exceeding
    the current 32-char ceiling. Production handlers swallow the
    exception (see ``_safe_close``) so a runaway payload still yields
    a clean no-reason close rather than crashing the connection
    handler.
    """
    payload: dict[str, Any] = {"c": error_code.value, "r": request_id}
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > _CLOSE_REASON_MAX_BYTES:
        raise RuntimeError(
            f"WebSocket close-reason payload exceeds {_CLOSE_REASON_MAX_BYTES} bytes "
            f"(got {len(encoded.encode('utf-8'))}); RFC 6455 truncates silently. "
            f"This usually means an ErrorCode name is too long ({len(error_code.value)} "
            f"chars for {error_code.value!r}). Either rename the ErrorCode or pin "
            f"the request_id to a non-UUID compact form. Payload was: {encoded!r}"
        )
    return encoded


async def _safe_close(
    websocket: WebSocket,
    *,
    code: int,
    error_code: ErrorCode,
    request_id: str,
) -> None:
    """``websocket.close()`` that never raises out of an auth-failure path.

    Pre-launch audit backlog #3: starlette's ``close()`` is documented as
    idempotent on already-disconnected sockets via an internal
    ``application_state`` check, but relying on a private state machine is
    fragile. If the peer hard-closes mid-handshake or our send buffer is
    already torn down, ``close()`` can raise ``RuntimeError`` /
    ``ConnectionClosed`` / ``WebSocketDisconnect``.

    Swallowing those failures is correct because we're already on a path
    that has decided to terminate the socket; surfacing the error to
    FastAPI just turns a clean reject into a noisy 500 in logs without
    changing the wire outcome.

    The close-frame reason is always the compact JSON payload
    ``{"c":"<error_code>","r":"<request_id>"}`` (sprint #11b RFC §4.1).
    Callers should use ``_send_error_and_close`` which additionally sends
    an application error-frame with the full ``ACNErrorResponse`` payload
    (including ``details``) before closing.
    """
    try:
        reason = _build_compact_close_reason(
            error_code=error_code,
            request_id=request_id,
        )
    except RuntimeError as exc:
        logger.error(
            "websocket_close_reason_payload_oversize",
            error_code=error_code.value,
            request_id=request_id,
            error=str(exc),
        )
        reason = ""
    try:
        await websocket.close(code=code, reason=reason)
    except Exception as exc:  # noqa: BLE001 — defensive: we are tearing down anyway
        logger.debug(
            "websocket_close_swallowed",
            code=code,
            reason=reason,
            error=str(exc),
            error_type=type(exc).__name__,
        )


async def _send_error_and_close(
    websocket: WebSocket,
    *,
    code: int,
    error_code: ErrorCode,
    request_id: str,
    details: dict[str, Any] | None = None,
    message: str | None = None,
) -> None:
    """Emit a typed error and close the WebSocket.

    Sprint #11b RFC §4.2: every handshake-phase failure goes through this
    combo helper, which:

    1. Sends an application error-frame with the ``ACNErrorResponse``-shaped
       payload (``type`` discriminator + four canonical fields). No size
       budget — the close-reason is capped, but application frames are not.

       ::

           {"type":"error","error_code":"<>","message":"<>",
            "details":{...},"request_id":"<>"}

    2. Closes the socket with the ``code`` from the RFC 6455-mapped
       dictionary (4400/4401/4403/4429/1011) and a compact close-reason
       ``{c, r}`` so close-only SDK clients (browsers that don't read
       pending frames before ``onclose``) still receive a typed
       ``error_code`` and correlation id, even if they miss the frame.

    The application-frame send is wrapped in ``try`` so a peer that
    hard-closed mid-handshake doesn't surface a noisy 500 in logs —
    same defensive contract as ``_safe_close``.

    The cross-channel ``details`` invariant (each ``ErrorCode`` emits a
    consistent ``details`` keys-set across HTTP and WS) is enforced by
    the AST walker in ``tests/test_error_code_details_consistency.py``,
    which walks ``_send_error_and_close`` calls alongside ``raise
    ACNHTTPError``.

    The ``message`` argument is OPTIONAL — when omitted the helper
    falls back to ``_DEFAULT_MESSAGES[error_code]``, the same default
    ``ACNHTTPError`` uses.
    """
    frame_message = message if message is not None else _DEFAULT_MESSAGES[error_code]
    frame_payload = json.dumps(
        {
            "type": "error",
            "error_code": error_code.value,
            "message": frame_message,
            "details": details or {},
            "request_id": request_id,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    try:
        await websocket.send_text(frame_payload)
    except Exception as exc:  # noqa: BLE001 — peer may have hard-closed mid-handshake
        logger.debug(
            "websocket_error_frame_send_failed",
            error_code=error_code.value,
            request_id=request_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    await _safe_close(
        websocket,
        code=code,
        error_code=error_code,
        request_id=request_id,
    )


@router.websocket("/ws/{agent_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    agent_id: str,
    token: str | None = Query(None, description="[Deprecated] Agent API key — use first-message auth instead"),
):
    """WebSocket endpoint for real-time communication.

    Authentication options (in order of preference):

    1. **Authorization header** (recommended for non-browser clients):
       handshake with ``Authorization: Bearer <API_KEY>``.  The header
       never appears in server access logs and is not sniffable from a
       browser tab's URL bar.
    2. **First-message auth** (recommended for browsers):
       after connecting, send JSON ``{"type": "auth", "token": "<API_KEY>"}``.
       Server responds with ``{"type": "auth_ok"}`` or closes with 4401.
       This is the only option in browsers (the WebSocket constructor
       can't set headers).
    3. **URL query param** (deprecated, off by default in production):
       ``?token=<API_KEY>``.  Disabled unless ``WEBSOCKET_ALLOW_QUERY_TOKEN=true``
       (auto-enabled in ``DEV_MODE=true``).  The key appears in server
       access logs, browser Referer headers, and shoulder-surfable URL
       bars — see security audit M14.
    """
    ws_manager = get_ws_manager()
    agent_service = get_agent_service()
    settings = get_settings()

    # Per-connection request_id — same UUID v4 format the HTTP middleware
    # uses for ``X-Request-ID``. Operators correlate close-frame failures
    # with server-side audit logs by this id; sprint #11b RFC §4.1 + Q5.
    request_id = str(uuid4())

    # --- Resolve token (priority: header > query > first-message) ---
    header_token = _extract_bearer_token(websocket)
    resolved_token: str | None = None
    via: str = "first_message"

    if header_token is not None:
        resolved_token = header_token
        via = "authorization_header"
    elif token is not None:
        # Query-string token is gated by the operator-controlled flag.
        # We accept the WS handshake first so we can send a clean close
        # frame with a structured reason rather than a raw 403 — the
        # client otherwise just sees ``ConnectionClosed`` with no detail.
        if not settings.websocket_allow_query_token:
            logger.warning(
                "websocket_query_token_rejected",
                agent_id=agent_id,
                request_id=request_id,
                reason=(
                    "WEBSOCKET_ALLOW_QUERY_TOKEN=false: query-string tokens "
                    "are disabled in production. Use Authorization: Bearer "
                    "or first-message auth."
                ),
            )
            await websocket.accept()
            # Sprint #11b site #1 — query token disabled. RFC D2b: 4401
            # (auth class), AUTHENTICATION_REQUIRED + reason
            # ``ws_query_token_disabled``. ``details.reason`` matches the
            # ``UNION_SCHEMA_CODES`` registration for AUTHENTICATION_REQUIRED.
            await _send_error_and_close(
                websocket,
                code=4401,
                error_code=ErrorCode.AUTHENTICATION_REQUIRED,
                request_id=request_id,
                details={"reason": "ws_query_token_disabled"},
            )
            return
        resolved_token = token
        via = "query_string"
        # Even when allowed, every query-token use leaves a deprecation
        # breadcrumb so operators can see the migration tail in dashboards.
        logger.warning(
            "websocket_token_in_url_deprecated",
            agent_id=agent_id,
            request_id=request_id,
            message="API key passed as URL param — migrate to header / first-message auth",
        )

    await websocket.accept()

    if resolved_token is None:
        # First-message auth: wait for {"type": "auth", "token": "..."}
        try:
            raw = await websocket.receive_text()
            # M4: reject oversized auth frames before parsing to prevent
            # memory exhaustion from a malicious multi-MB first message.
            if len(raw.encode("utf-8")) > _MAX_WS_AUTH_FRAME_BYTES:
                await _send_error_and_close(
                    websocket,
                    code=4400,
                    error_code=ErrorCode.AUTHENTICATION_REQUIRED,
                    request_id=request_id,
                    details={"reason": "ws_frame_too_large"},
                )
                return
            msg = json.loads(raw)
            if msg.get("type") == "auth" and msg.get("token"):
                resolved_token = msg["token"]
            else:
                # Sprint #11b site #2 — first-message JSON parsed but
                # shape wrong. RFC D2b: 4400 (bad-request class),
                # AUTHENTICATION_REQUIRED + reason
                # ``ws_invalid_auth_message``.
                await _send_error_and_close(
                    websocket,
                    code=4400,
                    error_code=ErrorCode.AUTHENTICATION_REQUIRED,
                    request_id=request_id,
                    details={"reason": "ws_invalid_auth_message"},
                )
                return
        except Exception:  # noqa: BLE001 — JSON parse / disconnect / timeout
            # Sprint #11b site #3 — first-message JSON parse failure
            # (or peer disconnect / receive timeout). RFC D2b: 4400
            # (bad-request class), AUTHENTICATION_REQUIRED + reason
            # ``ws_invalid_auth_message_format``. Distinguishing this
            # from site #2 in the wire payload lets clients distinguish
            # "I sent the wrong shape" from "my JSON failed to parse" —
            # both are caller-actionable but require different fixes.
            await _send_error_and_close(
                websocket,
                code=4400,
                error_code=ErrorCode.AUTHENTICATION_REQUIRED,
                request_id=request_id,
                details={"reason": "ws_invalid_auth_message_format"},
            )
            return

    # Validate API key. Sprint #11b RFC Q3 — split site #4 into two halves
    # so close-code dictionary and ErrorCode both reflect the actual
    # failure mode:
    #   * key did not resolve to any agent → 4401 + AUTHENTICATION_REQUIRED
    #     with reason=invalid_api_key (same vocabulary as analytics /
    #     dependencies sprints #9 and #10).
    #   * key resolved but to a different agent → 4403 + API_KEY_AGENT_MISMATCH
    #     with the cross-module strict ``{path_agent, key_agent}`` shape
    #     (matches HTTP route #11a precedent + 17 other emitters).
    #
    # Why split (rather than keep both halves at 4401)? Pre-migration the
    # WS path collapsed both halves into one 4401 with the same prose
    # reason. The HTTP analogue (`get_agent_websocket_status` and every
    # other path-key gated route under sprints #1-#11a) already
    # distinguishes 401 from 403 for these two failure modes — a SDK
    # client that builds one ``error_code -> domain class`` mapping
    # (the post-#11b SDK 0.6.0 contract) needs the WS channel to
    # discriminate too, otherwise the WS branch collapses two distinct
    # SDK exceptions into one ambiguous "AuthError". This is purely
    # SDK ergonomics; it does NOT close a security oracle. The HTTP
    # 401/403 distinction already lets a pure-HTTP attacker enumerate
    # "key bad" vs "key for wrong agent" via response status alone, so
    # the WS split is parity-with-HTTP, not parity-with-no-disclosure.
    # The true confidentiality lever is the policy decision (echoed
    # explicitly across HTTP and WS) to surface ``key_agent`` so the
    # caller can debug their mistake; if a future reviewer wants to
    # close that disclosure they should change BOTH transports
    # together.
    agent = await agent_service.get_agent_by_api_key(resolved_token)
    if agent is None:
        await _send_error_and_close(
            websocket,
            code=4401,
            error_code=ErrorCode.AUTHENTICATION_REQUIRED,
            request_id=request_id,
            details={"reason": "invalid_api_key"},
        )
        return
    if agent.agent_id != agent_id:
        await _send_error_and_close(
            websocket,
            code=4403,
            error_code=ErrorCode.API_KEY_AGENT_MISMATCH,
            request_id=request_id,
            details={
                "path_agent": agent_id,
                "key_agent": agent.agent_id,
            },
        )
        return

    # Notify client auth succeeded — only meaningful for the
    # first-message flow, where the client explicitly waits for the
    # ``auth_ok`` echo before sending application messages. Header /
    # query auth callers don't expect it.
    if via == "first_message":
        await websocket.send_text(json.dumps({"type": "auth_ok"}))

    logger.info("websocket_connected", agent_id=agent_id, via=via, request_id=request_id)

    # websocket.accept() was already called above for the auth handshake,
    # so tell the manager to skip its own accept() call.
    # The try block starts here so that if connect() itself raises, the
    # except clause can still call disconnect() with a defined connection_id
    # (empty string is a safe no-op in WebSocketManager.disconnect).
    connection_id = ""
    try:
        connection_id = await ws_manager.connect(
            websocket,
            user_id=agent_id,
            metadata={"principal_type": "agent"},
            already_accepted=True,
        )

        # Keep connection alive and handle messages
        while True:
            data = await websocket.receive_text()
            # M4: drop oversized frames immediately so a single connection
            # cannot exhaust process memory.  Close with 4400 (bad-request)
            # rather than 1009 (message-too-big) so the SDK error vocabulary
            # stays consistent with the auth-phase size rejection above.
            if len(data.encode("utf-8")) > _MAX_WS_MESSAGE_FRAME_BYTES:
                logger.warning(
                    "websocket_frame_too_large",
                    agent_id=agent_id,
                    frame_bytes=len(data.encode("utf-8")),
                    limit=_MAX_WS_MESSAGE_FRAME_BYTES,
                    request_id=request_id,
                )
                await _send_error_and_close(
                    websocket,
                    code=4400,
                    error_code=ErrorCode.AUTHENTICATION_REQUIRED,
                    request_id=request_id,
                    details={"reason": "ws_frame_too_large"},
                )
                return
            logger.debug("websocket_message_received", agent_id=agent_id, data=data)

            # Echo back for now (can extend with message routing)
            await websocket.send_text(f"Received: {data}")

    except WebSocketDisconnect:
        logger.info(
            "websocket_disconnected", agent_id=agent_id, request_id=request_id
        )
        await ws_manager.disconnect(connection_id)

    except Exception as e:
        # Sprint #11b RFC Q5 — emit ``request_id`` on the audit log line
        # so operators can correlate an opaque 1011 close (Starlette's
        # default response when the handler raises) with the server-side
        # stack trace. Mirrors the same pattern HTTP 5xx already follows
        # via the central ``_unhandled_exception_handler``.
        logger.error(
            "websocket_error",
            agent_id=agent_id,
            request_id=request_id,
            error=str(e),
        )
        await ws_manager.disconnect(connection_id)
        raise


@router.get("/api/v1/websocket/connections")
async def get_active_connections(_: InternalTokenDep, ws_manager: WsManagerDep = None):
    """Get active WebSocket connections summary (requires X-Internal-Token)"""
    stats = ws_manager.get_stats()
    return stats


@router.get("/api/v1/websocket/agent/{agent_id}/status")
async def get_agent_websocket_status(
    agent_id: str,
    agent_info: AgentApiKeyDep,
    ws_manager: WsManagerDep = None,
):
    """Check if agent has active WebSocket connection (requires Agent API Key)

    An agent may only query its own connection status.
    """
    if agent_info["agent_id"] != agent_id:
        raise ACNHTTPError(
            ErrorCode.API_KEY_AGENT_MISMATCH,
            status_code=403,
            details={
                "path_agent": agent_id,
                "key_agent": agent_info["agent_id"],
            },
        )
    is_connected = ws_manager.is_user_connected(agent_id)
    return {"agent_id": agent_id, "connected": is_connected}
