#!/usr/bin/env python3
"""Extract Interfaze/Host `usage` from OpenClaw `agent --json` output.

This is a reference adapter, not ACN core. The writeback contract is the
JSON this prints — CLI 1.0.3+ forwards it. Settlement uses cumulative
input/output only; extras are stored, not billed.

Usage:
  openclaw agent --json ... > /tmp/oc.json
  python3 scripts/openclaw_chat_usage.py /tmp/oc.json
  python3 scripts/openclaw_chat_usage.py < /tmp/oc.json

Exit 0 even when no tokens were found (prints {}). Do not invent 0/0.
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


def dig(obj: Any, *paths: str) -> Any:
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok:
            return cur
    return None


def as_str(v: Any, n: int = 200) -> str:
    if not isinstance(v, str):
        return ""
    s = v.strip()
    return s[:n] if len(s) > n else s


def extract_usage(data: dict[str, Any]) -> dict[str, Any]:
    # Cumulative hop usage — never lastCallUsage.
    u = dig(
        data,
        "result.meta.agentMeta.usage",
        "meta.agentMeta.usage",
        "agentMeta.usage",
    )
    if not isinstance(u, dict):
        u = {}

    out: dict[str, Any] = {}
    inp = first_int(u.get("input"), u.get("input_tokens"), u.get("prompt_tokens"))
    tok = first_int(u.get("output"), u.get("output_tokens"), u.get("completion_tokens"))
    if inp is not None or tok is not None:
        out["input_tokens"] = inp or 0
        out["output_tokens"] = tok or 0
        out["meter_source"] = "peer_self"

    provider = as_str(
        dig(
            data,
            "result.meta.agentMeta.provider",
            "meta.agentMeta.provider",
            "result.meta.provider",
            "provider",
        ),
        80,
    )
    raw_model = as_str(
        dig(
            data,
            "result.meta.agentMeta.model",
            "meta.agentMeta.model",
            "result.meta.model",
            "model",
        )
    )
    if provider and raw_model and "/" not in raw_model:
        out["model_id"] = f"{provider}/{raw_model}"[:200]
    elif raw_model:
        out["model_id"] = raw_model[:200]
    if provider:
        out["provider"] = provider

    reason = first_int(u.get("reasoningTokens"), u.get("reasoning_tokens"))
    cache_r = first_int(u.get("cacheRead"), u.get("cache_read"), u.get("cache_read_tokens"))
    cache_w = first_int(u.get("cacheWrite"), u.get("cache_write"), u.get("cache_write_tokens"))
    total = first_int(u.get("total"), u.get("total_tokens"))
    duration = first_int(
        dig(data, "result.meta.durationMs", "meta.durationMs", "durationMs"),
        dig(data, "result.meta.duration_ms", "meta.duration_ms"),
    )
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
    sample = {
        "result": {
            "meta": {
                "durationMs": 15876,
                "agentMeta": {
                    "provider": "tencenttokenplan",
                    "model": "kimi-k2.5",
                    "usage": {
                        "input": 42214,
                        "output": 220,
                        "reasoningTokens": 216,
                        "total": 42434,
                    },
                    "lastCallUsage": {"input": 1, "output": 1},
                },
            }
        }
    }
    got = extract_usage(sample)
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
    if extract_usage({}) != {}:
        raise SystemExit("empty payload must not invent zeros")
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
