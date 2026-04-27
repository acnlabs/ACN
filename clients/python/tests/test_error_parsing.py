"""Tests for ``ACNError`` construction from HTTP responses (audit 14.5-2).

Pre-fix, ``_request`` parsed only ``detail`` / ``message`` / ``response.text``
and threw away ``error_code`` and ``request_id`` — both of which are
required by the H4 sanitised-5xx contract so callers can quote a stable
token in support tickets and branch on a stable error category.

These tests pin the new behaviour:

* 4xx with string ``detail``                    →  message = detail.
* 422 with list-of-dicts ``detail``             →  readable single-line summary.
* 5xx sanitised body ``{error,message,request_id}`` → all three fields parsed.
* ``X-Request-ID`` header used as fallback when body lacks it.
* Empty / non-JSON body                          →  message = response.text or status fallback.
* Pathologically large body                      →  truncated.
* Backward-compat: ``ACNError(status_code, message)`` two-arg form still works.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from acn_client.client import ACNClient, ACNError, _build_acn_error

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_response(
    status_code: int,
    body: Any | None = None,
    *,
    headers: dict[str, str] | None = None,
    text: str | None = None,
) -> httpx.Response:
    """Build an httpx.Response with either a JSON body or raw text.

    We construct via ``httpx.Response(...)`` rather than mocking because
    the parser relies on ``response.json()`` / ``response.text`` /
    ``response.content`` / ``response.headers`` working together — the
    real httpx.Response is the cheapest fixture that gives us all of
    those at once.
    """
    if body is not None:
        content = json.dumps(body).encode()
        merged_headers = {"content-type": "application/json"}
    elif text is not None:
        content = text.encode()
        merged_headers = {"content-type": "text/plain"}
    else:
        content = b""
        merged_headers = {}

    if headers:
        merged_headers.update(headers)

    return httpx.Response(
        status_code=status_code,
        content=content,
        headers=merged_headers,
    )


# --------------------------------------------------------------------------- #
# Backward compatibility
# --------------------------------------------------------------------------- #


def test_two_arg_form_still_works():
    """``ACNError(status_code, message)`` (old call sites) constructs cleanly."""
    err = ACNError(500, "boom")
    assert err.status_code == 500
    assert err.message == "boom"
    assert err.error_code is None
    assert err.request_id is None
    assert "ACN Error 500: boom" in str(err)
    # No request_id suffix when there isn't one.
    assert "request_id=" not in str(err)


def test_kw_only_extras_render_in_str():
    err = ACNError(
        503,
        "service unavailable",
        error_code="upstream_unavailable",
        request_id="abc-123",
    )
    rendered = str(err)
    assert "503" in rendered
    assert "service unavailable" in rendered
    assert "[request_id=abc-123]" in rendered
    assert err.error_code == "upstream_unavailable"
    assert err.request_id == "abc-123"


# --------------------------------------------------------------------------- #
# 4xx: string detail
# --------------------------------------------------------------------------- #


def test_4xx_string_detail_becomes_message():
    resp = _make_response(404, {"detail": "agent not found"})
    err = _build_acn_error(resp)
    assert err.status_code == 404
    assert err.message == "agent not found"
    # 4xx don't go through H4 sanitisation, so no error_code/request_id.
    assert err.error_code is None
    assert err.request_id is None


def test_4xx_string_detail_with_header_request_id_still_picked_up():
    """Some 4xx may still carry ``X-Request-ID`` (e.g. rate-limit responses).

    Even when the body has no ``request_id`` field we should pull it from
    the header so support tickets can correlate.
    """
    resp = _make_response(
        429,
        {"detail": "rate limit exceeded"},
        headers={"X-Request-ID": "rl-xyz"},
    )
    err = _build_acn_error(resp)
    assert err.status_code == 429
    assert err.message == "rate limit exceeded"
    assert err.request_id == "rl-xyz"


# --------------------------------------------------------------------------- #
# 422: list-of-dicts detail
# --------------------------------------------------------------------------- #


def test_422_list_detail_summarised():
    resp = _make_response(
        422,
        {
            "detail": [
                {
                    "loc": ["body", "from_agent"],
                    "msg": "field required",
                    "type": "value_error.missing",
                },
                {
                    "loc": ["body", "message", "text"],
                    "msg": "ensure this value has at least 1 character",
                    "type": "value_error.any_str.min_length",
                },
            ]
        },
    )
    err = _build_acn_error(resp)
    assert err.status_code == 422
    # The "body" prefix is dropped — caller cares about the field, not
    # which part of the request it came from.
    assert "from_agent: field required" in err.message
    assert "message.text: ensure this value has at least 1 character" in err.message
    # Joined with "; " not newlines (so it survives single-line log lines).
    assert "; " in err.message


def test_422_list_detail_truncates_when_huge():
    items = [
        {"loc": ["body", f"f{i}"], "msg": "bad", "type": "value_error"}
        for i in range(20)
    ]
    resp = _make_response(422, {"detail": items})
    err = _build_acn_error(resp)
    # Cap at 5 entries + " (... +15 more)" suffix so the message stays short.
    assert "+15 more" in err.message
    assert err.message.count("; ") <= 4  # 5 items → 4 separators


def test_422_list_detail_handles_loc_only():
    """Defensive: some 422 entries may have only ``loc`` (no ``msg``)."""
    resp = _make_response(
        422,
        {"detail": [{"loc": ["body", "x"], "type": "value_error.missing"}]},
    )
    err = _build_acn_error(resp)
    # Falls back to ``type`` when ``msg`` is absent.
    assert "value_error.missing" in err.message


# --------------------------------------------------------------------------- #
# 5xx: H4 sanitised body
# --------------------------------------------------------------------------- #


def test_5xx_sanitised_body_parses_all_three_fields():
    """The whole point of the change — H4 contract end-to-end."""
    resp = _make_response(
        500,
        {
            "error": "internal_server_error",
            "message": "An internal error occurred. Please try again later.",
            "request_id": "550e8400-e29b-41d4-a716-446655440000",
        },
        headers={"X-Request-ID": "550e8400-e29b-41d4-a716-446655440000"},
    )
    err = _build_acn_error(resp)
    assert err.status_code == 500
    assert err.message == "An internal error occurred. Please try again later."
    assert err.error_code == "internal_server_error"
    assert err.request_id == "550e8400-e29b-41d4-a716-446655440000"
    # str(err) must include the request_id so it survives through layers
    # that only log err.args[0].
    assert "[request_id=550e8400-e29b-41d4-a716-446655440000]" in str(err)


def test_5xx_body_request_id_wins_over_header_on_conflict():
    """If a buggy proxy rewrites ``X-Request-ID``, trust the body."""
    resp = _make_response(
        500,
        {
            "error": "internal_server_error",
            "message": "boom",
            "request_id": "from-body",
        },
        headers={"X-Request-ID": "from-proxy"},
    )
    err = _build_acn_error(resp)
    assert err.request_id == "from-body"


def test_5xx_header_only_request_id_falls_through():
    """Defensive: if a future endpoint mints a request_id but doesn't echo
    it into the body, header alone is enough."""
    resp = _make_response(
        503,
        {"error": "service_unavailable", "message": "down"},
        headers={"X-Request-ID": "header-rid"},
    )
    err = _build_acn_error(resp)
    assert err.request_id == "header-rid"
    assert err.error_code == "service_unavailable"


# --------------------------------------------------------------------------- #
# Edge cases — non-JSON / empty / huge bodies
# --------------------------------------------------------------------------- #


def test_empty_body_falls_through_to_status_string():
    resp = _make_response(504)  # no body
    err = _build_acn_error(resp)
    assert err.status_code == 504
    # Empty content + no ``message`` field → falls through to "HTTP 504".
    assert err.message == "HTTP 504"


def test_html_body_falls_through_to_text():
    """A misconfigured load balancer may return an HTML 502."""
    resp = _make_response(502, text="<html><body>Bad Gateway</body></html>")
    err = _build_acn_error(resp)
    assert err.status_code == 502
    # Not parseable as JSON → ``message`` falls back to the raw text.
    assert "Bad Gateway" in err.message
    assert err.error_code is None
    assert err.request_id is None


def test_pathologically_long_body_truncated():
    huge = "x" * 5000
    resp = _make_response(500, text=huge)
    err = _build_acn_error(resp)
    assert err.message.endswith("...(truncated)")
    assert len(err.message) < len(huge)


def test_dict_detail_stringified_defensively():
    """Some custom routes may return ``detail`` as a dict; we shouldn't crash."""
    resp = _make_response(409, {"detail": {"reason": "version_conflict", "expected": 5}})
    err = _build_acn_error(resp)
    assert err.status_code == 409
    # Stringified form preserves the relevant info even if not pretty.
    assert "version_conflict" in err.message


# --------------------------------------------------------------------------- #
# Integration with ACNClient._request
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_request_raises_acn_error_with_request_id_on_5xx(monkeypatch):
    """End-to-end: a 5xx response from the real client path produces a
    structured ``ACNError`` with the request_id surfaced."""
    client = ACNClient(base_url="http://test")

    async def fake_request(*_args, **_kwargs):
        return httpx.Response(
            status_code=500,
            content=json.dumps(
                {
                    "error": "internal_server_error",
                    "message": "An internal error occurred. Please try again later.",
                    "request_id": "rid-from-5xx",
                }
            ).encode(),
            headers={
                "content-type": "application/json",
                "X-Request-ID": "rid-from-5xx",
            },
        )

    monkeypatch.setattr(client._client, "request", fake_request)

    with pytest.raises(ACNError) as exc_info:
        await client._request("GET", "/api/v1/agents/x")

    err = exc_info.value
    assert err.status_code == 500
    assert err.error_code == "internal_server_error"
    assert err.request_id == "rid-from-5xx"
    assert "rid-from-5xx" in str(err)

    await client.close()


@pytest.mark.asyncio
async def test_request_raises_acn_error_with_validation_summary_on_422(monkeypatch):
    client = ACNClient(base_url="http://test")

    async def fake_request(*_args, **_kwargs):
        return httpx.Response(
            status_code=422,
            content=json.dumps(
                {
                    "detail": [
                        {
                            "loc": ["body", "from_agent"],
                            "msg": "field required",
                            "type": "value_error.missing",
                        }
                    ]
                }
            ).encode(),
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(client._client, "request", fake_request)

    with pytest.raises(ACNError) as exc_info:
        await client._request("POST", "/api/v1/agents", json={})

    err = exc_info.value
    assert err.status_code == 422
    assert "from_agent: field required" in err.message
    assert err.error_code is None
    assert err.request_id is None

    await client.close()
