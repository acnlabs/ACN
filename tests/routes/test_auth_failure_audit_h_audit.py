"""Security audit H-audit: every authn/authz failure must hit the audit pipeline.

Before this fix the ``SECURITY_AUTH_FAILURE`` event type existed in the
``AuditEventType`` enum but was never emitted by any production code path
— attackers could brute-force credentials with zero forensic trail.

These tests cover the authentication entry points where 401/403 is
raised on a bad credential and verify ``record_auth_failure`` is invoked
with a meaningful ``reason`` tag. They also pin down the SSRF audit hook
on the proxy path (Phase B of H-audit) and bulk-delete auditing (Phase C).

We patch ``acn.routes.dependencies._audit_record_auth_failure`` (the
module-local alias), ``acn.auth.middleware.record_auth_failure`` (for
JWT/permission paths in the middleware module), and
``acn.routes.registry.fire_and_forget_event`` rather than monkeying with
the real ``AuditLogger`` singleton so the tests stay fast and don't
depend on Redis at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.routes.dependencies import get_agent_service, limiter


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    """slowapi would otherwise call Redis on every request."""
    was = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = was


@pytest.fixture
def agent_svc_no_match() -> AsyncMock:
    """An AgentService that never finds the supplied API key."""
    svc = AsyncMock()
    svc.get_agent_by_api_key = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def agent_svc_with_caller() -> AsyncMock:
    """An AgentService whose API-key lookup succeeds (used by SSRF tests)."""
    svc = AsyncMock()
    target_agent = SimpleNamespace(
        agent_id="target-agent",
        endpoint="http://target.internal.local/a2a",
    )
    caller_agent = SimpleNamespace(agent_id="caller-agent", name="Caller")
    svc.get_agent = AsyncMock(return_value=target_agent)
    svc.get_agent_by_api_key = AsyncMock(return_value=caller_agent)
    return svc


def _override_agent_service(svc: AsyncMock) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


# ─────────────────────────────────────────────
# Phase A: authn/authz failures
# ─────────────────────────────────────────────


class TestApiKeyFailures:
    """``verify_agent_api_key`` / ``verify_proxy_caller`` paths.

    Both lean on ``_resolve_agent_by_bearer``; the test exercises each
    distinct failure mode (bad header format vs. bad key value) so a future
    refactor that loses one branch is caught.
    """

    def test_invalid_api_key_records_audit(self, agent_svc_no_match: AsyncMock) -> None:
        _override_agent_service(agent_svc_no_match)
        try:
            with patch(
                "acn.routes.dependencies._audit_record_auth_failure"
            ) as record:
                with TestClient(app) as client:
                    r = client.post(
                        "/api/v1/agents/some-agent/heartbeat",
                        headers={"Authorization": "Bearer bogus"},
                    )
                assert r.status_code == 401
                assert record.called, (
                    "_resolve_agent_by_bearer must emit SECURITY_AUTH_FAILURE "
                    "when the API key is unknown"
                )
                kwargs = record.call_args.kwargs
                assert kwargs["reason"] == "api_key_invalid"
        finally:
            _clear_overrides()

    def test_missing_bearer_prefix_records_audit(
        self, agent_svc_no_match: AsyncMock
    ) -> None:
        _override_agent_service(agent_svc_no_match)
        try:
            with patch(
                "acn.routes.dependencies._audit_record_auth_failure"
            ) as record:
                with TestClient(app) as client:
                    r = client.post(
                        "/api/v1/agents/some-agent/heartbeat",
                        headers={"Authorization": "Basic foo"},
                    )
                assert r.status_code == 401
                assert record.called, (
                    "verify_agent_api_key must audit malformed Authorization headers"
                )
                kwargs = record.call_args.kwargs
                assert kwargs["reason"] == "bearer_format_invalid"
        finally:
            _clear_overrides()

    def test_invalid_x_acn_authorization_records_audit(
        self, agent_svc_no_match: AsyncMock
    ) -> None:
        """Proxy uses a different header — its own bad-format branch must audit."""
        _override_agent_service(agent_svc_no_match)
        try:
            with patch(
                "acn.routes.dependencies._audit_record_auth_failure"
            ) as record:
                with TestClient(app) as client:
                    r = client.post(
                        "/api/v1/agents/target-agent",
                        headers={"X-ACN-Authorization": "Basic foo"},
                        json={"jsonrpc": "2.0"},
                    )
                assert r.status_code == 401
                assert record.called
                kwargs = record.call_args.kwargs
                assert kwargs["reason"] == "x_acn_authorization_format_invalid"
        finally:
            _clear_overrides()


class TestInternalTokenFailure:
    """``verify_internal_token`` is the gate for admin/operator routes.

    A bad token here is the canonical signal of either a misconfigured
    backend or active probing — both warrant an audit entry.
    """

    def test_bad_internal_token_records_audit(self) -> None:
        with patch("acn.routes.dependencies._audit_record_auth_failure") as record:
            with TestClient(app) as client:
                # Any internal-only endpoint will do — DELETE bulk is convenient
                # because it doesn't require a body or path-specific stubs.
                r = client.delete(
                    "/api/v1/agents",
                    headers={"X-Internal-Token": "definitely-wrong"},
                )
            assert r.status_code == 403
            assert record.called, (
                "verify_internal_token must audit failed attempts so operators "
                "can spot probing or stale-secret deploys"
            )
            kwargs = record.call_args.kwargs
            assert kwargs["reason"] == "internal_token_invalid"


# ─────────────────────────────────────────────
# Phase B: SSRF block on proxy path
# ─────────────────────────────────────────────


class TestProxySSRFAudit:
    def test_ssrf_block_records_audit(
        self, agent_svc_with_caller: AsyncMock
    ) -> None:
        """When ``safe_resolve_target`` raises, the proxy must:
        1. return 502 (existing behaviour, regression-pinned),
        2. emit a ``SECURITY_SSRF_BLOCKED`` audit event tagged with caller +
           target so a SOC can chase the source.
        """
        _override_agent_service(agent_svc_with_caller)
        try:
            with patch(
                "acn.routes.registry.safe_resolve_target",
                new=AsyncMock(
                    side_effect=__import__(
                        "acn.security", fromlist=["SSRFViolation"]
                    ).SSRFViolation("blocked"),
                ),
            ), patch(
                "acn.routes.registry.fire_and_forget_event"
            ) as fire:
                with TestClient(app) as client:
                    r = client.post(
                        "/api/v1/agents/target-agent",
                        headers={"X-ACN-Authorization": "Bearer acn_test"},
                        json={"jsonrpc": "2.0"},
                    )
                assert r.status_code == 502
                assert fire.called, (
                    "Proxy SSRF block must emit a fire-and-forget audit event"
                )
                kwargs = fire.call_args.kwargs
                ev = kwargs["event_type"]
                # Compare by ``.value`` so an enum import path mismatch can't
                # silently make the assertion vacuous.
                assert ev.value == "security_ssrf_blocked"
                assert kwargs["actor_id"] == "caller-agent"
                assert kwargs["target_id"] == "target-agent"
                assert kwargs["details"]["target_url"].startswith(
                    "http://target.internal.local"
                )
        finally:
            _clear_overrides()


# ─────────────────────────────────────────────
# Phase C: bulk-delete audit
# ─────────────────────────────────────────────


class TestBulkDeleteAudit:
    """``admin_bulk_delete_agents`` must:
    1.  Skip audit on dry-run (preview is read-only).
    2.  Emit an ``AGENT_UNREGISTERED`` event per successful delete plus one
        ``ADMIN_BULK_DELETE`` summary event when ``dry_run=false``.
    """

    def _make_agent(self, agent_id: str, name: str, owner: str) -> SimpleNamespace:
        return SimpleNamespace(
            agent_id=agent_id, name=name, owner=owner, endpoint="http://x"
        )

    def _agent_svc(self, agents: list) -> AsyncMock:
        svc = AsyncMock()
        svc.search_agents = AsyncMock(return_value=agents)
        # `repository.delete` is awaited per agent — give it a real coroutine.
        repo = MagicMock()
        repo.delete = AsyncMock(return_value=True)
        svc.repository = repo
        return svc

    def test_dry_run_emits_no_audit(self) -> None:
        from acn.routes.dependencies import verify_internal_token

        agents = [self._make_agent("a-1", "test-agent", "alice")]
        svc = self._agent_svc(agents)
        app.dependency_overrides[get_agent_service] = lambda: svc
        app.dependency_overrides[verify_internal_token] = lambda: None
        try:
            with patch("acn.routes.registry.get_audit_singleton") as get_audit:
                fake_audit = AsyncMock()
                fake_audit.log_event = AsyncMock()
                get_audit.return_value = fake_audit
                with TestClient(app) as client:
                    r = client.delete(
                        "/api/v1/agents?name_prefix=test-&dry_run=true",
                        headers={"X-Internal-Token": "ignored"},
                    )
                assert r.status_code == 200
                assert r.json()["dry_run"] is True
                fake_audit.log_event.assert_not_awaited()
        finally:
            _clear_overrides()

    def test_execute_writes_per_agent_and_summary_audit(self) -> None:
        from acn.routes.dependencies import verify_internal_token

        agents = [
            self._make_agent("a-1", "test-one", "alice"),
            self._make_agent("a-2", "test-two", "alice"),
        ]
        svc = self._agent_svc(agents)
        app.dependency_overrides[get_agent_service] = lambda: svc
        app.dependency_overrides[verify_internal_token] = lambda: None

        try:
            with patch("acn.routes.registry.get_audit_singleton") as get_audit:
                fake_audit = AsyncMock()
                fake_audit.log_event = AsyncMock()
                get_audit.return_value = fake_audit

                with TestClient(app) as client:
                    r = client.delete(
                        "/api/v1/agents?name_prefix=test-&dry_run=false",
                        headers={"X-Internal-Token": "ignored"},
                    )
                assert r.status_code == 200
                body = r.json()
                assert body["deleted"] == 2

                # Two AGENT_UNREGISTERED + one ADMIN_BULK_DELETE summary.
                assert fake_audit.log_event.await_count == 3
                event_types = [
                    c.kwargs["event_type"].value
                    for c in fake_audit.log_event.await_args_list
                ]
                assert event_types.count("agent_unregistered") == 2
                assert event_types.count("admin_bulk_delete") == 1

                # Summary event details preserve filter params and counts so
                # an analyst can answer "did anyone delete by prefix=X today?".
                summary = next(
                    c
                    for c in fake_audit.log_event.await_args_list
                    if c.kwargs["event_type"].value == "admin_bulk_delete"
                )
                details = summary.kwargs["details"]
                assert details["name_prefix"] == "test-"
                assert details["matched"] == 2
                assert details["deleted"] == 2
                assert details["failed"] == 0
        finally:
            _clear_overrides()

    def test_execute_without_any_filter_is_rejected(self) -> None:
        """H-audit follow-up: ``dry_run=false`` with no ``name_prefix`` /
        ``owner`` would target the entire agent table — a single operator
        typo (``?dry_run=false`` and forgetting filters) is enough to wipe
        the database. The route must reject this before fetching agents.
        """
        from acn.routes.dependencies import verify_internal_token

        agents = [self._make_agent("a-1", "anything", "alice")]
        svc = self._agent_svc(agents)
        app.dependency_overrides[get_agent_service] = lambda: svc
        app.dependency_overrides[verify_internal_token] = lambda: None
        try:
            with patch("acn.routes.registry.get_audit_singleton") as get_audit:
                fake_audit = AsyncMock()
                fake_audit.log_event = AsyncMock()
                get_audit.return_value = fake_audit

                with TestClient(app) as client:
                    r = client.delete(
                        "/api/v1/agents?dry_run=false",
                        headers={"X-Internal-Token": "ignored"},
                    )

                assert r.status_code == 400
                # Sprint #2b migrated this raise from ``HTTPException`` to
                # ``ACNHTTPError(INVALID_REQUEST, …)`` — the body is the
                # flat ACN schema (``error_code`` / ``message`` / ``details``)
                # instead of the legacy ``{"detail": "..."}`` shape. The
                # operator-facing prose explaining the safety guard is now
                # in ``body["message"]`` and the structured reason marker
                # in ``body["details"]["reason"]``.
                body = r.json()
                assert body["error_code"] == "invalid_request"
                assert body["details"]["reason"] == "bulk_delete_filter_required"
                assert "name_prefix" in body["message"]
                # Guard must short-circuit BEFORE we read agents or delete
                # anything — both would be observable side effects in prod.
                svc.search_agents.assert_not_awaited()
                svc.repository.delete.assert_not_awaited()
                fake_audit.log_event.assert_not_awaited()
        finally:
            _clear_overrides()

    def test_dry_run_without_filter_still_allowed_for_preview(self) -> None:
        """The guard intentionally exempts ``dry_run=true`` so an operator
        can survey the whole population before picking a filter.
        """
        from acn.routes.dependencies import verify_internal_token

        agents = [
            self._make_agent("a-1", "alpha", "alice"),
            self._make_agent("a-2", "beta", "bob"),
        ]
        svc = self._agent_svc(agents)
        app.dependency_overrides[get_agent_service] = lambda: svc
        app.dependency_overrides[verify_internal_token] = lambda: None
        try:
            with patch("acn.routes.registry.get_audit_singleton") as get_audit:
                fake_audit = AsyncMock()
                fake_audit.log_event = AsyncMock()
                get_audit.return_value = fake_audit

                with TestClient(app) as client:
                    r = client.delete(
                        "/api/v1/agents?dry_run=true",
                        headers={"X-Internal-Token": "ignored"},
                    )

                assert r.status_code == 200
                body = r.json()
                assert body["dry_run"] is True
                assert body["would_delete"] == 2
                # Preview is still read-only (no audit on dry-run).
                fake_audit.log_event.assert_not_awaited()
        finally:
            _clear_overrides()

    def test_execute_with_empty_string_owner_is_rejected(self) -> None:
        """``?owner=`` (empty string) must be treated the same as omitted —
        a half-typed query string should not bypass the filter guard.
        """
        from acn.routes.dependencies import verify_internal_token

        agents = [self._make_agent("a-1", "anything", "alice")]
        svc = self._agent_svc(agents)
        app.dependency_overrides[get_agent_service] = lambda: svc
        app.dependency_overrides[verify_internal_token] = lambda: None
        try:
            with TestClient(app) as client:
                r = client.delete(
                    "/api/v1/agents?dry_run=false&owner=&name_prefix=",
                    headers={"X-Internal-Token": "ignored"},
                )
            assert r.status_code == 400
            svc.repository.delete.assert_not_awaited()
        finally:
            _clear_overrides()


# ─────────────────────────────────────────────
# Phase A (continued): JWT / permission / bearer_missing branches
# ─────────────────────────────────────────────


class TestJwtAndPermissionBranches:
    """``acn.auth.middleware`` raises 401/403 from four distinct paths.

    Each path has its own ``record_auth_failure`` call and we want a
    failing test if any branch silently regresses to no-op.

    We hit ``DELETE /api/v1/subnets/{id}`` because it is gated by
    ``require_permission("acn:write")`` — the simplest reachable handler
    that exercises the full middleware stack.
    """

    def _settings_with_auth0(self):
        """A real ACN ``Settings`` with Auth0 enabled (dev_mode=False).

        ``Settings`` enforces a non-wildcard ``cors_origins`` whenever
        ``dev_mode`` is False (security audit C2), so the fixture must
        supply a concrete value.
        """
        from acn.config import Settings

        return Settings(
            dev_mode=False,
            auth0_domain="https://example.auth0.com",
            auth0_audience="https://api.example.com",
            internal_api_token="x" * 32,
            redis_url="redis://localhost:6379/0",
            cors_origins=["https://app.example.com"],
        )

    def test_bearer_missing_records_audit(self) -> None:
        """No Authorization header at all -> ``bearer_missing``.

        We override ``_get_settings`` so the middleware exits dev-mode and
        actually enforces credentials.
        """
        with patch(
            "acn.auth.middleware._get_settings",
            return_value=self._settings_with_auth0(),
        ), patch("acn.auth.middleware.record_auth_failure") as record:
            with TestClient(app) as client:
                r = client.delete("/api/v1/subnets/some-id")
            assert r.status_code == 401
            # ``record_auth_failure`` may be called once (verify_token) or
            # not at all if the route never reaches the dependency — pin the
            # specific reason rather than the count.
            reasons = [c.kwargs.get("reason") for c in record.call_args_list]
            assert "bearer_missing" in reasons

    def test_jwt_invalid_records_audit(self) -> None:
        """A token that fails ``jwt.decode`` -> ``jwt_invalid``.

        ``_get_jwks`` returns a single dummy key whose ``kid`` matches the
        unverified header, so signing-key lookup succeeds and verification
        fails on signature instead — exercising the ``JWTError`` branch.
        """
        from jose import jwt

        # Forge an unsigned JWT with a known ``kid`` so JWKS lookup matches
        # before signature verification fails.
        bogus = jwt.encode(
            {"sub": "auth0|user-1"},
            "wrong-secret",
            algorithm="HS256",
            headers={"kid": "test-kid"},
        )
        # Build a JWKS the key-lookup loop will accept; signature check
        # will still fail because the token wasn't signed with this RSA key.
        jwks = {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "test-kid",
                    "use": "sig",
                    "n": "x" * 32,
                    "e": "AQAB",
                }
            ]
        }
        with patch(
            "acn.auth.middleware._get_settings",
            return_value=self._settings_with_auth0(),
        ), patch(
            "acn.auth.middleware._get_jwks",
            new=AsyncMock(return_value=jwks),
        ), patch(
            "acn.auth.middleware.record_auth_failure"
        ) as record:
            with TestClient(app) as client:
                r = client.delete(
                    "/api/v1/subnets/some-id",
                    headers={"Authorization": f"Bearer {bogus}"},
                )
            assert r.status_code == 401
            reasons = [c.kwargs.get("reason") for c in record.call_args_list]
            assert "jwt_invalid" in reasons, (
                f"_verify_jwt JWTError branch must record audit; got reasons={reasons}"
            )

    def test_permission_denied_records_audit_with_actor_not_target(self) -> None:
        """When JWT verifies but lacks the permission -> ``permission_denied``.

        Crucially, the audit event must tag the failing principal as
        ``actor_id`` (not ``target_id``) so analyst queries by target stay
        clean. We assert against the keyword passed to
        ``record_auth_failure`` since that is the helper's contract with
        the audit pipeline.
        """
        # Make ``verify_token`` succeed by short-circuiting JWT verification:
        # we replace ``_verify_jwt`` with a stub that returns a payload
        # missing the required permission.
        async def _verify_stub(token, request=None):  # noqa: ARG001
            return {"sub": "auth0|user-42", "permissions": ["acn:read"]}

        with patch(
            "acn.auth.middleware._get_settings",
            return_value=self._settings_with_auth0(),
        ), patch(
            "acn.auth.middleware._verify_jwt",
            new=_verify_stub,
        ), patch(
            "acn.auth.middleware.record_auth_failure"
        ) as record:
            with TestClient(app) as client:
                r = client.delete(
                    "/api/v1/subnets/some-id",
                    headers={"Authorization": "Bearer not-checked"},
                )
            assert r.status_code == 403
            # Find the permission_denied call specifically.
            denied_calls = [
                c for c in record.call_args_list
                if c.kwargs.get("reason") == "permission_denied"
            ]
            assert denied_calls, (
                "require_permission must record audit on permission denial"
            )
            kwargs = denied_calls[0].kwargs
            assert kwargs.get("actor_id") == "auth0|user-42"
            # The middleware variant doesn't have access to a proxy-aware IP,
            # but it must still pass *some* path/method context.
            assert kwargs.get("path") == "/api/v1/subnets/some-id"
            assert kwargs.get("method") == "DELETE"
            assert kwargs.get("extra", {}).get("permission") == "acn:write"


# ─────────────────────────────────────────────
# Phase B (continued): SSRF audit honours trusted_proxies for source_ip
# ─────────────────────────────────────────────


class TestProxySSRFSourceIp:
    """The SSRF audit hook must use the same proxy-aware IP resolver that
    the auth-failure path uses (``_get_real_ip``). Otherwise an attacker
    coming through a trusted reverse proxy would be attributed to the
    proxy IP, defeating SOC chase-down.
    """

    def test_ssrf_block_source_ip_uses_get_real_ip(self) -> None:
        agent_svc = AsyncMock()
        target_agent = SimpleNamespace(
            agent_id="target-agent",
            endpoint="http://target.internal.local/a2a",
        )
        caller_agent = SimpleNamespace(agent_id="caller-agent", name="Caller")
        agent_svc.get_agent = AsyncMock(return_value=target_agent)
        agent_svc.get_agent_by_api_key = AsyncMock(return_value=caller_agent)
        _override_agent_service(agent_svc)

        try:
            # Make ``_get_real_ip`` return a deterministic value so we can
            # assert the SSRF audit picked it up. The runtime module is
            # imported under the alias used inside ``registry``.
            with patch(
                "acn.routes.registry._get_real_ip",
                return_value="198.51.100.7",
            ), patch(
                "acn.routes.registry.safe_resolve_target",
                new=AsyncMock(
                    side_effect=__import__(
                        "acn.security", fromlist=["SSRFViolation"]
                    ).SSRFViolation("blocked"),
                ),
            ), patch(
                "acn.routes.registry.fire_and_forget_event"
            ) as fire:
                with TestClient(app) as client:
                    r = client.post(
                        "/api/v1/agents/target-agent",
                        headers={
                            "X-ACN-Authorization": "Bearer acn_test",
                            "X-Forwarded-For": "198.51.100.7",
                        },
                        json={"jsonrpc": "2.0"},
                    )
                assert r.status_code == 502
                assert fire.called
                kwargs = fire.call_args.kwargs
                assert kwargs["source_ip"] == "198.51.100.7", (
                    "SSRF audit must record the proxy-resolved client IP, "
                    "not the direct TCP peer"
                )
        finally:
            _clear_overrides()
