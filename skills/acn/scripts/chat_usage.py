#!/usr/bin/env python3
"""Normalize hop totals into the Interfaze/Host `usage` contract.

Runtime-agnostic. You already measured the hop; this only renames aliases
and drops fields that must not be billed. CLI 1.0.3+ forwards the result.

Usage:
  python3 scripts/chat_usage.py totals.json
  python3 scripts/chat_usage.py < totals.json

Input is a JSON object. Fields may sit at the top level or under `usage`.
Known aliases (first hit wins):

  input            / input_tokens  / prompt_tokens
  output           / output_tokens / completion_tokens
  reasoning_tokens / reasoningTokens
  cache_read_tokens / cacheRead / cache_read
  cache_write_tokens / cacheWrite / cache_write
  total_tokens     / total
  duration_ms      / durationMs
  model_id         / model
  provider
  meter_source

Do not invent 0/0. Do not read last-call-only blobs or session paths.
Exit 0 and print {} when nothing usable is present.
"""
from __future__ import annotations

import json
import sys
from typing import Any


def load_json_object(raw: str) -> dict[str, Any]:
    raw = raw.lstrip("\ufeff")
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def as_int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and v >= 0:
        return int(v)
    return None


def first_int(*vals: Any) -> int | None:
    for v in vals:
        n = as_int(v)
        if n is not None:
            return n
    return None


def first_str(*vals: Any, limit: int = 200) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            s = v.strip()
            return s[:limit]
    return ""


def extract_usage(data: dict[str, Any]) -> dict[str, Any]:
    blob = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    def pick_int(*keys: str) -> int | None:
        for key in keys:
            for src in (blob, data):
                if key in src:
                    n = as_int(src[key])
                    if n is not None:
                        return n
        return None

    def pick_str(*keys: str, limit: int = 200) -> str:
        for key in keys:
            for src in (blob, data):
                if key in src:
                    s = first_str(src[key], limit=limit)
                    if s:
                        return s
        return ""

    out: dict[str, Any] = {}
    inp = pick_int("input_tokens", "input", "prompt_tokens")
    tok = pick_int("output_tokens", "output", "completion_tokens")
    if inp is not None or tok is not None:
        out["input_tokens"] = inp or 0
        out["output_tokens"] = tok or 0

    ms = None
    for src in (blob, data):
        raw_ms = src.get("meter_source")
        if raw_ms in ("peer_self", "gateway", "runtime_attested", "protocol"):
            ms = raw_ms
            break
    if ms:
        out["meter_source"] = ms
    elif "input_tokens" in out:
        out["meter_source"] = "peer_self"

    provider = pick_str("provider", limit=80)
    raw_model = pick_str("model_id", "model")
    if provider and raw_model and "/" not in raw_model:
        out["model_id"] = f"{provider}/{raw_model}"[:200]
    elif raw_model:
        out["model_id"] = raw_model[:200]
    if provider:
        out["provider"] = provider

    reason = pick_int("reasoning_tokens", "reasoningTokens")
    cache_r = pick_int("cache_read_tokens", "cacheRead", "cache_read")
    cache_w = pick_int("cache_write_tokens", "cacheWrite", "cache_write")
    total = pick_int("total_tokens", "total")
    duration = pick_int("duration_ms", "durationMs")
    if reason is not None:
        out["reasoning_tokens"] = reason
    if cache_r is not None:
        out["cache_read_tokens"] = cache_r
    if cache_w is not None:
        out["cache_write_tokens"] = cache_w
    if total is not None:
        out["total_tokens"] = total
    if duration is not None:
        out["duration_ms"] = duration
    return out


def _self_test() -> None:
    got = extract_usage(
        {
            "input": 42214,
            "output": 220,
            "reasoningTokens": 216,
            "total": 42434,
            "durationMs": 15876,
            "provider": "tencenttokenplan",
            "model": "kimi-k2.5",
            "lastCallUsage": {"input": 1, "output": 1},
            "sessionFile": "/tmp/x.jsonl",
        }
    )
    expect = {
        "input_tokens": 42214,
        "output_tokens": 220,
        "meter_source": "peer_self",
        "model_id": "tencenttokenplan/kimi-k2.5",
        "provider": "tencenttokenplan",
        "reasoning_tokens": 216,
        "total_tokens": 42434,
        "duration_ms": 15876,
    }
    if got != expect:
        raise SystemExit(f"self-test failed: {got}")
    nested = extract_usage({"usage": {"input_tokens": 3, "output_tokens": 1}})
    if nested != {
        "input_tokens": 3,
        "output_tokens": 1,
        "meter_source": "peer_self",
    }:
        raise SystemExit(f"nested usage failed: {nested}")
    if extract_usage({"sessionId": "x", "lastCallUsage": {"input": 9}}) != {}:
        raise SystemExit("must ignore session / last-call blobs")
    fallback = extract_usage(
        {
            "usage": {
                "input_tokens": None,
                "output_tokens": "x",
                "input": 10,
                "output": 2,
                "model_id": "",
                "model": "kimi-k2.5",
            }
        }
    )
    if fallback.get("input_tokens") != 10 or fallback.get("output_tokens") != 2:
        raise SystemExit(f"alias fallback failed: {fallback}")
    if fallback.get("model_id") != "kimi-k2.5":
        raise SystemExit(f"model alias fallback failed: {fallback}")
    print("self-test OK", file=sys.stderr)


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        _self_test()
        return 0
    if len(argv) > 1 and argv[1] not in ("-", "--"):
        raw = open(argv[1], encoding="utf-8").read()
    else:
        raw = sys.stdin.read()
    print(json.dumps(extract_usage(load_json_object(raw)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
