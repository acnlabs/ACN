"""Manifest Queue Service

Phase 2 prototype PR #1 — manifest mode storage layer.

The manifest queue is the secondary inbox for messages addressed to
an agent whose ``communication_policy.mode == "manifest"``. Instead
of writing the full payload into the agent's inbox (and emitting a
loud ``agent_message`` WS push), the router stashes a *summary*
entry plus the original content in this queue and notifies the agent
via the lightweight ``MANIFEST_NOTIFICATION`` channel. The recipient
decides whether to pull the content via
``GET /communication/content/{mid}``.

Storage layout (Redis Cluster–safe):

* ``acn:manifest:{<owner_id>}`` (ZSET)
    Score = entry creation timestamp (ms). Member = ``mid``. Used as
    a chronological index for pagination and for capacity trimming.

* ``acn:manifest:{<owner_id>}:<mid>`` (HASH)
    Manifest entry detail: ``sender_id``, ``summary``, ``ts`` (ms),
    ``content_size`` (bytes). The ``mid`` is reused as the key
    suffix and as the cross-key id.

* ``acn:content:{<owner_id>}:<mid>`` (STRING, JSON-encoded)
    Full original payload, stored separately so we can apply
    different TTLs in the future (e.g. quick-expire content but keep
    the manifest summary for the audit-style timeline).

All three keys share the ``{<owner_id>}`` hash tag so a single
``MULTI/EXEC`` block places them on the same Redis Cluster slot,
making the write atomic and the read path failure-mode obvious
(either all three are gone after TTL, or all three exist).

Why per-owner hash tags:
- ZSET trimming (ZREMRANGEBYRANK), HASH read (HGETALL), STRING read
  (GET / DEL) are independent commands; without a hash tag they
  could land on different slots, forcing the caller into a
  ``Watch/Multi`` retry loop or a non-atomic sequence.
- Putting the recipient's id (rather than the ``mid``) in the tag
  also keeps per-recipient operations scoped — listing one agent's
  manifest does not pull keys from other agents into the same slot.

Why ``mid`` is *not* the hash tag:
- The producer side needs to atomically write three keys for the
  same recipient. If the tag were ``{<mid>}``, the ZSET (one per
  owner) would be on a different slot than the HASH/STRING (one per
  message), defeating atomicity.
- A predictable counter / timestamp ``mid`` would also let attackers
  guess other agents' message ids; we mint a UUID4 instead.

See docs/features/acn-communication-economic-model.md
"Phase 2 决策 — Group A #4 (manifest queue)" and
"Phase 2 原型 PR 验收清单" for the full rationale.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import uuid4

import redis.asyncio as redis

logger = logging.getLogger(__name__)


# Phase 2 globally-tuned constants (Group B #1 / #2). These are
# deliberately not per-agent settings: the trade-offs (storage cost
# vs. notification utility, replay window vs. privacy) are platform-
# wide concerns. Promoting them to agent-owner controls would require
# a per-agent Settings story we don't yet have.
MAX_SUMMARY_LEN: Final = 200  # bytes/chars — short enough for a notification toast
MAX_CONTENT_BYTES: Final = 64 * 1024  # 64 KB — same ceiling as inbox payload
DEFAULT_TTL_SECONDS: Final = 7 * 24 * 3600  # 7 days
MIN_TTL_SECONDS: Final = 5 * 60  # 5 min — below this, the manifest is effectively unread before expiry
MAX_TTL_SECONDS: Final = 30 * 24 * 3600  # 30 days — capped to bound cluster memory
QUEUE_CAPACITY: Final = 200  # max manifest entries retained per agent (oldest evicted)


class AlreadyAckedError(Exception):
    """Raised by ``ManifestService.mark_acked`` on a replay/double-ack.

    Phase 3 ``attention_fee`` flow uses HSETNX to make the ack
    operation idempotent. The 0-return branch means a prior ack
    already stamped the entry and released funds. We surface this
    as a distinct exception (rather than a sentinel) so the route
    layer can map it cleanly to ``ATTENTION_FEE_ALREADY_ACKED``
    without inspecting return tuples.
    """

    def __init__(self, *, owner_id: str, mid: str) -> None:
        super().__init__(f"manifest entry {mid!r} already acked by {owner_id!r}")
        self.owner_id = owner_id
        self.mid = mid


@dataclass(frozen=True)
class ManifestEntry:
    """A single manifest queue row (excluding the full content body).

    ``content_size`` is exposed so the recipient client can decide
    whether to pull the full payload (e.g. skip a 60 KB report on
    mobile bandwidth). Content itself is fetched via a separate
    endpoint to keep the listing endpoint cheap and cache-friendly.
    """

    mid: str
    sender_id: str
    summary: str
    ts_ms: int
    content_size: int
    extra: dict[str, Any] = field(default_factory=dict)
    # Phase 3 attention_fee — ms timestamp of the ack call that
    # released the fee from escrow. ``None`` when the entry was
    # never acked or when no fee was attached. Stored in the detail
    # HASH as a separate top-level field (rather than inside
    # ``extra``) so HSETNX can guarantee single-write semantics on
    # the ack hot path.
    acked_at_ms: int | None = None
    # Wall-clock expiry in ms (epoch). Stored in the detail HASH at
    # write time so the TTL refund worker can compare ``now >
    # expires_at_ms`` without a separate Redis TTL command per entry.
    # ``None`` for entries written before Phase 3 (legacy rows); the
    # worker treats ``None`` as "cannot determine expiry, skip".
    expires_at_ms: int | None = None
    # Phase 3 self-hosted content path. When set, the full message
    # body lives at ``content_url`` on the sender's own server; ACN
    # stores only this pointer. ``content_size`` is 0 for self-hosted
    # entries. ``content_hash`` is optional sender-supplied integrity
    # hint (e.g. ``sha256:<hex>``).
    content_url: str | None = None
    content_hash: str | None = None
    # ACN-level message category (e.g. "task_request", "collaboration",
    # "inquiry", "broadcast", "session_invite"). Stored on the detail
    # HASH so recipients can filter manifest listings by type without
    # pulling content. ``None`` for entries written via the legacy
    # Path 1 route that does not surface a top-level message_type.
    message_type: str | None = None


def _zset_key(owner_id: str) -> str:
    """ZSET key for the chronological manifest index of an owner."""
    return f"acn:manifest:{{{owner_id}}}"


def _detail_key(owner_id: str, mid: str) -> str:
    """HASH key holding the per-entry metadata (sender, summary, ts)."""
    return f"acn:manifest:{{{owner_id}}}:{mid}"


def _content_key(owner_id: str, mid: str) -> str:
    """STRING key holding the full JSON-encoded payload."""
    return f"acn:content:{{{owner_id}}}:{mid}"


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _clamp_ttl(ttl_seconds: int | None) -> int:
    """Snap caller-supplied TTLs into the [MIN, MAX] window.

    A ``None`` argument defers to ``DEFAULT_TTL_SECONDS``. Out-of-
    range values are silently clamped (rather than raising) because
    the call site is the message router on the hot path — making it
    surface a 4xx for "TTL too short" would force every sender to
    know about the platform's TTL bounds.
    """
    if ttl_seconds is None:
        return DEFAULT_TTL_SECONDS
    if ttl_seconds < MIN_TTL_SECONDS:
        return MIN_TTL_SECONDS
    if ttl_seconds > MAX_TTL_SECONDS:
        return MAX_TTL_SECONDS
    return ttl_seconds


def _truncate_summary(text: str) -> str:
    """Trim a free-form summary to ``MAX_SUMMARY_LEN`` characters.

    The router is expected to validate length at the API layer (and
    return 422 for over-long summaries supplied by the sender), but
    we still defensively truncate here so a misbehaving caller can
    never blow past the cap and bloat the queue. We add a single
    ``…`` ellipsis (1 char) when truncation happens so the consumer
    can tell the summary was clipped.
    """
    if len(text) <= MAX_SUMMARY_LEN:
        return text
    # Keep room for the ellipsis so the on-wire size never exceeds
    # MAX_SUMMARY_LEN even after the marker is appended.
    return text[: MAX_SUMMARY_LEN - 1] + "…"


class ManifestService:
    """Read/write API for the manifest queue.

    The service is thin — it owns Redis key naming, atomic writes,
    and TTL bounds. Higher-level concerns (policy decision, WS
    notification, summary derivation) live in
    ``MessageRouter._route_to_manifest``.
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    async def write(
        self,
        owner_id: str,
        sender_id: str,
        summary: str,
        content: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
        extra: dict[str, Any] | None = None,
        content_url: str | None = None,
        content_hash: str | None = None,
        message_type: str | None = None,
    ) -> ManifestEntry:
        """Persist a manifest entry + payload atomically.

        Args:
            owner_id: ACN agent id of the recipient (the key tenant).
            sender_id: ACN agent id of the sender. Stored on the
                manifest entry so the recipient can decide whether
                to pull the content without any extra lookup.
            summary: Short human-readable preview, capped to
                ``MAX_SUMMARY_LEN``. Must already be sanitized for
                control characters by the caller.
            content: Full message payload (will be JSON-encoded).
                Size is capped at ``MAX_CONTENT_BYTES``; oversize
                payloads raise ``ValueError`` so the router can
                surface a 422 to the sender. Ignored when
                ``content_url`` is provided (self-hosted path).
            ttl_seconds: Optional TTL override; clamped into
                ``[MIN_TTL_SECONDS, MAX_TTL_SECONDS]``. Defaults to
                ``DEFAULT_TTL_SECONDS``.
            extra: Optional flat metadata dict. Persisted on the
                detail HASH as JSON-encoded ``extra`` field. Useful
                for forwarding compatibility (e.g. ``priority``,
                ``payment_intent_id``) without changing the schema.
            content_url: Phase 3 self-hosted content path. When set,
                ACN stores only the URL + optional hash in the detail
                HASH and skips the ``acn:content:{owner}:{mid}``
                blob entirely. The recipient calls
                ``GET /communication/content/{mid}`` to receive the
                URL and then fetches the content directly from the
                sender's own server.
            content_hash: Optional SHA-256 or similar hash of the
                self-hosted content (format: ``<algo>:<hex>``). Stored
                alongside ``content_url`` for recipient-side integrity
                verification. Ignored when ``content_url`` is absent.

        Returns:
            The minted ``ManifestEntry`` (with its server-generated
            ``mid`` and ``ts_ms``). The router uses these to build
            the WS notification payload.

        Raises:
            ValueError: When ACN-hosted ``content`` exceeds
                ``MAX_CONTENT_BYTES`` after JSON encoding. Not raised
                on the self-hosted path (no local storage).
        """
        ttl = _clamp_ttl(ttl_seconds)
        clipped = _truncate_summary(summary)
        ts_ms = _now_ms()
        mid = uuid4().hex
        expires_at_ms = ts_ms + ttl * 1000

        self_hosted = content_url is not None

        if self_hosted:
            # Self-hosted path: no content blob storage on ACN.
            # Store the URL + hash in the detail HASH so the content
            # endpoint can redirect the recipient without loading any
            # local content key. ``content_size = 0`` signals "no
            # local copy" to the listing endpoint.
            size = 0
            content_blob: str | None = None
        else:
            # ACN-hosted path: encode and size-check the payload.
            content_blob = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            content_bytes = content_blob.encode("utf-8")
            size = len(content_bytes)
            if size > MAX_CONTENT_BYTES:
                raise ValueError(
                    f"manifest content exceeds {MAX_CONTENT_BYTES} bytes (got {size})"
                )

        detail: dict[str, Any] = {
            "mid": mid,
            "sender_id": sender_id,
            "summary": clipped,
            "ts": ts_ms,
            "content_size": size,
            "expires_at": expires_at_ms,
        }
        if message_type:
            detail["message_type"] = message_type
        if self_hosted:
            detail["content_url"] = content_url
            if content_hash:
                detail["content_hash"] = content_hash
        if extra:
            detail["extra"] = json.dumps(extra, ensure_ascii=False, separators=(",", ":"))

        zset = _zset_key(owner_id)
        detail_key = _detail_key(owner_id, mid)
        content_key = _content_key(owner_id, mid)

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zadd(zset, {mid: ts_ms})
            pipe.hset(detail_key, mapping=detail)
            if not self_hosted and content_blob is not None:
                pipe.set(content_key, content_blob)
                pipe.expire(content_key, ttl)
            pipe.expire(detail_key, ttl)
            pipe.expire(zset, ttl)
            pipe.zremrangebyrank(zset, 0, -(QUEUE_CAPACITY + 1))
            await pipe.execute()

        return ManifestEntry(
            mid=mid,
            sender_id=sender_id,
            summary=clipped,
            ts_ms=ts_ms,
            content_size=size,
            extra=extra or {},
            expires_at_ms=expires_at_ms,
            content_url=content_url,
            content_hash=content_hash if content_url else None,
            message_type=message_type or None,
        )

    async def read_since(
        self,
        owner_id: str,
        *,
        since_ms: int | None = None,
        limit: int = 50,
        message_type: str | None = None,
    ) -> list[ManifestEntry]:
        """List manifest entries newer than ``since_ms`` (inclusive lower bound).

        Args:
            owner_id: Recipient agent id.
            since_ms: Lower bound on entry ``ts`` in ms. ``None`` =
                "from the beginning of the queue". Used as a cursor
                by the manifest list API.
            limit: Maximum number of entries to return. Caller-side
                pagination cap.

        Returns:
            Entries ordered oldest → newest. Excludes detail rows
            whose HASH has already been trimmed/expired (we skip
            silently rather than 500 — a stale ZSET row can briefly
            outlive its detail when TTLs race).
        """
        if limit <= 0:
            return []

        zset = _zset_key(owner_id)
        # ZRANGEBYSCORE with ``min`` inclusive matches the
        # ``since_ms`` "give me everything from this cursor onward"
        # contract. ``-inf`` covers the "no cursor" case without
        # branching the call.
        min_score: float | int = since_ms if since_ms is not None else "-inf"  # type: ignore[assignment]
        mids: list[bytes | str] = await self.redis.zrangebyscore(
            zset,
            min=min_score,
            max="+inf",
            start=0,
            num=limit,
        )
        if not mids:
            return []

        results: list[ManifestEntry] = []
        # Pipeline the HGETALLs. Using a non-transactional pipeline
        # is fine here — we don't need atomicity across reads; even
        # if a row gets evicted between ZRANGE and HGETALL, dropping
        # it from the result is the right behaviour.
        async with self.redis.pipeline(transaction=False) as pipe:
            for raw_mid in mids:
                mid_str = raw_mid.decode() if isinstance(raw_mid, bytes) else raw_mid
                pipe.hgetall(_detail_key(owner_id, mid_str))
            details = await pipe.execute()

        for raw_mid, detail in zip(mids, details, strict=True):
            if not detail:
                # Detail expired/trimmed — silently skip; the ZSET
                # row will be cleaned up on next eviction.
                continue
            mid_str = raw_mid.decode() if isinstance(raw_mid, bytes) else raw_mid
            entry = _decode_entry(mid_str, detail)
            if message_type is not None and entry.message_type != message_type:
                continue
            results.append(entry)

        return results

    async def delete(self, owner_id: str, mid: str) -> bool:
        """Drop a manifest entry + its content.

        Used by the recipient client when they decide a manifest
        entry is not worth pulling (e.g. obvious spam). We delete
        all three keys atomically so list/content endpoints stay
        consistent.

        Returns:
            ``True`` if the entry existed (at least one of the
            three keys was deleted), ``False`` if it had already
            been evicted.

        Phase 3 note:
            For entries with an attached ``attention_fee``, callers
            that need to refund the locked escrow MUST pre-fetch
            ``get_entry`` first to capture ``extra.attention_fee``
            (this method only returns a bool). The route layer
            handles that orchestration — see ``manifest.py`` DELETE
            handler.
        """
        zset = _zset_key(owner_id)
        detail_key = _detail_key(owner_id, mid)
        content_key = _content_key(owner_id, mid)

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrem(zset, mid)
            pipe.delete(detail_key)
            pipe.delete(content_key)
            results = await pipe.execute()

        # ZREM returns int (count removed), DELETE returns int. Any
        # nonzero entry means we actually touched something.
        return any(int(r) > 0 for r in results)

    async def get_entry(
        self,
        owner_id: str,
        mid: str,
    ) -> ManifestEntry | None:
        """Read a single manifest entry's metadata (without the body).

        Returns ``None`` when the detail HASH has been trimmed/expired
        or when the ``mid`` belongs to a different owner. Callers
        translate ``None`` to 404 — same surface as
        ``fetch_content`` so an attacker cannot distinguish "wrong
        owner" from "expired" by status code.

        Phase 3 ``attention_fee`` flow needs this read separately
        from ``fetch_content``: the ack path inspects the
        ``extra.attention_fee`` block before performing the release,
        and the body itself is not required to make that decision.
        """
        detail_key = _detail_key(owner_id, mid)
        raw = await self.redis.hgetall(detail_key)
        if not raw:
            return None
        return _decode_entry(mid, raw)

    async def mark_acked(
        self,
        owner_id: str,
        mid: str,
        *,
        ts_ms: int | None = None,
    ) -> int | None:
        """Atomically flip the manifest entry's ``acked_at`` field.

        Returns:
            * The newly stamped ``acked_at`` timestamp (ms) on a
              first-time ack — caller proceeds to release the
              attention_fee from escrow.
            * ``None`` when the detail HASH does not exist (entry
              expired / wrong owner / never written) — caller
              surfaces 404.

        Raises:
            ``AlreadyAckedError`` when ``acked_at`` was already set
            on the HASH. Letting the caller see this as a distinct
            exception (vs ``None``) keeps the ack endpoint's
            surfaces tight: 404 for "no such entry" and a 4xx
            ``ATTENTION_FEE_ALREADY_ACKED`` for "already released".

        Atomicity notes:
            ``HSETNX`` is the lynchpin — it only stamps the field
            when absent, returning 1 on first ack and 0 on replay.
            Two concurrent ack calls cannot both observe a
            "first-time ack" state, so the downstream escrow
            release runs at most once per manifest entry per ACN
            instance. (The backend escrow itself is also idempotent
            on release, so even a cross-instance race is safe — but
            this guard keeps the metric / receipt count honest.)

            We DON'T pre-check the HASH's existence in a separate
            round-trip: HSETNX on a non-existent key would still
            return 1 and leave a degenerate hash with only the
            ``acked_at`` field. Instead we issue HEXISTS on a
            stable field (``mid``) AFTER the HSETNX and rollback
            (HDEL ``acked_at``) if the entry doesn't actually exist.
            This trades one extra round-trip for correctness on
            the cold path.
        """
        detail_key = _detail_key(owner_id, mid)
        stamped_at = ts_ms if ts_ms is not None else _now_ms()

        # HSETNX returns 1 when the field was newly written, 0 when
        # it already existed. The 0 path means a prior ack already
        # claimed this entry — surface as a distinct exception so
        # the caller can return 4xx ATTENTION_FEE_ALREADY_ACKED.
        wrote = await self.redis.hsetnx(detail_key, "acked_at", str(stamped_at))
        if int(wrote) == 0:
            # Could be "already acked" *or* "key never existed" (HSETNX
            # on a missing key still creates a one-field HASH and
            # returns 1, so 0 is unambiguously "field already set").
            raise AlreadyAckedError(owner_id=owner_id, mid=mid)

        # Cold-path correctness: if the entry was missing, HSETNX
        # silently created a degenerate one-field hash. Detect via
        # HEXISTS on a stable field that the writer always sets and
        # rollback so we don't leak orphan rows.
        has_mid = await self.redis.hexists(detail_key, "mid")
        if not has_mid:
            await self.redis.hdel(detail_key, "acked_at")
            return None

        return stamped_at

    async def unmark_acked(self, owner_id: str, mid: str) -> bool:
        """Roll back the ``acked_at`` field on a manifest entry.

        Phase 3 attention_fee uses this on the failure path of the
        ack endpoint: when the backend escrow ``release_partial``
        rejects the release after ``mark_acked`` already stamped
        the entry, we drop the stamp so the SDK can retry the ack
        without immediately tripping ``ATTENTION_FEE_ALREADY_ACKED``.

        Returns ``True`` when the field was actually removed,
        ``False`` when it had already been cleared (or the entry
        is gone). The route layer ignores the return value — the
        rollback is best-effort by design (we don't want a Redis
        hiccup during error handling to mask the *original* error).

        We expose this as a service method (rather than letting the
        route reach into ``service.redis`` directly) to keep the
        Redis key naming encapsulated. Without it the route would
        have to recompute ``acn:manifest:{<owner>}:<mid>`` itself,
        guaranteeing drift the moment the storage layout shifts.
        """
        detail_key = _detail_key(owner_id, mid)
        result = await self.redis.hdel(detail_key, "acked_at")
        return int(result) > 0

    async def fetch_content_raw(
        self,
        owner_id: str,
        mid: str,
    ) -> bytes | None:
        """Return the raw JSON bytes stored for a manifest entry.

        Unlike ``fetch_content`` (which parses the JSON), this method
        returns the serialised blob so callers can slice it for cursor-
        based pagination without re-serialising back to bytes.

        Returns ``None`` when the key is absent or expired (same
        semantics as ``fetch_content``).
        """
        content_key = _content_key(owner_id, mid)
        raw = await self.redis.get(content_key)
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return raw

    async def fetch_content_chunk(
        self,
        owner_id: str,
        mid: str,
        offset: int,
        size: int,
    ) -> tuple[bytes, bool] | None:
        """Return a byte-range chunk of a manifest entry's content.

        Args:
            owner_id: Recipient agent id.
            mid: Manifest entry id.
            offset: Byte offset into the JSON-serialised payload.
            size: Maximum bytes to return in this chunk.

        Returns:
            ``(chunk_bytes, has_more)`` when the content key exists.
            ``has_more`` is ``True`` when there are bytes beyond
            ``offset + size``. Returns ``None`` when the content key
            is absent or expired.
        """
        raw = await self.fetch_content_raw(owner_id, mid)
        if raw is None:
            return None
        chunk = raw[offset : offset + size]
        has_more = (offset + size) < len(raw)
        return chunk, has_more

    async def fetch_content(
        self,
        owner_id: str,
        mid: str,
    ) -> dict[str, Any] | None:
        """Pull the full payload for a manifest entry.

        Group A #4 / P1-9 semantics:
        * Repeatable: no read-once flag, no fee debit. Phase 3 will
          bolt an explicit ``ack`` step on top for fee release.
        * Cross-tenant: returns ``None`` when the ``mid`` does not
          belong to ``owner_id`` (the caller is expected to translate
          ``None`` into a 404 — never 403, to avoid leaking
          existence of other agents' entries).
        * Expired: also returns ``None``. The route layer cannot
          distinguish "never existed" from "expired" by design.

        Note that we *do not* check the ZSET membership here — only
        the content key. The detail HASH and ZSET row may have been
        trimmed for capacity reasons while the content key is still
        within its TTL; in that case the consumer who already saw
        the entry can still retrieve it. (Eviction happens by rank,
        not by TTL race, so this window is rare in practice.)
        """
        content_key = _content_key(owner_id, mid)
        raw = await self.redis.get(content_key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Defensive: if a future writer stores something other
            # than JSON, surface as 404 rather than 500. The audit
            # log captures the corruption via a warning.
            logger.warning(
                "manifest_content_decode_failed",
                extra={"owner_id": owner_id, "mid": mid},
            )
            return None


def _decode_entry(mid: str, raw: dict[Any, Any]) -> ManifestEntry:
    """Coerce a Redis HGETALL response into a ``ManifestEntry``.

    Redis may return either ``bytes`` or ``str`` keys/values
    depending on the client's ``decode_responses`` flag; we
    normalise here so callers don't have to.
    """

    def _u(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    decoded = {_u(k): _u(v) for k, v in raw.items()}
    extra_blob = decoded.get("extra")
    extra: dict[str, Any] = {}
    if extra_blob:
        try:
            parsed = json.loads(extra_blob)
            if isinstance(parsed, dict):
                extra = parsed
        except json.JSONDecodeError:
            extra = {}

    # ts / content_size are numeric on write; decode defensively in
    # case future writes pre-stringify them differently.
    try:
        ts_ms = int(decoded.get("ts", "0"))
    except ValueError:
        ts_ms = 0
    try:
        content_size = int(decoded.get("content_size", "0"))
    except ValueError:
        content_size = 0

    # ``acked_at`` is only present on entries whose attention_fee has
    # been released. Defensive int parse — if a future writer stores
    # something other than a numeric ms timestamp we just treat the
    # entry as un-acked rather than blowing up the read path.
    acked_at_raw = decoded.get("acked_at")
    acked_at_ms: int | None
    if acked_at_raw is None or acked_at_raw == "":
        acked_at_ms = None
    else:
        try:
            acked_at_ms = int(acked_at_raw)
        except ValueError:
            acked_at_ms = None

    # ``expires_at`` is only present on entries written after Phase 3.
    # Legacy rows (written before this field was added) return None —
    # the TTL refund worker skips those entries gracefully.
    expires_at_raw = decoded.get("expires_at")
    expires_at_ms: int | None
    if expires_at_raw is None or expires_at_raw == "":
        expires_at_ms = None
    else:
        try:
            expires_at_ms = int(expires_at_raw)
        except ValueError:
            expires_at_ms = None

    return ManifestEntry(
        mid=decoded.get("mid", mid),
        sender_id=decoded.get("sender_id", ""),
        summary=decoded.get("summary", ""),
        ts_ms=ts_ms,
        content_size=content_size,
        extra=extra,
        acked_at_ms=acked_at_ms,
        expires_at_ms=expires_at_ms,
        content_url=decoded.get("content_url") or None,
        content_hash=decoded.get("content_hash") or None,
        message_type=decoded.get("message_type") or None,
    )
