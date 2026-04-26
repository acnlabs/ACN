"""Pydantic ``max_length`` regression tests for security audit H6.

These tests pin the field-level ceilings on every request model that
previously had unbounded string / list fields.  They protect against two
specific regression modes:

1. **Accidental relaxation** — a future PR widens or removes a ``max_length``
   under the assumption that the body cap "covers it".  The body cap is the
   *last* line of defence; per-field caps reject earlier with a clear 422
   detail and prevent giant strings from polluting downstream storage even
   if the body fits inside 1 MiB.
2. **Skipped fields** — H6 enumerated the fields that needed caps; when a
   new write endpoint copies one of these models, dropping the constraint
   silently regresses the audit.  These tests fail loudly the moment a
   covered field stops enforcing its ceiling.

We test by constructing models directly (no HTTP layer) — Pydantic raises
``ValidationError`` synchronously, which is the contract callers actually
depend on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from acn.models import ExternalAgentJoinRequest
from acn.routes.communication import (
    AckInboxRequest,
    BroadcastByTagRequest,
    BroadcastRequest,
    SendMessageRequest,
)
from acn.routes.onchain import BindRequest
from acn.routes.payments import (
    BillUsageRequest,
    CreatePaymentTaskRequest,
    EstimateCostRequest,
    PaymentCapabilityRequest,
)
from acn.routes.registry import (
    AgentClaimRequest,
    AgentJoinRequest,
    AgentTransferRequest,
)
from acn.routes.tasks import (
    TaskAcceptRequest,
    TaskCreateRequest,
    TaskInviteRequest,
    TaskReviewRequest,
    TaskSubmitRequest,
)


def _msg() -> dict:
    """A minimal valid A2A Message dict for SendMessage/Broadcast bodies."""

    return {"role": "user", "parts": [{"text": "hi"}]}


# ---------------------------------------------------------------------------
# tasks.py
# ---------------------------------------------------------------------------


class TestTaskCreateRequest:
    """``TaskCreateRequest`` was the largest unbounded surface — many fields."""

    def _base(self, **overrides):
        kwargs = {
            "title": "Valid title",
            "description": "x" * 50,
            "deadline_hours": 24,
            "reward": "10",
        }
        kwargs.update(overrides)
        return kwargs

    def test_description_caps_at_10k(self) -> None:
        TaskCreateRequest(**self._base(description="x" * 10_000))
        with pytest.raises(ValidationError):
            TaskCreateRequest(**self._base(description="x" * 10_001))

    def test_reward_caps_at_64(self) -> None:
        TaskCreateRequest(**self._base(reward="1" * 64))
        with pytest.raises(ValidationError):
            TaskCreateRequest(**self._base(reward="1" * 65))

    def test_required_tags_capped_at_20(self) -> None:
        TaskCreateRequest(**self._base(required_tags=["t"] * 20))
        with pytest.raises(ValidationError):
            TaskCreateRequest(**self._base(required_tags=["t"] * 21))

    def test_subnet_id_caps_at_64_matching_subnet_create(self) -> None:
        """Round-2 audit: ``subnet_id`` must align with ``SubnetCreateRequest``.

        The subnet create path enforces ``max_length=64``; allowing 128
        here only let through values that were guaranteed to miss the
        subnet lookup.  Pin the two together so a future drift is caught.
        """

        from acn.models import SubnetCreateRequest

        subnet_create_max = SubnetCreateRequest.model_fields["subnet_id"].metadata
        max_len = next(
            (m.max_length for m in subnet_create_max if hasattr(m, "max_length")),
            None,
        )
        assert max_len == 64, "anchor: SubnetCreateRequest.subnet_id should still be 64"

        TaskCreateRequest(**self._base(subnet_id="s" * 64))
        with pytest.raises(ValidationError):
            TaskCreateRequest(**self._base(subnet_id="s" * 65))

    def test_max_total_budget_capped(self) -> None:
        with pytest.raises(ValidationError):
            TaskCreateRequest(**self._base(
                max_participants=None,
                completion_mode="competitive",
                max_total_budget="9" * 65,
            ))


class TestTaskOtherRequests:
    def test_accept_message_caps_at_2000(self) -> None:
        TaskAcceptRequest(message="x" * 2000)
        with pytest.raises(ValidationError):
            TaskAcceptRequest(message="x" * 2001)

    def test_invite_agent_id_caps(self) -> None:
        TaskInviteRequest(agent_id="a" * 128)
        with pytest.raises(ValidationError):
            TaskInviteRequest(agent_id="a" * 129)

    def test_submit_submission_caps_at_50k(self) -> None:
        TaskSubmitRequest(submission="x" * 50_000)
        with pytest.raises(ValidationError):
            TaskSubmitRequest(submission="x" * 50_001)

    def test_submit_artifacts_count_capped(self) -> None:
        TaskSubmitRequest(submission="x" * 10, artifacts=[{}] * 50)
        with pytest.raises(ValidationError):
            TaskSubmitRequest(submission="x" * 10, artifacts=[{}] * 51)

    def test_review_notes_capped(self) -> None:
        TaskReviewRequest(approved=True, notes="x" * 5000)
        with pytest.raises(ValidationError):
            TaskReviewRequest(approved=True, notes="x" * 5001)

    def test_review_agent_id_capped(self) -> None:
        with pytest.raises(ValidationError):
            TaskReviewRequest(approved=True, agent_id="a" * 129)


# ---------------------------------------------------------------------------
# communication.py — historically the worst offender (unbounded message dict)
# ---------------------------------------------------------------------------


class TestCommunicationRequests:
    def test_send_from_agent_capped(self) -> None:
        SendMessageRequest(from_agent="a" * 128, target_agent="b", message=_msg())
        with pytest.raises(ValidationError):
            SendMessageRequest(from_agent="a" * 129, target_agent="b", message=_msg())

    def test_send_target_agent_capped(self) -> None:
        with pytest.raises(ValidationError):
            SendMessageRequest(from_agent="a", target_agent="b" * 129, message=_msg())

    def test_send_priority_capped(self) -> None:
        with pytest.raises(ValidationError):
            SendMessageRequest(
                from_agent="a", target_agent="b", message=_msg(), priority="x" * 33
            )

    def test_broadcast_target_tags_count_capped(self) -> None:
        BroadcastRequest(from_agent="a", message=_msg(), target_tags=["t"] * 50)
        with pytest.raises(ValidationError):
            BroadcastRequest(from_agent="a", message=_msg(), target_tags=["t"] * 51)

    def test_broadcast_by_tag_tags_count_capped(self) -> None:
        BroadcastByTagRequest(from_agent="a", tags=["t"] * 50, message=_msg())
        with pytest.raises(ValidationError):
            BroadcastByTagRequest(from_agent="a", tags=["t"] * 51, message=_msg())

    def test_broadcast_by_tag_limit_bounded(self) -> None:
        BroadcastByTagRequest(from_agent="a", tags=["t"], message=_msg(), limit=10_000)
        with pytest.raises(ValidationError):
            BroadcastByTagRequest(from_agent="a", tags=["t"], message=_msg(), limit=10_001)

    def test_ack_route_ids_count_capped(self) -> None:
        AckInboxRequest(route_ids=["r"] * 500)
        with pytest.raises(ValidationError):
            AckInboxRequest(route_ids=["r"] * 501)


# ---------------------------------------------------------------------------
# payments.py
# ---------------------------------------------------------------------------


class TestPaymentRequests:
    def test_payment_capability_lists_capped(self) -> None:
        # Pydantic accepts the enum strings directly — using an empty
        # methods/networks list keeps the test focused on length policy.
        with pytest.raises(ValidationError):
            PaymentCapabilityRequest(
                supported_methods=["usdc"] * 21,  # type: ignore[list-item]
                supported_networks=["base"],  # type: ignore[list-item]
            )

    def test_payment_capability_endpoint_caps_at_500(self) -> None:
        with pytest.raises(ValidationError):
            PaymentCapabilityRequest(
                supported_methods=[],  # type: ignore[list-item]
                supported_networks=[],  # type: ignore[list-item]
                api_endpoint="x" * 501,
            )

    def test_create_payment_task_agent_ids_capped(self) -> None:
        with pytest.raises(ValidationError):
            CreatePaymentTaskRequest(
                from_agent="a" * 129,
                to_agent="b",
                amount=1.0,
                currency="USDC",
                payment_method="usdc",  # type: ignore[arg-type]
                network="base",  # type: ignore[arg-type]
            )

    def test_create_payment_task_description_capped(self) -> None:
        with pytest.raises(ValidationError):
            CreatePaymentTaskRequest(
                from_agent="a",
                to_agent="b",
                amount=1.0,
                currency="USDC",
                payment_method="usdc",  # type: ignore[arg-type]
                network="base",  # type: ignore[arg-type]
                description="x" * 2001,
            )

    def test_estimate_cost_agent_id_capped(self) -> None:
        with pytest.raises(ValidationError):
            EstimateCostRequest(agent_id="a" * 129)

    def test_bill_usage_ids_capped(self) -> None:
        with pytest.raises(ValidationError):
            BillUsageRequest(
                user_id="u" * 129, agent_id="a", input_tokens=1, output_tokens=1
            )


# ---------------------------------------------------------------------------
# registry.py
# ---------------------------------------------------------------------------


class TestRegistryRequests:
    def _join(self, **overrides):
        kwargs = {
            "name": "Test Agent",
            "description": "A real description that meets the min_length",
            "endpoint": "https://example.com/agent",
        }
        kwargs.update(overrides)
        return kwargs

    def test_join_referrer_id_capped(self) -> None:
        AgentJoinRequest(**self._join(referrer_id="r" * 128))
        with pytest.raises(ValidationError):
            AgentJoinRequest(**self._join(referrer_id="r" * 129))

    def test_join_payment_methods_count_capped(self) -> None:
        with pytest.raises(ValidationError):
            AgentJoinRequest(**self._join(payment_methods=["usdc"] * 21))

    def test_claim_verification_code_capped(self) -> None:
        AgentClaimRequest(verification_code="v" * 128)
        with pytest.raises(ValidationError):
            AgentClaimRequest(verification_code="v" * 129)

    def test_transfer_new_owner_capped(self) -> None:
        with pytest.raises(ValidationError):
            AgentTransferRequest(new_owner="o" * 129)


# ---------------------------------------------------------------------------
# onchain.py
# ---------------------------------------------------------------------------


class TestOnchainRequests:
    def test_bind_chain_capped(self) -> None:
        BindRequest(token_id=1, chain="x" * 64)
        with pytest.raises(ValidationError):
            BindRequest(token_id=1, chain="x" * 65)

    def test_bind_tx_hash_capped(self) -> None:
        BindRequest(token_id=1, tx_hash="x" * 128)
        with pytest.raises(ValidationError):
            BindRequest(token_id=1, tx_hash="x" * 129)


# ---------------------------------------------------------------------------
# models.py — ExternalAgentJoinRequest is a public/unauthenticated endpoint
# ---------------------------------------------------------------------------


class TestExternalAgentJoinRequest:
    """``/external-agents/join`` is open to the world (no API key) — the
    most exposed write surface in the API. Caught during H6 self-audit:
    only ``name`` / ``description`` had ceilings, every other field was
    unbounded.
    """

    def _base(self, **overrides):
        kwargs: dict = {"name": "External"}
        kwargs.update(overrides)
        return kwargs

    def test_tags_count_capped(self) -> None:
        ExternalAgentJoinRequest(**self._base(tags=["t"] * 20))
        with pytest.raises(ValidationError):
            ExternalAgentJoinRequest(**self._base(tags=["t"] * 21))

    def test_mode_capped_at_16(self) -> None:
        with pytest.raises(ValidationError):
            ExternalAgentJoinRequest(**self._base(mode="x" * 17))

    def test_endpoint_capped_at_500(self) -> None:
        with pytest.raises(ValidationError):
            ExternalAgentJoinRequest(**self._base(endpoint="x" * 501))

    def test_source_capped_at_64(self) -> None:
        with pytest.raises(ValidationError):
            ExternalAgentJoinRequest(**self._base(source="x" * 65))

    def test_referrer_capped_at_128(self) -> None:
        with pytest.raises(ValidationError):
            ExternalAgentJoinRequest(**self._base(referrer="r" * 129))
