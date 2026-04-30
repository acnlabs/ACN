"""Phase 1 integration review (P1-1) — owner-only / public agent-metadata
endpoints must carry a per-agent (or per-IP, for the public card)
``@limiter.limit`` decorator.

Why this test exists
--------------------
Each task in Phase 1 (L410 policy CRUD, L421 endpoint disclosure, L422
agent-card sanitization) added auth gates (``OwnerOrInternalDep``)
without adding rate limits. Auth alone doesn't stop a leaked owner
API key from looping these endpoints to:

  - drown the audit stream (``GET /endpoint`` writes
    ``agent_endpoint_disclosed`` per call),
  - thrash the cache + DB on policy mutation (``PATCH /policy`` writes
    Postgres + Redis + emits ``communication_policy_updated`` log),
  - enumerate the agent population at zero cost
    (``.well-known/agent-card.json`` is intentionally unauthenticated
    for A2A discovery).

The Phase 1 integration review (post-L418) added rate limits. This
file is the static contract preventing a future refactor from silently
removing them — same shape as ``test_tasks_rate_limit_h7.py`` (the H7
audit equivalent for /tasks write endpoints).

We pin both the existence of ``@limiter.limit`` and the ROUTE-KEYED
expected rate. The exact rate is informational so a tuning change
doesn't break the test, but the table sits next to the docs so drift
is visible during review.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from acn.api import app
from acn.routes.dependencies import limiter

# (path, method) → expected slowapi rate string (informational, not asserted).
#
# Excluded deliberately:
#   - ``GET /api/v1/agents/{id}`` (public metadata, already 120/minute)
#   - ``GET /api/v1/agents`` (search, already 60/minute)
#   - The 7 inbound-write endpoints already covered by L418 (see
#     ``test_wallet_rate_limit_l418.py`` for the dual-bucket contract).
#
# This file pins the FOUR Phase 1 management-plane endpoints that
# previously had no limit at all.
PHASE1_MANAGEMENT_ENDPOINTS: dict[tuple[str, str], str] = {
    ("/api/v1/agents/{agent_id}/.well-known/agent-card.json", "GET"): "60/minute",
    ("/api/v1/agents/{agent_id}/endpoint", "GET"): "60/minute",
    ("/api/v1/agents/{agent_id}/policy", "GET"): "60/minute",
    ("/api/v1/agents/{agent_id}/policy", "PATCH"): "30/minute",
}


def _slowapi_route_key(route: APIRoute) -> str:
    """Reproduce slowapi's ``module.qualname`` lookup key."""
    fn = route.endpoint
    seen: set[int] = set()
    while getattr(fn, "__wrapped__", None) is not None and id(fn) not in seen:
        seen.add(id(fn))
        fn = fn.__wrapped__
    return f"{fn.__module__}.{fn.__qualname__}"


def _find_route(path: str, method: str) -> APIRoute | None:
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == path and method in route.methods:
            return route
    return None


def _route_limit_count(route: APIRoute) -> int:
    """Number of ``@limiter.limit`` decorators registered on this
    route's handler. SlowAPI keys per-route limits in
    ``limiter._route_limits`` by ``module.qualname``; absence means
    ZERO limits (the regression we're guarding against)."""
    return len(limiter._route_limits.get(_slowapi_route_key(route), []))


@pytest.mark.parametrize(("path", "method"), list(PHASE1_MANAGEMENT_ENDPOINTS.keys()))
def test_management_endpoint_has_rate_limit(path: str, method: str) -> None:
    """Each Phase 1 management endpoint must carry at least one
    ``@limiter.limit`` decorator.

    Failure mode this test prevents: a refactor that drops the
    decorator (e.g. while moving the route into a different module
    or removing what looked like duplicate ``request: Request``
    parameters that slowapi actually requires) leaves the endpoint
    completely uncapped — auth still works, so functional tests
    still pass, but the abuse surface comes back silently.
    """
    route = _find_route(path, method)
    assert route is not None, f"route not found in app: {method} {path}"
    count = _route_limit_count(route)
    expected = PHASE1_MANAGEMENT_ENDPOINTS[(path, method)]
    assert count >= 1, (
        f"{method} {path} has no @limiter.limit decorator — Phase 1 "
        f"integration review (P1-1) requires at least the per-agent "
        f"cap ``@limiter.limit({expected!r})`` to bound leaked-API-key "
        f"replay loops on management-plane endpoints."
    )
