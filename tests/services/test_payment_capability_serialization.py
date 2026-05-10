"""Regression tests for the ``PaymentCapability`` ↔ ``PaymentCapabilityRequest``
field-name asymmetry.

Background:
- Write side (``POST /api/v1/payments/{id}/payment-capability``) uses
  ``supported_methods`` in the request body.
- Read side (``GET /api/v1/payments/{id}/payment-capability``) returns
  the ``PaymentCapability`` model whose canonical field is
  ``payment_methods``.

That mismatch forces every SDK to ship a translation layer. We
neutralize it by exposing ``supported_methods`` as a computed alias on
the response model so both names are emitted side by side. Storage and
internal logic still use ``payment_methods``.
"""

from __future__ import annotations

from acn.protocols.ap2.core import (
    PaymentCapability,
    SupportedNetwork,
    SupportedPaymentMethod,
)


def _make_capability() -> PaymentCapability:
    return PaymentCapability(
        accepts_payment=True,
        payment_methods=[
            SupportedPaymentMethod.USDC,
            SupportedPaymentMethod.PLATFORM_CREDITS,
        ],
        supported_networks=[SupportedNetwork.BASE],
        wallet_address="0xabc",
    )


def test_model_dump_emits_both_aliases() -> None:
    capability = _make_capability()
    dumped = capability.model_dump()

    assert "payment_methods" in dumped, "canonical field must remain"
    assert "supported_methods" in dumped, "alias must be present for readers"
    assert dumped["payment_methods"] == dumped["supported_methods"]
    assert dumped["payment_methods"] == [
        SupportedPaymentMethod.USDC,
        SupportedPaymentMethod.PLATFORM_CREDITS,
    ]


def test_model_dump_json_emits_both_aliases() -> None:
    capability = _make_capability()
    payload = capability.model_dump_json()

    # We don't care about ordering — just that both keys ship to the wire.
    assert '"payment_methods"' in payload
    assert '"supported_methods"' in payload


def test_round_trip_keeps_payment_methods_intact() -> None:
    """Re-loading a previously persisted JSON (which contains both
    fields after the upgrade) must not double-store or cross-contaminate.
    """
    original = _make_capability()
    payload = original.model_dump_json()

    reloaded = PaymentCapability.model_validate_json(payload)

    assert reloaded.payment_methods == original.payment_methods
    assert reloaded.supported_methods == original.payment_methods


def test_legacy_payload_without_supported_methods_still_loads() -> None:
    """Pre-upgrade Redis rows have only ``payment_methods``. They must
    keep validating cleanly, with ``supported_methods`` materialized at
    serialization time.
    """
    legacy = {
        "accepts_payment": True,
        "payment_methods": ["usdc"],
        "supported_networks": ["base"],
        "wallet_address": "0xabc",
        "wallet_addresses": {},
        "default_currency": "USD",
        "pricing": {},
    }

    capability = PaymentCapability.model_validate(legacy)

    assert capability.payment_methods == [SupportedPaymentMethod.USDC]
    assert capability.supported_methods == [SupportedPaymentMethod.USDC]
    assert "supported_methods" in capability.model_dump()


def test_extra_supported_methods_on_input_is_ignored_not_stored() -> None:
    """If a client (or a future-us re-load) hands us a JSON that already
    contains ``supported_methods``, the computed-field alias must remain
    a one-way derivation — we should not silently accept a divergent
    value as if it were authoritative.
    """
    payload = {
        "accepts_payment": True,
        "payment_methods": ["usdc"],
        # Intentionally divergent — must NOT win.
        "supported_methods": ["platform_credits"],
        "supported_networks": ["base"],
    }

    capability = PaymentCapability.model_validate(payload)

    assert capability.payment_methods == [SupportedPaymentMethod.USDC]
    assert capability.supported_methods == [SupportedPaymentMethod.USDC]
