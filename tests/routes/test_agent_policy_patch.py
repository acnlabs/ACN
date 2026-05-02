"""Tests for ``PATCH /api/v1/agents/{id}/policy``.

Phase 1 L410-B:
    Without a runtime policy mutation endpoint, ``closed`` mode
    was effectively unreachable for any agent already in
    production — the field was wired through the registration
    schema but invisible to the existing agent population. This
    endpoint is the user-facing knob that makes the rest of the
    L410 / L412–417 work meaningful.

These tests pin three categories of contract:

* **Authorization** — only the agent itself (Bearer API key) or
  ACN-internal tooling (X-Internal-Token) can change a policy.
  Anonymous callers, malformed credentials, and Bearer keys for
  *other* agents must all fail without touching the persistence
  layer.
* **Validation** — the same schema enforced at registration time
  must apply on PATCH. We share the validator
  (``validate_policy_dict``) so this is the second consumer
  ensuring the validator stays a single source of truth — bugs
  in either entry point would otherwise drift apart.
* **Persistence** — successful PATCH calls
  ``AgentService.update_communication_policy`` exactly once with
  the validated payload, and the response body echoes the
  post-update policy so callers know what was stored (especially
  important when passing ``null`` to reset, since the entity
  layer fills in the default).

Why we don't test cross-impact with ``check_inbound`` here: the
policy-engine semantics are already pinned in
``test_policy_service.py``. This file scopes itself to the
**route → service** seam.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.exceptions import AgentNotFoundException
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    limiter,
)

VALID_INTERNAL_TOKEN = "test-internal-token-min-32-chars-padding"


@pytest.fixture(autouse=True)
def _reset_state():
    limiter.enabled = False
    _api_key_cache.clear()
    yield
    limiter.enabled = True
    _api_key_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def stub_agent_service():
    """Minimal AgentService stub.

    ``get_agent_by_api_key`` resolves ``"owner-key" -> agent-target``
    so owner-via-API-key auth is exercisable.
    ``update_communication_policy`` echoes the input back into a
    MagicMock entity so tests can assert what was persisted without
    a real Agent dataclass.
    """
    svc = AsyncMock()

    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"

    other = MagicMock()
    other.agent_id = "agent-other"
    other.name = "Other"

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        if key == "other-key":
            return other
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)

    async def _update(agent_id: str, communication_policy):
        if agent_id != "agent-target":
            raise AgentNotFoundException(agent_id)
        # Echo whatever the service decided to persist into a
        # MagicMock so tests can assert via the same shape as a
        # real Agent.
        result = MagicMock()
        result.agent_id = agent_id
        result.communication_policy = (
            {"mode": "open"} if communication_policy is None else dict(communication_policy)
        )
        return result

    svc.update_communication_policy = AsyncMock(side_effect=_update)

    # Phase 2 PR #1 (Group B #3-bis): the PATCH /policy route now
    # pre-fetches the agent so it can record ``old_mode`` in the
    # POLICY_CHANGED audit event. Tests need ``get_agent`` to succeed
    # for ``agent-target`` and 404 for everyone else; the previous
    # always-404 default would break every happy-path test now that
    # the route reads the entity before the update.
    async def _default_get_agent(agent_id: str):
        if agent_id == "agent-target":
            existing = MagicMock()
            existing.agent_id = agent_id
            existing.communication_policy = {"mode": "open"}
            return existing
        raise AgentNotFoundException(agent_id)

    svc.get_agent = AsyncMock(side_effect=_default_get_agent)
    return svc


def _wire(svc) -> None:
    app.dependency_overrides[get_agent_service] = lambda: svc


# --------------------------------------------------------------------------- #
# Auth: anonymous / malformed
# --------------------------------------------------------------------------- #


class TestAuthRejectsAnonymous:
    def test_no_credentials_returns_401(self, stub_agent_service):
        """The mutation surface MUST NOT be readable/writable by
        anonymous callers — this is the same gate that
        ``GET /{id}/endpoint`` uses, and policy mutation is
        strictly more privileged than read."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/policy",
                json={"communication_policy": {"mode": "closed"}},
            )

        assert r.status_code == 401, r.text
        stub_agent_service.update_communication_policy.assert_not_awaited()

    def test_bearer_key_for_different_agent_returns_403(self, stub_agent_service):
        """Cross-agent boundary: a valid API key for agent X must
        NOT let X mutate Y's policy. Otherwise the "owner-only"
        guarantee collapses to "any-authenticated-agent" — the
        same hole this endpoint exists to close."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/policy",
                json={"communication_policy": {"mode": "closed"}},
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403, r.text
        stub_agent_service.update_communication_policy.assert_not_awaited()

    def test_invalid_internal_token_fails_closed(self, stub_agent_service):
        """A *present-but-wrong* X-Internal-Token must 403 instead
        of falling through to API-key auth (same rationale as in
        ``test_agent_endpoint_disclosure.py`` — silently routing a
        misconfigured ops tool through API-key auth would mask
        the misconfig)."""
        _wire(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/agent-target/policy",
                    json={"communication_policy": {"mode": "closed"}},
                    headers={
                        "X-Internal-Token": "wrong-token",
                        "Authorization": "Bearer owner-key",
                    },
                )

        assert r.status_code == 403, r.text
        stub_agent_service.update_communication_policy.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Validation: shared validator catches the same shapes here as on register
# --------------------------------------------------------------------------- #


class TestSchemaValidation:
    """We sample a handful of shapes that the validator must
    reject. Exhaustive validator behavior lives in the
    ``policy_service`` unit tests — here we only confirm that the
    PATCH endpoint actually wires the validator, i.e. doesn't have
    its own divergent schema. A sample is sufficient because we
    trust the unit tests for completeness; we just need to catch
    a future regression where someone bypasses
    ``validate_policy_dict`` (e.g. by switching to a manual
    ``isinstance(dict)`` check)."""

    def test_unknown_mode_rejected(self, stub_agent_service):
        """PR #2 ships ``open`` / ``closed`` / ``manifest`` /
        ``allowlist``. Modes outside this set (e.g. a half-baked
        forward-shipped ``paid`` mode) must surface 422 so a
        client cannot store a policy that "activates" on a future
        deploy."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/policy",
                json={"communication_policy": {"mode": "paid"}},
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 422, r.text
        # Pin the wire-shape: error mentions all currently-supported
        # modes plus the one we tried, so a frontend can surface a
        # useful message without parsing.
        body_text = r.text
        assert "paid" in body_text
        assert "open" in body_text and "closed" in body_text
        # PR #1 added ``manifest`` and PR #2 added ``allowlist`` —
        # make sure both appear in the diagnostic so the error
        # message tracks the live supported set, not a stale list.
        assert "manifest" in body_text
        assert "allowlist" in body_text
        stub_agent_service.update_communication_policy.assert_not_awaited()

    def test_unknown_top_level_key_rejected(self, stub_agent_service):
        """Phase 1 schema is strict: unknown keys (e.g. half-baked
        Phase 2 fields like ``manifest_threshold``) must be
        rejected, not silently stored — otherwise upgrading to
        Phase 2 would suddenly activate fields users didn't know
        they'd set."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/policy",
                json={
                    "communication_policy": {
                        "mode": "open",
                        "manifest_threshold": 1024,
                    }
                },
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 422, r.text
        assert "manifest_threshold" in r.text
        stub_agent_service.update_communication_policy.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Happy path: closed / open / null reset
# --------------------------------------------------------------------------- #


class TestPersistenceFlow:
    def test_owner_can_set_closed(self, stub_agent_service):
        """The core user story: an agent flips itself to
        ``closed`` and gets back the persisted shape. We assert on
        the service call, not just the response body, because the
        response is constructed AFTER persistence — checking the
        mock call ensures we actually hit the repository, not
        just echoed the input."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/policy",
                json={
                    "communication_policy": {
                        "mode": "closed",
                        "reject_reason": "on vacation",
                    }
                },
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "agent_id": "agent-target",
            "communication_policy": {
                "mode": "closed",
                "reject_reason": "on vacation",
            },
        }
        stub_agent_service.update_communication_policy.assert_awaited_once_with(
            agent_id="agent-target",
            communication_policy={"mode": "closed", "reject_reason": "on vacation"},
        )

    def test_owner_can_set_open_without_reason(self, stub_agent_service):
        """``reject_reason`` is optional. Pinning that the
        validator preserves a missing field as missing (rather
        than backfilling ``""`` or ``None``) — keeps the stored
        dict minimal."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/policy",
                json={"communication_policy": {"mode": "open"}},
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["communication_policy"] == {"mode": "open"}
        stub_agent_service.update_communication_policy.assert_awaited_once_with(
            agent_id="agent-target",
            communication_policy={"mode": "open"},
        )

    def test_null_resets_to_default_open(self, stub_agent_service):
        """An explicit ``null`` is a documented reset signal —
        useful for ops to clear a stuck custom policy. The
        response must echo the entity-layer default so the caller
        confirms what's now persisted."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.patch(
                "/api/v1/agents/agent-target/policy",
                json={"communication_policy": None},
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["communication_policy"] == {"mode": "open"}
        stub_agent_service.update_communication_policy.assert_awaited_once_with(
            agent_id="agent-target",
            communication_policy=None,
        )

    def test_internal_token_can_force_close(self, stub_agent_service):
        """Ops scenario: emergency forced-close of an abusive
        agent. Pinning that the X-Internal-Token branch reaches
        the same service method as the API-key branch — i.e.
        there's no separate "internal-only" path that could drift
        in semantics."""
        _wire(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/agent-target/policy",
                    json={
                        "communication_policy": {
                            "mode": "closed",
                            "reject_reason": "abuse review",
                        }
                    },
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 200, r.text
        stub_agent_service.update_communication_policy.assert_awaited_once_with(
            agent_id="agent-target",
            communication_policy={"mode": "closed", "reject_reason": "abuse review"},
        )

    def test_self_close_does_not_lock_owner_out(self, stub_agent_service):
        """Anti-self-lock invariant: an agent that flipped itself
        to ``closed`` MUST still be able to PATCH itself back to
        ``open``. Concretely: the management plane (PATCH /policy)
        is gated by ``verify_owner_or_internal`` — an *auth*
        check, not a policy check. ``closed`` only suppresses
        inbound *messages*; it must never bleed into the
        management surface, otherwise a legitimate user who
        closed by mistake would be permanently locked out with
        no recovery path short of internal-token intervention.

        We exercise the exact sequence (close, then re-open) on
        the same client to verify the second PATCH still
        authenticates and persists, rather than getting 403'd by
        any accidental future PolicyCheckService invocation."""
        _wire(stub_agent_service)

        with TestClient(app) as client:
            # Step 1: agent closes itself.
            r1 = client.patch(
                "/api/v1/agents/agent-target/policy",
                json={
                    "communication_policy": {
                        "mode": "closed",
                        "reject_reason": "oops, didn't mean to close",
                    }
                },
                headers={"Authorization": "Bearer owner-key"},
            )
            assert r1.status_code == 200, r1.text
            assert r1.json()["communication_policy"]["mode"] == "closed"

            # Step 2: same agent re-opens itself. Must still
            # authenticate & persist — closed mode must NOT bleed
            # into the management plane.
            r2 = client.patch(
                "/api/v1/agents/agent-target/policy",
                json={"communication_policy": {"mode": "open"}},
                headers={"Authorization": "Bearer owner-key"},
            )
            assert r2.status_code == 200, r2.text
            assert r2.json()["communication_policy"] == {"mode": "open"}

        # Two service calls happened — one per PATCH — confirming
        # neither was short-circuited.
        assert stub_agent_service.update_communication_policy.await_count == 2

    def test_unknown_agent_returns_404(self, stub_agent_service):
        """Once authenticated, the existence signal is fine — the
        404 distinguishes "you typed the wrong ID" from "you
        don't have permission". Auth must precede this check
        though; verified by the anonymous test above (which 401s
        before any agent lookup)."""
        _wire(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/does-not-exist/policy",
                    json={"communication_policy": {"mode": "open"}},
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 404, r.text


# --------------------------------------------------------------------------- #
# Registration + Join: the same schema validator covers those entry points
# --------------------------------------------------------------------------- #
#
# Strictly speaking these tests belong to the register/join routes,
# but pinning them here keeps the policy-validation contract
# co-located so a future maintainer reviewing the policy schema
# only has one file to read.


# --------------------------------------------------------------------------- #
# Phase 2 PR #1 (Group B #3-bis): POLICY_CHANGED audit emit
# --------------------------------------------------------------------------- #


class TestPolicyChangedAudit:
    """Phase 2 PR #1 promotes the historical INFO log to a structured
    ``POLICY_CHANGED`` audit event so platform tooling can correlate
    policy flips with subsequent traffic drops in
    ``MESSAGE_REJECTED``.

    These tests pin the audit-emit contract:

    * Every successful PATCH writes one ``POLICY_CHANGED`` event.
    * ``details`` carries ``old_mode`` (read pre-update) and
      ``new_mode``.
    * ``actor_type`` reflects the caller kind (``agent`` vs
      ``internal``) so audit consumers can filter by source.
    * Sensitive transitions (``open`` → ``closed`` / ``manifest``)
      escalate to ``WARNING`` level so on-call dashboards surface
      them by default.
    * Audit failures must not 5xx the PATCH — the policy mutation
      is already persisted; surfacing 500 would mislead clients
      into thinking the policy didn't change.
    """

    def test_open_to_closed_emits_warning_audit(self, stub_agent_service):
        """Sensitive transition: open → closed must emit
        ``POLICY_CHANGED`` at WARNING level.

        We patch ``fire_and_forget_event`` directly rather than
        ``log_event`` — the route uses fire-and-forget so the audit
        write doesn't block the response, but that means the
        coroutine isn't actually awaited inside ``TestClient``'s
        synchronous send loop. Capturing the args at the
        ``fire_and_forget_event`` boundary side-steps the timing
        issue entirely and matches what the route actually invokes.
        """
        from acn.monitoring.audit import AuditEventType, AuditLevel

        captured: list[dict] = []

        def _capture(*_args, **kwargs):
            captured.append(kwargs)

        _wire(stub_agent_service)

        with patch(
            "acn.routes.registry.fire_and_forget_event",
            side_effect=_capture,
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/agent-target/policy",
                    json={"communication_policy": {"mode": "closed"}},
                    headers={"Authorization": "Bearer owner-key"},
                )

        assert r.status_code == 200, r.text
        assert len(captured) == 1, captured
        kw = captured[0]
        assert kw["event_type"] == AuditEventType.POLICY_CHANGED
        assert kw["level"] == AuditLevel.WARNING
        assert kw["target_id"] == "agent-target"
        details = kw["details"]
        assert details["old_mode"] == "open"
        assert details["new_mode"] == "closed"
        assert details["caller_kind"] == "agent"

    def test_open_to_manifest_emits_warning_audit(self, stub_agent_service):
        """``manifest`` is also a tightening transition (the recipient
        is opting into a low-attention divert path); flip to
        WARNING level so it shows up on the same dashboards as
        ``closed``."""
        from acn.monitoring.audit import AuditEventType, AuditLevel

        captured: list[dict] = []

        def _capture(*_args, **kwargs):
            captured.append(kwargs)

        _wire(stub_agent_service)

        with patch(
            "acn.routes.registry.fire_and_forget_event",
            side_effect=_capture,
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/agent-target/policy",
                    json={"communication_policy": {"mode": "manifest"}},
                    headers={"Authorization": "Bearer owner-key"},
                )

        assert r.status_code == 200, r.text
        assert len(captured) == 1, captured
        kw = captured[0]
        assert kw["event_type"] == AuditEventType.POLICY_CHANGED
        assert kw["level"] == AuditLevel.WARNING
        assert kw["details"]["old_mode"] == "open"
        assert kw["details"]["new_mode"] == "manifest"

    def test_internal_token_caller_recorded_as_internal(self, stub_agent_service):
        """X-Internal-Token branch must record ``caller_kind=internal``
        so analysts can filter ops-tool flips out of the
        agent-self-service stream when investigating anomalies."""
        from acn.monitoring.audit import AuditEventType

        captured: list[dict] = []

        def _capture(*_args, **kwargs):
            captured.append(kwargs)

        _wire(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ), patch(
            "acn.routes.registry.fire_and_forget_event",
            side_effect=_capture,
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/agent-target/policy",
                    json={"communication_policy": {"mode": "closed"}},
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 200, r.text
        assert len(captured) == 1, captured
        kw = captured[0]
        assert kw["event_type"] == AuditEventType.POLICY_CHANGED
        assert kw["details"]["caller_kind"] == "internal"


# --------------------------------------------------------------------------- #
# X-ACN-SDK-Min-Version warning header (Phase 2 review v2 P1 #10)
# --------------------------------------------------------------------------- #


class TestSDKVersionWarningHeader:
    """``PATCH /policy`` resolving to ``manifest`` or ``allowlist`` is
    an implicit breaking change for any SDK that doesn't implement
    ``manifest_notification`` (and, for allowlist mode, the manifest
    queue polling path). Old clients silently miss every inbound
    message — a near-impossible-to-diagnose breakage from the agent
    author's perspective.

    The ``X-ACN-SDK-Min-Version`` response header is the warning
    gate: dashboards / SDK release tooling can pick it up
    automatically rather than relying on operators reading release
    notes. These tests pin five contracts:

    1. Switching INTO ``manifest`` emits the header.
    2. Switching INTO ``allowlist`` emits the header.
    3. Switching INTO ``open`` / ``closed`` does NOT emit (no SDK
       contract change).
    4. Idempotent re-application (``manifest`` → ``manifest``) STILL
       emits — operators inspecting any single PATCH must see the
       requirement, not just the first transition.
    5. 404 paths (unknown agent) must NOT emit — the header would
       advertise a contract for an entity that doesn't exist.

    See ``_MODES_REQUIRING_SDK_NOTIFY`` in ``acn/routes/registry.py``
    for the full rationale on always-emit vs transition-only.
    """

    SDK_HEADER = "X-ACN-SDK-Min-Version"

    def _patch_to(
        self,
        stub_agent_service,
        target_mode: str,
        *,
        existing_mode: str = "open",
        as_agent: bool = True,
    ):
        """Helper: drive a PATCH to ``target_mode`` from
        ``existing_mode``. ``as_agent`` switches between the
        owner-key path and the internal-token path so we can confirm
        the header logic is auth-channel-agnostic.

        For the ``as_agent=False`` branch we MUST also patch
        ``settings.internal_api_token`` to ``VALID_INTERNAL_TOKEN``
        — without it the dependency rejects the header as
        misconfigured (matches the existing
        ``test_invalid_internal_token_fails_closed`` pattern).
        """
        async def _get_with_mode(agent_id):
            if agent_id == "agent-target":
                existing = MagicMock()
                existing.agent_id = agent_id
                existing.communication_policy = {"mode": existing_mode}
                return existing
            raise AgentNotFoundException(agent_id)

        stub_agent_service.get_agent = AsyncMock(side_effect=_get_with_mode)

        _wire(stub_agent_service)

        body_policy = {"mode": target_mode}
        if as_agent:
            with TestClient(app) as client:
                return client.patch(
                    "/api/v1/agents/agent-target/policy",
                    json={"communication_policy": body_policy},
                    headers={"Authorization": "Bearer owner-key"},
                )
        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                return client.patch(
                    "/api/v1/agents/agent-target/policy",
                    json={"communication_policy": body_policy},
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

    def test_open_to_manifest_emits_header(self, stub_agent_service):
        r = self._patch_to(stub_agent_service, "manifest")
        assert r.status_code == 200, r.text
        # Default value matches ``Settings.policy_manifest_min_sdk_version``.
        assert r.headers.get(self.SDK_HEADER) == "0.5.0"

    def test_open_to_allowlist_emits_header(self, stub_agent_service):
        r = self._patch_to(stub_agent_service, "allowlist")
        assert r.status_code == 200, r.text
        assert r.headers.get(self.SDK_HEADER) == "0.5.0"

    def test_open_to_closed_does_not_emit(self, stub_agent_service):
        """``closed`` is a tightening but doesn't change the WS
        contract — closed recipients reject inbound on the policy
        layer; old SDKs lose nothing they were getting before. No
        header."""
        r = self._patch_to(stub_agent_service, "closed")
        assert r.status_code == 200, r.text
        assert self.SDK_HEADER not in r.headers, (
            f"closed mode must not advertise SDK requirement; got "
            f"{r.headers.get(self.SDK_HEADER)!r}"
        )

    def test_open_to_open_does_not_emit(self, stub_agent_service):
        """No-op transition. Header stays absent."""
        r = self._patch_to(stub_agent_service, "open")
        assert r.status_code == 200, r.text
        assert self.SDK_HEADER not in r.headers

    def test_manifest_to_manifest_idempotent_still_emits(self, stub_agent_service):
        """Idempotent re-apply still emits — the contract is "the
        resulting mode requires this SDK", not "this PATCH transitioned
        to a new mode". Deploy scripts confirming desired state must
        still see the requirement on any single PATCH they inspect."""
        r = self._patch_to(stub_agent_service, "manifest", existing_mode="manifest")
        assert r.status_code == 200, r.text
        assert r.headers.get(self.SDK_HEADER) == "0.5.0"

    def test_unknown_agent_404_does_not_emit(self, stub_agent_service):
        """404 path: pre-fetch fails before any update runs. The
        header would advertise a contract for an entity that doesn't
        exist — confusing to clients and to dashboards correlating
        headers with audit events.

        We use the internal-token auth channel here on purpose:
        the owner-key channel rejects with 403 ("API key does not
        match agent_id") before the route handler ever runs, which
        wouldn't exercise the route-layer 404 mapping at all.
        Internal-token bypasses the per-agent ownership check so
        the request reaches ``update_agent_policy`` and the
        ``AgentNotFoundException`` from ``get_agent`` is what
        produces the 404 — that's the path we want to pin.
        """
        _wire(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.patch(
                    "/api/v1/agents/agent-ghost/policy",
                    json={"communication_policy": {"mode": "manifest"}},
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 404, r.text
        assert self.SDK_HEADER not in r.headers

    def test_internal_token_caller_also_emits_header(self, stub_agent_service):
        """The header is auth-channel-agnostic — internal-token
        callers (ops scripts) must see the SDK requirement just as
        owner callers do, since the resulting policy state is what
        downstream agents will need to handle."""
        r = self._patch_to(stub_agent_service, "manifest", as_agent=False)
        assert r.status_code == 200, r.text
        assert r.headers.get(self.SDK_HEADER) == "0.5.0"

    def test_header_value_reads_from_settings(self, stub_agent_service):
        """The min version is configurable via
        ``Settings.policy_manifest_min_sdk_version`` so ops can pin
        a different threshold per deployment without a code rebuild.
        Pin that the route reads the live settings value rather than
        baking the default into a module-level constant — otherwise
        env-var overrides would be silently ignored."""
        from acn.routes import registry as registry_module

        original = registry_module.settings.policy_manifest_min_sdk_version
        registry_module.settings.policy_manifest_min_sdk_version = "9.9.9"
        try:
            r = self._patch_to(stub_agent_service, "manifest")
            assert r.headers.get(self.SDK_HEADER) == "9.9.9"
        finally:
            registry_module.settings.policy_manifest_min_sdk_version = original


# --------------------------------------------------------------------------- #
# GET /policy: symmetric counterpart so owners can read their own policy
# --------------------------------------------------------------------------- #


class TestGetPolicy:
    """``GET /agents/{id}/policy`` is the symmetric read path.

    Without it, an owner who wants to confirm their current
    ``mode`` would have to issue a redundant PATCH (or read raw
    Redis) — clumsy and racy. We pin three things:

    * Same auth gate as PATCH (``OwnerOrInternalDep``) — policy is
      not public metadata, ``reject_reason`` may carry sensitive
      context.
    * Returns the entity's stored policy verbatim.
    * Backfills ``{"mode": "open"}`` when the entity has ``None``
      so clients always see a non-null shape — agents created
      before the field existed are seamlessly handled.
    """

    def test_anonymous_read_rejected(self, stub_agent_service):
        # Make ``get_agent`` resolve so we'd see the policy if auth
        # were bypassed — guarantees the 401 actually came from the
        # auth gate, not from a missing entity.
        target = MagicMock()
        target.agent_id = "agent-target"
        target.communication_policy = {"mode": "closed", "reject_reason": "secret"}
        stub_agent_service.get_agent = AsyncMock(return_value=target)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get("/api/v1/agents/agent-target/policy")

        assert r.status_code == 401, r.text
        # Defense-in-depth: even an anonymous probe must NOT reach
        # ``get_agent`` — otherwise auth-precedes-existence is
        # broken and the route leaks ID enumeration.
        stub_agent_service.get_agent.assert_not_awaited()

    def test_owner_reads_own_policy(self, stub_agent_service):
        target = MagicMock()
        target.agent_id = "agent-target"
        target.communication_policy = {"mode": "closed", "reject_reason": "vacation"}
        stub_agent_service.get_agent = AsyncMock(return_value=target)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/policy",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json() == {
            "agent_id": "agent-target",
            "communication_policy": {"mode": "closed", "reject_reason": "vacation"},
        }

    def test_other_agent_cannot_read(self, stub_agent_service):
        """Cross-tenant boundary mirrors PATCH: a Bearer key for X
        cannot read Y's ``reject_reason``. This matters because
        ``reject_reason`` can carry private context (vacation
        dates, abuse tags) that the owner authored expecting only
        themselves to see."""
        target = MagicMock()
        target.agent_id = "agent-target"
        target.communication_policy = {"mode": "closed", "reject_reason": "secret"}
        stub_agent_service.get_agent = AsyncMock(return_value=target)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/policy",
                headers={"Authorization": "Bearer other-key"},
            )

        assert r.status_code == 403, r.text
        stub_agent_service.get_agent.assert_not_awaited()

    def test_legacy_agent_with_none_policy_returns_open_default(self, stub_agent_service):
        """Agents created before the field existed have
        ``communication_policy = None`` in storage. The route must
        backfill ``{"mode": "open"}`` so clients aren't forced to
        special-case the legacy null shape."""
        target = MagicMock()
        target.agent_id = "agent-target"
        target.communication_policy = None
        stub_agent_service.get_agent = AsyncMock(return_value=target)
        _wire(stub_agent_service)

        with TestClient(app) as client:
            r = client.get(
                "/api/v1/agents/agent-target/policy",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        assert r.json()["communication_policy"] == {"mode": "open"}

    def test_unknown_agent_returns_404(self, stub_agent_service):
        # Default stub raises AgentNotFoundException for everything
        # except agent-target — using a different ID exercises the
        # 404 branch.
        _wire(stub_agent_service)

        with patch(
            "acn.routes.dependencies.settings.internal_api_token",
            VALID_INTERNAL_TOKEN,
        ):
            with TestClient(app) as client:
                r = client.get(
                    "/api/v1/agents/does-not-exist/policy",
                    headers={"X-Internal-Token": VALID_INTERNAL_TOKEN},
                )

        assert r.status_code == 404, r.text


class TestRegistrationAcceptsPolicy:
    def test_pydantic_models_share_validator(self):
        """Direct unit test: ``AgentRegisterRequest`` and
        ``AgentJoinRequest`` both delegate to
        ``validate_policy_dict``. We construct both with the same
        invalid payload and confirm both raise the same error
        message. If a future maintainer copies a custom validator
        into one of the two classes, this test fails loud — and
        the alternative (a divergent schema between two paths)
        would be exactly the silent-bug class we want to prevent.
        """
        from pydantic import ValidationError

        from acn.models import AgentRegisterRequest
        from acn.routes.registry import AgentJoinRequest

        # PR #2 added ``allowlist`` to the supported set, so we pick
        # ``paid`` as a stand-in for "any mode that is not currently
        # supported". The shared-validator invariant is what's pinned
        # — whichever mode is "currently rejected" is incidental, the
        # important property is that BOTH classes reject it identically.
        bad_payload = {
            "communication_policy": {"mode": "paid"},
        }

        # Build the rest of the required fields just enough to
        # *get past* the other validators — we only care about
        # ``communication_policy`` failing the same way.
        common_register = {
            "owner": "user-1",
            "name": "Test",
            "endpoint": "https://example.com/a2a",
        }
        common_join = {
            "name": "TestAgent",
            "description": "A test agent that does things",
            "endpoint": "https://example.com/a2a",
        }

        with pytest.raises(ValidationError) as exc_register:
            AgentRegisterRequest(**common_register, **bad_payload)
        with pytest.raises(ValidationError) as exc_join:
            AgentJoinRequest(**common_join, **bad_payload)

        # The validator is the same — the error text contains the
        # same diagnostic substring on both sides.
        assert "paid" in str(exc_register.value)
        assert "paid" in str(exc_join.value)
        assert "open" in str(exc_register.value).lower()
        assert "open" in str(exc_join.value).lower()
