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
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, Field
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Durable outbox (ACN #162) Redis keys.
_OUTBOX_DUE_ZSET = "acn:webhooks:outbox:due"  # score = next-attempt epoch, member = delivery_id
_OUTBOX_ITEM_PREFIX = "acn:webhooks:outbox:item:"  # per-delivery re-drive payload
_OUTBOX_METRIC_PREFIX = "acn:webhooks:metrics:"  # {outcome}:{event} counters


class WebhookEventType(StrEnum):
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
    TASK_INVITED = "task.invited"
    TASK_ACCEPTED = "task.accepted"
    TASK_SUBMITTED = "task.submitted"
    TASK_COMPLETED = "task.completed"
    TASK_REJECTED = "task.rejected"
    TASK_CANCELLED = "task.cancelled"

    # Participation events (multi-participant tasks)
    PARTICIPATION_APPROVED = "participation.approved"
    PARTICIPATION_REJECTED = "participation.rejected"

    # ===== Subnet / Org Harness Events =====

    # Agent ↔ subnet membership lifecycle. Delivered to the subnet's
    # registered ``harness_url`` so external Org Harnesses (Paperclip,
    # OpenHarness, etc.) can initialise or tear down their internal
    # representation of the agent in that organisation.
    AGENT_JOINED_SUBNET = "agent.joined_subnet"
    AGENT_LEFT_SUBNET = "agent.left_subnet"

    # ===== Org Harness Kernel Events (ADR-0014) =====
    # Delivered to the Org's bound subnet ``harness_url`` (same transport
    # as agent.*_subnet). Failures must not break the mutating request.
    ORG_CREATED = "org.created"
    ORG_MEMBER_ADDED = "org.member_added"
    ORG_MEMBER_REMOVED = "org.member_removed"
    ORG_OWNER_CHANGED = "org.owner_changed"
    ORG_DISSOLVED = "org.dissolved"
    ORG_WORK_CREATED = "org.work_created"
    ORG_WORK_UPDATED = "org.work_updated"
    ORG_LOOP_TICK = "org.loop_tick"

    # ===== Ownership Events =====

    # Fired to the platform ``WEBHOOK_URL`` (Backend) whenever an agent's
    # human owner changes — first claim, P3 transfer-invite claim, direct
    # transfer, or release. Backend re-points the agent wallet's
    # ``owner_id`` so the new owner controls top-up/withdraw and the old
    # owner loses access (otherwise the giver could drain the wallet after
    # gifting). Delivered with the durable outbox (at-least-once).
    AGENT_OWNER_CHANGED = "agent.owner_changed"

    # ADR-0004 §"Webhook event catalogue" — eight new join-flow
    # lifecycle events fired through the same ``WebhookService.send_to``
    # transport as the two ``agent.*_subnet`` events above. The string
    # values match ``acn.core.interfaces.join_flow_event_publisher.
    # JoinFlowEventType`` 1-1; the no-drift contract is pinned by
    # ``tests/services/test_join_flow_webhook_enum_mapping.py``.
    #
    # Allowlist add / remove deliberately have **no** webhook entries:
    # ADR §"Webhook event catalogue" notes allowlist mutation is
    # configuration state, not lifecycle. A Harness audit replay reads
    # ``GET /allowlist`` instead.
    SUBNET_JOIN_REQUESTED = "subnet.join_requested"
    SUBNET_JOIN_APPROVED = "subnet.join_approved"
    SUBNET_JOIN_REJECTED = "subnet.join_rejected"
    SUBNET_JOIN_WITHDRAWN = "subnet.join_withdrawn"

    SUBNET_INVITATION_SENT = "subnet.invitation_sent"
    SUBNET_INVITATION_ACCEPTED = "subnet.invitation_accepted"
    SUBNET_INVITATION_REJECTED = "subnet.invitation_rejected"
    SUBNET_INVITATION_CANCELED = "subnet.invitation_canceled"

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


class OutboxItem(BaseModel):
    """A durable, re-drivable webhook delivery parked in Redis (ACN #162).

    Holds everything needed to re-POST the *exact* original body (so the HMAC
    signature still matches and the receiver can dedupe on ``delivery_id``),
    including the target ``secret``. This lives only in the internal Redis store
    — the same trust boundary that already holds the signed payloads and seller
    capabilities — and is never copied into the secret-free delivery history.
    """

    delivery_id: str
    url: str
    secret: str | None = None
    payload: str  # the exact JSON body that was signed/sent
    event: str  # WebhookEventType value, for the X-ACN-Event header
    timestamp: str  # original payload timestamp, for the X-ACN-Timestamp header
    task_id: str
    attempts: int = 0
    first_enqueued_at: float
    next_attempt_at: float
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

    def __init__(
        self,
        redis: Redis,
        default_config: WebhookConfig | None = None,
        *,
        outbox_enabled: bool = True,
        outbox_poll_interval: int = 5,
        outbox_max_age_seconds: int = 86400,
        outbox_max_backoff: int = 600,
    ):
        self.redis = redis
        self.default_config = default_config
        self._http_client: httpx.AsyncClient | None = None
        # Durable outbox (ACN #162)
        self._outbox_enabled = outbox_enabled
        self._outbox_poll_interval = max(1, outbox_poll_interval)
        self._outbox_max_age_seconds = outbox_max_age_seconds
        self._outbox_max_backoff = max(1, outbox_max_backoff)
        self._outbox_task: asyncio.Task[None] | None = None

    async def start(self):
        """Start the webhook service (and the durable-outbox worker)."""
        self._http_client = httpx.AsyncClient(timeout=30, trust_env=False)
        if self._outbox_enabled and self._outbox_task is None:
            self._outbox_task = asyncio.create_task(self._outbox_worker())
        logger.info("WebhookService started (outbox=%s)", self._outbox_enabled)

    async def stop(self):
        """Stop the webhook service and gracefully cancel the outbox worker."""
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
            self._outbox_task = None
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
        outbox: bool = True,
    ) -> bool:
        """
        Send a webhook event to configured endpoints.

        Returns True if delivered successfully (or no webhook configured).

        ``outbox`` (default True) controls durable at-least-once re-driving; see
        :meth:`_deliver_webhook`.
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

        return await self._deliver_webhook(payload, self.default_config, use_outbox=outbox)

    async def send_to(
        self,
        url: str,
        secret: str | None,
        event: WebhookEventType,
        task_id: str,
        data: dict[str, Any],
        *,
        buyer_agent: str | None = None,
        seller_agent: str | None = None,
        amount: str | None = None,
        currency: str | None = None,
        payment_method: str | None = None,
        timeout: int = 30,
        retry_count: int = 3,
        retry_delay: int = 5,
        outbox: bool = True,
    ) -> bool:
        """Deliver a webhook payload to an arbitrary URL.

        This is the per-target counterpart to :meth:`send_event`. Used by
        subnet-scoped Org Harness webhooks where each subnet registers its
        own ``harness_url`` + ``harness_secret``, independent of the
        platform-wide default webhook configured at startup.

        Args:
            url: Target webhook URL
            secret: HMAC-SHA256 secret used to sign the payload. If ``None``,
                no ``X-ACN-Signature`` header is sent.
            event: Webhook event type
            task_id: Task ID (or subnet ID / agent ID for non-task events)
            data: Free-form payload body
            buyer_agent, seller_agent, amount, currency, payment_method:
                Optional payment context, forwarded as top-level fields
            timeout: HTTP timeout in seconds
            retry_count: Number of delivery attempts
            retry_delay: Base delay between retries (exponential backoff)
            outbox: Durable at-least-once re-driving (default True). Pass False
                for fire-and-forget callers (e.g. join-flow / Org-Harness
                lifecycle events) that reconcile out-of-band.

        Returns:
            True if delivered successfully, False after exhausting retries.
        """
        if not url:
            return True  # No target configured → no-op success

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

        config = WebhookConfig(
            url=url,
            secret=secret,
            timeout=timeout,
            retry_count=retry_count,
            retry_delay=retry_delay,
            enabled=True,
        )

        return await self._deliver_webhook(payload, config, use_outbox=outbox)

    async def _deliver_webhook(
        self,
        payload: WebhookPayload,
        config: WebhookConfig,
        *,
        use_outbox: bool = True,
    ) -> bool:
        """Deliver webhook with retries.

        ``use_outbox`` gates the durable outbox (ACN #162). It defaults to True
        (payment/platform webhooks want at-least-once), but callers whose
        delivery semantics are "fire-and-forget, reconcile out-of-band" — e.g.
        subnet / Org-Harness join-flow lifecycle events (ADR-0004) — pass
        ``False`` to keep the historical "fail fast after in-process retries"
        behavior and avoid late/out-of-order re-delivery.
        """
        if not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=config.timeout, trust_env=False)

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

        # In-process retries exhausted. Park it in the durable outbox so a
        # background worker keeps re-driving across restarts (at-least-once);
        # fall back to the old terminal "failed" when the outbox is off OR the
        # caller opted out (use_outbox=False).
        if self._outbox_enabled and use_outbox:
            delivery.status = "queued"
            await self._save_delivery(delivery)
            await self._enqueue_outbox(delivery, config, payload_json)
            logger.warning(
                "Webhook queued to durable outbox after %d attempts: %s -> %s",
                config.retry_count,
                delivery_id,
                config.url,
            )
        else:
            delivery.status = "failed"
            await self._save_delivery(delivery)
            logger.error(f"Webhook failed after {config.retry_count} attempts: {delivery_id}")
        return False

    # ------------------------------------------------------------------ #
    # Durable outbox (ACN #162)                                          #
    # ------------------------------------------------------------------ #

    async def _enqueue_outbox(
        self,
        delivery: WebhookDelivery,
        config: WebhookConfig,
        payload_json: str,
    ) -> None:
        """Park a failed delivery in Redis for durable, background re-driving."""
        now = time.time()
        base = max(1, config.retry_delay)
        next_at = now + min(self._outbox_max_backoff, base * 2)
        item = OutboxItem(
            delivery_id=delivery.id,
            url=config.url,
            secret=config.secret,
            payload=payload_json,
            event=delivery.payload.event.value,
            timestamp=delivery.payload.timestamp,
            task_id=delivery.payload.task_id,
            attempts=delivery.attempts,
            first_enqueued_at=now,
            next_attempt_at=next_at,
            last_error=delivery.last_error,
        )
        await self._save_outbox_item(item)
        await self.redis.zadd(_OUTBOX_DUE_ZSET, {item.delivery_id: next_at})

    async def _save_outbox_item(self, item: OutboxItem) -> None:
        # Live a bit longer than the max age so a final dead-letter pass can read it.
        ttl = self._outbox_max_age_seconds + 3600
        await self.redis.set(
            f"{_OUTBOX_ITEM_PREFIX}{item.delivery_id}",
            item.model_dump_json(),
            ex=ttl,
        )

    async def _load_outbox_item(self, delivery_id: str) -> OutboxItem | None:
        data = await self.redis.get(f"{_OUTBOX_ITEM_PREFIX}{delivery_id}")
        if not data:
            return None
        return OutboxItem.model_validate_json(data)

    async def _remove_outbox_item(self, delivery_id: str) -> None:
        await self.redis.zrem(_OUTBOX_DUE_ZSET, delivery_id)
        await self.redis.delete(f"{_OUTBOX_ITEM_PREFIX}{delivery_id}")

    async def _incr_metric(self, outcome: str, event: str) -> None:
        try:
            await self.redis.incr(f"{_OUTBOX_METRIC_PREFIX}{outcome}:{event}")
        except Exception:  # metrics are best-effort, never block delivery
            pass

    async def _post_once(self, item: OutboxItem) -> bool:
        """One signed POST of the parked body. Returns True on 2xx."""
        if not self._http_client:
            self._http_client = httpx.AsyncClient(timeout=30, trust_env=False)
        headers = {
            "Content-Type": "application/json",
            "X-ACN-Webhook-ID": item.delivery_id,
            "X-ACN-Event": item.event,
            "X-ACN-Timestamp": item.timestamp,
        }
        if item.secret:
            headers["X-ACN-Signature"] = f"sha256={self._sign_payload(item.payload, item.secret)}"
        try:
            resp = await self._http_client.post(
                item.url, content=item.payload, headers=headers, timeout=30
            )
            if resp.is_success:
                return True
            item.last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return False
        except httpx.TimeoutException:
            item.last_error = "Request timeout"
            return False
        except httpx.RequestError as e:
            item.last_error = str(e)
            return False

    async def _mark_delivery_status(
        self, delivery_id: str, status: str, attempts: int, last_error: str | None
    ) -> None:
        """Update the (secret-free) history record's terminal status in place."""
        key = f"acn:webhooks:deliveries:{delivery_id}"
        data = await self.redis.get(key)
        if not data:
            return
        delivery = WebhookDelivery.model_validate_json(data)
        delivery.status = status
        delivery.attempts = attempts
        if last_error:
            delivery.last_error = last_error
        if status == "delivered":
            delivery.delivered_at = datetime.now(UTC).isoformat()
        await self.redis.set(key, delivery.model_dump_json(), ex=86400 * 7)

    async def _process_outbox_item(self, item: OutboxItem) -> None:
        """Attempt one re-drive of a claimed outbox item, then settle its fate."""
        item.attempts += 1
        if await self._post_once(item):
            await self._remove_outbox_item(item.delivery_id)
            await self._mark_delivery_status(
                item.delivery_id, "delivered", item.attempts, None
            )
            await self._incr_metric("delivered", item.event)
            logger.info("Webhook outbox delivered: %s -> %s", item.delivery_id, item.url)
            return

        now = time.time()
        if now - item.first_enqueued_at >= self._outbox_max_age_seconds:
            await self._remove_outbox_item(item.delivery_id)
            await self._mark_delivery_status(
                item.delivery_id, "dead", item.attempts, item.last_error
            )
            await self._incr_metric("dead", item.event)
            logger.error(
                "Webhook outbox dead-lettered after %d attempts (P0 queue remains the "
                "backstop): %s -> %s (%s)",
                item.attempts,
                item.delivery_id,
                item.url,
                item.last_error,
            )
            return

        backoff = min(self._outbox_max_backoff, self._outbox_poll_interval * (2**item.attempts))
        item.next_attempt_at = now + backoff
        await self._save_outbox_item(item)
        await self.redis.zadd(_OUTBOX_DUE_ZSET, {item.delivery_id: item.next_attempt_at})
        await self._incr_metric("failed", item.event)

    async def _drain_outbox_once(self) -> int:
        """Claim and process all currently-due outbox items. Returns count processed."""
        now = time.time()
        due_ids = await self.redis.zrangebyscore(_OUTBOX_DUE_ZSET, 0, now, start=0, num=100)
        processed = 0
        for raw in due_ids:
            delivery_id = raw.decode() if isinstance(raw, bytes) else raw
            # Atomic claim across replicas: only the worker whose ZREM removes
            # the member owns this attempt. Re-add happens on retry/reschedule.
            if not await self.redis.zrem(_OUTBOX_DUE_ZSET, delivery_id):
                continue
            item = await self._load_outbox_item(delivery_id)
            if item is None:
                continue  # item expired/cleaned; nothing to re-drive
            await self._process_outbox_item(item)
            processed += 1
        return processed

    async def _outbox_worker(self) -> None:
        """Background loop that re-drives parked webhook deliveries."""
        logger.info("Webhook outbox worker started (interval=%ss)", self._outbox_poll_interval)
        while True:
            try:
                await self._drain_outbox_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the worker die on a transient error
                logger.error("Webhook outbox sweep failed: %s", e)
            await asyncio.sleep(self._outbox_poll_interval)

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
