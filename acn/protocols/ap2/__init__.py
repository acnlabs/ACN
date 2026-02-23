"""
ACN Payments Integration

Integrates AP2 (Agent Payments Protocol) with ACN's unique value-add:

1. Payment Discovery - Find agents by payment capability (ACN unique)
2. A2A + AP2 Fusion - Combine task messages with payment requests (ACN unique)
3. Payment Tracking - Track payment status across agent interactions (ACN unique)
4. Transaction Audit - Record all payment-related events (ACN unique)

AP2 is the official payment protocol for A2A (Agent-to-Agent Protocol),
developed by Google and 60+ companies including Coinbase, PayPal, Mastercard,
and Ethereum Foundation.

Architecture:
    ┌─────────────────────────────────────────────────────────┐
    │  ACN Payments Layer                                      │
    │                                                          │
    │  ACN's Unique Value (not in AP2):                       │
    │  ├─ PaymentDiscoveryService: Find agents by payment     │
    │  │   "Find all agents accepting USDC on Base network"   │
    │  │                                                       │
    │  ├─ PaymentTaskManager: A2A + AP2 fusion               │
    │  │   One message for task request + payment request     │
    │  │                                                       │
    │  ├─ Payment Status Tracking                             │
    │  │   Track task + payment lifecycle together            │
    │  │                                                       │
    │  └─ Transaction Audit                                   │
    │      Full audit trail for compliance                    │
    │                                                          │
    │  AP2 Types (re-exported):                               │
    │  ├─ PaymentRequest, PaymentResponse                     │
    │  └─ PaymentReceipt, Success, Failure                    │
    └─────────────────────────────────────────────────────────┘

Usage:
    from acn.protocols.ap2 import (
        # ACN's unique services
        PaymentDiscoveryService,
        PaymentTaskManager,
        PaymentCapability,
        PaymentTask,
        # AP2 types
        PaymentRequest,
        PaymentResponse,
    )

    # Find agents that accept USDC
    agents = await discovery.find_agents_accepting_payment(
        payment_method=SupportedPaymentMethod.USDC,
        network=SupportedNetwork.BASE,
    )

    # Create a payment task (A2A + AP2 fusion)
    task = await task_manager.create_payment_task(
        buyer_agent="buyer-agent",
        seller_agent="seller-agent",
        task_description="Write a Python script",
        amount="50.00",
        currency="USDC",
    )
    # Automatically:
    # - Resolves seller's wallet from ACN Registry
    # - Creates audit log
    # - Tracks payment status

Resources:
    - AP2 Protocol: https://github.com/google-agentic-commerce/AP2
    - A2A Protocol: https://github.com/a2aproject/A2A
"""

# Re-export AP2 types when available
try:
    from ap2.types.payment_receipt import (  # type: ignore[import-untyped]
        Failure,
        PaymentReceipt,
        Success,
    )
    from ap2.types.payment_request import (  # type: ignore[import-untyped]
        PaymentCurrencyAmount,
        PaymentDetailsInit,
        PaymentItem,
        PaymentMethodData,
        PaymentOptions,
        PaymentRequest,
        PaymentResponse,
    )

    AP2_AVAILABLE = True
except ImportError:
    AP2_AVAILABLE = False
    PaymentRequest = None  # type: ignore
    PaymentResponse = None  # type: ignore
    PaymentReceipt = None  # type: ignore
    PaymentCurrencyAmount = None  # type: ignore
    PaymentItem = None  # type: ignore
    PaymentMethodData = None  # type: ignore
    PaymentOptions = None  # type: ignore
    PaymentDetailsInit = None  # type: ignore
    Success = None  # type: ignore
    Failure = None  # type: ignore

# ACN's unique payment services
from .core import (
    # Constants
    CREDITS_PER_USD,
    NETWORK_FEE_RATE,
    # Models
    PaymentCapability,
    # Services (ACN unique value)
    PaymentDiscoveryService,
    PaymentTask,
    PaymentTaskManager,
    PaymentTaskStatus,
    SupportedNetwork,
    SupportedPaymentMethod,
    TokenPricing,
    # Helpers
    create_payment_capability,
)

# Webhook for backend integration (e.g., PlatformBillingEngine)
from .webhook import (
    WebhookConfig,
    WebhookDelivery,
    WebhookEventType,
    WebhookPayload,
    WebhookService,
    create_webhook_config_from_settings,
)

__all__ = [
    # AP2 types (re-exported)
    "PaymentRequest",
    "PaymentResponse",
    "PaymentReceipt",
    "PaymentCurrencyAmount",
    "PaymentItem",
    "PaymentMethodData",
    "PaymentOptions",
    "PaymentDetailsInit",
    "Success",
    "Failure",
    # Constants
    "CREDITS_PER_USD",
    "NETWORK_FEE_RATE",
    # ACN Services (unique value)
    "PaymentDiscoveryService",
    "PaymentTaskManager",
    # ACN Models
    "PaymentCapability",
    "PaymentTask",
    "PaymentTaskStatus",
    "SupportedPaymentMethod",
    "SupportedNetwork",
    "TokenPricing",
    # Helpers
    "create_payment_capability",
    # Webhook (backend integration)
    "WebhookService",
    "WebhookConfig",
    "WebhookEventType",
    "WebhookPayload",
    "WebhookDelivery",
    "create_webhook_config_from_settings",
    # Flag
    "AP2_AVAILABLE",
]
