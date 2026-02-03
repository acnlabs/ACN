"""
ACN Payment Webhook Service

Sends payment event notifications to external backends.
This allows ACN to remain decoupled while integrating with
platform-specific payment systems (like PlatformBillingEngine).

Events:
- payment_task.created: New payment task created
- payment_task.payment_pending: Awaiting payment
- payment_task.payment_confirmed: Payment received
- payment_task.task_completed: Task finished
- payment_task.disputed: Payment disputed
- payment_task.refunded: Payment refunded
- payment_task.cancelled: Task cancelled
"""

import asyncio
import hashlib
import hmac
import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, Field
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class WebhookEventType(str, Enum):
    """Webhook event types for payments and tasks"""

    # ===== Payment Task Events (AP2) =====

    # Payment task lifecycle
    PAYMENT_TASK_CREATED = "payment_task.created"
    PAYMENT_TASK_UPDATED = "payment_task.updated"
    PAYMENT_TASK_CANCELLED = "payment_task.cancelled"

    # Payment lifecycle
    PAYMENT_PENDING = "payment_task.payment_pending"
    PAYMENT_CONFIRMED = "payment_task.payment_confirmed"
    PAYMENT_FAILED = "payment_task.payment_failed"

    # Payment task completion
    PAYMENT_TASK_IN_PROGRESS = "payment_task.in_progress"
    PAYMENT_TASK_COMPLETED = "payment_task.completed"

    # Disputes
    DISPUTED = "payment_task.disputed"
    REFUNDED = "payment_task.refunded"

    # ===== Generic Task Events (Task Pool) =====

    # Task lifecycle
    TASK_CREATED = "task.created"
    TASK_ACCEPTED = "task.accepted"
    TASK_SUBMITTED = "task.submitted"
    TASK_COMPLETED = "task.completed"
    TASK_REJECTED = "task.rejected"
    TASK_CANCELLED = "task.cancelled"

    # Backward compatibility aliases
    # These map old names to new values for existing code
    @classmethod
    def _missing_(cls, value):
        """Handle old event names for backward compatibility"""
        # Map old names to new
        compat_map = {
            "payment_task.created": cls.PAYMENT_TASK_CREATED,
            "payment_task.updated": cls.PAYMENT_TASK_UPDATED,
            "payment_task.cancelled": cls.PAYMENT_TASK_CANCELLED,
            "payment_task.in_progress": cls.PAYMENT_TASK_IN_PROGRESS,
            "payment_task.completed": cls.PAYMENT_TASK_COMPLETED,
        }
        return compat_map.get(value)


class WebhookPayload(BaseModel):
    """Webhook payload structure"""

    event: WebhookEventType
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    task_id: str
    data: dict[str, Any]

    # Optional context
    buyer_agent: str | None = None
    seller_agent: str | None = None
    amount: str | None = None
    currency: str | None = None
    payment_method: str | None = None


class WebhookDelivery(BaseModel):
    """Record of a webhook delivery attempt"""

    id: str
    payload: WebhookPayload
    url: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    delivered_at: str | None = None
    status: str = "pending"  # pending, delivered, failed
    response_code: int | None = None
    response_body: str | None = None
    attempts: int = 0
    last_error: str | None = None


class WebhookConfig(BaseModel):
    """Webhook configuration"""

    url: str
    secret: str | None = None
    timeout: int = 30
    retry_count: int = 3
    retry_delay: int = 5
    enabled: bool = True

    # Event filters (empty = all events)
    events: list[WebhookEventType] = Field(default_factory=list)


class WebhookService:
    """
    Manages webhook delivery for payment events.

    Features:
    - HMAC signature for security
    - Automatic retries with exponential backoff
    - Delivery history tracking
    - Multiple webhook endpoints support
    """

    def __init__(self, redis: Redis, default_config: WebhookConfig | None = None):
        self.redis = redis
        self.default_config = default_config
        self._http_client: httpx.AsyncClient | None = None

    async def start(self):
        """Start the webhook service"""
        self._http_client = httpx.AsyncClient(timeout=30)
        logger.info("WebhookService started")

    async def stop(self):
        """Stop the webhook service"""
        if self._http_client:
            await self._http_client.aclose()
        logger.info("WebhookService stopped")

    def _sign_payload(self, payload: str, secret: str) -> str:
        """Create HMAC-SHA256 signature for payload"""
        return hmac.new(
            secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def send_event(
        self,
        event: WebhookEventType,
        task_id: str,
        data: dict[str, Any],
        buyer_agent: str | None = None,
        seller_agent: str | None = None,
        amount: str | None = None,
        currency: str | None = None,
        payment_method: str | None = None,
    ) -> bool:
        """
        Send a webhook event to configured endpoints.

        Returns True if delivered successfully (or no webhook configured).
        """
        if not self.default_config or not self.default_config.enabled:
            logger.debug(f"Webhook not configured, skipping event: {event}")
            return True

        # Check event filter
        if self.default_config.events and event not in self.default_config.events:
            logger.debug(f"Event {event} not in filter, skipping")
            return True

        payload = WebhookPayload(
            event=event,
            task_id=task_id,
            data=data,
            buyer_agent=buyer_agent,
            seller_agent=seller_agent,
            amount=amount,
            currency=currency,
            payment_method=payment_method,
        )

        return await self._deliver_webhook(payload, self.default_config)

    async def _deliver_webhook(
        self,
        payload: WebhookPayload,
        config: WebhookConfig,
    ) -> bool:
        """Deliver webhook with retries"""
        if not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=config.timeout)

        delivery_id = f"wh_{payload.task_id}_{payload.event.value}_{datetime.now(UTC).timestamp()}"
        payload_json = payload.model_dump_json()

        # Build headers
        headers = {
            "Content-Type": "application/json",
            "X-ACN-Webhook-ID": delivery_id,
            "X-ACN-Event": payload.event.value,
            "X-ACN-Timestamp": payload.timestamp,
        }

        # Add signature if secret configured
        if config.secret:
            signature = self._sign_payload(payload_json, config.secret)
            headers["X-ACN-Signature"] = f"sha256={signature}"

        # Delivery record
        delivery = WebhookDelivery(
            id=delivery_id,
            payload=payload,
            url=config.url,
        )

        # Try delivery with retries
        for attempt in range(config.retry_count):
            delivery.attempts = attempt + 1

            try:
                response = await self._http_client.post(
                    config.url,
                    content=payload_json,
                    headers=headers,
                    timeout=config.timeout,
                )

                delivery.response_code = response.status_code
                delivery.response_body = response.text[:500]  # Truncate

                if response.is_success:
                    delivery.status = "delivered"
                    delivery.delivered_at = datetime.now(UTC).isoformat()
                    await self._save_delivery(delivery)
                    logger.info(f"Webhook delivered: {delivery_id} -> {config.url}")
                    return True

                delivery.last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(f"Webhook failed (attempt {attempt + 1}): {delivery.last_error}")

            except httpx.TimeoutException:
                delivery.last_error = "Request timeout"
                logger.warning(f"Webhook timeout (attempt {attempt + 1}): {config.url}")

            except httpx.RequestError as e:
                delivery.last_error = str(e)
                logger.warning(f"Webhook error (attempt {attempt + 1}): {e}")

            # Wait before retry (exponential backoff)
            if attempt < config.retry_count - 1:
                delay = config.retry_delay * (2**attempt)
                await asyncio.sleep(delay)

        # All retries failed
        delivery.status = "failed"
        await self._save_delivery(delivery)
        logger.error(f"Webhook failed after {config.retry_count} attempts: {delivery_id}")
        return False

    async def _save_delivery(self, delivery: WebhookDelivery):
        """Save delivery record to Redis"""
        key = f"acn:webhooks:deliveries:{delivery.id}"
        await self.redis.set(key, delivery.model_dump_json(), ex=86400 * 7)  # 7 days

        # Add to list for querying
        list_key = f"acn:webhooks:history:{delivery.payload.task_id}"
        await self.redis.lpush(list_key, delivery.id)
        await self.redis.ltrim(list_key, 0, 99)  # Keep last 100
        await self.redis.expire(list_key, 86400 * 7)

    async def get_delivery_history(
        self,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[WebhookDelivery]:
        """Get webhook delivery history"""
        if task_id:
            list_key = f"acn:webhooks:history:{task_id}"
            delivery_ids = await self.redis.lrange(list_key, 0, limit - 1)
        else:
            # Get recent deliveries across all tasks
            pattern = "acn:webhooks:deliveries:*"
            keys = []
            async for key in self.redis.scan_iter(pattern, count=limit):
                keys.append(key)
                if len(keys) >= limit:
                    break
            delivery_ids = [k.split(":")[-1] for k in keys]

        deliveries = []
        for did in delivery_ids:
            if isinstance(did, bytes):
                did = did.decode()
            key = f"acn:webhooks:deliveries:{did}"
            data = await self.redis.get(key)
            if data:
                deliveries.append(WebhookDelivery.model_validate_json(data))

        return deliveries

    async def retry_failed_delivery(self, delivery_id: str) -> bool:
        """Retry a failed webhook delivery"""
        key = f"acn:webhooks:deliveries:{delivery_id}"
        data = await self.redis.get(key)

        if not data:
            raise ValueError(f"Delivery not found: {delivery_id}")

        delivery = WebhookDelivery.model_validate_json(data)

        if delivery.status != "failed":
            raise ValueError(f"Delivery is not failed: {delivery.status}")

        if not self.default_config:
            raise ValueError("No webhook configured")

        # Reset and retry
        delivery.status = "pending"
        delivery.attempts = 0
        return await self._deliver_webhook(delivery.payload, self.default_config)


# Convenience function for creating webhook config from settings
def create_webhook_config_from_settings(settings) -> WebhookConfig | None:
    """Create WebhookConfig from ACN Settings"""
    if not settings.webhook_url:
        return None

    return WebhookConfig(
        url=settings.webhook_url,
        secret=settings.webhook_secret,
        timeout=settings.webhook_timeout,
        retry_count=settings.webhook_retry_count,
        retry_delay=settings.webhook_retry_delay,
    )
