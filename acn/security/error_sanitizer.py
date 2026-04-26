"""Sanitise outbound exception messages (security audit M12).

Why this exists
---------------
Several layers of ACN catch ``Exception`` and forward ``str(e)`` to the
client — directly (HTTP response body) or indirectly (200 ``{"error":
"..."}`` payloads from broadcasts / DLQ / WebSocket handlers). When the
underlying exception is a raw ``httpx`` failure, ``str(e)`` typically
carries:

* the *internal* hostname or IP of the agent endpoint we tried to call
  (``ConnectError("[Errno 111] Connection refused — http://10.0.1.5:8080/...")``),
* the response body of an upstream service that itself echoed our
  internal URL ("Internal Server Error at /api/v1/...").

That breaks the H4 invariant that callers never see internal infra
details, but H4 only fixed the explicit ``HTTPException(500, str(e))``
shape — the 200-response and best-effort broadcast paths went uncovered
because they were never exceptions to begin with. ``safe_external_error``
is the missing primitive: it maps any caught exception to a short,
white-listed category string that is safe to put in a response body.

Design
------
* **White-list, never blacklist.**  We don't try to scrub URLs out of
  ``str(e)`` — too many phrasings, locale-dependent, fragile. Instead we
  recognise specific well-known exception classes and return a constant
  category. Anything we don't recognise collapses to ``"delivery_failed"``.
* **Stable shape.**  Returns a short snake_case identifier so callers can
  branch on it (``"connection_refused"`` is actionable; ``"connection
  refused: [Errno 111] in build_request line 348"`` is not).
* **Status codes pass through verbatim.**  Upstream HTTP errors carry
  their numeric status (which is part of the API contract between
  agents) but never the response body.
* **No truncation gymnastics.**  We never include ``str(exc)`` itself —
  the whole point is that arbitrary exception messages are unsafe.

Used by
-------
* ``acn.services.message_service`` — broadcast best-effort responses.
* ``acn.infrastructure.messaging.broadcast_service`` — fan-out result map.
* ``acn.infrastructure.messaging.websocket_manager`` — WS handler errors.

Add new sites by importing :func:`safe_external_error` instead of
splicing ``str(e)`` into the response payload.
"""

from __future__ import annotations

from typing import Any

# We deliberately import httpx lazily inside the function instead of at
# module top so this helper is usable from layers that don't otherwise
# depend on httpx (e.g. unit tests of small slices). Hot path cost is
# negligible — the import is cached after first call.


def safe_external_error(exc: BaseException) -> str:
    """Return a sanitised, white-listed error category for an exception.

    The returned string is safe to expose to remote callers — it never
    contains URLs, hostnames, file paths, line numbers, or any portion
    of ``str(exc)``.

    Recognised categories (mostly httpx-driven):

    * ``connection_refused``       — host reachable, port closed
    * ``connection_timeout``       — TCP / TLS handshake timed out
    * ``read_timeout``             — connected but no body within window
    * ``write_timeout``            — request body upload timed out
    * ``protocol_error``           — malformed HTTP from upstream
    * ``upstream_status_<code>``   — got a structured non-2xx response
    * ``request_error``            — other httpx ``RequestError`` subtypes
    * ``invalid_request``          — caller-side ``ValueError``
    * ``permission_denied``        — caller-side ``PermissionError``
    * ``delivery_failed``          — fall-through for anything else

    The fall-through is the important contract: even a brand-new library
    raising an exception class we've never seen will never leak its
    ``str()`` to the client.
    """
    try:
        import httpx  # noqa: PLC0415  - intentional lazy import (see module doc)
    except ImportError:  # pragma: no cover  - httpx is a hard dep, this is just defensive
        httpx = None  # type: ignore[assignment]

    if httpx is not None:
        # Order matters: more-specific subclasses first.
        if isinstance(exc, httpx.ConnectError):
            return "connection_refused"
        if isinstance(exc, httpx.ConnectTimeout):
            return "connection_timeout"
        if isinstance(exc, httpx.ReadTimeout):
            return "read_timeout"
        if isinstance(exc, httpx.WriteTimeout):
            return "write_timeout"
        if isinstance(exc, httpx.RemoteProtocolError):
            return "protocol_error"
        if isinstance(exc, httpx.HTTPStatusError):
            # Status codes are public API contract — agents may want to
            # branch on a 401 vs 404 vs 5xx. The response body is *not*
            # public, so we only expose the integer.
            try:
                code = int(exc.response.status_code)
            except Exception:
                return "request_error"
            return f"upstream_status_{code}"
        if isinstance(exc, httpx.RequestError):
            return "request_error"

    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, ValueError):
        return "invalid_request"
    if isinstance(exc, TimeoutError):
        # Stdlib TimeoutError (PEP 657) — distinct from httpx.ReadTimeout.
        return "request_timeout"

    return "delivery_failed"


def safe_error_payload(exc: BaseException, **extra: Any) -> dict[str, Any]:
    """Convenience: build a ``{"error": "<category>", ...}`` dict.

    Use this when the surrounding code already returns ``dict``-shaped
    results (broadcast fan-out, WS handler error frames). ``extra``
    fields are passed through verbatim — the helper only handles the
    error category, leaving caller metadata (``agent_id``, ``status``,
    ``route_id`` etc.) untouched.

    Defensive note: ``extra`` cannot override the ``error`` key. The whole
    purpose of this helper is to guarantee the error field carries a
    sanitised category — letting a copy-paste mistake or an attacker-
    controlled call site shadow it would defeat the contract.
    """
    payload: dict[str, Any] = dict(extra)
    payload["error"] = safe_external_error(exc)
    return payload
