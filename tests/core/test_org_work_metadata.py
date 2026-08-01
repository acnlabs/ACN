"""OrgWorkItem.metadata normalization (opaque store)."""

from __future__ import annotations

import pytest

from acn.core.entities.org import OrgWorkItem, normalize_work_metadata


def test_normalize_none_and_object() -> None:
    assert normalize_work_metadata(None) is None
    assert normalize_work_metadata({"wave": {"role": "root"}}) == {
        "wave": {"role": "root"}
    }


def test_normalize_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        normalize_work_metadata(["not", "object"])
    with pytest.raises(ValueError, match="JSON object"):
        normalize_work_metadata("nope")


def test_work_item_round_trip_includes_metadata() -> None:
    w = OrgWorkItem(
        work_id="w1",
        org_id="org_a",
        title="t",
        metadata={"wave": {"wave_id": "wv_1", "role": "root"}},
    )
    d = w.to_dict()
    assert d["metadata"]["wave"]["wave_id"] == "wv_1"
    back = OrgWorkItem.from_dict(d)
    assert back.metadata == w.metadata


def test_work_item_legacy_dict_without_metadata() -> None:
    w = OrgWorkItem.from_dict(
        {
            "work_id": "w1",
            "org_id": "org_a",
            "title": "t",
            "status": "todo",
        }
    )
    assert w.metadata is None
