"""Phase 3 attention_fee — route-level contract tests.

Pins the public HTTP behaviour:

* ``POST /communication/manifest/{agent_id}/{mid}/ack``
    - 200 happy path with the release receipt forwarded back.
    - 404 ``MANIFEST_ENTRY_NOT_FOUND`` for missing / cross-tenant mids.
    - 400 ``ATTENTION_FEE_NOT_LOCKED`` when the entry has no fee.
    - 400 ``ATTENTION_FEE_ALREADY_ACKED`` on replay.
    - 400 ``ATTENTION_FEE_RELEASE_FAILED`` (with rollback) when the
      backend escrow rejects the release.

Service-layer guarantees (HSETNX idempotency, escrow lock-before-write
ordering) live in ``tests/services/test_attention_fee.py``; this file
focuses on the route → service seam and the ACN-error-schema mapping.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from acn.api import app
from acn.core.interfaces.escrow_provider import EscrowDetailResult, ReleaseResult
from acn.routes.dependencies import (
    _api_key_cache,
    get_agent_service,
    get_escrow_provider,
    get_escrow_provider_optional,
    get_manifest_service,
    limiter,
)
from acn.services.manifest_service import AlreadyAckedError, ManifestEntry


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
    """Wire ``owner-key`` to ``agent-target`` so AgentApiKeyDep
    + OwnerOrInternalDep accept the path."""
    svc = AsyncMock()
    target = MagicMock()
    target.agent_id = "agent-target"
    target.name = "Target"
    target.wallet_address = None

    async def _by_api_key(key: str):
        if key == "owner-key":
            return target
        return None

    svc.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
    return svc


def _entry_with_fee(mid: str, *, escrow_id: str = "esc_abc123") -> ManifestEntry:
    return ManifestEntry(
        mid=mid,
        sender_id="agent-alice",
        summary="paid msg",
        ts_ms=1_700_000_000_000,
        content_size=42,
        extra={
            "attention_fee": {
                "escrow_id": escrow_id,
                "task_id": "acn:attn:cafebabe",
                "amount": 50,
                "currency": "credits",
            }
        },
    )


def _entry_without_fee(mid: str) -> ManifestEntry:
    return ManifestEntry(
        mid=mid,
        sender_id="agent-alice",
        summary="free msg",
        ts_ms=1_700_000_000_000,
        content_size=42,
    )


@pytest.fixture
def stub_manifest_service():
    """Default: entry exists *with* fee, ack succeeds at the service
    layer. Individual tests override side effects."""
    svc = MagicMock()
    svc.get_entry = AsyncMock(return_value=_entry_with_fee("mid-aaa"))
    svc.mark_acked = AsyncMock(return_value=1_700_000_001_000)
    # ``unmark_acked`` is the rollback hook the route layer calls when
    # release_partial fails after mark_acked succeeded. Default to
    # ``True`` (rollback succeeded) — failure-mode tests override.
    svc.unmark_acked = AsyncMock(return_value=True)
    # ``delete`` is needed for the DELETE manifest path tests below.
    svc.delete = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def stub_escrow_provider():
    escrow = MagicMock()
    escrow.release_partial = AsyncMock(
        return_value=ReleaseResult(
            success=True,
            agent_amount=42.5,
            acn_amount=1.5,
            provider_amount=6.0,
            proof="receipt_xyz",
        )
    )
    # DELETE on a paid manifest entry refunds via this method; default
    # to a successful refund. Failure-mode tests override.
    escrow.refund_v2 = AsyncMock(
        return_value=EscrowDetailResult(
            success=True,
            escrow_id="esc_abc123",
            task_id="acn:attn:cafebabe",
            creator_id="agent-alice",
            creator_type="agent",
            amount=50.0,
            currency="credits",
            status="REFUNDED",
        )
    )
    return escrow


def _wire(*, manifest, agent, escrow) -> None:
    app.dependency_overrides[get_manifest_service] = lambda: manifest
    app.dependency_overrides[get_agent_service] = lambda: agent
    app.dependency_overrides[get_escrow_provider] = lambda: escrow
    # DELETE on manifest takes the *optional* escrow dep so the
    # no-fee path keeps working under ESCROW_ENABLED=false. Override
    # both so paid-fee tests can exercise refund_v2 regardless of
    # which entry point the route uses.
    app.dependency_overrides[get_escrow_provider_optional] = lambda: escrow


# --------------------------------------------------------------------------- #
# POST /communication/manifest/{agent_id}/{mid}/ack
# --------------------------------------------------------------------------- #


class TestAckHappyPath:
    def test_owner_ack_releases_and_returns_receipt(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )

        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/agent-target/mid-aaa/ack",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["acked"] is True
        assert body["acked_at"] == 1_700_000_001_000
        assert body["attention_fee"]["escrow_id"] == "esc_abc123"
        assert body["attention_fee"]["receipt_id"] == "receipt_xyz"
        assert body["attention_fee"]["agent_amount"] == 42.5

        # mark_acked + release_partial were both called exactly once
        # in the right order.
        stub_manifest_service.mark_acked.assert_awaited_once()
        stub_escrow_provider.release_partial.assert_awaited_once()
        kwargs = stub_escrow_provider.release_partial.await_args.kwargs
        assert kwargs["escrow_id"] == "esc_abc123"
        assert kwargs["recipient_id"] == "agent-target"
        assert kwargs["recipient_type"] == "agent"
        assert kwargs["amount"] == 50


class TestAckErrorSurfaces:
    def test_missing_entry_returns_404(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        stub_manifest_service.get_entry = AsyncMock(return_value=None)
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/agent-target/mid-missing/ack",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 404, r.text
        body = r.json()
        assert body["error_code"] == "manifest_entry_not_found"
        stub_manifest_service.mark_acked.assert_not_awaited()
        stub_escrow_provider.release_partial.assert_not_awaited()

    def test_entry_without_fee_returns_400_not_locked(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        stub_manifest_service.get_entry = AsyncMock(
            return_value=_entry_without_fee("mid-no-fee")
        )
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/agent-target/mid-no-fee/ack",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 400, r.text
        assert r.json()["error_code"] == "attention_fee_not_locked"
        stub_escrow_provider.release_partial.assert_not_awaited()

    def test_replay_ack_returns_400_already_acked(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        stub_manifest_service.mark_acked = AsyncMock(
            side_effect=AlreadyAckedError(owner_id="agent-target", mid="mid-aaa")
        )
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/agent-target/mid-aaa/ack",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 400, r.text
        assert r.json()["error_code"] == "attention_fee_already_acked"
        # Critical: when ack is a replay we MUST NOT issue a second
        # release_partial — that would let any attacker double-release
        # by replaying the request.
        stub_escrow_provider.release_partial.assert_not_awaited()

    def test_release_failure_rolls_back_acked_at(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        stub_escrow_provider.release_partial = AsyncMock(
            return_value=ReleaseResult(success=False, error="escrow already released")
        )
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/agent-target/mid-aaa/ack",
                headers={"Authorization": "Bearer owner-key"},
            )
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error_code"] == "attention_fee_release_failed"
        assert body["details"]["reason"] == "escrow already released"
        # acked_at rollback was attempted (via the ``unmark_acked``
        # service method, not direct Redis access) so the SDK can
        # retry without tripping ATTENTION_FEE_ALREADY_ACKED on the
        # next attempt.
        stub_manifest_service.unmark_acked.assert_awaited_once_with(
            owner_id="agent-target", mid="mid-aaa"
        )


class TestDeleteWithAttentionFee:
    """DELETE /communication/manifest/{agent_id}/{mid} on paid entries.

    The recipient declining a paid manifest entry MUST refund the
    locked escrow back to the sender. Without this guarantee, a
    receiver could DELETE every paid message they received and the
    sender's funds would sit locked until the operator intervened —
    breaking the locked-or-released contract attention_fee rests
    on.
    """

    def test_delete_with_fee_refunds_escrow_then_deletes(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/communication/manifest/agent-target/mid-aaa",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] is True
        assert body["attention_fee"]["escrow_id"] == "esc_abc123"
        assert body["attention_fee"]["refunded"] is True

        # Refund-first ordering: refund_v2 must be called before
        # delete to avoid orphaning the escrow.
        stub_escrow_provider.refund_v2.assert_awaited_once()
        stub_manifest_service.delete.assert_awaited_once()
        kwargs = stub_escrow_provider.refund_v2.await_args.kwargs
        assert kwargs["escrow_id"] == "esc_abc123"

    def test_delete_without_fee_skips_refund(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        """Backward-compat: free manifest entries still delete in a
        single call — no refund_v2 round-trip even when the escrow
        provider is wired in."""
        stub_manifest_service.get_entry = AsyncMock(
            return_value=_entry_without_fee("mid-free")
        )
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/communication/manifest/agent-target/mid-free",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] is True
        assert "attention_fee" not in body
        stub_escrow_provider.refund_v2.assert_not_awaited()
        stub_manifest_service.delete.assert_awaited_once()

    def test_delete_after_ack_skips_refund(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        """If the recipient already acked (funds released), DELETE
        is just cleanup. We must NOT refund — the funds are long
        gone, and the backend would 4xx on the second
        state-transition request anyway."""
        already_acked = _entry_with_fee("mid-aaa")
        already_acked = ManifestEntry(
            mid=already_acked.mid,
            sender_id=already_acked.sender_id,
            summary=already_acked.summary,
            ts_ms=already_acked.ts_ms,
            content_size=already_acked.content_size,
            extra=already_acked.extra,
            acked_at_ms=1_700_000_999_000,
        )
        stub_manifest_service.get_entry = AsyncMock(return_value=already_acked)
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/communication/manifest/agent-target/mid-aaa",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] is True
        assert "attention_fee" not in body
        stub_escrow_provider.refund_v2.assert_not_awaited()
        stub_manifest_service.delete.assert_awaited_once()

    def test_refund_failure_aborts_delete(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        """If the refund fails, the manifest row MUST stay put.
        Otherwise we lose the escrow_id (and thus the ability to
        retry the refund) and the funds get stuck.
        """
        stub_escrow_provider.refund_v2 = AsyncMock(
            return_value=EscrowDetailResult(
                success=False,
                error="escrow already in REFUNDED state",
            )
        )
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )

        with TestClient(app) as client:
            r = client.delete(
                "/api/v1/communication/manifest/agent-target/mid-aaa",
                headers={"Authorization": "Bearer owner-key"},
            )

        assert r.status_code == 400, r.text
        body = r.json()
        assert body["error_code"] == "attention_fee_release_failed"
        assert body["details"]["operation"] == "refund"
        assert "already in REFUNDED state" in body["details"]["reason"]
        # Crucial: delete MUST NOT have run.
        stub_manifest_service.delete.assert_not_awaited()


class TestAckAuth:
    def test_anonymous_returns_401(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/agent-target/mid-aaa/ack"
            )
        assert r.status_code == 401, r.text
        stub_manifest_service.mark_acked.assert_not_awaited()
        stub_escrow_provider.release_partial.assert_not_awaited()

    def test_other_agent_cannot_ack_target_queue(
        self, stub_manifest_service, stub_agent_service, stub_escrow_provider
    ):
        """OwnerOrInternalDep guarantee: a valid key for a different
        agent must 403, else any authenticated agent could harvest
        every other agent's locked attention fees."""

        async def _by_api_key(key: str):
            other = MagicMock()
            other.agent_id = "agent-other"
            other.name = "Other"
            other.wallet_address = None
            return other if key == "other-key" else None

        stub_agent_service.get_agent_by_api_key = AsyncMock(side_effect=_by_api_key)
        _wire(
            manifest=stub_manifest_service,
            agent=stub_agent_service,
            escrow=stub_escrow_provider,
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/v1/communication/manifest/agent-target/mid-aaa/ack",
                headers={"Authorization": "Bearer other-key"},
            )
        assert r.status_code == 403, r.text
        stub_manifest_service.mark_acked.assert_not_awaited()
        stub_escrow_provider.release_partial.assert_not_awaited()
