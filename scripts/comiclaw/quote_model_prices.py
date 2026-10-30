#!/usr/bin/env python3
"""EXAMPLE Comiclaw quote hook (TokenPlan) — not Host-operated pricing.

Agents own their prices: fetch from your provider, keep a local book if you
want, report Host market currency. Copy this pattern; do not expect Host
staff to maintain your ``model_prices`` file.

Local TokenPlan stickers and scraped ¥ pages are CNY. stdout is Host market
currency (AgentPlanet: USD / million tokens, no currency field) — see
``COMICLAW_CNY_PER_USD`` (default 7.2).
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

PROVIDER = (os.environ.get("ACN_PROVIDER") or "tencenttokenplan").strip() or "tencenttokenplan"
MODELS_URL = (
    os.environ.get("ACN_PROVIDER_MODELS_URL")
    or "https://api.lkeap.cloud.tencent.com/plan/v3/models"
).strip()
API_KEY = (os.environ.get("ACN_PROVIDER_API_KEY") or "").strip()
LOCAL_BOOK = (
    os.environ.get("COMICLAW_MODEL_PRICES_FILE")
    or os.path.expanduser("~/.config/comiclaw/model_prices.json")
)


def _finite(v: Any) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n or n < 0:
        return None
    return n


def _cny_per_usd() -> float:
    n = _finite(os.environ.get("COMICLAW_CNY_PER_USD") or "7.2")
    return n if n and n > 0 else 7.2


def _host_currency() -> str:
    return (os.environ.get("COMICLAW_HOST_CURRENCY") or "USD").strip().upper() or "USD"


def _cny_to_host(inn_cny: float, out_cny: float) -> tuple[float, float]:
    """Convert vendor CNY stickers to Host market units."""
    if _host_currency() == "CNY":
        return inn_cny, out_cny
    rate = _cny_per_usd()
    return inn_cny / rate, out_cny / rate


def _round_host(n: float) -> float:
    # Enough for menu stickers; avoid binary noise.
    return float(f"{n:.6g}")


def _bare(mid: str) -> str:
    mid = mid.strip()
    return mid.split("/", 1)[-1].strip() if "/" in mid else mid


def _norm_id(mid: str) -> str:
    mid = mid.strip()
    if not mid:
        return ""
    return mid if "/" in mid else f"{PROVIDER}/{mid}"


def _row_prices_cny(row: dict[str, Any]) -> tuple[float, float] | None:
    """Extract a CNY pair from a catalog / book row."""
    # Prefer explicit CNY keys.
    for src in (row, row.get("pricing") if isinstance(row.get("pricing"), dict) else None):
        if not isinstance(src, dict):
            continue
        inn = _finite(src.get("input_price_cny_per_million"))
        out = _finite(src.get("output_price_cny_per_million"))
        if inn is not None and out is not None:
            return inn, out

    candidates = [
        ("input_price_per_million", "output_price_per_million"),
        ("prompt_price_per_million", "completion_price_per_million"),
        ("input_price", "output_price"),
        ("price_in", "price_out"),
        ("prompt", "completion"),
    ]
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else None
    sources = [row] + ([pricing] if pricing else [])
    for src in sources:
        for ik, ok in candidates:
            inn = _finite(src.get(ik))
            out = _finite(src.get(ok))
            if inn is None or out is None:
                continue
            if ik in ("input_price", "output_price", "prompt", "completion") and inn < 0.0001 and out < 0.0001:
                inn *= 1_000_000
                out *= 1_000_000
            # TokenPlan / local seed store CNY under these names.
            return inn, out
    return None


def fetch_catalog() -> list[dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(MODELS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return []
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return [r for r in body["data"] if isinstance(r, dict)]
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    return []


def load_local_book() -> dict[str, tuple[float, float]]:
    """model_id → CNY (in, out) per million."""
    path = LOCAL_BOOK
    if not path or not os.path.isfile(path):
        return {}
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, tuple[float, float]] = {}
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        mid = _norm_id(str(row.get("model_id") or ""))
        pair = _row_prices_cny(row)
        if mid and pair is not None:
            out[mid.lower()] = pair
            out[_bare(mid).lower()] = pair
    return out


def index_catalog(rows: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    """Catalog id → CNY pair (TokenPlan vendor currency)."""
    idx: dict[str, tuple[float, float]] = {}
    for row in rows:
        rid = str(row.get("id") or row.get("model") or row.get("model_id") or "").strip()
        if not rid:
            continue
        pair = _row_prices_cny(row)
        if not pair:
            continue
        full = _norm_id(rid)
        idx[full.lower()] = pair
        idx[_bare(rid).lower()] = pair
    return idx


def fetch_tokenplan_page(bare: str) -> tuple[float, float] | None:
    """Scrape TokenPlan model page; amounts are ¥ (CNY)."""
    url = f"https://tokenplan.youdao.com/models/{bare}"
    req = urllib.request.Request(url, headers={"User-Agent": "comiclaw-quote/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError):
        return None

    def grab(label: str) -> float | None:
        m = re.search(rf"{label}[^¥\d]{{0,40}}¥\s*([0-9]+(?:\.[0-9]+)?)", html, flags=re.I)
        return _finite(m.group(1)) if m else None

    inn = grab("标准输入") or grab("输入Token") or grab("输入")
    out = grab("标准输出") or grab("输出Token") or grab("输出")
    if inn is not None and out is not None:
        return inn, out
    return None


def main() -> int:
    try:
        ask = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("invalid_json", file=sys.stderr)
        return 1
    if not isinstance(ask, dict):
        print("invalid_json", file=sys.stderr)
        return 1
    ids = ask.get("model_ids") if isinstance(ask.get("model_ids"), list) else []
    model_ids: list[str] = []
    seen: set[str] = set()
    for item in ids:
        mid = str(item or "").strip()[:200]
        if not mid:
            continue
        key = mid.lower()
        if key in seen:
            continue
        seen.add(key)
        model_ids.append(mid)
        if len(model_ids) >= 50:
            break
    retry = bool(ask.get("retry"))
    local = load_local_book()
    catalog_idx = index_catalog(fetch_catalog())
    prices = []
    for mid in model_ids:
        key = mid.lower()
        bare = _bare(mid).lower()
        pair_cny = local.get(key) or local.get(bare) or catalog_idx.get(key) or catalog_idx.get(bare)
        if pair_cny is None and retry:
            pair_cny = fetch_tokenplan_page(_bare(mid))
        if pair_cny is None:
            continue
        inn_h, out_h = _cny_to_host(pair_cny[0], pair_cny[1])
        prices.append(
            {
                "model_id": mid,
                "input_price_per_million": _round_host(inn_h),
                "output_price_per_million": _round_host(out_h),
            }
        )
    json.dump({"model_prices": prices}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
