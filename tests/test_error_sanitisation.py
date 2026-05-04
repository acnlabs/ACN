"""H4 security tests: 5xx responses must not leak internal exception detail.

Pre-launch audit finding H4: dozens of route handlers do
``raise HTTPException(status_code=500, detail=str(e))``, which echoes the
raw exception message — sometimes a stack-trace-level string from the DB
driver or a third-party library — straight back to the caller.

Rather than fix every call-site, we installed global exception handlers in
``acn/api.py`` that:

* Log the real detail (so operators can debug).
* Replace the response body with a constant-shape generic error.
* Add an ``X-Request-ID`` header so callers can quote it.
* Apply only to ≥500 status codes; 4xx keep their messages verbatim
  because they are part of the public API contract.

These tests pin down that contract.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException


def _build_test_app() -> FastAPI:
    """Build a minimal FastAPI app wired with the same handlers as ``acn.api``.

    We don't import the real app to keep this test fast and focused; we
    re-register the same handlers and assert they do the right thing.
    Importing the production app would also trigger DB/Redis lifespans.
    """

    from acn.api import _http_exception_handler, _unhandled_exception_handler

    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    @app.get("/leak-500")
    async def leak_500() -> JSONResponse:
        raise HTTPException(status_code=500, detail="DB error: password=hunter2 host=10.0.0.5")

    @app.get("/leak-503")
    async def leak_503() -> JSONResponse:
        raise HTTPException(status_code=503, detail="Redis Connection refused on internal-redis.svc")

    @app.get("/legit-404")
    async def legit_404() -> JSONResponse:
        raise HTTPException(status_code=404, detail="Agent not found")

    @app.get("/legit-403")
    async def legit_403() -> JSONResponse:
        raise HTTPException(status_code=403, detail="Forbidden: insufficient role")

    @app.get("/uncaught")
    async def uncaught() -> JSONResponse:
        # Any unhandled exception (KeyError here) must not leak.
        raise KeyError("internal_secret_lookup")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_test_app(), raise_server_exceptions=False)


# ─────────────────────────────────────────────
# 5xx HTTPException must be sanitised
# ─────────────────────────────────────────────


class TestFiveHundredsAreSanitised:
    def test_500_does_not_leak_detail(self, client: TestClient) -> None:
        r = client.get("/leak-500")
        assert r.status_code == 500
        body = r.json()
        assert body["error_code"] == "internal_server_error"
        assert "error" not in body, "legacy 'error' field must be absent"
        assert "request_id" in body
        assert "password" not in r.text
        assert "hunter2" not in r.text
        assert "10.0.0.5" not in r.text
        assert "DB error" not in r.text

    def test_503_uses_constant_shape_response(self, client: TestClient) -> None:
        r = client.get("/leak-503")
        assert r.status_code == 503
        body = r.json()
        assert set(body.keys()) >= {"error_code", "message", "request_id"}
        assert body["error_code"] == "internal_server_error"
        assert "error" not in body, "legacy 'error' field must be absent"
        assert "internal-redis.svc" not in r.text

    def test_request_id_is_uuid_and_in_header(self, client: TestClient) -> None:
        r = client.get("/leak-500")
        rid = r.json()["request_id"]
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", rid
        ), f"request_id should be a UUID, got {rid!r}"
        assert r.headers.get("x-request-id") == rid


# ─────────────────────────────────────────────
# 4xx must remain verbatim — caller-actionable
# ─────────────────────────────────────────────


class TestFourHundredsArePreserved:
    def test_404_keeps_caller_facing_detail(self, client: TestClient) -> None:
        r = client.get("/legit-404")
        assert r.status_code == 404
        assert r.json() == {"detail": "Agent not found"}

    def test_403_keeps_detail(self, client: TestClient) -> None:
        r = client.get("/legit-403")
        assert r.status_code == 403
        assert r.json() == {"detail": "Forbidden: insufficient role"}


# ─────────────────────────────────────────────
# Bare uncaught exceptions become generic 500
# ─────────────────────────────────────────────


class TestUncaughtExceptions:
    def test_keyerror_becomes_generic_500(self, client: TestClient) -> None:
        r = client.get("/uncaught")
        assert r.status_code == 500
        body = r.json()
        assert body["error_code"] == "internal_server_error"
        assert "error" not in body, "legacy 'error' field must be absent"
        assert "internal_secret_lookup" not in r.text, (
            "Raw exception args must never appear in the response body"
        )
        assert "KeyError" not in r.text
