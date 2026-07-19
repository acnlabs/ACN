"""Hosted ACN region presets (ADR-0013).

Agents should register on the ACN instance matching where they are hosted:
China infra → ``cn``, overseas → ``global``. API keys are not portable.
"""

from __future__ import annotations

import os
import re
from typing import Final, Literal, Mapping

AcnRegion = Literal["global", "cn"]

ACN_HOSTED_URLS: Final[dict[str, str]] = {
    "global": "https://api.acnlabs.dev",
    "cn": "https://acn.acnlabs.cn",
}

_API_V1_SUFFIX = re.compile(r"/api/v1/?$", re.IGNORECASE)


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes and a mistaken ``/api/v1`` suffix."""
    u = url.strip().rstrip("/")
    u = _API_V1_SUFFIX.sub("", u).rstrip("/")
    return u


def hosted_base_url(region: str) -> str:
    key = region.strip().lower()
    if key not in ACN_HOSTED_URLS:
        raise ValueError(f"Unknown region {region!r}. Valid: global | cn")
    return ACN_HOSTED_URLS[key]


def resolve_hosted_base_url(
    *,
    region: str | None = None,
    base_url: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve origin: ``base_url`` → ``region`` → ``ACN_BASE_URL`` env.

    Raises ``ValueError`` if none are provided (callers that want a local
    default should supply it themselves).
    """
    if base_url is not None and region is not None:
        raise ValueError("Use either base_url or region, not both")
    if base_url is not None:
        return normalize_base_url(base_url)
    if region is not None:
        return hosted_base_url(region)
    environ = env if env is not None else os.environ
    from_env = (environ.get("ACN_BASE_URL") or "").strip()
    if from_env:
        return normalize_base_url(from_env)
    raise ValueError("base_url, region, or ACN_BASE_URL is required")
