"""P1/P3: build redacted summary for collab_pull envelopes (no secrets)."""

from __future__ import annotations

import re
from typing import Any

# Heuristic secret patterns — never put matches into wake envelopes.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token|private[_-]?key)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bacn_[a-zA-Z0-9]{16,}\b"),
    re.compile(r"(?i)\bsk-[a-zA-Z0-9]{20,}\b"),
    re.compile(r"(?i)\b-----BEGIN[^-]+PRIVATE KEY-----"),
)


def redact_text(text: str, *, max_len: int = 240) -> str:
    s = (text or "").strip().replace("\n", " ")
    for pat in _SECRET_PATTERNS:
        s = pat.sub("[REDACTED]", s)
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def task_summary(task: dict[str, Any]) -> str:
    """Short L1-safe blurb: title + truncated description (redacted)."""
    title = redact_text(str(task.get("title") or ""), max_len=120)
    desc = redact_text(str(task.get("description") or ""), max_len=160)
    if title and desc:
        return f"{title} — {desc}"
    return title or desc or "(no summary)"
