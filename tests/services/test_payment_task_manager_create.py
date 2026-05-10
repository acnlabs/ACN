"""Regression tests for PaymentTaskManager.create_payment_task.

Buyer-supplied ``network`` must be honored end-to-end. Previously the
manager hardcoded ``capability.supported_networks[0]`` and silently
dropped the buyer's choice — a buyer asking for ``base`` would end up
on whichever network the seller had listed first (typically
``ethereum``), which is a correctness bug for multi-chain sellers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from acn.protocols.ap2.core import (
    PaymentCapability,
    PaymentTaskManager,
    SupportedNetwork,
    SupportedPaymentMethod,
)


def _make_capability(
    *,
    networks: list[SupportedNetwork],
    wallet_addresses: dict[str, str] | None = None,
) -> PaymentCapability:
    return PaymentCapability(
        accepts_payment=True,
        payment_methods=[SupportedPaymentMethod.USDC],
        wallet_address="0xfallback",
        wallet_addresses=wallet_addresses or {},
        supported_networks=networks,
    )


def _make_mgr(capability: PaymentCapability) -> PaymentTaskManager:
    discovery = AsyncMock()
    discovery.get_agent_payment_capability = AsyncMock(return_value=capability)
    fake_redis = AsyncMock()
    return PaymentTaskManager(redis=fake_redis, discovery=discovery)


@pytest.mark.asyncio
async def test_create_uses_buyer_supplied_network() -> None:
    capability = _make_capability(
        networks=[SupportedNetwork.ETHEREUM, SupportedNetwork.BASE],
        wallet_addresses={
            SupportedNetwork.ETHEREUM.value: "0xeth",
            SupportedNetwork.BASE.value: "0xbase",
        },
    )
    mgr = _make_mgr(capability)

    task = await mgr.create_payment_task(
        buyer_agent="buyer-1",
        seller_agent="seller-1",
        task_description="ship it",
        amount="1.00",
        payment_method=SupportedPaymentMethod.USDC,
        network=SupportedNetwork.BASE,
    )

    assert task.network == SupportedNetwork.BASE
    assert task.recipient_wallet == "0xbase"


@pytest.mark.asyncio
async def test_create_falls_back_to_first_network_when_omitted() -> None:
    """No regression: if the buyer omits ``network`` we keep the legacy
    "first declared network wins" behavior so older clients keep working.
    """
    capability = _make_capability(
        networks=[SupportedNetwork.ETHEREUM, SupportedNetwork.BASE],
        wallet_addresses={
            SupportedNetwork.ETHEREUM.value: "0xeth",
            SupportedNetwork.BASE.value: "0xbase",
        },
    )
    mgr = _make_mgr(capability)

    task = await mgr.create_payment_task(
        buyer_agent="buyer-1",
        seller_agent="seller-1",
        task_description="ship it",
        amount="1.00",
        payment_method=SupportedPaymentMethod.USDC,
    )

    assert task.network == SupportedNetwork.ETHEREUM
    assert task.recipient_wallet == "0xeth"


@pytest.mark.asyncio
async def test_create_rejects_network_not_supported_by_seller() -> None:
    capability = _make_capability(
        networks=[SupportedNetwork.ETHEREUM],
        wallet_addresses={SupportedNetwork.ETHEREUM.value: "0xeth"},
    )
    mgr = _make_mgr(capability)

    with pytest.raises(ValueError, match="does not accept network"):
        await mgr.create_payment_task(
            buyer_agent="buyer-1",
            seller_agent="seller-1",
            task_description="ship it",
            amount="1.00",
            payment_method=SupportedPaymentMethod.USDC,
            network=SupportedNetwork.SOLANA,
        )


@pytest.mark.asyncio
async def test_create_falls_back_to_legacy_wallet_when_per_network_missing() -> None:
    """If the seller declared the chosen network but never registered a
    per-network address for it, we should still produce a task using the
    legacy single ``wallet_address`` rather than blowing up.
    """
    capability = _make_capability(
        networks=[SupportedNetwork.BASE],
        wallet_addresses={},  # intentionally missing the BASE entry
    )
    mgr = _make_mgr(capability)

    task = await mgr.create_payment_task(
        buyer_agent="buyer-1",
        seller_agent="seller-1",
        task_description="ship it",
        amount="1.00",
        payment_method=SupportedPaymentMethod.USDC,
        network=SupportedNetwork.BASE,
    )

    assert task.network == SupportedNetwork.BASE
    assert task.recipient_wallet == "0xfallback"
