"""ADR-0013 hosted region helpers."""

from __future__ import annotations

import pytest

from acn_client import ACNClient
from acn_client.regions import (
    ACN_HOSTED_URLS,
    hosted_base_url,
    normalize_base_url,
    resolve_hosted_base_url,
)


def test_hosted_presets() -> None:
    assert hosted_base_url("cn") == ACN_HOSTED_URLS["cn"]
    assert hosted_base_url("GLOBAL") == ACN_HOSTED_URLS["global"]
    with pytest.raises(ValueError, match="Unknown region"):
        hosted_base_url("eu")


def test_normalize_strips_api_v1() -> None:
    assert normalize_base_url("https://acn.acnlabs.cn/api/v1/") == ACN_HOSTED_URLS["cn"]


def test_resolve_precedence() -> None:
    assert resolve_hosted_base_url(region="cn") == ACN_HOSTED_URLS["cn"]
    assert (
        resolve_hosted_base_url(base_url="https://custom.example/api/v1")
        == "https://custom.example"
    )
    assert (
        resolve_hosted_base_url(env={"ACN_BASE_URL": "https://env.example/"})
        == "https://env.example"
    )
    with pytest.raises(ValueError, match="not both"):
        resolve_hosted_base_url(region="cn", base_url="https://x")


def test_client_region_kwarg() -> None:
    client = ACNClient(region="cn", api_key="acn_test")
    assert client.base_url == ACN_HOSTED_URLS["cn"]


def test_client_default_localhost() -> None:
    client = ACNClient()
    assert client.base_url == "http://localhost:9000"
