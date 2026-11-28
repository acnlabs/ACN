"""The reporter reads analytics totals and PATCHes the directory. No secrets out."""

import json

import pytest
from network_registry.report_stats import ReportError

from network_registry import report_stats


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _fake_urlopen(payloads: dict[str, dict], captured: list | None = None):
    def _open(request, timeout: int = 0):
        if captured is not None:
            captured.append(request)
        for suffix, payload in payloads.items():
            if request.full_url.endswith(suffix):
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected url {request.full_url}")

    return _open


def test_fetch_counts_reads_both_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        report_stats.urllib.request,
        "urlopen",
        _fake_urlopen(
            {
                "/api/v1/analytics/agents": {"total": 128},
                "/api/v1/analytics/subnets": {"total": 9},
            }
        ),
    )
    assert report_stats.fetch_counts("http://127.0.0.1:8000", "token") == (128, 9)


def test_fetch_counts_rejects_a_payload_without_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        report_stats.urllib.request,
        "urlopen",
        _fake_urlopen({"/api/v1/analytics/agents": {"total": 1}, "/api/v1/analytics/subnets": {}}),
    )
    with pytest.raises(ReportError):
        report_stats.fetch_counts("http://127.0.0.1:8000", "token")


def test_report_patches_the_record_with_the_registry_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list = []
    monkeypatch.setattr(
        report_stats.urllib.request,
        "urlopen",
        _fake_urlopen({"/networks/acn-global": {"seq": 1}}, captured),
    )
    record = report_stats.report("https://registry.invalid", "secret", "acn-global", 3, 1)
    assert record == {"seq": 1}
    (request,) = captured
    assert request.get_method() == "PATCH"
    assert request.full_url == "https://registry.invalid/networks/acn-global"
    assert request.get_header("Authorization") == "Bearer secret"
    body = json.loads(request.data.decode())
    assert body["agents"] == 3
    assert body["subnets"] == 1
    assert "T" in body["reported_at"]


def test_main_needs_its_env(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    for var in ("NETWORK_ID", "REGISTRY_KEY", "INTERNAL_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert report_stats.main() == 2
    assert "missing env var" in capsys.readouterr().err
