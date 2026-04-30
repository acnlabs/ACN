"""Unit tests for the reverse-proxy ↔ PolicyCheckService integration.

Pre-fix gap (Phase 1 review finding P0-1):
    The four reverse-proxy endpoints (``POST/PUT/PATCH /{agent_id}``
    and the ``/{agent_id}/{rest_path}`` catch-all) used to forward to
    the agent's real endpoint without consulting
    ``communication_policy``. This made ``closed`` mode toothless on
    the highest-surface-area inbound path: any agent holding a valid
    ACN API key could push traffic at any other agent regardless of
    their declared policy.

These tests exercise ``_proxy_to_agent`` directly so we can pin the
contract without standing up the FastAPI app / Redis lifespan:

* a ``closed`` recipient short-circuits with ``HTTP 403`` carrying the
  same structured detail body the ``/communication/send`` path uses,
* the rejection happens *before* DNS / SSRF resolution and before any
  ``httpx.AsyncClient`` instantiation — i.e. zero observable side
  effects toward the recipient's network,
* ``open`` and ``policy_service=None`` (legacy / opt-out) keep the
  existing forwarding behaviour intact, so adopting the gate doesn't
  break callers that haven't been wired yet,
* the ``system:*`` namespace bypasses policy on this path too,
  matching the global exemption rule documented in
  ``Phase 1 网关执行点决策``.

Why we don't go through the full FastAPI route layer here:
    The routes layer test (``test_proxy_policy_routes.py``) covers the
    HTTP wire shape. Driving ``_proxy_to_agent`` directly lets these
    tests run in the local sandbox (no Redis required) and pin the
    rejection-side-effect invariants — which are the security-
    critical part — independently of FastAPI plumbing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from acn.routes.registry import _proxy_to_agent
from acn.services.policy_service import PolicyCheckService


def _make_agent(
    *,
    endpoint: str = "https://target.example.com/a2a",
    communication_policy: dict | None = None,
):
    """Build a stand-in Agent entity for the proxy code path.

    The real ``Agent`` dataclass has ~30 fields; the proxy only reads
    ``endpoint`` and ``communication_policy``, so a MagicMock with
    those two attributes is sufficient and keeps tests independent of
    unrelated entity churn.
    """
    agent = MagicMock()
    agent.endpoint = endpoint
    agent.communication_policy = communication_policy
    return agent


def _make_request() -> MagicMock:
    """Build a minimal FastAPI Request stand-in.

    ``_proxy_to_agent`` only touches ``request.body()`` and
    ``request.headers`` after the policy short-circuit, so on the
    rejected paths these never need to be realistic.
    """
    request = MagicMock()
    request.body = AsyncMock(return_value=b"")
    request.headers = {}
    return request


def _make_caller(agent_id: str = "agent-sender", name: str = "Sender") -> dict:
    return {"agent_id": agent_id, "name": name}


@pytest.fixture
def policy_service() -> PolicyCheckService:
    return PolicyCheckService()


@pytest.fixture
def closed_agent_service():
    """AgentService stub returning a ``closed`` recipient."""
    svc = MagicMock()
    svc.get_agent = AsyncMock(
        return_value=_make_agent(
            communication_policy={
                "mode": "closed",
                "reject_reason": "On vacation until 2026-05",
            }
        )
    )
    return svc


@pytest.fixture
def open_agent_service():
    """AgentService stub returning an ``open`` recipient."""
    svc = MagicMock()
    svc.get_agent = AsyncMock(
        return_value=_make_agent(communication_policy={"mode": "open"})
    )
    return svc


# --------------------------------------------------------------------------- #
# Closed recipient → HTTP 403 with structured detail, no network side effects
# --------------------------------------------------------------------------- #


class TestClosedRecipientRejected:
    """The whole point of the gate: a closed recipient must produce a
    structured 403 *and* zero observable side effects toward the
    recipient (no DNS resolve, no httpx client, no body read of the
    upstream response). Each negative below maps to one piece of
    "before policy" infrastructure we deliberately skip."""

    @pytest.mark.asyncio
    async def test_closed_returns_403_with_structured_detail(
        self, closed_agent_service, policy_service
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _proxy_to_agent(
                request=_make_request(),
                agent_id="agent-target",
                method="POST",
                rest_path="",
                agent_service=closed_agent_service,
                caller=_make_caller(),
                policy_service=policy_service,
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == {
            "detail": "communication_rejected",
            "reason": "policy_closed",
            "reject_reason": "On vacation until 2026-05",
        }

    @pytest.mark.asyncio
    async def test_closed_does_not_resolve_dns(
        self, closed_agent_service, policy_service
    ):
        """SSRF resolution is the next step after policy check. If the
        gate fired correctly, ``safe_resolve_target`` must not run —
        any DNS round-trip on a rejected request is wasted upstream
        load and a leak of "this agent exists / has this hostname"
        signal to the recipient's DNS provider."""
        with patch("acn.routes.registry.safe_resolve_target") as mock_resolve:
            with pytest.raises(HTTPException):
                await _proxy_to_agent(
                    request=_make_request(),
                    agent_id="agent-target",
                    method="POST",
                    rest_path="",
                    agent_service=closed_agent_service,
                    caller=_make_caller(),
                    policy_service=policy_service,
                )

        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_closed_does_not_open_http_client(
        self, closed_agent_service, policy_service
    ):
        """The httpx.AsyncClient instantiation is the most expensive
        side effect: it allocates a connection pool *and* would fire a
        real TCP SYN if the test ever escaped the sandbox. Pinning that
        the rejection happens early enough to skip it entirely."""
        with patch("acn.routes.registry.httpx.AsyncClient") as mock_client:
            with pytest.raises(HTTPException):
                await _proxy_to_agent(
                    request=_make_request(),
                    agent_id="agent-target",
                    method="POST",
                    rest_path="",
                    agent_service=closed_agent_service,
                    caller=_make_caller(),
                    policy_service=policy_service,
                )

        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_closed_with_subpath_still_rejected(
        self, closed_agent_service, policy_service
    ):
        """The catch-all path (``/{agent_id}/{rest_path}``) is the
        biggest surface area — anything the agent exposes via REST
        flows through here. Pinning that ``rest_path`` does not
        accidentally exempt traffic from the gate."""
        with pytest.raises(HTTPException) as exc_info:
            await _proxy_to_agent(
                request=_make_request(),
                agent_id="agent-target",
                method="GET",
                rest_path="some/deep/api/path",
                agent_service=closed_agent_service,
                caller=_make_caller(),
                policy_service=policy_service,
            )

        assert exc_info.value.status_code == 403


# --------------------------------------------------------------------------- #
# Open recipient → policy is transparent
# --------------------------------------------------------------------------- #


class TestOpenRecipientPassesThrough:
    @pytest.mark.asyncio
    async def test_open_does_not_short_circuit(
        self, open_agent_service, policy_service
    ):
        """``open`` recipients must not surface the 403 path. We patch
        the *next* step (SSRF resolution) to fail fast so we know
        execution actually progressed past the policy check; without
        the patch the test would hit the real network."""
        with patch(
            "acn.routes.registry.safe_resolve_target",
            new=AsyncMock(side_effect=RuntimeError("expected — proves we passed policy")),
        ):
            with pytest.raises(RuntimeError, match="expected"):
                await _proxy_to_agent(
                    request=_make_request(),
                    agent_id="agent-target",
                    method="POST",
                    rest_path="",
                    agent_service=open_agent_service,
                    caller=_make_caller(),
                    policy_service=policy_service,
                )

    @pytest.mark.asyncio
    async def test_no_policy_field_is_treated_as_open(self, policy_service):
        """Backward-compat: agents that registered before the
        policy field landed have ``communication_policy=None``. They
        must continue to receive proxied traffic exactly as before —
        this is the entire premise of the rollout being safe."""
        svc = MagicMock()
        svc.get_agent = AsyncMock(
            return_value=_make_agent(communication_policy=None)
        )

        with patch(
            "acn.routes.registry.safe_resolve_target",
            new=AsyncMock(side_effect=RuntimeError("passed policy")),
        ):
            with pytest.raises(RuntimeError, match="passed policy"):
                await _proxy_to_agent(
                    request=_make_request(),
                    agent_id="agent-target",
                    method="POST",
                    rest_path="",
                    agent_service=svc,
                    caller=_make_caller(),
                    policy_service=policy_service,
                )


# --------------------------------------------------------------------------- #
# Service is None (rollout opt-out) — gate is bypassed
# --------------------------------------------------------------------------- #


class TestNoPolicyServiceBypassesGate:
    """When the routes layer hasn't been wired with a policy service
    yet (legacy CLI tools, partial-bring-up smoke tests),
    ``_proxy_to_agent`` must keep working as before. This is the same
    rollout-safety contract MessageRouter / SubnetManager use: ``None``
    means "no gate", behaviour matches Phase 0.

    Production guards against accidental ``None`` via a lifespan-time
    assertion in ``acn/api.py`` (Step 2.7), so this branch can only
    fire in test / dev contexts."""

    @pytest.mark.asyncio
    async def test_closed_recipient_passes_when_policy_service_is_none(
        self, closed_agent_service
    ):
        with patch(
            "acn.routes.registry.safe_resolve_target",
            new=AsyncMock(side_effect=RuntimeError("would-have-rejected")),
        ):
            # Without policy_service, even a closed recipient flows
            # through — execution must reach the SSRF guard, NOT
            # raise HTTPException(403). The RuntimeError below is our
            # sentry for "got past the gate".
            with pytest.raises(RuntimeError, match="would-have-rejected"):
                await _proxy_to_agent(
                    request=_make_request(),
                    agent_id="agent-target",
                    method="POST",
                    rest_path="",
                    agent_service=closed_agent_service,
                    caller=_make_caller(),
                    policy_service=None,
                )


# --------------------------------------------------------------------------- #
# system:* sender bypasses policy (matches the global exemption rule)
# --------------------------------------------------------------------------- #


class TestSystemSenderBypassesPolicy:
    """ACN-internal callers send proxy traffic via the
    ``system:<slug>`` namespace (e.g. backend dispatching chat-mention
    notifications). They must clear the gate even toward a ``closed``
    recipient — same contract as the rest of the gateway. Without
    this, an admin who closes their agent would also lock out
    legitimate platform notifications, breaking core product flows.
    """

    @pytest.mark.asyncio
    async def test_system_sender_passes_closed_recipient(
        self, closed_agent_service, policy_service
    ):
        with patch(
            "acn.routes.registry.safe_resolve_target",
            new=AsyncMock(side_effect=RuntimeError("passed policy")),
        ):
            with pytest.raises(RuntimeError, match="passed policy"):
                await _proxy_to_agent(
                    request=_make_request(),
                    agent_id="agent-target",
                    method="POST",
                    rest_path="",
                    agent_service=closed_agent_service,
                    caller=_make_caller(agent_id="system:agentplanet-backend"),
                    policy_service=policy_service,
                )


# --------------------------------------------------------------------------- #
# Agent not found — 404 still wins over policy (precondition ordering)
# --------------------------------------------------------------------------- #


class TestPolicyRejectedIncrementsMetric:
    """v2 review finding R1 — proxy is the highest-surface-area
    inbound path. Without a metric inc, ops has no way to dashboard
    or alert on policy rejections coming from the reverse-proxy
    routes (which is exactly the path most likely to be abused by a
    leaked ACN API key — easier to enumerate ``closed`` agents than
    to forge A2A messages). Pin the dimension contract here so a
    future refactor that drops the inc reintroduces the gap loudly.
    """

    @pytest.mark.asyncio
    async def test_inc_counter_called_with_proxy_path_and_reason(
        self, closed_agent_service, policy_service
    ):
        from unittest.mock import AsyncMock

        metrics = MagicMock()
        metrics.inc_counter = AsyncMock()

        with pytest.raises(HTTPException):
            await _proxy_to_agent(
                request=_make_request(),
                agent_id="agent-target",
                method="POST",
                rest_path="",
                agent_service=closed_agent_service,
                caller=_make_caller(),
                policy_service=policy_service,
                metrics=metrics,
            )

        # Pinning the exact label set — `path` must be "proxy"
        # (not "single" — single-send is the /communication/send
        # path, mixing them up would corrupt the per-channel
        # dashboards).
        metrics.inc_counter.assert_awaited_once_with(
            "messages_rejected_by_policy_total",
            labels={"path": "proxy", "reason": "policy_closed"},
        )

    @pytest.mark.asyncio
    async def test_metric_inc_failure_does_not_break_403(
        self, closed_agent_service, policy_service
    ):
        """Best-effort observability: a Redis blip during counter
        write must not turn a clean 403 into a 500 — clients of
        ``/proxy`` would otherwise see flaky behaviour driven by an
        unrelated counter backend hiccup. The route still rejects
        with the structured 403 detail."""
        from unittest.mock import AsyncMock

        metrics = MagicMock()
        metrics.inc_counter = AsyncMock(side_effect=RuntimeError("redis down"))

        with pytest.raises(HTTPException) as exc_info:
            await _proxy_to_agent(
                request=_make_request(),
                agent_id="agent-target",
                method="POST",
                rest_path="",
                agent_service=closed_agent_service,
                caller=_make_caller(),
                policy_service=policy_service,
                metrics=metrics,
            )

        # 403 wins despite the metric inc failure — same wire
        # contract as the no-metrics case.
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["reason"] == "policy_closed"

    @pytest.mark.asyncio
    async def test_open_recipient_does_not_inc_metric(
        self, open_agent_service, policy_service
    ):
        """Negative-side guarantee: the metric must only fire on
        rejection, not on every proxy invocation. Otherwise the
        counter would double as a generic "proxy traffic" gauge,
        defeating its purpose as an abuse signal."""
        from unittest.mock import AsyncMock, patch

        metrics = MagicMock()
        metrics.inc_counter = AsyncMock()

        with patch(
            "acn.routes.registry.safe_resolve_target",
            new=AsyncMock(side_effect=RuntimeError("expected — passed policy")),
        ):
            with pytest.raises(RuntimeError, match="expected"):
                await _proxy_to_agent(
                    request=_make_request(),
                    agent_id="agent-target",
                    method="POST",
                    rest_path="",
                    agent_service=open_agent_service,
                    caller=_make_caller(),
                    policy_service=policy_service,
                    metrics=metrics,
                )

        metrics.inc_counter.assert_not_called()


class TestPreconditionOrdering:
    @pytest.mark.asyncio
    async def test_agent_not_found_returns_404_before_policy_check(
        self, policy_service
    ):
        """Existence check must run *before* the policy gate, otherwise
        we'd leak which agent ids exist via the choice between 403 and
        404. AgentNotFoundException → 404, no policy lookup attempted."""
        from acn.core.exceptions import AgentNotFoundException

        svc = MagicMock()
        svc.get_agent = AsyncMock(side_effect=AgentNotFoundException("nope"))

        with pytest.raises(HTTPException) as exc_info:
            await _proxy_to_agent(
                request=_make_request(),
                agent_id="agent-missing",
                method="POST",
                rest_path="",
                agent_service=svc,
                caller=_make_caller(),
                policy_service=policy_service,
            )

        assert exc_info.value.status_code == 404
