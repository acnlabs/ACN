"""Agent-to-agent blob objects for A2A FilePart URIs.

Mailbox, not a netdisk: upload is a short free hold. Fetch does not
keep it. Expiry deletes bytes. Extending (keeping the URI alive) is
paid by the caller (integer Credits, ceil, minimum 1) and moves the
object onto their quota. Spend hits the payer; the same amount is
credited to ``ACN_REVENUE_WALLET_ID`` as a PLATFORM wallet.

HMAC signs blob_id only so a FilePart URI stays valid after extend;
expiry lives in Redis meta.

Not hunter ``mbx:`` — no chat price tag.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import structlog
from redis.asyncio import Redis

from ..config import Settings
from ..core.errors import ACNHTTPError, ErrorCode
from ..infrastructure.blob_store import FilesystemBlobStore
from .wallet_client import WalletClient, WalletResult

logger = structlog.get_logger()

_META_TTL_PAD = 86400
GIB = 1024 ** 3
DAY = 86400.0
_BLOB_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_EXPIRY_KEY = "acn:blob:expiry"
_PUT_LOCK_TTL = 15
_EXTEND_LOCK_PAD = 15
_EXTEND_LOCK_MIN = 45
# spend then platform-credit; lock must cover both HTTP calls, and httpx
# Timeout(float) applies per phase so connect+read can stack on each call.
_CHARGE_HTTP_CALLS = 2
_HTTPX_PHASES = 2
# Keep a last-second extend retryable after charge rollback (GET/GC must not drop).
_PENDING_TTL = 86400
# Re-check retained-but-expired blobs soon; do not push zset by a full pending TTL
# or GC/orphan skip would keep bytes after the pending key expires.
_RETAIN_REQUEUE = 60
_MIME_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)
_UNLOCK = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def safe_blob_filename(name: object) -> str:
    """Basename only — reject path segments from upload / Content-Disposition."""
    raw = str(name or "file").replace("\x00", "").replace("\r", "").replace("\n", "")
    raw = raw.replace('"', "").replace("\\", "/")
    base = Path(raw).name.strip()
    if not base or base in {".", ".."}:
        return "file"
    return base[:200]


def safe_blob_mime_type(value: object) -> str:
    """RFC type/subtype only — strip CR/LF and parameters so GET cannot inject headers."""
    raw = str(value or "").replace("\x00", "").replace("\r", "").replace("\n", "")
    raw = raw.split(";", 1)[0].strip()
    if _MIME_TYPE.fullmatch(raw) and len(raw) <= 200:
        return raw.lower()
    return "application/octet-stream"


class BlobStore(Protocol):
    async def put(self, blob_id: str, data: bytes) -> None: ...
    async def get(self, blob_id: str) -> bytes | None: ...
    async def delete(self, blob_id: str) -> None: ...
    async def list_ids(self) -> list[str]: ...


class BlobService:
    def __init__(
        self,
        redis: Redis,
        store: BlobStore,
        settings: Settings,
        wallet: WalletClient | None = None,
    ) -> None:
        self.redis = redis
        self.store = store
        self.settings = settings
        self.wallet = wallet

    def _secret(self) -> str:
        dedicated = (self.settings.blob_signing_secret or "").strip()
        if dedicated:
            return dedicated
        if self.settings.dev_mode:
            return (
                (self.settings.internal_api_token or "").strip()
                or "dev-blob-signing-secret-not-for-prod"
            )
        raise RuntimeError("BLOB_SIGNING_SECRET is required when DEV_MODE=false")

    def _meta_key(self, blob_id: str) -> str:
        return f"acn:blob:{blob_id}"

    def _owner_key(self, agent_id: str) -> str:
        return f"acn:blob:owner:{agent_id}"

    def sign(self, blob_id: str) -> str:
        return hmac.new(
            self._secret().encode(),
            blob_id.encode(),
            hashlib.sha256,
        ).hexdigest()

    def public_uri(self, blob_id: str) -> str:
        sig = self.sign(blob_id)
        base = self.settings.gateway_base_url.rstrip("/")
        return f"{base}/api/v1/blobs/{blob_id}?sig={sig}"

    def check_sig(self, blob_id: str, sig: str | None) -> bool:
        if not sig or not isinstance(sig, str):
            return False
        expected = self.sign(blob_id)
        if len(sig) != len(expected):
            return False
        return hmac.compare_digest(expected, sig)

    async def _load_meta(self, blob_id: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self._meta_key(blob_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None

    def _pending_index_key(self, blob_id: str) -> str:
        return f"acn:blob:extend_pending_index:{blob_id}"

    def _pending_key(self, blob_id: str, payer_id: str) -> str:
        return f"acn:blob:extend_pending:{blob_id}:{payer_id}"

    def _legacy_pending_key(self, blob_id: str) -> str:
        """Unscoped key from before per-payer pending."""
        return f"acn:blob:extend_pending:{blob_id}"

    @staticmethod
    def _decode_member(raw: object) -> str:
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    async def _parse_pending(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(key)
        if not raw:
            return None
        try:
            data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    async def _load_pending(self, blob_id: str, payer_id: str) -> dict[str, Any] | None:
        data = await self._parse_pending(self._pending_key(blob_id, payer_id))
        if data is not None:
            return data
        legacy = await self._parse_pending(self._legacy_pending_key(blob_id))
        if legacy is not None and legacy.get("payer_id") == payer_id:
            return legacy
        return None

    async def _save_pending(
        self,
        blob_id: str,
        payer_id: str,
        old_exp: int,
        extra_seconds: int,
        *,
        replace: bool = False,
    ) -> None:
        if not replace:
            existing = await self._load_pending(blob_id, payer_id)
            if existing is not None:
                return
        await self.redis.set(
            self._pending_key(blob_id, payer_id),
            json.dumps(
                {
                    "payer_id": payer_id,
                    "old_exp": int(old_exp),
                    "extra_seconds": int(extra_seconds),
                }
            ),
            ex=_PENDING_TTL,
        )
        index = self._pending_index_key(blob_id)
        await self.redis.sadd(index, payer_id)
        expire = getattr(self.redis, "expire", None)
        if expire is not None:
            await expire(index, _PENDING_TTL)

    async def _clear_pending(self, blob_id: str, payer_id: str) -> None:
        await self.redis.delete(self._pending_key(blob_id, payer_id))
        await self.redis.srem(self._pending_index_key(blob_id), payer_id)
        legacy = await self._parse_pending(self._legacy_pending_key(blob_id))
        if legacy is not None and legacy.get("payer_id") == payer_id:
            await self.redis.delete(self._legacy_pending_key(blob_id))

    async def _pending_keys(self, blob_id: str) -> list[str]:
        keys = [self._legacy_pending_key(blob_id), self._pending_index_key(blob_id)]
        raw_ids = await self.redis.smembers(self._pending_index_key(blob_id))
        for raw_id in raw_ids:
            keys.append(self._pending_key(blob_id, self._decode_member(raw_id)))
        return keys

    async def _retain_expired(self, blob_id: str) -> bool:
        if await self._parse_pending(self._legacy_pending_key(blob_id)) is not None:
            return True
        raw_ids = await self.redis.smembers(self._pending_index_key(blob_id))
        for raw_id in raw_ids:
            payer_id = self._decode_member(raw_id)
            if await self._parse_pending(self._pending_key(blob_id, payer_id)) is not None:
                return True
            await self.redis.srem(self._pending_index_key(blob_id), payer_id)
        return False

    async def _owner_usage(
        self,
        agent_id: str,
        now: int,
        *,
        retained: bool | None = None,
    ) -> int:
        ids = await self.redis.smembers(self._owner_key(agent_id))
        total = 0
        for raw_id in ids:
            blob_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            meta = await self._load_meta(blob_id)
            if not meta or int(meta.get("exp") or 0) <= now:
                if not meta or not await self._retain_expired(blob_id):
                    await self.redis.srem(self._owner_key(agent_id), blob_id)
                    continue
            is_retained = bool(meta.get("retained"))
            if retained is True and not is_retained:
                continue
            if retained is False and is_retained:
                continue
            total += int(meta.get("size") or 0)
        return total

    def _credits_for(self, extra_bytes: int, ttl_seconds: int) -> int:
        """Integer Credits for Backend ``/spend`` (gt=0 int). Formula > 0 → at least 1.

        Callers must pass the **requested** TTL (``extra_seconds``), not the
        cap-truncated ``gained``. ``now`` would change the amount on retry and
        409 the idempotency key.
        """
        if extra_bytes <= 0 or ttl_seconds <= 0:
            return 0
        gib_days = (extra_bytes / GIB) * (ttl_seconds / DAY)
        raw = gib_days * float(self.settings.blob_credits_per_gib_day)
        return max(1, math.ceil(raw))

    def _revenue_wallet_id(self) -> str:
        return (getattr(self.settings, "acn_revenue_wallet_id", None) or "").strip()

    def _billing_error(
        self,
        code: ErrorCode,
        agent_id: str,
        credits: int,
        reason: str,
    ) -> ACNHTTPError:
        return ACNHTTPError(
            code,
            402,
            details={
                "agent_id": agent_id,
                "credits": credits,
                "reason": reason,
            },
        )

    def _require_billing(self, agent_id: str, credits: int) -> None:
        """Fail closed before persist. Dev + no wallet still skips charge later."""
        if credits <= 0:
            return
        if self.wallet is None:
            if self.settings.dev_mode:
                return
            raise self._billing_error(
                ErrorCode.BLOB_BILLING_UNAVAILABLE,
                agent_id,
                credits,
                "no_wallet",
            )
        if not self._revenue_wallet_id():
            raise self._billing_error(
                ErrorCode.BLOB_BILLING_UNAVAILABLE,
                agent_id,
                credits,
                "no_revenue_wallet",
            )

    def _should_write_pending(self, exc: BaseException) -> bool:
        """Write/renew pending only if this request's spend may have committed.

        ``on_spend_ok`` already records a successful spend. This covers timeout /
        connect / credit-fail (ambiguous or paid). Insufficient funds and spend
        409 did not apply this charge — leave any prior pending untouched.
        """
        if not isinstance(exc, ACNHTTPError):
            return True
        if exc.code in {ErrorCode.INSUFFICIENT_BALANCE, ErrorCode.RESOURCE_CONFLICT}:
            return False
        if exc.code is ErrorCode.BLOB_BILLING_UNAVAILABLE:
            reason = str((exc.details or {}).get("reason") or "")
            return reason not in {"no_wallet", "no_revenue_wallet"}
        return True

    def _spend_failure(
        self, agent_id: str, amount: int, result: WalletResult
    ) -> ACNHTTPError:
        """5xx / missing status = spend may have committed. 4xx (not 409) did not."""
        if result.status_code == 409:
            return ACNHTTPError(
                ErrorCode.RESOURCE_CONFLICT,
                409,
                details={"reason": "idempotency_conflict"},
            )
        status = result.status_code
        ambiguous = status is None or (isinstance(status, int) and status >= 500)
        if ambiguous:
            return self._billing_error(
                ErrorCode.BLOB_BILLING_UNAVAILABLE,
                agent_id,
                amount,
                result.error or "wallet_unavailable",
            )
        return self._billing_error(
            ErrorCode.INSUFFICIENT_BALANCE,
            agent_id,
            amount,
            result.error or "spend_failed",
        )

    async def _charge(
        self,
        agent_id: str,
        credits: int,
        description: str,
        *,
        idempotency_key: str | None = None,
        on_spend_ok: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if credits <= 0:
            return
        if self.settings.dev_mode and self.wallet is None:
            logger.info("blob_charge_skipped_dev", agent_id=agent_id, credits=credits)
            return
        if self.wallet is None:
            raise self._billing_error(
                ErrorCode.BLOB_BILLING_UNAVAILABLE,
                agent_id,
                credits,
                "no_wallet",
            )
        revenue_id = self._revenue_wallet_id()
        if not revenue_id:
            raise self._billing_error(
                ErrorCode.BLOB_BILLING_UNAVAILABLE,
                agent_id,
                credits,
                "no_revenue_wallet",
            )
        amount = int(credits)
        result: WalletResult = await self.wallet.spend(
            agent_id,
            amount,
            description,
            idempotency_key=idempotency_key,
        )
        if not result.success:
            raise self._spend_failure(agent_id, amount, result)
        if on_spend_ok is not None:
            await on_spend_ok()
        credited: WalletResult = await self.wallet.credit_platform(
            revenue_id,
            amount,
            description,
            idempotency_key=(f"rev:{idempotency_key}" if idempotency_key else None),
        )
        if credited.success:
            return
        if credited.status_code == 409:
            raise ACNHTTPError(
                ErrorCode.RESOURCE_CONFLICT,
                409,
                details={"reason": "idempotency_conflict"},
            )
        raise self._billing_error(
            ErrorCode.BLOB_BILLING_UNAVAILABLE,
            agent_id,
            amount,
            credited.error or "revenue_credit_failed",
        )

    def _extend_lock_ttl(self) -> int:
        timeout = 30.0
        if self.wallet is not None:
            raw = getattr(self.wallet, "timeout", 30.0)
            try:
                timeout = float(raw)
            except (TypeError, ValueError):
                timeout = 30.0
        return max(
            _EXTEND_LOCK_MIN,
            int(timeout) * _CHARGE_HTTP_CALLS * _HTTPX_PHASES + _EXTEND_LOCK_PAD,
        )

    @staticmethod
    def _extend_idempotency_key(
        blob_id: str, payer_id: str, old_exp: int, extra_seconds: int
    ) -> str:
        """Stable across retries: not ``new_exp`` (that follows ``now`` at the TTL cap)."""
        return f"blob_extend:{blob_id}:{payer_id}:{old_exp}:{extra_seconds}"

    async def _try_lock(self, key: str, ttl: int = _PUT_LOCK_TTL) -> str | None:
        token = str(uuid4())
        ok = await self.redis.set(key, token, ex=ttl, nx=True)
        return token if ok else None

    async def _release_lock(self, key: str, token: str) -> None:
        try:
            await self.redis.eval(_UNLOCK, 1, key, token)
        except Exception:
            logger.exception("blob_lock_release_failed", key=key)

    async def _index_exp(self, blob_id: str, exp: int) -> None:
        await self.redis.zadd(_EXPIRY_KEY, {blob_id: exp})

    async def _write_meta(self, blob_id: str, meta: dict[str, Any], now: int) -> None:
        ttl_left = max(60, int(meta["exp"]) - now)
        await self.redis.set(
            self._meta_key(blob_id),
            json.dumps(meta),
            ex=ttl_left + _META_TTL_PAD,
        )
        await self._index_exp(blob_id, int(meta["exp"]))

    async def _drop(self, blob_id: str, meta: dict[str, Any] | None) -> None:
        await self.store.delete(blob_id)
        await self.redis.delete(self._meta_key(blob_id), *await self._pending_keys(blob_id))
        await self.redis.zrem(_EXPIRY_KEY, blob_id)
        owner = str((meta or {}).get("owner_id") or "")
        if owner:
            await self.redis.srem(self._owner_key(owner), blob_id)

    async def _purge_zset_batch(self, ts: int, limit: int) -> int:
        raw_ids = await self.redis.zrangebyscore(_EXPIRY_KEY, 0, ts, start=0, num=limit)
        purged = 0
        for raw_id in raw_ids:
            blob_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            meta = await self._load_meta(blob_id)
            if meta and int(meta.get("exp") or 0) > ts:
                await self._index_exp(blob_id, int(meta["exp"]))
                continue
            if await self._retain_expired(blob_id):
                await self._index_exp(blob_id, ts + _RETAIN_REQUEUE)
                continue
            await self._drop(blob_id, meta)
            purged += 1
            logger.info("blob_purged", blob_id=blob_id, owner_id=(meta or {}).get("owner_id"))
        return purged

    async def purge_orphans(self, *, now: int, limit: int = 5000) -> int:
        """Delete files with no live meta; reindex live files missing from the zset."""
        list_ids = getattr(self.store, "list_ids", None)
        if list_ids is None:
            return 0
        ids = await list_ids()
        purged = 0
        for blob_id in ids:
            if purged >= limit:
                break
            meta = await self._load_meta(blob_id)
            score = await self.redis.zscore(_EXPIRY_KEY, blob_id)
            expired = not meta or int(meta.get("exp") or 0) <= now
            if expired:
                if meta and await self._retain_expired(blob_id):
                    await self._index_exp(blob_id, now + _RETAIN_REQUEUE)
                    continue
                await self._drop(blob_id, meta)
                purged += 1
                logger.info(
                    "blob_orphan_purged",
                    blob_id=blob_id,
                    owner_id=(meta or {}).get("owner_id"),
                )
                continue
            if score is None:
                await self._index_exp(blob_id, int(meta["exp"]))
                logger.info("blob_expiry_reindexed", blob_id=blob_id, exp=meta["exp"])
        return purged

    async def purge_expired(
        self,
        *,
        now: int | None = None,
        limit: int = 500,
        max_batches: int = 40,
    ) -> int:
        """Delete expired mailbox objects (metadata + bytes) and disk orphans."""
        ts = int(time.time()) if now is None else now
        purged = 0
        for _ in range(max(1, max_batches)):
            n = await self._purge_zset_batch(ts, limit)
            purged += n
            if n < limit:
                break
        purged += await self.purge_orphans(now=ts)
        return purged

    async def put(
        self,
        *,
        owner_id: str,
        data: bytes,
        name: str,
        mime_type: str,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        settings = self.settings
        size = len(data)
        if size <= 0:
            raise ACNHTTPError(
                ErrorCode.INVALID_REQUEST,
                400,
                details={"reason": "empty_blob"},
            )
        if size > settings.blob_max_file_bytes:
            raise ACNHTTPError(
                ErrorCode.BLOB_TOO_LARGE,
                413,
                details={
                    "size": size,
                    "max_file_bytes": settings.blob_max_file_bytes,
                },
            )
        ttl = ttl_seconds or settings.blob_free_ttl_seconds
        ttl = max(60, min(ttl, settings.blob_free_ttl_seconds))
        now = int(time.time())
        lock_key = f"acn:blob:putlock:{owner_id}"
        token = await self._try_lock(lock_key, ttl=_PUT_LOCK_TTL)
        if token is None:
            raise ACNHTTPError(
                ErrorCode.RESOURCE_CONFLICT,
                409,
                details={"reason": "blob_busy"},
            )
        try:
            return await self._put_locked(
                owner_id=owner_id,
                data=data,
                name=name,
                mime_type=mime_type,
                size=size,
                ttl=ttl,
                now=now,
            )
        finally:
            await self._release_lock(lock_key, token)

    async def _put_locked(
        self,
        *,
        owner_id: str,
        data: bytes,
        name: str,
        mime_type: str,
        size: int,
        ttl: int,
        now: int,
    ) -> dict[str, Any]:
        settings = self.settings
        usage = await self._owner_usage(owner_id, now, retained=False)
        if usage + size > settings.blob_free_bytes:
            raise ACNHTTPError(
                ErrorCode.BLOB_CAP_EXCEEDED,
                403,
                details={
                    "used_bytes": usage,
                    "size": size,
                    "cap_bytes": settings.blob_free_bytes,
                    "kind": "mailbox",
                },
            )

        blob_id = str(uuid4())
        exp = now + ttl
        sha = hashlib.sha256(data).hexdigest()
        await self.store.put(blob_id, data)
        meta = {
            "id": blob_id,
            "owner_id": owner_id,
            "name": safe_blob_filename(name),
            "mime_type": safe_blob_mime_type(mime_type),
            "size": size,
            "sha256": sha,
            "exp": exp,
            "credits": 0.0,
            "retained": False,
        }
        try:
            await self.redis.set(
                self._meta_key(blob_id),
                json.dumps(meta),
                ex=ttl + _META_TTL_PAD,
            )
            await self.redis.sadd(self._owner_key(owner_id), blob_id)
            await self._index_exp(blob_id, exp)
        except Exception:
            await self.store.delete(blob_id)
            await self.redis.delete(self._meta_key(blob_id))
            await self.redis.srem(self._owner_key(owner_id), blob_id)
            await self.redis.zrem(_EXPIRY_KEY, blob_id)
            logger.exception("blob_put_index_failed", blob_id=blob_id, owner_id=owner_id)
            raise
        uri = self.public_uri(blob_id)
        logger.info("blob_put", blob_id=blob_id, owner_id=owner_id, size=size, exp=exp)
        return {**meta, "uri": uri}

    def _require_id(self, blob_id: str) -> str:
        if not _BLOB_ID.match(blob_id or ""):
            raise ACNHTTPError(
                ErrorCode.BLOB_NOT_FOUND,
                404,
                details={"blob_id": blob_id},
            )
        return blob_id

    async def get_bytes(
        self,
        blob_id: str,
        *,
        caller_id: str | None,
        sig: str | None,
    ) -> tuple[bytes, dict[str, Any]]:
        blob_id = self._require_id(blob_id)
        meta = await self._load_meta(blob_id)
        now = int(time.time())
        if not meta or (
            int(meta.get("exp") or 0) <= now and not await self._retain_expired(blob_id)
        ):
            await self._drop(blob_id, meta)
            raise ACNHTTPError(
                ErrorCode.BLOB_NOT_FOUND,
                404,
                details={"blob_id": blob_id},
            )
        owner = str(meta.get("owner_id") or "")
        allowed = caller_id == owner or self.check_sig(blob_id, sig)
        if not allowed:
            raise ACNHTTPError(
                ErrorCode.BLOB_NOT_FOUND,
                404,
                details={"blob_id": blob_id},
            )
        data = await self.store.get(blob_id)
        if data is None:
            await self._drop(blob_id, meta)
            raise ACNHTTPError(
                ErrorCode.BLOB_NOT_FOUND,
                404,
                details={"blob_id": blob_id},
            )
        expected_sha = str(meta.get("sha256") or "")
        if expected_sha and hashlib.sha256(data).hexdigest() != expected_sha:
            await self._drop(blob_id, meta)
            logger.warning("blob_checksum_mismatch", blob_id=blob_id)
            raise ACNHTTPError(
                ErrorCode.BLOB_NOT_FOUND,
                404,
                details={"blob_id": blob_id},
            )
        return data, meta

    async def _restore_extend(
        self,
        blob_id: str,
        old_meta: dict[str, Any],
        old_owner: str,
        payer_id: str,
        _now: int,
    ) -> None:
        if old_owner != payer_id:
            await self.redis.srem(self._owner_key(payer_id), blob_id)
            await self.redis.sadd(self._owner_key(old_owner), blob_id)
        restored = dict(old_meta)
        clock = int(time.time())
        # Do not clamp exp forward: unpaid last-second extend must not gain free TTL.
        # GET/GC keep bytes via pending; retry uses max(stored_exp, now).
        await self._write_meta(blob_id, restored, clock)

    async def extend(
        self,
        blob_id: str,
        payer_id: str,
        extra_seconds: int,
        *,
        sig: str | None = None,
    ) -> dict[str, Any]:
        blob_id = self._require_id(blob_id)
        extra_seconds = max(60, extra_seconds)
        lock_key = f"acn:blob:lock:{blob_id}"
        token = await self._try_lock(lock_key, ttl=self._extend_lock_ttl())
        if token is None:
            raise ACNHTTPError(
                ErrorCode.RESOURCE_CONFLICT,
                409,
                details={"reason": "blob_busy"},
            )
        try:
            return await self._extend_locked(
                blob_id, payer_id, extra_seconds, sig=sig
            )
        finally:
            await self._release_lock(lock_key, token)

    async def _extend_locked(
        self,
        blob_id: str,
        payer_id: str,
        extra_seconds: int,
        *,
        sig: str | None,
    ) -> dict[str, Any]:
        meta = await self._load_meta(blob_id)
        now = int(time.time())
        if not meta or (
            int(meta.get("exp") or 0) <= now and not await self._retain_expired(blob_id)
        ):
            await self._drop(blob_id, meta)
            raise ACNHTTPError(
                ErrorCode.BLOB_NOT_FOUND,
                404,
                details={"blob_id": blob_id},
            )
        pending = await self._load_pending(blob_id, payer_id)
        owner = str(meta.get("owner_id") or "")
        allowed = payer_id == owner or self.check_sig(blob_id, sig)
        if not allowed:
            raise ACNHTTPError(
                ErrorCode.BLOB_NOT_FOUND,
                404,
                details={"blob_id": blob_id},
            )
        stored_exp = int(meta["exp"])
        key_old_exp = stored_exp
        if (
            pending
            and pending.get("payer_id") == payer_id
            and int(pending.get("extra_seconds") or 0) == extra_seconds
        ):
            key_old_exp = int(pending["old_exp"])
            stored_exp = max(stored_exp, now)
        new_exp = min(
            stored_exp + extra_seconds,
            now + self.settings.blob_max_ttl_seconds,
        )
        gained = new_exp - stored_exp
        if gained <= 0:
            return {**meta, "uri": self.public_uri(blob_id)}

        size = int(meta["size"])
        already = bool(meta.get("retained")) and owner == payer_id
        retained_usage = await self._owner_usage(payer_id, now, retained=True)
        projected = retained_usage if already else retained_usage + size
        if projected > self.settings.blob_max_agent_bytes:
            raise ACNHTTPError(
                ErrorCode.BLOB_CAP_EXCEEDED,
                403,
                details={
                    "used_bytes": retained_usage,
                    "size": size,
                    "cap_bytes": self.settings.blob_max_agent_bytes,
                    "kind": "retained",
                },
            )

        credits = self._credits_for(size, extra_seconds)
        self._require_billing(payer_id, credits)
        old_meta = dict(meta)
        if owner != payer_id:
            meta["owner_id"] = payer_id
        meta["retained"] = True
        meta["exp"] = new_exp
        meta["credits"] = float(old_meta.get("credits") or 0) + credits
        try:
            if owner != payer_id:
                await self.redis.srem(self._owner_key(owner), blob_id)
                await self.redis.sadd(self._owner_key(payer_id), blob_id)
            await self._write_meta(blob_id, meta, now)
        except Exception:
            try:
                await self._restore_extend(blob_id, old_meta, owner, payer_id, now)
            except Exception:
                logger.exception("blob_extend_persist_rollback_failed", blob_id=blob_id)
            logger.exception("blob_extend_persist_failed", blob_id=blob_id, payer_id=payer_id)
            raise

        async def _mark_spend_ok() -> None:
            await self._save_pending(
                blob_id, payer_id, key_old_exp, extra_seconds, replace=True
            )

        try:
            await self._charge(
                payer_id,
                credits,
                f"blob_extend blob={blob_id}",
                idempotency_key=self._extend_idempotency_key(
                    blob_id, payer_id, key_old_exp, extra_seconds
                ),
                on_spend_ok=_mark_spend_ok,
            )
        except Exception as exc:
            try:
                await self._restore_extend(blob_id, old_meta, owner, payer_id, now)
            except Exception:
                logger.exception("blob_extend_charge_rollback_failed", blob_id=blob_id)
            if self._should_write_pending(exc):
                try:
                    if await self._load_pending(blob_id, payer_id) is None:
                        await self._save_pending(
                            blob_id, payer_id, key_old_exp, extra_seconds
                        )
                except Exception:
                    logger.exception("blob_extend_pending_save_failed", blob_id=blob_id)
            raise
        await self._clear_pending(blob_id, payer_id)
        logger.info(
            "blob_extended",
            blob_id=blob_id,
            payer_id=payer_id,
            gained=gained,
            credits=credits,
        )
        return {**meta, "uri": self.public_uri(blob_id), "charged": credits}

    async def usage(self, agent_id: str) -> dict[str, Any]:
        now = int(time.time())
        mailbox = await self._owner_usage(agent_id, now, retained=False)
        retained = await self._owner_usage(agent_id, now, retained=True)
        return {
            "agent_id": agent_id,
            "mailbox_bytes": mailbox,
            "mailbox_cap_bytes": self.settings.blob_free_bytes,
            "retained_bytes": retained,
            "retained_cap_bytes": self.settings.blob_max_agent_bytes,
            "used_bytes": mailbox + retained,
            "free_bytes": self.settings.blob_free_bytes,
            "max_agent_bytes": self.settings.blob_max_agent_bytes,
            "max_file_bytes": self.settings.blob_max_file_bytes,
            "free_ttl_seconds": self.settings.blob_free_ttl_seconds,
            "credits_per_gib_day": self.settings.blob_credits_per_gib_day,
        }


def build_blob_service(redis: Redis, settings: Settings) -> BlobService:
    store = FilesystemBlobStore(settings.blob_store_path)
    wallet: WalletClient | None = None
    if settings.backend_url and settings.internal_api_token:
        wallet = WalletClient(
            settings.backend_url,
            internal_token=settings.internal_api_token,
        )
    return BlobService(redis, store, settings, wallet)
