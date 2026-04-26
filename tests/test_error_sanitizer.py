"""M12 security tests: outbound exception → safe category mapping.

What this pins down
-------------------
``safe_external_error`` is the helper several layers (broadcast, WS,
DLQ) call instead of splicing ``str(e)`` into the response payload. The
contract is:

* every recognised exception class collapses to a stable, snake_case
  category that **never** contains the original ``str(exc)`` substring,
* unrecognised classes fall through to ``"delivery_failed"`` (so an
  attacker introducing a brand-new exception type cannot leak its
  message),
* the helper is robust to subclass inheritance order (specific subclass
  wins over generic ``RequestError``),
* the optional ``safe_error_payload`` returns a dict with the category
  *plus* any caller-supplied metadata (``agent_id``, ``status``, …)
  passed through verbatim.

These are pure unit tests — no httpx network traffic is generated; we
just construct exception instances directly.
"""

from __future__ import annotations

import httpx
import pytest

from acn.security.error_sanitizer import safe_error_payload, safe_external_error

# ─────────────────────────────────────────────
# httpx → category mapping
# ─────────────────────────────────────────────


class TestHttpxCategoryMapping:
    """Each well-known httpx exception class must collapse to a stable
    category. We construct the exceptions with realistic messages
    containing internal URLs, then assert the helper output:

    * matches the documented category, and
    * does NOT contain any substring of ``str(exc)``.

    The second assertion is the security invariant — even if a future
    change accidentally returned ``str(exc)`` instead of a constant, the
    test would fail.
    """

    def test_connect_error_maps_to_connection_refused(self):
        exc = httpx.ConnectError("Connection refused: http://10.0.1.5:8080/api")
        assert safe_external_error(exc) == "connection_refused"
        assert "10.0.1.5" not in safe_external_error(exc)

    def test_connect_timeout_maps_to_connection_timeout(self):
        exc = httpx.ConnectTimeout("connect timeout to internal-svc.local:9000")
        assert safe_external_error(exc) == "connection_timeout"
        assert "internal-svc" not in safe_external_error(exc)

    def test_read_timeout_maps_to_read_timeout(self):
        exc = httpx.ReadTimeout("read timeout reading from 192.168.1.1")
        assert safe_external_error(exc) == "read_timeout"

    def test_write_timeout_maps_to_write_timeout(self):
        exc = httpx.WriteTimeout("write timeout uploading to backend")
        assert safe_external_error(exc) == "write_timeout"

    def test_remote_protocol_error_maps_to_protocol_error(self):
        exc = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        assert safe_external_error(exc) == "protocol_error"

    def test_generic_request_error_falls_back_to_request_error(self):
        # ``RequestError`` is the base class — instances that aren't a
        # more-specific subclass should land here, not in connection_refused.
        exc = httpx.RequestError("some generic transport problem with internal-host:8080")
        assert safe_external_error(exc) == "request_error"
        assert "internal-host" not in safe_external_error(exc)


class TestHttpStatusError:
    """``HTTPStatusError`` carries the upstream response — the body is
    private (could echo the internal URL) but the status code is part
    of the contract between agents."""

    @staticmethod
    def _status_error(code: int) -> httpx.HTTPStatusError:
        # Build a minimal response/request pair so the exception is
        # well-formed. ``url`` deliberately includes an "internal" host.
        request = httpx.Request("POST", "http://internal-backend:8080/api/v1/foo")
        response = httpx.Response(
            status_code=code,
            request=request,
            content=b'{"detail":"backend stack trace at /app/server.py:142"}',
        )
        return httpx.HTTPStatusError("server returned 500", request=request, response=response)

    def test_5xx_status_in_category_only_no_body(self):
        exc = self._status_error(503)
        out = safe_external_error(exc)
        assert out == "upstream_status_503"
        # Critical: the category must NOT carry the response body or URL.
        assert "internal-backend" not in out
        assert "stack trace" not in out
        assert "server.py" not in out

    def test_4xx_status_pass_through(self):
        exc = self._status_error(404)
        assert safe_external_error(exc) == "upstream_status_404"

    def test_malformed_status_falls_back(self):
        # A bizarre exception where ``response.status_code`` raises:
        # we should NOT propagate the original exception, just fall back.
        class _Bad:
            @property
            def status_code(self):
                raise RuntimeError("totally broken")

        exc = httpx.HTTPStatusError(
            "irrelevant",
            request=httpx.Request("GET", "http://x"),
            response=httpx.Response(200, request=httpx.Request("GET", "http://x")),
        )
        # Force ``response.status_code`` to be unreadable post-construction.
        exc.response = _Bad()  # type: ignore[assignment]
        assert safe_external_error(exc) == "request_error"


# ─────────────────────────────────────────────
# Stdlib exception classes
# ─────────────────────────────────────────────


class TestStdlibClasses:
    def test_value_error_maps_to_invalid_request(self):
        # Caller-side bad input — typically already validated by Pydantic
        # but message_service / broadcast may still surface ``ValueError``
        # for "no targets matched" etc.
        assert safe_external_error(ValueError("bad subnet id")) == "invalid_request"

    def test_permission_error_maps_to_permission_denied(self):
        assert safe_external_error(PermissionError("not allowed")) == "permission_denied"

    def test_timeout_error_maps_to_request_timeout(self):
        assert safe_external_error(TimeoutError("op timed out")) == "request_timeout"


# ─────────────────────────────────────────────
# Fall-through: unknown class → "delivery_failed"
# ─────────────────────────────────────────────


class TestUnknownClassFallthrough:
    """The fall-through is the most important contract — even classes
    we've never heard of must collapse to the constant ``delivery_failed``
    so an attacker cannot rely on a custom exception class to smuggle a
    message into the response."""

    def test_generic_exception(self):
        assert safe_external_error(Exception("internal info: token=abc123")) == "delivery_failed"

    def test_runtime_error(self):
        assert safe_external_error(RuntimeError("traceback here")) == "delivery_failed"

    def test_custom_subclass(self):
        class _Custom(Exception):
            pass

        out = safe_external_error(_Custom("super-secret-internal-token-xxxxx"))
        assert out == "delivery_failed"
        assert "super-secret" not in out


# ─────────────────────────────────────────────
# Inheritance order: specific subclass beats RequestError
# ─────────────────────────────────────────────


class TestInheritanceOrder:
    """``ConnectError`` is a subclass of ``RequestError``. If the helper
    used a naive ``if isinstance(exc, RequestError)`` first it would
    bucket every connection failure as ``request_error`` — losing the
    actionable distinction between "host unreachable" and "host
    returned a malformed response". Pin the dispatch order explicitly.
    """

    def test_connect_error_does_not_fall_through_to_request_error(self):
        assert safe_external_error(httpx.ConnectError("x")) == "connection_refused"

    def test_read_timeout_does_not_fall_through_to_request_error(self):
        assert safe_external_error(httpx.ReadTimeout("x")) == "read_timeout"


# ─────────────────────────────────────────────
# safe_error_payload: dict shape + extras pass-through
# ─────────────────────────────────────────────


class TestSafeErrorPayload:
    def test_returns_error_only_when_no_extras(self):
        out = safe_error_payload(httpx.ConnectError("x"))
        assert out == {"error": "connection_refused"}

    def test_extras_passed_through_verbatim(self):
        out = safe_error_payload(
            httpx.ReadTimeout("x"),
            agent_id="acn_123",
            status="failed",
        )
        assert out == {
            "error": "read_timeout",
            "agent_id": "acn_123",
            "status": "failed",
        }

    def test_extras_cannot_override_error_field(self):
        # Defensive: even if a caller passes ``error="..."`` as an
        # extra, the sanitised category must win. This protects against
        # accidental copy-paste regressions in calling code.
        out = safe_error_payload(httpx.ConnectError("x"), error="raw leak")
        assert out["error"] == "connection_refused"


# ─────────────────────────────────────────────
# Belt-and-braces: the helper output never contains the
# original exception message, ever.
# ─────────────────────────────────────────────


class TestNeverLeaksRawMessage:
    """Property-style: across the entire sample of recognised exception
    classes, the helper's output must not contain any substring of the
    raw message we passed in. This is the single contract a reviewer
    wants to be sure of."""

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("Internal URL: http://10.1.2.3:8080/secret"),
            httpx.ReadTimeout("Reading from sensitive-host.internal:9000"),
            httpx.RemoteProtocolError("upstream returned: SECRET=xyzzy"),
            ValueError("malformed subnet acn_secret_subnet"),
            RuntimeError("traceback at /app/server.py:42"),
        ],
    )
    def test_no_substring_of_raw_message_in_output(self, exc):
        raw = str(exc)
        out = safe_external_error(exc)
        # Pick a few suspicious tokens from the raw message and assert
        # none of them appear in the output.
        for token in ["10.1.2.3", "sensitive-host", "SECRET", "xyzzy", "secret_subnet", "/app/"]:
            if token in raw:
                assert token not in out, (
                    f"safe_external_error leaked '{token}' from {type(exc).__name__}"
                )
