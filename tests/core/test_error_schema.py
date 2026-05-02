"""Tests for ``acn.core.errors`` (Phase 2 review v2 P1 #11).

Pins the ACN error code contract:

* **Catalog completeness** — every declared ``ErrorCode`` has a default
  human-readable message; missing entries would let a route raise a
  code that has no fallback prose.
* **4xx-only construction** — ``ACNHTTPError`` rejects 5xx status
  codes at construction time, preventing a route author from
  accidentally bypassing the 5xx sanitisation chain by emitting a
  raw internal error message in ``message=`` / ``details=``.
* **Central handler shape** — the response body is the canonical flat
  ``{error_code, message, details, request_id}``; no nested ``detail``
  field; ``X-Request-ID`` is an injected UUID that overrides any
  caller-supplied value; arbitrary ``exc.headers`` pass through; the
  handler does not emit error-level logs.
* **5xx deprecation double-emit** — during the 30-day window the
  sanitised 5xx body carries both ``error`` (legacy) and ``error_code``
  (new), with equal values, plus an empty ``details: {}``.

We *don't* test that every catalog code is raised by some route. The
catalog is a forward catalog (codes may be reserved for future
routes) — coupling that to the migration sprint would force every
new code addition into a route-touching PR and break the small-PR
cadence.

The test app pattern mirrors ``tests/test_error_sanitisation.py``: a
minimal FastAPI app that re-registers the production handlers under
test, so we don't pay for the real ACN startup (DB / Redis lifespans)
just to exercise an exception path.
"""

from __future__ import annotations

import logging
import re
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from acn.core.errors import (
    _DEFAULT_MESSAGES,
    ACNErrorResponse,
    ACNHTTPError,
    ErrorCode,
)


def _build_test_app() -> FastAPI:
    """Build a minimal FastAPI app wired with the production handlers.

    Mirrors ``tests/test_error_sanitisation.py::_build_test_app`` —
    re-importing the real handlers and registering them on a fresh
    app keeps the test fast (no DB / Redis lifespans) while still
    exercising the exact code paths.
    """

    from acn.api import (
        _acn_http_error_handler,
        _http_exception_handler,
        _unhandled_exception_handler,
    )

    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
    app.add_exception_handler(ACNHTTPError, _acn_http_error_handler)

    @app.get("/raise-acn-default")
    async def raise_acn_default() -> dict:
        raise ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, 404)

    @app.get("/raise-acn-with-details")
    async def raise_acn_with_details() -> dict:
        raise ACNHTTPError(
            ErrorCode.COMMUNICATION_REJECTED,
            403,
            details={"reason": "policy_closed", "reject_reason": "vacation"},
        )

    @app.get("/raise-acn-with-headers")
    async def raise_acn_with_headers() -> dict:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            headers={"X-Foo": "bar", "Retry-After": "30"},
        )

    @app.get("/raise-acn-with-spoofed-request-id")
    async def raise_acn_with_spoofed_request_id() -> dict:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            headers={"X-Request-ID": "spoofed-by-caller"},
        )

    @app.get("/raise-acn-custom-message")
    async def raise_acn_custom_message() -> dict:
        raise ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            message="Specific override prose for this site",
        )

    @app.get("/raise-http-500")
    async def raise_http_500() -> dict:
        raise HTTPException(status_code=500, detail="boom")

    @app.get("/raise-http-503")
    async def raise_http_503() -> dict:
        raise HTTPException(status_code=503, detail="boom")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_test_app(), raise_server_exceptions=False)


# ─────────────────────────────────────────────
# Catalog completeness — forward-catalog discipline
# ─────────────────────────────────────────────


class TestCatalogStructure:
    def test_every_code_has_default_message(self) -> None:
        """Every ``ErrorCode`` member must have a ``_DEFAULT_MESSAGES``
        entry; otherwise a route raising that code with no explicit
        ``message=`` would crash with KeyError. The contract is
        enforced as set equality so adding a code without a default
        prose fails the test loudly."""
        assert set(_DEFAULT_MESSAGES.keys()) == set(ErrorCode), (
            "Mismatch between ErrorCode members and _DEFAULT_MESSAGES — "
            "every code needs a default human-readable prose."
        )

    def test_default_messages_are_non_empty_strings(self) -> None:
        """Defensive: an empty default would silently swap "agent not
        found" for "" in responses where ``message=`` was omitted."""
        for code, message in _DEFAULT_MESSAGES.items():
            assert isinstance(message, str), code
            assert message.strip(), f"Empty default for {code}"


# ─────────────────────────────────────────────
# ACNHTTPError construction-time guarantees
# ─────────────────────────────────────────────


class TestACNHTTPErrorConstruction:
    def test_rejects_5xx_status_code(self) -> None:
        """5xx responses must go through the sanitised 5xx handler so
        internal exception messages don't leak to anonymous callers.
        ``ACNHTTPError`` enforces 4xx-only at construction time as
        the front-line guard against that footgun."""
        with pytest.raises(ValueError, match="4xx status code"):
            ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, 500)
        with pytest.raises(ValueError, match="4xx status code"):
            ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, 503)

    def test_rejects_2xx_and_3xx_status_codes(self) -> None:
        """Symmetric: a 200 / 302 ACNHTTPError makes no semantic
        sense — fail loudly rather than silently emit a malformed
        error body for a "successful" status."""
        with pytest.raises(ValueError, match="4xx status code"):
            ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, 200)
        with pytest.raises(ValueError, match="4xx status code"):
            ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, 302)

    def test_accepts_4xx_range(self) -> None:
        """All of [400, 500) must succeed; 400 and 499 are the
        boundaries we care about."""
        for status in (400, 403, 404, 422, 429, 499):
            err = ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, status)
            assert err.status_code == status

    def test_default_message_when_omitted(self) -> None:
        """Omitting ``message=`` falls back to
        ``_DEFAULT_MESSAGES[code]`` so callers don't have to repeat
        boilerplate at every site."""
        err = ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, 404)
        assert err.message == _DEFAULT_MESSAGES[ErrorCode.AGENT_NOT_FOUND]

    def test_custom_message_overrides_default(self) -> None:
        err = ACNHTTPError(
            ErrorCode.AGENT_NOT_FOUND,
            404,
            message="explicit override",
        )
        assert err.message == "explicit override"

    def test_default_details_is_empty_dict(self) -> None:
        """``None`` details normalises to ``{}`` so the handler can
        always emit ``details: {}`` rather than ``null`` — keeps the
        SDK contract uniform."""
        err = ACNHTTPError(ErrorCode.AGENT_NOT_FOUND, 404)
        assert err.details == {}


# ─────────────────────────────────────────────
# Central handler — flat shape, header semantics, no error logs
# ─────────────────────────────────────────────


class TestCentralHandlerShape:
    def test_flat_top_level_keys(self, client: TestClient) -> None:
        """Body is flat ``{error_code, message, details, request_id}``;
        no ``detail`` wrapper. This is the core contract — SDK
        clients should be able to write a single parser that handles
        4xx + 5xx without a nested-vs-flat branch."""
        r = client.get("/raise-acn-default")
        assert r.status_code == 404
        body = r.json()
        assert set(body.keys()) == {"error_code", "message", "details", "request_id"}
        assert "detail" not in body, (
            "Body must NOT carry a ``detail`` field — that would "
            "mirror the legacy nested HTTPException shape and "
            "defeat the purpose of the schema unification."
        )

    def test_error_code_is_string_value(self, client: TestClient) -> None:
        r = client.get("/raise-acn-default")
        assert r.json()["error_code"] == "agent_not_found"

    def test_default_message_emitted(self, client: TestClient) -> None:
        r = client.get("/raise-acn-default")
        assert r.json()["message"] == _DEFAULT_MESSAGES[ErrorCode.AGENT_NOT_FOUND]

    def test_custom_message_emitted(self, client: TestClient) -> None:
        r = client.get("/raise-acn-custom-message")
        assert r.json()["message"] == "Specific override prose for this site"

    def test_details_pass_through(self, client: TestClient) -> None:
        """Code-specific structured context lands verbatim in
        ``body.details`` — this is the SDK's typed access path for
        per-code data (e.g. ``details.reason`` for
        ``communication_rejected``)."""
        r = client.get("/raise-acn-with-details")
        body = r.json()
        assert body["details"] == {
            "reason": "policy_closed",
            "reject_reason": "vacation",
        }

    def test_pydantic_shape_round_trip(self, client: TestClient) -> None:
        """The body parses cleanly through ``ACNErrorResponse``; the
        OpenAPI schema this model documents is therefore a faithful
        contract that SDK code-gen can rely on."""
        r = client.get("/raise-acn-with-details")
        parsed = ACNErrorResponse(**r.json())
        assert parsed.error_code == ErrorCode.COMMUNICATION_REJECTED.value
        assert parsed.details["reason"] == "policy_closed"

    def test_caller_headers_pass_through(self, client: TestClient) -> None:
        """Arbitrary headers on the exception (e.g. ``Retry-After``
        for 429) reach the response; this is how routes communicate
        e.g. backoff hints to clients."""
        r = client.get("/raise-acn-with-headers")
        assert r.headers.get("X-Foo") == "bar"
        assert r.headers.get("Retry-After") == "30"

    def test_x_request_id_overrides_caller(self, client: TestClient) -> None:
        """``X-Request-ID`` is **always** the handler-issued UUID,
        even if the caller supplied one via ``exc.headers``. Mirrors
        the 5xx handler's behaviour; the alternative would let a
        confused route author emit a non-UUID into the header,
        breaking support correlation."""
        r = client.get("/raise-acn-with-spoofed-request-id")
        rid = r.headers.get("X-Request-ID")
        assert rid != "spoofed-by-caller", (
            "Caller value MUST be ignored — the handler injects a "
            "fresh UUID every time."
        )
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", rid
        ), f"X-Request-ID must be a UUID, got {rid!r}"

    def test_request_id_matches_body_and_header(self, client: TestClient) -> None:
        """``X-Request-ID`` header and ``body.request_id`` must be the
        same string — that's the cross-channel correlation guarantee
        support / on-call rely on."""
        r = client.get("/raise-acn-default")
        rid_header = r.headers.get("X-Request-ID")
        rid_body = r.json()["request_id"]
        assert rid_header == rid_body
        # Sanity: it's a UUID, not e.g. an empty string.
        uuid.UUID(rid_body)

    def test_handler_does_not_emit_error_log(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """4xx is part of the API contract; emitting ``logger.error``
        per response would flood the log pipeline during normal
        operation (a misconfigured client retrying with a bad API
        key would burn out the alerting). Routes that *do* want to
        log a noteworthy 4xx (audit-worthy events) emit
        ``logger.info`` / ``logger.warning`` at the call site
        instead."""
        with caplog.at_level(logging.ERROR):
            r = client.get("/raise-acn-default")
        assert r.status_code == 404
        # Filter for ERROR-level records emitted *while handling*
        # the 4xx (the test app has no other paths that log; any
        # ERROR record here would be from the handler).
        error_records = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
        assert error_records == [], (
            f"ACNHTTPError handler emitted ERROR-level log(s): {error_records}"
        )


# ─────────────────────────────────────────────
# 5xx deprecation double-emit
# ─────────────────────────────────────────────


class TestFiveHundredsDoubleEmitForDeprecation:
    """During the 30-day deprecation window the sanitised 5xx body
    carries BOTH ``error`` (legacy field, removed at end of window)
    and ``error_code`` (new alignment with 4xx flat schema). Both
    fields hold the same ``"internal_server_error"`` value so SDK
    clients can read either during the transition.

    See ``docs/BACKLOG.md`` for the deprecation owner / due date.
    """

    def test_500_emits_both_error_and_error_code(self, client: TestClient) -> None:
        r = client.get("/raise-http-500")
        assert r.status_code == 500
        body = r.json()
        assert body["error"] == "internal_server_error"
        assert body["error_code"] == "internal_server_error"
        assert body["error"] == body["error_code"]

    def test_500_emits_empty_details(self, client: TestClient) -> None:
        """``details: {}`` is in scope to keep the 5xx + 4xx schema
        aligned — SDK clients can read ``body.details`` uniformly
        rather than guarding ``body.get("details", {})`` only on
        5xx."""
        r = client.get("/raise-http-500")
        body = r.json()
        assert body["details"] == {}

    def test_503_also_double_emits(self, client: TestClient) -> None:
        r = client.get("/raise-http-503")
        assert r.status_code == 503
        body = r.json()
        assert body["error"] == "internal_server_error"
        assert body["error_code"] == "internal_server_error"
        assert body["details"] == {}
