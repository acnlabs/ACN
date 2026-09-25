"""POST /communication/send body for a live direct push.

The router returns the peer's A2A body with no delivery label. The HTTP
layer nests that body so callers read ``status`` as the delivery result.
Envelopes that already name a pipe pass through.
"""

from pydantic import BaseModel

from acn.routes.communication import _public_send_body


def test_labeled_envelope_passes_through():
    envelope = {
        "status": "queued",
        "delivery_mode": "inbox",
        "route_id": "r1",
    }
    assert _public_send_body(envelope) is envelope


def test_live_dict_nests_peer_status():
    peer = {"status": {"state": "completed"}, "message_id": "m1"}
    body = _public_send_body(peer)
    assert body["status"] == "delivered"
    assert body["delivery_mode"] == "direct"
    assert body["message_id"] == "m1"
    assert body["response"]["status"] == {"state": "completed"}


def test_live_model_nests_peer_body():
    class _Peer(BaseModel):
        kind: str = "task"
        id: str = "task-1"

    body = _public_send_body(_Peer())
    assert body == {
        "status": "delivered",
        "delivery_mode": "direct",
        "response": {"kind": "task", "id": "task-1"},
    }
