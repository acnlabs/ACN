#!/usr/bin/env python3
"""Comiclaw quote hook: price Host-asked model ids in market currency (CNY)."""
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


def _bare(mid: str) -> str:
    mid = mid.strip()
    return mid.split("/", 1)[-1].strip() if "/" in mid else mid


def _norm_id(mid: str) -> str:
    mid = mid.strip()
    if not mid:
        return ""
    return mid if "/" in mid else f"{PROVIDER}/{mid}"


def _row_prices(row: dict[str, Any]) -> tuple[float, float] | None:
    candidates = [
        ("input_price_per_million", "output_price_per_million"),
        ("input_price_cny_per_million", "output_price_cny_per_million"),
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
        inn = _finite(row.get("input_price_per_million"))
        outp = _finite(row.get("output_price_per_million"))
        if mid and inn is not None and outp is not None:
            out[mid.lower()] = (inn, outp)
            out[_bare(mid).lower()] = (inn, outp)
    return out


def index_catalog(rows: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    idx: dict[str, tuple[float, float]] = {}
    for row in rows:
        rid = str(row.get("id") or row.get("model") or row.get("model_id") or "").strip()
        if not rid:
            continue
        pair = _row_prices(row)
        if not pair:
            continue
        full = _norm_id(rid)
        idx[full.lower()] = pair
        idx[_bare(rid).lower()] = pair
    return idx


def fetch_tokenplan_page(bare: str) -> tuple[float, float] | None:
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
        pair = local.get(key) or local.get(bare) or catalog_idx.get(key) or catalog_idx.get(bare)
        if pair is None and retry:
            pair = fetch_tokenplan_page(_bare(mid))
        if pair is None:
            continue
        prices.append(
            {
                "model_id": mid,
                "input_price_per_million": pair[0],
                "output_price_per_million": pair[1],
            }
        )
    json.dump({"model_prices": prices}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
