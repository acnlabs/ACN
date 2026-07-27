"""Unit tests for examples/org-orchestrator idempotency store."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "org-orchestrator"
sys.path.insert(0, str(EXAMPLES))

from idempotency import IdempotencyStore  # noqa: E402
from run_orchestrator import wake_key  # noqa: E402


def test_wake_key_includes_assignee() -> None:
    a = wake_key("org_1", "work_1", "agt_a")
    b = wake_key("org_1", "work_1", "agt_b")
    assert a != b
    assert a.endswith(":agt_a")
    assert b.endswith(":agt_b")


def test_try_claim_confirm_release(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "idem.json")
    key = "org_1:work_1:wake:1:agt_a"
    assert store.try_claim(key, work_id="work_1", assignee="agt_a") is True
    assert store.try_claim(key, work_id="work_1", assignee="agt_a") is False
    store.release(key)
    assert store.try_claim(key, work_id="work_1", assignee="agt_a") is True
    store.confirm(key)
    assert store.has(key) is True
    assert store.try_claim(key, work_id="work_1", assignee="agt_a") is False


def test_disk_before_memory_on_save_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "idem.json"
    store = IdempotencyStore(path)
    key = "org_1:work_1:wake:1:agt_a"
    assert store.try_claim(key, work_id="work_1", assignee="agt_a") is True

    def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save_unlocked", boom)
    with pytest.raises(OSError):
        store.confirm(key)
    # Memory rolled back for confirm failure; reloaded has still has pending claim
    store2 = IdempotencyStore(path)
    assert store2.has(key) is True
