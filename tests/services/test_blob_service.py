"""BlobService: mailbox hold, consumer-paid extend, expiry deletes bytes."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from acn.core.errors import ACNHTTPError, ErrorCode
from acn.infrastructure.blob_store import FilesystemBlobStore
from acn.services.blob_service import GIB, BlobService, safe_blob_filename, safe_blob_mime_type
from acn.services.wallet_client import WalletResult


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool:
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.kv.pop(key, None)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def sadd(self, key: str, *vals: str) -> None:
        self.sets.setdefault(key, set()).update(vals)

    async def srem(self, key: str, *vals: str) -> None:
        bucket = self.sets.get(key)
        if not bucket:
            return
        for val in vals:
            bucket.discard(val)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zsets.setdefault(key, {}).update(
            {str(member): float(score) for member, score in mapping.items()}
        )

    async def zrangebyscore(
        self,
        key: str,
        min: float,
        max: float,
        start: int | None = None,
        num: int | None = None,
    ) -> list[str]:
        items = [
            (member, score)
            for member, score in self.zsets.get(key, {}).items()
            if min <= score <= max
        ]
        items.sort(key=lambda pair: pair[1])
        ids = [member for member, _score in items]
        if start is not None and num is not None:
            return ids[start : start + num]
        return ids

    async def zrem(self, key: str, *members: str) -> None:
        zset = self.zsets.get(key)
        if not zset:
            return
        for member in members:
            zset.pop(member, None)

    async def zscore(self, key: str, member: str) -> float | None:
        zset = self.zsets.get(key)
        if not zset or member not in zset:
            return None
        return zset[member]

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> int:
        key = keys_and_args[0]
        token = keys_and_args[1]
        current = self.kv.get(key)
        if current == token:
            self.kv.pop(key, None)
            return 1
        return 0


class BoomOnSetRedis(FakeRedis):
    def __init__(self, fail_on: int) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.sets_done = 0

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool:
        if (
            key.startswith("acn:blob:")
            and not key.startswith("acn:blob:lock:")
            and not key.startswith("acn:blob:putlock:")
            and not key.startswith("acn:blob:owner:")
        ):
            self.sets_done += 1
            if self.sets_done >= self.fail_on:
                raise RuntimeError("redis down")
        return await super().set(key, value, ex=ex, nx=nx)


class BoomOnSaddRedis(FakeRedis):
    def __init__(self, fail_key: str) -> None:
        super().__init__()
        self.fail_key = fail_key

    async def sadd(self, key: str, *vals: str) -> None:
        if key == self.fail_key:
            raise RuntimeError("redis down")
        await super().sadd(key, *vals)


def _settings(tmp_path, **overrides: object) -> SimpleNamespace:
    values = {
        "blob_store_path": str(tmp_path),
        "blob_free_bytes": 100,
        "blob_max_file_bytes": 200,
        "blob_max_agent_bytes": 500,
        "blob_free_ttl_seconds": 3600,
        "blob_max_ttl_seconds": 86400,
        "blob_credits_per_gib_day": 1.0,
        "blob_signing_secret": "test-blob-secret",
        "gateway_base_url": "http://acn.test",
        "internal_api_token": "test-internal-token-must-be-at-least-32-characters-long",
        "dev_mode": True,
        "backend_url": "http://backend.test",
        "acn_revenue_wallet_id": "w_plat_test_acn_revenue",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service(tmp_path, wallet=None, **overrides: object) -> BlobService:
    settings = _settings(tmp_path, **overrides)
    return BlobService(
        FakeRedis(),  # type: ignore[arg-type]
        FilesystemBlobStore(tmp_path),
        settings,  # type: ignore[arg-type]
        wallet,
    )


def _uri_sig(uri: str) -> str:
    qs = parse_qs(urlparse(uri).query)
    return qs["sig"][0]


def _wallet_ok() -> AsyncMock:
    wallet = AsyncMock()
    wallet.spend = AsyncMock(return_value=WalletResult(success=True, message="ok"))
    wallet.credit_platform = AsyncMock(return_value=WalletResult(success=True, message="ok"))
    return wallet


@pytest.mark.asyncio
async def test_put_within_mailbox_skips_wallet(tmp_path) -> None:
    wallet = AsyncMock()
    svc = _service(tmp_path, wallet=wallet)
    result = await svc.put(
        owner_id="agent-a",
        data=b"hello",
        name="hi.txt",
        mime_type="text/plain",
    )
    assert result["size"] == 5
    assert result["credits"] == 0.0
    assert result["retained"] is False
    wallet.spend.assert_not_awaited()
    wallet.credit_platform.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_over_mailbox_cap_rejected_without_charge(tmp_path) -> None:
    wallet = AsyncMock()
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_free_bytes=100,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
    )
    with pytest.raises(ACNHTTPError) as ei:
        await svc.put(
            owner_id="agent-a",
            data=b"x" * 1_048_576,
            name="b.bin",
            mime_type="application/octet-stream",
        )
    assert ei.value.code is ErrorCode.BLOB_CAP_EXCEEDED
    assert ei.value.details["kind"] == "mailbox"
    wallet.spend.assert_not_awaited()
    wallet.credit_platform.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_ttl_clamped_to_free_window(tmp_path) -> None:
    svc = _service(tmp_path, blob_free_ttl_seconds=3600, blob_max_ttl_seconds=86400)
    result = await svc.put(
        owner_id="agent-a",
        data=b"hello",
        name="hi.txt",
        mime_type="text/plain",
        ttl_seconds=86400,
    )
    assert result["exp"] <= int(time.time()) + 3600 + 2


@pytest.mark.asyncio
async def test_too_large_file(tmp_path) -> None:
    svc = _service(tmp_path, blob_max_file_bytes=3)
    with pytest.raises(ACNHTTPError) as ei:
        await svc.put(
            owner_id="agent-a",
            data=b"abcd",
            name="hi.txt",
            mime_type="text/plain",
        )
    assert ei.value.code is ErrorCode.BLOB_TOO_LARGE


@pytest.mark.asyncio
async def test_signed_get_and_owner_get(tmp_path) -> None:
    svc = _service(tmp_path)
    put = await svc.put(
        owner_id="agent-a",
        data=b"payload",
        name="x.bin",
        mime_type="application/octet-stream",
    )
    blob_id = put["id"]
    data, meta = await svc.get_bytes(blob_id, caller_id="agent-a", sig=None)
    assert data == b"payload"
    assert meta["name"] == "x.bin"

    sig = _uri_sig(put["uri"])
    assert "exp=" not in put["uri"]
    data2, _ = await svc.get_bytes(blob_id, caller_id=None, sig=sig)
    assert data2 == b"payload"

    with pytest.raises(ACNHTTPError) as ei:
        await svc.get_bytes(blob_id, caller_id="agent-b", sig=None)
    assert ei.value.code is ErrorCode.BLOB_NOT_FOUND

    with pytest.raises(ACNHTTPError) as ei:
        await svc.get_bytes(blob_id, caller_id=None, sig="ab")
    assert ei.value.code is ErrorCode.BLOB_NOT_FOUND


@pytest.mark.asyncio
async def test_consumer_extend_charges_and_leaves_sender_mailbox(tmp_path) -> None:
    wallet = _wallet_ok()
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
    )
    data = b"x" * 1_048_576
    put = await svc.put(
        owner_id="agent-a",
        data=data,
        name="out.bin",
        mime_type="application/octet-stream",
    )
    wallet.spend.assert_not_awaited()
    sig = _uri_sig(put["uri"])
    extended = await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    wallet.spend.assert_awaited_once()
    assert wallet.spend.await_args.args[0] == "agent-b"
    assert wallet.spend.await_args.args[1] == 1
    assert wallet.spend.await_args.kwargs["idempotency_key"] == (
        f"blob_extend:{put['id']}:agent-b:{put['exp']}:86400"
    )
    wallet.credit_platform.assert_awaited_once()
    assert wallet.credit_platform.await_args.args[0] == "w_plat_test_acn_revenue"
    assert wallet.credit_platform.await_args.args[1] == 1
    assert wallet.credit_platform.await_args.kwargs["idempotency_key"].startswith("rev:blob_extend:")
    assert extended["charged"] == 1
    assert extended["owner_id"] == "agent-b"
    assert extended["retained"] is True
    assert _uri_sig(extended["uri"]) == sig
    data2, _ = await svc.get_bytes(put["id"], caller_id=None, sig=sig)
    assert data2 == data
    usage_a = await svc.usage("agent-a")
    usage_b = await svc.usage("agent-b")
    assert usage_a["mailbox_bytes"] == 0
    assert usage_b["retained_bytes"] == 1_048_576


@pytest.mark.asyncio
async def test_extend_insufficient_balance_does_not_transfer(tmp_path) -> None:
    wallet = AsyncMock()
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False, message="no", error="empty", status_code=400
        )
    )
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.INSUFFICIENT_BALANCE
    wallet.credit_platform.assert_not_awaited()
    assert await svc._load_pending(put["id"], "agent-b") is None
    usage_a = await svc.usage("agent-a")
    assert usage_a["mailbox_bytes"] == 1_048_576
    assert (await svc.usage("agent-b"))["retained_bytes"] == 0
    meta = await svc._load_meta(put["id"])
    assert meta is not None
    meta["exp"] = 1
    await svc._write_meta(put["id"], meta, int(time.time()))
    with pytest.raises(ACNHTTPError) as expired:
        await svc.get_bytes(put["id"], caller_id="agent-a", sig=None)
    assert expired.value.code is ErrorCode.BLOB_NOT_FOUND
    assert await svc.store.get(put["id"]) is None


@pytest.mark.asyncio
async def test_extend_insufficient_near_expiry_does_not_clamp_ttl(
    tmp_path, monkeypatch
) -> None:
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    wallet = AsyncMock()
    wallet.timeout = 30.0
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False, message="no", error="empty", status_code=400
        )
    )
    svc = _extend_svc(tmp_path, wallet)
    payload = b"drop-me"
    put, sig = await _put_near_expiry(svc, owner="agent-a", payload=payload, clock=clock)
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.INSUFFICIENT_BALANCE
    assert await svc._load_pending(put["id"], "agent-b") is None
    clock["t"] += 200.0
    with pytest.raises(ACNHTTPError) as expired:
        await svc.get_bytes(put["id"], caller_id="agent-a", sig=None)
    assert expired.value.code is ErrorCode.BLOB_NOT_FOUND
    assert await svc.store.get(put["id"]) is None


@pytest.mark.asyncio
async def test_extend_wallet_timeout_keeps_pending_near_expiry(
    tmp_path, monkeypatch
) -> None:
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    wallet = AsyncMock()
    wallet.timeout = 30.0
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False,
            message="Wallet service unavailable",
            error="timed out",
        )
    )
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
        blob_max_ttl_seconds=90 * 86400,
        blob_free_ttl_seconds=3600,
    )
    payload = b"keep-me"
    put = await svc.put(
        owner_id="agent-a",
        data=payload,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    meta = await svc._load_meta(put["id"])
    assert meta is not None
    meta["exp"] = int(clock["t"]) + 5
    await svc._write_meta(put["id"], meta, int(clock["t"]))
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.BLOB_BILLING_UNAVAILABLE
    wallet.credit_platform.assert_not_awaited()
    clock["t"] += 200.0
    data, _ = await svc.get_bytes(put["id"], caller_id="agent-a", sig=None)
    assert data == payload
    pending = await svc._load_pending(put["id"], "agent-b")
    assert pending is not None
    assert pending["payer_id"] == "agent-b"


async def _put_near_expiry(
    svc: BlobService, *, owner: str, payload: bytes, clock: dict[str, float]
) -> tuple[dict, str]:
    put = await svc.put(
        owner_id=owner,
        data=payload,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    meta = await svc._load_meta(put["id"])
    assert meta is not None
    meta["exp"] = int(clock["t"]) + 5
    await svc._write_meta(put["id"], meta, int(clock["t"]))
    return put, _uri_sig(put["uri"])


@pytest.mark.asyncio
async def test_extend_insufficient_does_not_clear_other_payer_pending(
    tmp_path, monkeypatch
) -> None:
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    wallet = AsyncMock()
    wallet.timeout = 30.0
    wallet.spend = AsyncMock(
        side_effect=[
            WalletResult(
                success=False,
                message="Wallet service unavailable",
                error="timed out",
            ),
            WalletResult(success=False, message="no", error="empty", status_code=400),
        ]
    )
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
        blob_max_ttl_seconds=90 * 86400,
        blob_free_ttl_seconds=3600,
    )
    payload = b"keep-paid"
    put, sig = await _put_near_expiry(svc, owner="agent-a", payload=payload, clock=clock)
    with pytest.raises(ACNHTTPError) as timeout:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert timeout.value.code is ErrorCode.BLOB_BILLING_UNAVAILABLE
    assert (await svc._load_pending(put["id"], "agent-b"))["payer_id"] == "agent-b"
    with pytest.raises(ACNHTTPError) as broke:
        await svc.extend(put["id"], "agent-a", 86400)
    assert broke.value.code is ErrorCode.INSUFFICIENT_BALANCE
    pending = await svc._load_pending(put["id"], "agent-b")
    assert pending is not None
    assert pending["payer_id"] == "agent-b"
    clock["t"] += 200.0
    data, _ = await svc.get_bytes(put["id"], caller_id="agent-a", sig=None)
    assert data == payload


@pytest.mark.asyncio
async def test_extend_insufficient_does_not_refresh_timeout_pending(
    tmp_path, monkeypatch
) -> None:
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    wallet = AsyncMock()
    wallet.timeout = 30.0
    wallet.spend = AsyncMock(
        side_effect=[
            WalletResult(
                success=False,
                message="Wallet service unavailable",
                error="timed out",
            ),
            WalletResult(success=False, message="no", error="empty", status_code=400),
        ]
    )
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
        blob_max_ttl_seconds=90 * 86400,
        blob_free_ttl_seconds=3600,
    )
    put, sig = await _put_near_expiry(svc, owner="agent-a", payload=b"x", clock=clock)
    with pytest.raises(ACNHTTPError):
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    original = await svc._load_pending(put["id"], "agent-b")
    assert original is not None
    saved = AsyncMock(wraps=svc._save_pending)
    svc._save_pending = saved  # type: ignore[method-assign]
    with pytest.raises(ACNHTTPError) as broke:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert broke.value.code is ErrorCode.INSUFFICIENT_BALANCE
    saved.assert_not_awaited()
    assert await svc._load_pending(put["id"], "agent-b") == original


def _extend_svc(tmp_path, wallet: object) -> BlobService:
    return _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
        blob_max_ttl_seconds=90 * 86400,
        blob_free_ttl_seconds=3600,
    )


@pytest.mark.asyncio
async def test_extend_spend_500_keeps_pending_without_timeout_wording(
    tmp_path, monkeypatch
) -> None:
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    wallet = AsyncMock()
    wallet.timeout = 30.0
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False,
            message="Failed to spend",
            error="Internal Server Error",
            status_code=500,
        )
    )
    svc = _extend_svc(tmp_path, wallet)
    payload = b"keep-5xx"
    put, sig = await _put_near_expiry(svc, owner="agent-a", payload=payload, clock=clock)
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.BLOB_BILLING_UNAVAILABLE
    wallet.credit_platform.assert_not_awaited()
    pending = await svc._load_pending(put["id"], "agent-b")
    assert pending is not None
    clock["t"] += 200.0
    data, _ = await svc.get_bytes(put["id"], caller_id="agent-a", sig=None)
    assert data == payload


@pytest.mark.asyncio
async def test_extend_spend_400_is_insufficient_even_with_timeout_wording(
    tmp_path,
) -> None:
    wallet = AsyncMock()
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False,
            message="Failed to spend",
            error="timed out",
            status_code=400,
        )
    )
    svc = _extend_svc(tmp_path, wallet)
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 32,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.INSUFFICIENT_BALANCE
    assert await svc._load_pending(put["id"], "agent-b") is None
    assert await svc._retain_expired(put["id"]) is False


@pytest.mark.asyncio
async def test_extend_timeout_pending_is_per_payer(tmp_path, monkeypatch) -> None:
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    wallet = AsyncMock()
    wallet.timeout = 30.0
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False,
            message="Wallet service unavailable",
            error="timed out",
        )
    )
    svc = _extend_svc(tmp_path, wallet)
    payload = b"two-payers"
    put, sig = await _put_near_expiry(svc, owner="agent-a", payload=payload, clock=clock)
    old_exp = int(clock["t"]) + 5
    with pytest.raises(ACNHTTPError):
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    b_pending = await svc._load_pending(put["id"], "agent-b")
    assert b_pending is not None
    assert b_pending["old_exp"] == old_exp
    with pytest.raises(ACNHTTPError):
        await svc.extend(put["id"], "agent-c", 172800, sig=sig)
    b_after = await svc._load_pending(put["id"], "agent-b")
    c_pending = await svc._load_pending(put["id"], "agent-c")
    assert b_after == b_pending
    assert c_pending is not None
    assert c_pending["payer_id"] == "agent-c"
    assert c_pending["extra_seconds"] == 172800
    clock["t"] += 200.0
    data, _ = await svc.get_bytes(put["id"], caller_id="agent-a", sig=None)
    assert data == payload
    expected_b = BlobService._extend_idempotency_key(put["id"], "agent-b", old_exp, 86400)
    keys: list[str] = []

    async def spend(*_args, **kwargs):
        keys.append(str(kwargs.get("idempotency_key")))
        return WalletResult(success=True, message="ok")

    wallet.spend = spend
    wallet.credit_platform = AsyncMock(return_value=WalletResult(success=True, message="ok"))
    extended = await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert extended["retained"] is True
    assert keys == [expected_b]
    assert await svc._load_pending(put["id"], "agent-b") is None
    assert await svc._load_pending(put["id"], "agent-c") is not None


@pytest.mark.asyncio
async def test_extend_repeat_timeout_does_not_refresh_pending(
    tmp_path, monkeypatch
) -> None:
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    wallet = AsyncMock()
    wallet.timeout = 30.0
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False,
            message="Wallet service unavailable",
            error="timed out",
        )
    )
    svc = _extend_svc(tmp_path, wallet)
    put, sig = await _put_near_expiry(svc, owner="agent-a", payload=b"x", clock=clock)
    with pytest.raises(ACNHTTPError):
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    original = await svc._load_pending(put["id"], "agent-b")
    assert original is not None
    saved = AsyncMock(wraps=svc._save_pending)
    svc._save_pending = saved  # type: ignore[method-assign]
    with pytest.raises(ACNHTTPError) as again:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert again.value.code is ErrorCode.BLOB_BILLING_UNAVAILABLE
    saved.assert_not_awaited()
    assert await svc._load_pending(put["id"], "agent-b") == original


@pytest.mark.asyncio
async def test_purge_drops_expired_blob_after_pending_cleared(
    tmp_path, monkeypatch
) -> None:
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    wallet = AsyncMock()
    wallet.timeout = 30.0
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False,
            message="Wallet service unavailable",
            error="timed out",
        )
    )
    svc = _extend_svc(tmp_path, wallet)
    payload = b"gc-me"
    put, sig = await _put_near_expiry(svc, owner="agent-a", payload=payload, clock=clock)
    with pytest.raises(ACNHTTPError):
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    clock["t"] += 200.0
    now = int(clock["t"])
    assert await svc.purge_expired(now=now) == 0
    assert await svc.store.get(put["id"]) == payload
    await svc._clear_pending(put["id"], "agent-b")
    assert await svc.purge_expired(now=now) == 1
    assert await svc.store.get(put["id"]) is None


@pytest.mark.asyncio
async def test_purge_expired_deletes_bytes(tmp_path) -> None:
    svc = _service(tmp_path)
    put = await svc.put(
        owner_id="agent-a",
        data=b"payload",
        name="x.bin",
        mime_type="application/octet-stream",
    )
    blob_id = put["id"]
    assert await svc.store.get(blob_id) == b"payload"
    purged = await svc.purge_expired(now=put["exp"] + 1)
    assert purged == 1
    assert await svc.store.get(blob_id) is None
    assert await svc._load_meta(blob_id) is None
    assert (await svc.usage("agent-a"))["mailbox_bytes"] == 0


@pytest.mark.asyncio
async def test_get_expired_purges_file(tmp_path) -> None:
    svc = _service(tmp_path)
    put = await svc.put(
        owner_id="agent-a",
        data=b"payload",
        name="x.bin",
        mime_type="application/octet-stream",
    )
    blob_id = put["id"]
    meta = await svc._load_meta(blob_id)
    assert meta is not None
    meta["exp"] = 1
    await svc.redis.set(svc._meta_key(blob_id), json.dumps(meta))
    with pytest.raises(ACNHTTPError) as ei:
        await svc.get_bytes(blob_id, caller_id="agent-a", sig=None)
    assert ei.value.code is ErrorCode.BLOB_NOT_FOUND
    assert await svc.store.get(blob_id) is None


@pytest.mark.asyncio
async def test_invalid_blob_id_is_not_found(tmp_path) -> None:
    svc = _service(tmp_path)
    with pytest.raises(ACNHTTPError) as ei:
        await svc.get_bytes("../etc/passwd", caller_id=None, sig=None)
    assert ei.value.code is ErrorCode.BLOB_NOT_FOUND


def test_credits_one_gib_day(tmp_path) -> None:
    svc = _service(tmp_path)
    assert svc._credits_for(GIB, 86400) == 1
    assert svc._credits_for(1, 86400) == 1
    assert svc._credits_for(GIB, 86400 * 2) == 2
    assert svc._credits_for(0, 86400) == 0


@pytest.mark.asyncio
async def test_put_redis_failure_deletes_bytes(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = FilesystemBlobStore(tmp_path)
    svc = BlobService(
        BoomOnSetRedis(fail_on=1),  # type: ignore[arg-type]
        store,
        settings,  # type: ignore[arg-type]
        wallet=None,
    )
    with pytest.raises(RuntimeError, match="redis down"):
        await svc.put(
            owner_id="agent-a",
            data=b"payload",
            name="x.bin",
            mime_type="application/octet-stream",
        )
    assert await store.list_ids() == []


@pytest.mark.asyncio
async def test_extend_persist_failure_does_not_charge(tmp_path) -> None:
    wallet = AsyncMock()
    wallet.spend = AsyncMock(return_value=WalletResult(success=True, message="ok"))
    settings = _settings(
        tmp_path,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
        dev_mode=False,
    )
    redis = BoomOnSetRedis(fail_on=2)
    svc = BlobService(
        redis,  # type: ignore[arg-type]
        FilesystemBlobStore(tmp_path),
        settings,  # type: ignore[arg-type]
        wallet,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(RuntimeError, match="redis down"):
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    wallet.spend.assert_not_awaited()
    wallet.credit_platform.assert_not_awaited()
    usage_a = await svc.usage("agent-a")
    assert usage_a["mailbox_bytes"] == 1_048_576
    assert (await svc.usage("agent-b"))["retained_bytes"] == 0


@pytest.mark.asyncio
async def test_purge_expired_drains_beyond_batch_limit(tmp_path) -> None:
    svc = _service(tmp_path)
    ids = []
    exp = 0
    for i in range(3):
        put = await svc.put(
            owner_id="agent-a",
            data=f"p{i}".encode(),
            name=f"{i}.bin",
            mime_type="application/octet-stream",
        )
        ids.append(put["id"])
        exp = put["exp"]
    purged = await svc.purge_expired(now=exp + 1, limit=1, max_batches=10)
    assert purged == 3
    for blob_id in ids:
        assert await svc.store.get(blob_id) is None


@pytest.mark.asyncio
async def test_purge_orphans_deletes_file_without_meta(tmp_path) -> None:
    svc = _service(tmp_path)
    blob_id = "11111111-1111-1111-1111-111111111111"
    await svc.store.put(blob_id, b"orphan")
    purged = await svc.purge_expired(now=int(time.time()), limit=10)
    assert purged >= 1
    assert await svc.store.get(blob_id) is None


@pytest.mark.asyncio
async def test_purge_reindexes_live_file_missing_from_zset(tmp_path) -> None:
    svc = _service(tmp_path)
    put = await svc.put(
        owner_id="agent-a",
        data=b"payload",
        name="x.bin",
        mime_type="application/octet-stream",
    )
    blob_id = put["id"]
    await svc.redis.zrem("acn:blob:expiry", blob_id)
    assert await svc.redis.zscore("acn:blob:expiry", blob_id) is None
    purged = await svc.purge_expired(now=int(time.time()) - 10)
    assert purged == 0
    assert await svc.store.get(blob_id) == b"payload"
    assert await svc.redis.zscore("acn:blob:expiry", blob_id) == put["exp"]


@pytest.mark.asyncio
async def test_extend_sadd_failure_restores_sender_quota(tmp_path) -> None:
    settings = _settings(
        tmp_path,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
    )
    redis = BoomOnSaddRedis("acn:blob:owner:agent-b")
    svc = BlobService(
        redis,  # type: ignore[arg-type]
        FilesystemBlobStore(tmp_path),
        settings,  # type: ignore[arg-type]
        wallet=AsyncMock(),
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(RuntimeError, match="redis down"):
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert (await svc.usage("agent-a"))["mailbox_bytes"] == 1_048_576
    assert (await svc.usage("agent-b"))["retained_bytes"] == 0


@pytest.mark.asyncio
async def test_checksum_mismatch_purges_and_404(tmp_path) -> None:
    svc = _service(tmp_path)
    put = await svc.put(
        owner_id="agent-a",
        data=b"payload",
        name="x.bin",
        mime_type="application/octet-stream",
    )
    blob_id = put["id"]
    path = tmp_path / blob_id[:2] / blob_id
    path.write_bytes(b"corrupt")
    with pytest.raises(ACNHTTPError) as ei:
        await svc.get_bytes(blob_id, caller_id="agent-a", sig=None)
    assert ei.value.code is ErrorCode.BLOB_NOT_FOUND
    assert await svc.store.get(blob_id) is None


@pytest.mark.asyncio
async def test_extend_wallet_timeout_is_billing_unavailable(tmp_path) -> None:
    wallet = AsyncMock()
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False, message="Wallet service unavailable", error="timeout"
        )
    )
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.BLOB_BILLING_UNAVAILABLE
    wallet.credit_platform.assert_not_awaited()
    assert (await svc.usage("agent-a"))["mailbox_bytes"] == 1_048_576


def test_safe_blob_filename_strips_path() -> None:
    assert safe_blob_filename("../../tmp/pwned") == "pwned"
    assert safe_blob_filename("/etc/passwd") == "passwd"
    assert safe_blob_filename("..\\..\\tmp\\pwned") == "pwned"
    assert safe_blob_filename("hi.txt") == "hi.txt"
    assert safe_blob_filename(".") == "file"
    assert safe_blob_filename("") == "file"


@pytest.mark.asyncio
async def test_put_stores_basename_only(tmp_path) -> None:
    svc = _service(tmp_path)
    put = await svc.put(
        owner_id="agent-a",
        data=b"payload",
        name="../../tmp/pwned.txt",
        mime_type="text/plain",
    )
    assert put["name"] == "pwned.txt"


def test_safe_blob_mime_type_rejects_header_injection() -> None:
    assert safe_blob_mime_type("text/plain") == "text/plain"
    assert safe_blob_mime_type("TEXT/HTML") == "text/html"
    assert safe_blob_mime_type("image/svg+xml") == "image/svg+xml"
    assert safe_blob_mime_type("text/plain; charset=utf-8") == "text/plain"
    assert safe_blob_mime_type("text/plain\r\nLocation: evil") == (
        "application/octet-stream"
    )
    assert safe_blob_mime_type("text/plain\nX-Injected: 1") == "application/octet-stream"
    assert safe_blob_mime_type("") == "application/octet-stream"


@pytest.mark.asyncio
async def test_put_sanitizes_injected_mime(tmp_path) -> None:
    svc = _service(tmp_path)
    put = await svc.put(
        owner_id="agent-a",
        data=b"payload",
        name="hi.txt",
        mime_type="text/plain\r\nLocation: https://evil.example",
    )
    assert put["mime_type"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_release_lock_does_not_delete_stolen_token(tmp_path) -> None:
    svc = _service(tmp_path)
    token = await svc._try_lock("acn:blob:lock:x", ttl=30)
    assert token is not None
    await svc.redis.set("acn:blob:lock:x", "stolen")
    await svc._release_lock("acn:blob:lock:x", token)
    assert await svc.redis.get("acn:blob:lock:x") == "stolen"


@pytest.mark.asyncio
async def test_extend_busy_lock_is_conflict(tmp_path) -> None:
    wallet = AsyncMock()
    wallet.spend = AsyncMock(return_value=WalletResult(success=True, message="ok"))
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
        blob_max_ttl_seconds=90 * 86400,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    await svc.redis.set(f"acn:blob:lock:{put['id']}", "1")
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.RESOURCE_CONFLICT
    assert ei.value.details["reason"] == "blob_busy"
    wallet.spend.assert_not_awaited()
    wallet.credit_platform.assert_not_awaited()


@pytest.mark.asyncio
async def test_sequential_extend_reads_fresh_exp(tmp_path) -> None:
    wallet = _wallet_ok()
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
        blob_max_ttl_seconds=90 * 86400,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    first = await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    second = await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert second["exp"] == first["exp"] + 86400
    assert wallet.spend.await_count == 2
    assert wallet.credit_platform.await_count == 2


def test_extend_lock_ttl_covers_spend_and_credit_timeouts(tmp_path) -> None:
    wallet = SimpleNamespace(timeout=30.0)
    svc = _service(tmp_path, wallet=wallet)
    assert svc._extend_lock_ttl() >= 135
    svc.wallet = SimpleNamespace(timeout=60.0)
    assert svc._extend_lock_ttl() >= 255


@pytest.mark.asyncio
async def test_prod_extend_without_revenue_wallet_does_not_spend(tmp_path) -> None:
    wallet = _wallet_ok()
    svc = _service(
        tmp_path,
        wallet=wallet,
        dev_mode=False,
        acn_revenue_wallet_id="",
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.BLOB_BILLING_UNAVAILABLE
    assert ei.value.details["reason"] == "no_revenue_wallet"
    wallet.spend.assert_not_awaited()
    wallet.credit_platform.assert_not_awaited()
    assert (await svc.usage("agent-a"))["mailbox_bytes"] == 1_048_576
    assert (await svc.usage("agent-b"))["retained_bytes"] == 0


@pytest.mark.asyncio
async def test_dev_extend_without_revenue_wallet_does_not_spend(tmp_path) -> None:
    wallet = _wallet_ok()
    svc = _service(
        tmp_path,
        wallet=wallet,
        dev_mode=True,
        acn_revenue_wallet_id="",
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.BLOB_BILLING_UNAVAILABLE
    assert ei.value.details["reason"] == "no_revenue_wallet"
    wallet.spend.assert_not_awaited()
    wallet.credit_platform.assert_not_awaited()
    assert (await svc.usage("agent-a"))["mailbox_bytes"] == 1_048_576


@pytest.mark.asyncio
async def test_extend_rolls_back_when_revenue_credit_fails(tmp_path) -> None:
    wallet = AsyncMock()
    wallet.spend = AsyncMock(return_value=WalletResult(success=True, message="ok"))
    wallet.credit_platform = AsyncMock(
        return_value=WalletResult(success=False, message="no", error="down")
    )
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.BLOB_BILLING_UNAVAILABLE
    assert ei.value.details["reason"] == "down"
    wallet.spend.assert_awaited_once()
    wallet.credit_platform.assert_awaited_once()
    assert (await svc.usage("agent-a"))["mailbox_bytes"] == 1_048_576
    assert (await svc.usage("agent-b"))["retained_bytes"] == 0


@pytest.mark.asyncio
async def test_extend_idempotency_key_stable_when_ttl_cap_binds(
    tmp_path, monkeypatch
) -> None:
    keys: list[str] = []
    amounts: list[int] = []
    wallet = AsyncMock()

    async def spend(*args, **kwargs):
        keys.append(str(kwargs.get("idempotency_key")))
        amounts.append(int(args[1]))
        return WalletResult(success=True, message="ok")

    wallet.spend = spend
    wallet.credit_platform = AsyncMock(
        return_value=WalletResult(success=False, message="no", error="down")
    )
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=GIB + 1,
        blob_max_agent_bytes=GIB + 1,
        blob_free_bytes=GIB + 1,
        blob_max_ttl_seconds=90 * 86400,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x",
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    now0 = int(time.time())
    meta = await svc._load_meta(put["id"])
    assert meta is not None
    meta["size"] = GIB
    meta["exp"] = now0 + 80 * 86400
    await svc._write_meta(put["id"], meta, now0)
    extra = 30 * 86400
    expected_key = BlobService._extend_idempotency_key(
        put["id"], "agent-b", int(meta["exp"]), extra
    )
    expected_credits = svc._credits_for(GIB, extra)
    assert expected_credits == 30
    sig = _uri_sig(put["uri"])
    clock = {"t": float(now0)}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    with pytest.raises(ACNHTTPError):
        await svc.extend(put["id"], "agent-b", extra, sig=sig)
    clock["t"] += 40.0
    with pytest.raises(ACNHTTPError):
        await svc.extend(put["id"], "agent-b", extra, sig=sig)
    assert keys == [expected_key, expected_key]
    assert amounts == [expected_credits, expected_credits]


@pytest.mark.asyncio
async def test_extend_spend_conflict_is_not_insufficient_balance(tmp_path) -> None:
    wallet = AsyncMock()
    wallet.spend = AsyncMock(
        return_value=WalletResult(
            success=False,
            message="Failed to spend",
            error="Idempotency key already used with different parameters",
            status_code=409,
        )
    )
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
    )
    put = await svc.put(
        owner_id="agent-a",
        data=b"x" * 1_048_576,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", 86400, sig=sig)
    assert ei.value.code is ErrorCode.RESOURCE_CONFLICT
    assert ei.value.details["reason"] == "idempotency_conflict"
    wallet.credit_platform.assert_not_awaited()
    assert await svc._load_pending(put["id"], "agent-b") is None
    assert (await svc.usage("agent-a"))["mailbox_bytes"] == 1_048_576
    assert (await svc.usage("agent-b"))["retained_bytes"] == 0
    meta = await svc._load_meta(put["id"])
    assert meta is not None
    meta["exp"] = 1
    await svc._write_meta(put["id"], meta, int(time.time()))
    with pytest.raises(ACNHTTPError) as expired:
        await svc.get_bytes(put["id"], caller_id="agent-a", sig=None)
    assert expired.value.code is ErrorCode.BLOB_NOT_FOUND
    assert await svc.store.get(put["id"]) is None


@pytest.mark.asyncio
async def test_extend_near_expiry_credit_fail_does_not_drop_blob(
    tmp_path, monkeypatch
) -> None:
    keys: list[str] = []
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr("acn.services.blob_service.time.time", lambda: clock["t"])
    wallet = AsyncMock()
    wallet.timeout = 30.0
    credits_n = {"n": 0}

    async def spend(*_args, **kwargs):
        keys.append(str(kwargs.get("idempotency_key")))
        clock["t"] += 30.0
        return WalletResult(success=True, message="ok")

    async def credit(*_args, **_kwargs):
        credits_n["n"] += 1
        if credits_n["n"] == 1:
            return WalletResult(success=False, message="no", error="down")
        return WalletResult(success=True, message="ok")

    wallet.spend = spend
    wallet.credit_platform = credit
    svc = _service(
        tmp_path,
        wallet=wallet,
        blob_max_file_bytes=2_000_000,
        blob_max_agent_bytes=5_000_000,
        blob_free_bytes=2_000_000,
        blob_max_ttl_seconds=90 * 86400,
        blob_free_ttl_seconds=3600,
    )
    payload = b"x" * 32
    put = await svc.put(
        owner_id="agent-a",
        data=payload,
        name="hi.bin",
        mime_type="application/octet-stream",
    )
    meta = await svc._load_meta(put["id"])
    assert meta is not None
    old_exp = int(clock["t"]) + 5
    meta["exp"] = old_exp
    await svc._write_meta(put["id"], meta, int(clock["t"]))
    extra = 86400
    expected = BlobService._extend_idempotency_key(
        put["id"], "agent-b", old_exp, extra
    )
    sig = _uri_sig(put["uri"])
    with pytest.raises(ACNHTTPError) as ei:
        await svc.extend(put["id"], "agent-b", extra, sig=sig)
    assert ei.value.code is ErrorCode.BLOB_BILLING_UNAVAILABLE
    after = await svc._load_meta(put["id"])
    assert after is not None
    assert await svc._load_pending(put["id"], "agent-b") is not None
    data, _ = await svc.get_bytes(put["id"], caller_id="agent-a", sig=None)
    assert data == payload
    clock["t"] += 200.0
    data2, _ = await svc.get_bytes(put["id"], caller_id="agent-a", sig=None)
    assert data2 == payload
    purged = await svc.purge_expired(now=int(clock["t"]))
    assert purged == 0
    extended = await svc.extend(put["id"], "agent-b", extra, sig=sig)
    assert extended["retained"] is True
    assert keys == [expected, expected]
    assert await svc._load_pending(put["id"], "agent-b") is None
