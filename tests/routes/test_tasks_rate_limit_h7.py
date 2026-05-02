"""Security audit H7: every task **write** endpoint must have a per-identity
``@limiter.limit`` decorator and ``require_task_write_auth`` must set
``request.state.rate_limit_key`` on every accepted auth path.

Pre-fix gaps:
    - 12 POST endpoints under ``/api/v1/tasks`` had no rate limit at all.
      An authenticated agent could fire ``create_task`` /
      ``submit_task`` etc in a tight loop, blowing out PG/Redis/escrow.
    - The auth helper only set the rate-limit key for the ``acn_xxx``
      flow inside ``verify_agent_api_key``. ``require_task_write_auth``
      (which serves JWT/internal/dev callers) never wrote one, so even
      if a route had ``@limiter.limit`` it would silently fall back to
      ``ip:<peer>`` — easily spoofable behind a proxy that doesn't
      strip XFF.

Two layers of test:
    1. ``TestEndpointDecorators`` — static guard. Iterates the actual
       FastAPI router and asserts each known write route still carries
       a slowapi rate-limit decorator. Catches "decorator removed during
       refactor" regressions without spinning up the whole stack.
    2. ``TestRateLimitKeyAssignment`` — invokes the real
       ``require_task_write_auth`` checker with a stub request and
       verifies the key is set on every auth branch (dev / internal /
       agent / jwt). Catches "we added a new auth branch and forgot to
       bucket it" regressions.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.routing import APIRoute

from acn.api import app
from acn.routes.tasks import require_task_write_auth

# ─────────────────────────────────────────────
# Layer 1 — every write endpoint must have a rate limit
# ─────────────────────────────────────────────


# (path, method) → human-readable expected limit. We only assert the
# decorator is present; the exact rate is informational so an ops-led
# tweak doesn't break the test, but we still keep the table close to
# the docs to make drift visible in code review.
WRITE_ENDPOINTS: dict[tuple[str, str], str] = {
    ("/api/v1/tasks", "POST"): "20/minute",
    ("/api/v1/tasks/{task_id}/accept", "POST"): "60/minute",
    ("/api/v1/tasks/{task_id}/invite", "POST"): "30/minute",
    ("/api/v1/tasks/{task_id}/submit", "POST"): "30/minute",
    ("/api/v1/tasks/{task_id}/review", "POST"): "60/minute",
    ("/api/v1/tasks/{task_id}/cancel", "POST"): "30/minute",
    ("/api/v1/tasks/{task_id}/participations/{participation_id}/cancel", "POST"): "60/minute",
    ("/api/v1/tasks/{task_id}/participations/{participation_id}/approve", "POST"): "60/minute",
    ("/api/v1/tasks/{task_id}/participations/{participation_id}/reject", "POST"): "60/minute",
    ("/api/v1/tasks/agent/create", "POST"): "20/minute",
    ("/api/v1/tasks/agent/{task_id}/accept", "POST"): "60/minute",
    ("/api/v1/tasks/agent/{task_id}/submit", "POST"): "30/minute",
}


_SLOWAPI_WRAPPER_CODE_NAMES = {"async_wrapper", "sync_wrapper"}


def _has_rate_limit(endpoint) -> bool:
    """Detect whether slowapi has wrapped this route handler.

    Walks the ``__wrapped__`` chain looking for a frame whose
    ``__code__.co_name`` is the slowapi wrapper. ``functools.wraps``
    copies ``__module__`` / ``__name__`` / ``__qualname__`` from the
    wrapped function so those are useless markers, but it does **not**
    copy ``__code__`` — that's the stable fingerprint we rely on.
    """
    fn = endpoint
    seen: set[int] = set()
    while fn is not None and id(fn) not in seen:
        seen.add(id(fn))
        code = getattr(fn, "__code__", None)
        if code is not None and code.co_name in _SLOWAPI_WRAPPER_CODE_NAMES:
            return True
        fn = getattr(fn, "__wrapped__", None)
    return False


class TestEndpointDecorators:
    """Static contract: every write endpoint listed must be rate-limited."""

    @pytest.mark.parametrize(("path", "method"), list(WRITE_ENDPOINTS.keys()))
    def test_write_endpoint_has_rate_limit(self, path: str, method: str) -> None:
        route = self._find_route(path, method)
        assert route is not None, f"route not found in app: {method} {path}"
        assert _has_rate_limit(route.endpoint), (
            f"{method} {path} is missing @limiter.limit; H7 requires every "
            f"task write endpoint to be rate-limited per identity. Recommended: "
            f"{WRITE_ENDPOINTS[(path, method)]}"
        )

    def test_no_unrate_limited_post_under_tasks(self) -> None:
        """Negative guard: any POST/PUT/PATCH/DELETE we add later under
        ``/api/v1/tasks`` must also be rate-limited or explicitly listed
        in ``WRITE_ENDPOINTS``. Forces the next contributor to think
        about H7 instead of silently shipping an unlimited write path.
        """
        write_methods = {"POST", "PUT", "PATCH", "DELETE"}
        unrated: list[str] = []
        for r in app.routes:
            if not isinstance(r, APIRoute):
                continue
            if not r.path.startswith("/api/v1/tasks"):
                continue
            if not (r.methods & write_methods):
                continue
            # The internal-token endpoint is GET-only and lives under
            # /internal — we still block by requiring it to be in our
            # whitelist if anyone makes it a write later.
            for m in r.methods & write_methods:
                key = (r.path, m)
                if key not in WRITE_ENDPOINTS:
                    unrated.append(f"{m} {r.path} (not in WRITE_ENDPOINTS)")
                elif not _has_rate_limit(r.endpoint):
                    unrated.append(f"{m} {r.path} (no @limiter.limit)")
        assert not unrated, (
            "These task write routes are not rate-limited per H7:\n  - "
            + "\n  - ".join(unrated)
        )

    @staticmethod
    def _find_route(path: str, method: str) -> APIRoute | None:
        for r in app.routes:
            if isinstance(r, APIRoute) and r.path == path and method in r.methods:
                return r
        return None


# ─────────────────────────────────────────────
# Layer 2 — every auth branch sets request.state.rate_limit_key
# ─────────────────────────────────────────────


class TestRateLimitKeyAssignment:
    """``require_task_write_auth`` must seed ``rate_limit_key`` on every
    accepted auth path so per-identity bucketing actually works."""

    @pytest.fixture
    def stub_request(self):
        """Minimal Request stand-in — we only touch ``state`` and
        ``headers`` from inside the checker."""

        def _make(headers: dict[str, str] | None = None):
            return SimpleNamespace(
                state=SimpleNamespace(),
                headers=headers or {},
            )

        return _make

    @pytest.fixture
    def stub_agent_service(self):
        svc = MagicMock()
        svc.get_agent_by_api_key = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_dev_mode_sets_dev_prefixed_key(self, stub_request) -> None:
        from acn.routes import tasks as tasks_module

        creds = SimpleNamespace(scheme="Bearer", credentials="anything")
        with patch.object(tasks_module.settings, "dev_mode", True):
            req = stub_request()
            checker = require_task_write_auth()
            payload = await checker(
                request=req,
                credentials=creds,
                x_internal_token=None,
                agent_service=MagicMock(),
            )
        assert payload["type"] == "dev"
        assert req.state.rate_limit_key == "dev:anything"

    @pytest.mark.asyncio
    async def test_internal_token_buckets_by_creator(
        self, stub_request, stub_agent_service
    ) -> None:
        from acn.routes import tasks as tasks_module

        with patch.object(tasks_module.settings, "dev_mode", False), patch.object(
            tasks_module.settings, "internal_api_token", "secret-internal-token-min-32-chars"
        ):
            req = stub_request(headers={"x-creator-id": "user-42"})
            checker = require_task_write_auth()
            payload = await checker(
                request=req,
                credentials=None,
                x_internal_token="secret-internal-token-min-32-chars",
                agent_service=stub_agent_service,
            )

        assert payload["type"] == "internal"
        assert req.state.rate_limit_key == "internal:user-42", (
            "internal token call must bucket per X-Creator-Id so a single "
            "runaway user behind the backend cannot exhaust the shared "
            "backend@internal budget"
        )

    @pytest.mark.asyncio
    async def test_internal_token_without_creator_falls_back_to_shared(
        self, stub_request, stub_agent_service
    ) -> None:
        from acn.routes import tasks as tasks_module

        with patch.object(tasks_module.settings, "dev_mode", False), patch.object(
            tasks_module.settings, "internal_api_token", "secret-internal-token-min-32-chars"
        ):
            req = stub_request()
            checker = require_task_write_auth()
            await checker(
                request=req,
                credentials=None,
                x_internal_token="secret-internal-token-min-32-chars",
                agent_service=stub_agent_service,
            )

        assert req.state.rate_limit_key == "internal:backend"

    @pytest.mark.asyncio
    async def test_agent_api_key_buckets_per_agent(
        self, stub_request, stub_agent_service
    ) -> None:
        from acn.routes import tasks as tasks_module

        agent = MagicMock(agent_id="agent-007", name="Bond")
        stub_agent_service.get_agent_by_api_key.return_value = agent

        with patch.object(tasks_module.settings, "dev_mode", False), patch.object(
            tasks_module.settings, "internal_api_token", "secret-internal-token-min-32-chars"
        ):
            creds = SimpleNamespace(scheme="Bearer", credentials="acn_secret_007")
            req = stub_request()
            checker = require_task_write_auth()
            payload = await checker(
                request=req,
                credentials=creds,
                x_internal_token=None,
                agent_service=stub_agent_service,
            )

        assert payload["type"] == "agent"
        assert payload["sub"] == "agent-007"
        assert req.state.rate_limit_key == "agent:agent-007"

    @pytest.mark.asyncio
    async def test_jwt_buckets_per_sub(
        self, stub_request, stub_agent_service
    ) -> None:
        from acn.routes import tasks as tasks_module

        creds = SimpleNamespace(scheme="Bearer", credentials="eyJ.fake.jwt")
        jwt_payload = {"sub": "auth0|user-99", "permissions": ["acn:write", "acn:read"]}

        with patch.object(tasks_module.settings, "dev_mode", False), patch.object(
            tasks_module.settings, "internal_api_token", "secret-internal-token-min-32-chars"
        ), patch(
            "acn.routes.tasks.verify_token", new=AsyncMock(return_value=jwt_payload)
        ):
            req = stub_request()
            checker = require_task_write_auth()
            payload = await checker(
                request=req,
                credentials=creds,
                x_internal_token=None,
                agent_service=stub_agent_service,
            )

        assert payload["type"] == "jwt"
        assert req.state.rate_limit_key == "jwt:auth0|user-99"

    @pytest.mark.asyncio
    async def test_failed_auth_does_not_set_key(
        self, stub_request, stub_agent_service
    ) -> None:
        """Negative case: when JWT verification fails or permissions are
        missing, the checker raises ``ACNHTTPError`` (sprint #4-followup
        migrated this from ``HTTPException``) before assigning a key. The
        rate-limit decorator never even runs in this path — FastAPI's
        exception handler maps the ACN error to 403 directly, so the
        request is not counted toward any bucket. That is a *known* gap
        (BACKLOG: dependency-stage abuse), and this test's job is just
        to pin down "we don't accidentally bucket a rejected caller
        under the previous successful caller's key" —
        ``request.state.rate_limit_key`` must remain unset."""
        from acn.core.errors import ACNHTTPError, ErrorCode
        from acn.routes import tasks as tasks_module

        creds = SimpleNamespace(scheme="Bearer", credentials="eyJ.fake.jwt")
        jwt_payload = {"sub": "auth0|reader", "permissions": ["acn:read"]}

        with patch.object(tasks_module.settings, "dev_mode", False), patch.object(
            tasks_module.settings, "internal_api_token", "secret-internal-token-min-32-chars"
        ), patch(
            "acn.routes.tasks.verify_token", new=AsyncMock(return_value=jwt_payload)
        ):
            req = stub_request()
            checker = require_task_write_auth()
            with pytest.raises(ACNHTTPError) as exc:
                await checker(
                    request=req,
                    credentials=creds,
                    x_internal_token=None,
                    agent_service=stub_agent_service,
                )
            assert exc.value.status_code == 403
            assert exc.value.code is ErrorCode.MISSING_PERMISSION

        assert not hasattr(req.state, "rate_limit_key")
