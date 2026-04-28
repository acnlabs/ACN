"""Path-parameter ``max_length`` regression tests for P2-#3 (H6 follow-up).

H6 fenced off body-side abuse with per-string ``max_length`` and a 1 MiB
total body cap. Path / query parameters were left unbounded because
Starlette's URL parser caps headers at ~64 KB anyway — but a ~60 KB
``subnet_id`` still flows downstream into Redis keys, SQL ``WHERE``
clauses, and audit log structured fields.

After the fix, FastAPI's ``Path(..., max_length=N)`` returns ``422`` for
oversize ids before the request hits any service code.

We use ``TestClient`` to exercise the validation layer end-to-end —
which is the only place the ``Path`` constraint is enforced (the
underlying functions still accept any string at the Python level).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from acn.routes import registry as registry_routes
from acn.routes import subnets as subnets_routes
from acn.routes import tasks as tasks_routes
from acn.routes.dependencies import (
    MAX_AGENT_ID_LEN,
    MAX_PARTICIPATION_ID_LEN,
    MAX_SUBNET_ID_LEN,
    MAX_TASK_ID_LEN,
    get_agent_service,
    get_subnet_service,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """Build an isolated FastAPI app mounting only the routers we care about.

    All service / auth dependencies are overridden with AsyncMock stubs so
    a route that *would* otherwise blow up on uninitialised globals still
    reaches FastAPI's parameter validation step. We only care about the
    422 vs non-422 distinction emitted by ``Path(max_length=...)``; the
    actual handler return value is irrelevant (and the mock will produce
    one cleanly).
    """
    app = FastAPI()
    app.include_router(subnets_routes.router)
    app.include_router(registry_routes.router)
    app.include_router(tasks_routes.router)

    # Stub out service deps so handlers don't blow up on uninitialised
    # module-level globals.  The mock objects need ``.method()`` to work
    # for any service call the handler might make; AsyncMock auto-creates
    # awaitable child mocks on attribute access.
    app.dependency_overrides[get_agent_service] = lambda: AsyncMock()
    app.dependency_overrides[get_subnet_service] = lambda: AsyncMock()

    # ``tasks.py`` uses module-level state — patch the global directly
    # rather than the dependency, so any call site (factory, helper,
    # internal route) sees the same stub.
    tasks_routes.set_task_service(MagicMock())  # type: ignore[arg-type]

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# subnet_id (subnets routes)
# ---------------------------------------------------------------------------


def test_subnet_id_oversize_is_422(client: TestClient):
    """``GET /subnets/{subnet_id}`` must reject 60 KB ids before the
    service layer sees them — no Redis lookups, no SQL WHERE."""
    oversize = "x" * (MAX_SUBNET_ID_LEN + 1)
    resp = client.get(f"/api/v1/subnets/{oversize}")
    assert resp.status_code == 422, (
        f"oversize subnet_id ({len(oversize)} chars) must return 422; got "
        f"{resp.status_code}: {resp.text[:200]}"
    )
    body = resp.json()
    assert "detail" in body
    assert any(
        "max_length" in str(err).lower() or "string_too_long" in str(err).lower()
        for err in body.get("detail", [])
    ), f"422 must mention max_length: {body}"


def test_subnet_id_at_boundary_passes_validation(client: TestClient):
    """Exact ``MAX_SUBNET_ID_LEN`` must still be accepted."""
    boundary = "s" * MAX_SUBNET_ID_LEN
    resp = client.get(f"/api/v1/subnets/{boundary}")
    # Anything *but* 422 is fine — the route may 401/404/500 depending on
    # the dep wiring, but we only care that ``Path`` validation didn't fire.
    assert resp.status_code != 422, (
        f"boundary length {MAX_SUBNET_ID_LEN} must pass Path validation; "
        f"got 422: {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# agent_id (registry + subnets-internal routes)
# ---------------------------------------------------------------------------


def test_agent_id_oversize_on_registry_route_is_422(client: TestClient):
    oversize = "a" * (MAX_AGENT_ID_LEN + 1)
    resp = client.get(f"/api/v1/agents/{oversize}")
    assert resp.status_code == 422, (
        f"oversize agent_id must 422 on /agents/{{agent_id}}: "
        f"{resp.status_code} {resp.text[:200]}"
    )


def test_agent_id_oversize_on_subnet_internal_route_is_422(client: TestClient):
    """Internal subnet membership routes also enforce the cap."""
    subnet_id = "valid"
    oversize_agent = "a" * (MAX_AGENT_ID_LEN + 1)
    resp = client.post(f"/api/v1/subnets/{subnet_id}/members/{oversize_agent}")
    assert resp.status_code == 422


def test_agent_id_at_boundary_passes_validation(client: TestClient):
    boundary = "a" * MAX_AGENT_ID_LEN
    resp = client.get(f"/api/v1/agents/{boundary}")
    assert resp.status_code != 422


# ---------------------------------------------------------------------------
# task_id + participation_id (tasks routes)
# ---------------------------------------------------------------------------


def test_task_id_oversize_is_422(client: TestClient):
    oversize = "t" * (MAX_TASK_ID_LEN + 1)
    resp = client.get(f"/api/v1/tasks/{oversize}")
    assert resp.status_code == 422


def test_participation_id_oversize_is_422(client: TestClient):
    """Composite path with valid task_id but oversize participation_id
    must still 422 — both segments are independently validated."""
    valid_task = "t" * MAX_TASK_ID_LEN
    oversize_pid = "p" * (MAX_PARTICIPATION_ID_LEN + 1)
    resp = client.post(
        f"/api/v1/tasks/{valid_task}/participations/{oversize_pid}/cancel"
    )
    assert resp.status_code == 422


def test_task_id_at_boundary_passes_validation(client: TestClient):
    boundary = "t" * MAX_TASK_ID_LEN
    resp = client.get(f"/api/v1/tasks/{boundary}")
    assert resp.status_code != 422


# ---------------------------------------------------------------------------
# Numeric sanity: caps cover documented downstream constraints
# ---------------------------------------------------------------------------


def test_subnet_id_cap_matches_postgres_schema():
    """``MAX_SUBNET_ID_LEN`` must not exceed the Postgres ``String(100)``
    constraint on ``tasks.subnet_id`` — otherwise an oversize id slips
    past the route layer and 500s on PG insert.
    """
    assert MAX_SUBNET_ID_LEN == 100, (
        f"MAX_SUBNET_ID_LEN={MAX_SUBNET_ID_LEN} no longer matches the "
        "tasks.subnet_id Postgres VARCHAR(100); update either the cap or "
        "the schema, but keep them in lockstep."
    )


def test_agent_task_caps_have_buffer_over_typical_ids():
    """Typical ACN ids are ``acn:<UUID4>`` ≈ 41 chars; the caps should
    leave ample headroom for legacy/exotic id formats while still
    rejecting 60 KB attack payloads."""
    typical_uuid_id_len = 41  # ``acn:`` + 36-char UUID + 1 char headroom
    assert MAX_AGENT_ID_LEN >= typical_uuid_id_len * 2
    assert MAX_TASK_ID_LEN >= typical_uuid_id_len * 2
    assert MAX_PARTICIPATION_ID_LEN >= typical_uuid_id_len * 2
