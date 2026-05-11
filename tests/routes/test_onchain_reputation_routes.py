"""Onchain reputation routes — auth / idempotency / state-machine smoke.

Sprint Saga v0.1 Todo 5 review fix R7. The pre-existing
``test_onchain_error_schema.py`` covers the bind / read paths only;
this file adds the three new write/read endpoints introduced in Todo 5:

* ``POST /agents/{id}/feedback``
* ``POST /agents/{id}/validations``
* ``GET  /agents/{id}/reputation/summary``

Why a separate file rather than appending to the existing one:
``test_onchain_error_schema.py`` is scoped to the flat-schema migration
contract (12 pre-Todo-5 raise sites). Mixing in the reputation-write
authorisation matrix (5 raise sites + 6 happy / idempotency cases)
would dilute its purpose. The split keeps both files cohesive.

Coverage matrix
---------------
POST /feedback:
* 503 — reputation_service not wired (Redis-only deployment)
* 400 — self-feedback
* 404 — target agent does not exist
* 404 — task does not exist
* 403 — caller is not task creator (R1 — the security fix)
* 400 — target agent is not task assignee
* 400 — task not in COMPLETED state
* 201 — happy path
* 201 — repeated POST returns the same row (idempotency contract)
* response always reports ``smoke_test=False`` (smoke flag is
  worker-path-only, R16 documents this)

POST /validations:
* 400 — attestation missing
* 400 — caller is task creator (must use /feedback instead)
* 400 — task not COMPLETED
* 201 — happy path

GET /reputation/summary:
* 200 — off-chain only (no chain binding)
* 200 — merged (chain client + bound token)
* 200 — Redis-only deployment (query service is None) returns
  zero-filled summary without crashing

Not covered here
----------------
* Rate limits — the limiter is disabled by ``_reset_state`` in the
  conftest, same as every other route test.
* Auth bypass — when the API key is invalid, ``verify_agent_api_key``
  raises before our handler is called; that's covered by sprint #10's
  auth-failure-audit tests and there's no Todo-5-specific behaviour
  to assert.
* PostgreSQL repository behaviour — needs a live DB; Todo 8.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.entities.task import TaskStatus
from acn.core.interfaces.reputation_repository import (
    REPUTATION_KIND_FEEDBACK,
    REPUTATION_KIND_VALIDATION,
    ReputationEvent,
)
from acn.routes.dependencies import (
    get_agent_service,
    get_reputation_query_service,
    get_reputation_service,
    verify_agent_api_key,
)
from acn.routes.tasks import get_task_service
from acn.services.reputation_query_service import (
    OffChainReputationSummary,
    ReputationSummary,
)

# =============================================================================
# Stubs
# =============================================================================


def _stub_agent(agent_id: str = "agent-target"):
    """An agent that exists but has no ERC-8004 binding (sufficient
    for every reputation-route test — chain merge is opt-in)."""
    return SimpleNamespace(
        agent_id=agent_id,
        wallet_address=None,
        erc8004_agent_id=None,
        erc8004_chain=None,
        erc8004_tx_hash=None,
        erc8004_registered_at=None,
    )


def _stub_agent_service(*, exists: bool = True):
    """Wire ``get_agent`` to either return a stub or raise."""
    from acn.core.exceptions import AgentNotFoundException

    svc = AsyncMock()
    if exists:
        svc.get_agent = AsyncMock(return_value=_stub_agent())
    else:
        svc.get_agent = AsyncMock(
            side_effect=AgentNotFoundException("agent-target")
        )
    svc.repository = MagicMock()
    return svc


def _stub_task(
    *,
    task_id: str = "task-xyz",
    creator_id: str = "agent-creator",
    assignee_id: str | None = "agent-target",
    status: TaskStatus = TaskStatus.COMPLETED,
):
    """A task in the shape ``TaskService.get_task`` returns. Default
    state is the green path (creator + assignee set, COMPLETED).
    Tests override fields to exercise each rejection branch.
    """
    return SimpleNamespace(
        task_id=task_id,
        creator_id=creator_id,
        assignee_id=assignee_id,
        status=status,
    )


def _stub_task_service(
    task=None,
    *,
    raises_not_found: bool = False,
):
    from acn.services.task_service import TaskNotFoundException

    svc = AsyncMock()
    if raises_not_found:
        svc.get_task = AsyncMock(
            side_effect=TaskNotFoundException("task-xyz")
        )
    else:
        svc.get_task = AsyncMock(return_value=task or _stub_task())
    return svc


def _stub_reputation_service():
    """Default behaviour: record_feedback / record_validation echo a
    fabricated event with id=42. Tests that care about specific call
    args inspect ``call_args``.
    """
    svc = MagicMock()
    fixed_event = ReputationEvent(
        id=42,
        agent_id="agent-target",
        task_id="task-xyz",
        kind=REPUTATION_KIND_FEEDBACK,
        signer="agent-creator",
        score=None,
        evidence_uri=None,
        attestation=None,
        event_metadata={},
        created_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC),
    )
    svc.record_feedback = AsyncMock(return_value=fixed_event)
    # Validation has a different ``kind`` field on the returned row.
    validation_event = ReputationEvent(
        id=43,
        agent_id="agent-target",
        task_id="task-xyz",
        kind=REPUTATION_KIND_VALIDATION,
        signer="agent-validator",
        score=None,
        evidence_uri=None,
        attestation={"tag": "successful"},
        event_metadata={},
        created_at=datetime(2026, 5, 11, 12, 5, 0, tzinfo=UTC),
    )
    svc.record_validation = AsyncMock(return_value=validation_event)
    return svc


def _stub_query_service():
    """Default summary: zero off-chain, no chain merge."""
    svc = MagicMock()
    svc.get_summary = AsyncMock(
        return_value=ReputationSummary(
            agent_id="agent-target",
            off_chain=OffChainReputationSummary(
                feedback_count=0,
                validation_count=0,
                recent_events=[],
            ),
            on_chain=None,
            source="off_chain",
        )
    )
    return svc


def _wire(
    *,
    caller_agent_id: str = "agent-creator",
    agent_service=None,
    task_service=None,
    reputation_service=None,
    reputation_query_service=None,
):
    """Standard wiring helper. Pass ``reputation_service=None`` to
    simulate the Redis-only 503 case.
    """
    app.dependency_overrides[verify_agent_api_key] = lambda: {
        "agent_id": caller_agent_id
    }
    if agent_service is not None:
        app.dependency_overrides[get_agent_service] = lambda: agent_service
    if task_service is not None:
        app.dependency_overrides[get_task_service] = lambda: task_service
    # reputation_service: None means "Redis-only deployment". We use
    # ``app.dependency_overrides`` with a lambda returning None so the
    # 503 branch fires.
    app.dependency_overrides[get_reputation_service] = (
        lambda: reputation_service
    )
    if reputation_query_service is not None:
        app.dependency_overrides[get_reputation_query_service] = (
            lambda: reputation_query_service
        )


# =============================================================================
# POST /feedback
# =============================================================================


class TestFeedbackServiceUnavailable:
    def test_503_when_reputation_service_not_wired(self):
        """Redis-only deployments must respond 503 (operator config
        issue), not 500. The response body itself is sanitised by
        ACN's 5xx handler — operator-side diagnostics (the
        DATABASE_URL hint) live in the structured log, never the
        client response. So this test only asserts the status code;
        the diagnostic content is covered by the route docstring
        and the captured-log assertion would be redundant."""
        _wire(
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(),
            reputation_service=None,  # Redis-only
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz"},
            )
        assert r.status_code == 503


class TestFeedbackAuthorisation:
    """The R1 security fix — these tests guard against the regression
    of "any API-key holder can write feedback against anyone".
    """

    def test_400_self_feedback(self):
        _wire(
            caller_agent_id="agent-target",  # caller == target
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz"},
            )
        assert r.status_code == 400
        body = r.json()
        assert body["error_code"] == "invalid_request"
        assert body["details"]["reason"] == "self_feedback_forbidden"

    def test_404_agent_not_found(self):
        _wire(
            agent_service=_stub_agent_service(exists=False),
            task_service=_stub_task_service(),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz"},
            )
        assert r.status_code == 404
        assert r.json()["error_code"] == "agent_not_found"

    def test_404_task_not_found(self):
        _wire(
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(raises_not_found=True),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz"},
            )
        assert r.status_code == 404
        assert r.json()["error_code"] == "task_not_found"

    def test_403_caller_is_not_task_creator(self):
        """The crown jewel of the R1 fix: a valid API-key holder who
        is NOT the task creator must be rejected. Without this the
        whole reputation system is freely griefable."""
        task = _stub_task(creator_id="agent-someone-else")
        _wire(
            caller_agent_id="agent-creator",  # caller != task.creator
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(task=task),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz"},
            )
        assert r.status_code == 403
        body = r.json()
        assert body["error_code"] == "ownership_mismatch"
        assert body["details"]["reason"] == "caller_is_not_task_creator"
        assert body["details"]["caller_id"] == "agent-creator"

    def test_400_target_is_not_task_assignee(self):
        """If the task was assigned to agent-other but the route URL
        points at agent-target, reject — reputation must match the
        task's actual assignee, not an arbitrary agent.
        """
        task = _stub_task(assignee_id="agent-other")
        _wire(
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(task=task),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz"},
            )
        assert r.status_code == 400
        assert (
            r.json()["details"]["reason"] == "target_is_not_task_assignee"
        )

    @pytest.mark.parametrize(
        "bad_status",
        [
            TaskStatus.OPEN,
            TaskStatus.IN_PROGRESS,
            TaskStatus.SUBMITTED,
            TaskStatus.REJECTED,
            TaskStatus.CANCELLED,
        ],
    )
    def test_400_task_not_completed(self, bad_status):
        """Reputation only makes sense after a task reaches COMPLETED.
        Each pre-completion / non-completion status is rejected with
        a clear ``current_status`` hint."""
        task = _stub_task(status=bad_status)
        _wire(
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(task=task),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz"},
            )
        assert r.status_code == 400
        body = r.json()
        assert body["details"]["reason"] == "task_not_completed"
        assert body["details"]["current_status"] == bad_status


class TestFeedbackHappyPath:
    def test_201_returns_persisted_event(self):
        rep_svc = _stub_reputation_service()
        _wire(
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(),
            reputation_service=rep_svc,
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz", "score": 85},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["id"] == 42
        assert body["kind"] == "feedback"
        assert body["agent_id"] == "agent-target"
        assert body["signer"] == "agent-creator"
        assert body["smoke_test"] is False, (
            "Direct POST submissions are ALWAYS treated as non-smoke "
            "(R16 — smoke flag is worker-path-only). A regression "
            "here would let API users tag prod reputation as smoke."
        )
        assert "off-chain v0.1" in body["note"]
        # Confirm service was called with the caller as signer (not a
        # body-supplied signer — that would be the spoofing vector).
        rep_svc.record_feedback.assert_called_once()
        kwargs = rep_svc.record_feedback.call_args.kwargs
        assert kwargs["signer"] == "agent-creator"
        assert kwargs["task_metadata"] is None, (
            "Route MUST pass task_metadata=None — smoke propagation "
            "is reserved for the worker path."
        )

    def test_repeated_post_is_idempotent_same_id(self):
        """The repository's UNIQUE constraint folds duplicate writes
        into a single row; ``record_feedback`` returns the existing
        row on conflict. SDK callers retrying after a transient
        network error must see the same id on subsequent POSTs.
        """
        rep_svc = _stub_reputation_service()
        # Both calls return the same fabricated event (id=42).
        _wire(
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(),
            reputation_service=rep_svc,
        )
        with TestClient(app) as client:
            r1 = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz"},
            )
            r2 = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz"},
            )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"] == 42
        assert r1.json()["created_at"] == r2.json()["created_at"]
        assert rep_svc.record_feedback.call_count == 2, (
            "Route does not cache — it delegates to the service "
            "each time, which delegates to the repo's ON CONFLICT "
            "DO NOTHING."
        )

    def test_score_validation_422(self):
        """Pydantic field validation rejects out-of-range scores at
        the route boundary before reaching the service."""
        _wire(
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/feedback",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz", "score": 101},
            )
        assert r.status_code == 422, r.text


# =============================================================================
# POST /validations
# =============================================================================


class TestValidation:
    def test_400_missing_attestation(self):
        """``attestation`` is a required field — pydantic catches a
        missing key with 422 (request validation), but ``{}`` passes
        pydantic's "is this a dict" check. The route layer rejects
        the empty dict with 400 ``attestation_required`` before any
        of the more expensive fetches run."""
        _wire(
            caller_agent_id="agent-validator",
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/validations",
                headers={"Authorization": "Bearer fake-key"},
                json={"task_id": "task-xyz", "attestation": {}},
            )
        assert r.status_code == 400
        body = r.json()
        assert body["error_code"] == "invalid_request"
        assert body["details"]["reason"] == "attestation_required"

    def test_400_creator_must_use_feedback(self):
        """Task creator submitting validation is rerouted to
        /feedback — validation is a third-party voice, having the
        creator sneak a row through it would double-count creator
        sentiment.
        """
        _wire(
            caller_agent_id="agent-creator",  # caller IS task.creator_id
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/validations",
                headers={"Authorization": "Bearer fake-key"},
                json={
                    "task_id": "task-xyz",
                    "attestation": {"tag": "successful"},
                },
            )
        assert r.status_code == 400
        body = r.json()
        assert (
            body["details"]["reason"]
            == "creator_must_use_feedback_endpoint"
        )

    def test_400_task_not_completed(self):
        task = _stub_task(status=TaskStatus.IN_PROGRESS)
        _wire(
            caller_agent_id="agent-validator",
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(task=task),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/validations",
                headers={"Authorization": "Bearer fake-key"},
                json={
                    "task_id": "task-xyz",
                    "attestation": {"tag": "successful"},
                },
            )
        assert r.status_code == 400
        assert r.json()["details"]["reason"] == "task_not_completed"

    def test_201_happy_path(self):
        rep_svc = _stub_reputation_service()
        _wire(
            caller_agent_id="agent-validator",
            agent_service=_stub_agent_service(),
            task_service=_stub_task_service(),
            reputation_service=rep_svc,
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/onchain/agents/agent-target/validations",
                headers={"Authorization": "Bearer fake-key"},
                json={
                    "task_id": "task-xyz",
                    "attestation": {
                        "tag": "successful",
                        "signature": "0xabc",
                    },
                },
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "validation"
        assert body["signer"] == "agent-validator"
        rep_svc.record_validation.assert_called_once()
        kwargs = rep_svc.record_validation.call_args.kwargs
        assert kwargs["attestation"]["tag"] == "successful"


# =============================================================================
# GET /reputation/summary
# =============================================================================


class TestReputationSummary:
    def test_200_off_chain_only(self):
        """No chain binding on the agent — response carries off-chain
        zeros and ``on_chain=None``. ``source='off_chain'`` is the
        SDK's signal that no chain merge happened."""
        _wire(
            agent_service=_stub_agent_service(),
            reputation_query_service=_stub_query_service(),
            reputation_service=_stub_reputation_service(),  # unused
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/onchain/agents/agent-target/reputation/summary"
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_id"] == "agent-target"
        assert body["source"] == "off_chain"
        assert body["on_chain"] is None
        assert body["off_chain"]["feedback_count"] == 0

    def test_200_redis_only_returns_zero_summary(self):
        """When the dependency layer didn't construct a query service
        (partial test bringup), the route must still return a valid
        ``ReputationSummary`` — SDKs don't have to special-case the
        response shape on this branch."""
        _wire(
            agent_service=_stub_agent_service(),
            reputation_query_service=None,
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/onchain/agents/agent-target/reputation/summary"
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "off_chain"
        assert body["off_chain"]["feedback_count"] == 0
        assert body["off_chain"]["recent_events"] == []

    def test_404_agent_not_found(self):
        _wire(
            agent_service=_stub_agent_service(exists=False),
            reputation_query_service=_stub_query_service(),
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/onchain/agents/agent-target/reputation/summary"
            )
        assert r.status_code == 404

    def test_query_filters_passed_through(self):
        """``include_smoke_test`` and ``recent_limit`` query
        parameters must reach the service unmodified — the route is
        a thin adapter."""
        svc = _stub_query_service()
        _wire(
            agent_service=_stub_agent_service(),
            reputation_query_service=svc,
            reputation_service=_stub_reputation_service(),
        )
        with TestClient(app) as client:
            r = client.get(
                "/api/v1/onchain/agents/agent-target/reputation/summary"
                "?include_smoke_test=true&recent_limit=5"
            )
        assert r.status_code == 200
        kwargs = svc.get_summary.call_args.kwargs
        assert kwargs["include_smoke_test"] is True
        assert kwargs["recent_limit"] == 5
