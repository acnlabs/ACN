"""Tests for ``_payload_to_a2a_message`` (Phase 3 fix).

Background:
    Pre-fix, ``_payload_to_a2a_message`` always wrapped the entire request
    payload via ``str(payload)``, producing a Python repr string in the
    recipient's ``parts[0].text`` regardless of what shape the caller sent.
    Both the CLI's ``{"text": ...}`` shape and the SDK-documented A2A
    envelope ``{"role": ..., "parts": [...]}`` produced unusable output.

These tests pin the new contract:
    1. A2A envelopes are accepted and parsed (with the caller's
       ``messageId`` overridden to prevent id-spoofing).
    2. Simple ``{"text": ...}`` payloads are wrapped cleanly.
    3. Anything else falls back to the legacy ``str(payload)`` so existing
       callers that send arbitrary structured payloads keep working.
    4. Malformed envelopes (claim to be A2A but fail validation) gracefully
       fall through to the legacy fallback rather than 500ing.
"""

from __future__ import annotations

from a2a.compat.v0_3.types import DataPart, TextPart  # type: ignore[import-untyped]

from acn.routes.communication import _payload_to_a2a_message


def _first_part(message):
    """Unwrap ``parts[0]`` regardless of whether the A2A library wraps each
    part in a ``Part`` discriminated union (``part.root``) or exposes the
    concrete part type directly. Older a2a versions used the union; newer
    versions sometimes return the concrete subclass — both are valid and
    we want this test to ride either."""
    p = message.parts[0]
    return p.root if hasattr(p, "root") else p


# ─── Branch 1: structured A2A envelope ──────────────────────────────────────


def test_a2a_envelope_text_part_preserved():
    """A2A envelope with a TextPart survives the round-trip with the actual
    text intact (no str(payload) wrapping)."""
    payload = {
        "role": "user",
        "parts": [{"kind": "text", "text": "hello world"}],
    }
    msg = _payload_to_a2a_message(payload)

    part = _first_part(msg)
    assert isinstance(part, TextPart)
    assert part.text == "hello world"  # NOT "{'role': 'user', ...}"
    assert msg.role == "user"


def test_a2a_envelope_message_id_is_regenerated():
    """A caller-supplied ``messageId`` is overridden — REST traffic must
    have a server-generated id to prevent replay/id-spoofing."""
    payload = {
        "role": "user",
        "parts": [{"kind": "text", "text": "hi"}],
        "messageId": "spoofed-id-from-client",
    }
    msg = _payload_to_a2a_message(payload)

    msg_id = getattr(msg, "message_id", None) or getattr(msg, "messageId", None)
    assert msg_id is not None
    assert msg_id != "spoofed-id-from-client"
    # UUID4 hex form is 36 chars with hyphens
    assert len(msg_id) == 36


def test_a2a_envelope_with_data_part():
    """Multi-part envelopes (text + data) are passed through faithfully."""
    payload = {
        "role": "user",
        "parts": [
            {"kind": "text", "text": "process this"},
            {"kind": "data", "data": {"foo": "bar"}},
        ],
    }
    msg = _payload_to_a2a_message(payload)

    parts = [p.root if hasattr(p, "root") else p for p in msg.parts]
    assert len(parts) == 2
    assert isinstance(parts[0], TextPart) and parts[0].text == "process this"
    assert isinstance(parts[1], DataPart) and parts[1].data == {"foo": "bar"}


# ─── Branch 2: simple text shape ────────────────────────────────────────────


def test_simple_text_shape_wraps_clean_text():
    """``{"text": "..."}`` produces a TextPart with just the text — not
    the ugly ``"{'text': '...'}"`` repr from the pre-fix behaviour."""
    msg = _payload_to_a2a_message({"text": "Hello, can you help?"})

    part = _first_part(msg)
    assert isinstance(part, TextPart)
    assert part.text == "Hello, can you help?"


def test_simple_text_shape_ignores_extra_fields():
    """Extra fields like ``type`` (CLI's current shape) don't pollute the
    text — only the ``text`` field is read."""
    msg = _payload_to_a2a_message({"text": "hello", "type": "text"})

    part = _first_part(msg)
    assert isinstance(part, TextPart)
    assert part.text == "hello"


def test_simple_text_shape_non_string_text_falls_through():
    """Non-string ``text`` (e.g. ``{"text": 42}``) doesn't match branch 2;
    falls through to the legacy fallback to preserve forward compat."""
    msg = _payload_to_a2a_message({"text": 42})

    part = _first_part(msg)
    assert isinstance(part, TextPart)
    # Legacy fallback: str(payload)
    assert "42" in part.text and "text" in part.text


# ─── Branch 3: legacy fallback ──────────────────────────────────────────────


def test_legacy_fallback_for_arbitrary_dict():
    """A payload that's neither A2A envelope nor simple text shape falls
    back to ``str(payload)`` — the pre-Phase-3 behaviour. This matters for
    backward compat with any callers sending custom structured payloads."""
    payload = {"foo": "bar", "nested": {"x": 1}}
    msg = _payload_to_a2a_message(payload)

    part = _first_part(msg)
    assert isinstance(part, TextPart)
    # str(dict) repr should appear verbatim
    assert "foo" in part.text and "bar" in part.text


def test_legacy_fallback_for_empty_dict():
    """An empty dict still produces a valid Message (doesn't raise)."""
    msg = _payload_to_a2a_message({})

    part = _first_part(msg)
    assert isinstance(part, TextPart)
    assert part.text == "{}"


# ─── Resilience: malformed A2A envelope ─────────────────────────────────────


def test_malformed_a2a_envelope_falls_through_not_500():
    """An envelope that claims to be A2A but has an invalid ``parts``
    member must not raise — we fall through to the legacy fallback so
    the request still routes (the recipient will see the repr string,
    matching the pre-fix surface)."""
    payload = {
        "role": "user",
        # `kind: "bogus"` is not a valid A2A part kind
        "parts": [{"kind": "bogus", "anything": "here"}],
    }

    # Must not raise.
    msg = _payload_to_a2a_message(payload)

    # Either branch 2 (no `text` field) is skipped, then branch 3 wraps
    # the str(payload). We just assert no exception and a valid Message.
    part = _first_part(msg)
    assert isinstance(part, TextPart)
    # The recipient sees something (legacy fallback is opaque but routed)
    assert part.text  # non-empty


def test_envelope_with_role_but_non_list_parts_falls_through():
    """``role`` set but ``parts`` is not a list — type guard skips branch 1
    and falls through correctly."""
    payload = {"role": "user", "parts": "not-a-list"}
    msg = _payload_to_a2a_message(payload)

    part = _first_part(msg)
    assert isinstance(part, TextPart)
    # Legacy fallback path
    assert "role" in part.text


# ─── Branch ordering: A2A envelope wins over simple text ────────────────────


def test_a2a_envelope_takes_priority_over_text_shape():
    """If a payload has BOTH a valid A2A envelope AND a top-level ``text``
    field, branch 1 wins — the envelope's parts are what get delivered,
    not the top-level text."""
    payload = {
        "role": "user",
        "parts": [{"kind": "text", "text": "from envelope"}],
        "text": "from top-level field",
    }
    msg = _payload_to_a2a_message(payload)

    part = _first_part(msg)
    assert isinstance(part, TextPart)
    assert part.text == "from envelope"
