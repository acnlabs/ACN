"""Phase 3 attention_fee — service & dispatcher contract tests.

The economic-model PR adds a "sender pays for the recipient's
attention" flow on top of the manifest queue. This file pins the
*service* layer:

* ``ManifestService.mark_acked`` is HSETNX-driven and idempotent on
  the recipient side — the second ack on the same mid raises
  ``AlreadyAckedError`` instead of double-releasing.
* ``ManifestService.get_entry`` exposes the new ``acked_at_ms``
  field so the route layer / list endpoint can colour-code unread
  entries.
* ``ManifestDispatcher.dispatch`` locks ``attention_fee`` in escrow
  *before* persisting the manifest entry, surfaces lock failures as
  ``AttentionFeeLockError``, and wires the escrow_id back into the
  manifest entry's ``extra`` payload so the ack endpoint can drive
  the release.

Route-level wiring (``POST /communication/send`` →
``AttentionFeeWrongModeError`` → 4xx, ``POST .../ack`` →
``ATTENTION_FEE_*`` codes) is covered separately in
``tests/routes/test_attention_fee_routes.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.compat.v0_3.types import (  # type: ignore[import-untyped]
    Message,
    Part,
    Role,
    TextPart,
)

from acn.core.interfaces.escrow_provider import EscrowDetailResult
from acn.infrastructure.messaging.manifest_dispatcher import (
    AttentionFeeLockError,
    ManifestDispatcher,
)
from acn.services.manifest_service import (
    AlreadyAckedError,
    ManifestService,
)

OWNER = "agent-bob"
SENDER = "agent-alice"


@pytest.fixture
async def manifest_service(manifest_redis):
    return ManifestService(manifest_redis)


def _make_text_message(text: str) -> Message:
    return Message(
        role=Role.user,
        message_id="msg-1",
        parts=[Part(root=TextPart(text=text))],
    )


# ---------------------------------------------------------------------------
# 1. ManifestService.mark_acked — HSETNX-driven idempotency
# ---------------------------------------------------------------------------


class TestMarkAcked:
    """``mark_acked`` flips ``acked_at`` exactly once per manifest entry."""

    @pytest.mark.asyncio
    async def test_first_ack_returns_timestamp(self, manifest_service):
        entry = await manifest_service.write(
            owner_id=OWNER,
            sender_id=SENDER,
            summary="hi",
            content={"text": "hi"},
            extra={"attention_fee": {"escrow_id": "esc_x", "amount": 50}},
        )

        ts = await manifest_service.mark_acked(owner_id=OWNER, mid=entry.mid)

        assert ts is not None and ts > 0
        # Re-reading the entry should reflect the acked_at field.
        refreshed = await manifest_service.get_entry(owner_id=OWNER, mid=entry.mid)
        assert refreshed is not None
        assert refreshed.acked_at_ms == ts

    @pytest.mark.asyncio
    async def test_second_ack_raises_already_acked(self, manifest_service):
        entry = await manifest_service.write(
            owner_id=OWNER,
            sender_id=SENDER,
            summary="hi",
            content={"text": "hi"},
            extra={"attention_fee": {"escrow_id": "esc_x", "amount": 50}},
        )
        await manifest_service.mark_acked(owner_id=OWNER, mid=entry.mid)

        with pytest.raises(AlreadyAckedError) as exc:
            await manifest_service.mark_acked(owner_id=OWNER, mid=entry.mid)
        assert exc.value.owner_id == OWNER
        assert exc.value.mid == entry.mid

    @pytest.mark.asyncio
    async def test_ack_on_missing_entry_returns_none_and_rolls_back(
        self, manifest_service, manifest_redis
    ):
        """Cold path: HSETNX would create a degenerate one-field hash on
        a missing key. We detect the lack of ``mid`` field and roll
        back so ``read_since`` doesn't surface ghost entries."""
        result = await manifest_service.mark_acked(
            owner_id=OWNER, mid="nonexistent-mid-1234567890abcdef12345678"
        )
        assert result is None
        # The detail key must not survive the cold-path call.
        leftover = await manifest_redis.exists(
            f"acn:manifest:{{{OWNER}}}:nonexistent-mid-1234567890abcdef12345678"
        )
        assert int(leftover) == 0


# ---------------------------------------------------------------------------
# 2. ManifestService.get_entry — surfaces ``acked_at_ms``
# ---------------------------------------------------------------------------


class TestGetEntry:
    @pytest.mark.asyncio
    async def test_get_entry_unacked(self, manifest_service):
        entry = await manifest_service.write(
            owner_id=OWNER,
            sender_id=SENDER,
            summary="hi",
            content={"k": "v"},
        )
        loaded = await manifest_service.get_entry(owner_id=OWNER, mid=entry.mid)
        assert loaded is not None
        assert loaded.mid == entry.mid
        assert loaded.acked_at_ms is None

    @pytest.mark.asyncio
    async def test_get_entry_returns_none_for_missing(self, manifest_service):
        loaded = await manifest_service.get_entry(
            owner_id=OWNER, mid="0" * 32
        )
        assert loaded is None


# ---------------------------------------------------------------------------
# 3. ManifestDispatcher — attention_fee lock before write
# ---------------------------------------------------------------------------


class TestDispatchAttentionFee:
    @pytest.fixture
    def stub_escrow(self) -> MagicMock:
        escrow = MagicMock()
        escrow.lock_v2 = AsyncMock(
            return_value=EscrowDetailResult(
                success=True,
                escrow_id="esc_abc123",
                task_id="acn:attn:cafebabecafebabe",
                status="locked",
                total_amount=50.0,
            )
        )
        return escrow

    @pytest.mark.asyncio
    async def test_no_attention_fee_skips_escrow_call(
        self, manifest_service, stub_escrow
    ):
        dispatcher = ManifestDispatcher(
            manifest_service=manifest_service,
            escrow_provider=stub_escrow,
        )
        await dispatcher.dispatch(
            owner_id=OWNER,
            sender_id=SENDER,
            message=_make_text_message("hello"),
            path="router",
        )
        stub_escrow.lock_v2.assert_not_called()

    @pytest.mark.asyncio
    async def test_attention_fee_locks_escrow_and_persists_extra(
        self, manifest_service, stub_escrow
    ):
        dispatcher = ManifestDispatcher(
            manifest_service=manifest_service,
            escrow_provider=stub_escrow,
        )
        entry = await dispatcher.dispatch(
            owner_id=OWNER,
            sender_id=SENDER,
            message=_make_text_message("paid msg"),
            path="router",
            attention_fee={"amount": 50, "currency": "credits"},
        )

        # Escrow lock invoked with the correct shape.
        stub_escrow.lock_v2.assert_awaited_once()
        kwargs = stub_escrow.lock_v2.await_args.kwargs
        assert kwargs["creator_id"] == SENDER
        assert kwargs["creator_type"] == "agent"
        assert kwargs["amount"] == 50
        assert kwargs["currency"] == "credits"
        assert kwargs["task_id"].startswith("acn:attn:")
        # auto_release_days is the buffer beyond manifest TTL — at least
        # one day past the default 7-day TTL, so 8.
        assert kwargs["auto_release_days"] >= 8

        # Manifest entry's extra carries the locked escrow id back to
        # the recipient so the ack endpoint can release without an
        # extra backend lookup.
        assert entry.extra["attention_fee"]["escrow_id"] == "esc_abc123"
        assert entry.extra["attention_fee"]["amount"] == 50
        assert entry.extra["attention_fee"]["currency"] == "credits"
        assert entry.extra["attention_fee"]["task_id"].startswith("acn:attn:")

    @pytest.mark.asyncio
    async def test_lock_failure_raises_and_skips_manifest_write(
        self, manifest_service, stub_escrow
    ):
        """The whole point of locking before writing: a failed lock
        leaves no manifest row + no WS notification."""
        stub_escrow.lock_v2 = AsyncMock(
            return_value=EscrowDetailResult(success=False, error="insufficient balance")
        )
        dispatcher = ManifestDispatcher(
            manifest_service=manifest_service,
            escrow_provider=stub_escrow,
        )
        with pytest.raises(AttentionFeeLockError) as exc:
            await dispatcher.dispatch(
                owner_id=OWNER,
                sender_id=SENDER,
                message=_make_text_message("paid msg"),
                path="router",
                attention_fee={"amount": 50, "currency": "credits"},
            )
        assert "insufficient balance" in exc.value.reason

        # No manifest entry was written for OWNER.
        entries = await manifest_service.read_since(OWNER)
        assert entries == []

    @pytest.mark.asyncio
    async def test_attention_fee_without_escrow_provider_raises(
        self, manifest_service
    ):
        """Configuration guard — sending an attention_fee against a
        dispatcher without a wired escrow provider must fail loudly,
        not silently drop the lock."""
        dispatcher = ManifestDispatcher(
            manifest_service=manifest_service,
            escrow_provider=None,
        )
        with pytest.raises(AttentionFeeLockError) as exc:
            await dispatcher.dispatch(
                owner_id=OWNER,
                sender_id=SENDER,
                message=_make_text_message("paid msg"),
                path="router",
                attention_fee={"amount": 50},
            )
        assert "escrow provider not wired" in exc.value.reason
