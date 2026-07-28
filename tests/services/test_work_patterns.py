"""Unit tests for Org work-pattern registry (Phase 2a)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from acn.core.exceptions import OrgConflictError
from acn.services.work_patterns import (
    DEFAULT_ORG_PLUGINS,
    normalize_org_plugins,
    resolve_work_pattern,
    validate_org_plugins,
)
from acn.services.work_patterns.builtin import BuiltinWorkPattern


def test_default_plugins_are_canonical():
    assert DEFAULT_ORG_PLUGINS == {
        "work": "builtin_work",
        "loop": "heartbeat",
        "memory": "noop",
        "knowledge": "noop",
    }


def test_normalize_legacy_aliases():
    assert normalize_org_plugins({"work": "minimal", "loop": "thin"}) == {
        "work": "builtin_work",
        "loop": "heartbeat",
        "memory": "noop",
        "knowledge": "noop",
    }


def test_normalize_knowledge_default_and_git():
    assert normalize_org_plugins(None)["knowledge"] == "noop"
    assert normalize_org_plugins({"knowledge": "git"})["knowledge"] == "git"
    assert normalize_org_plugins({"knowledge": ""})["knowledge"] == "noop"


def test_validate_unknown_plugin():
    with pytest.raises(OrgConflictError) as ei:
        validate_org_plugins({"work": "totally_unknown"})
    assert ei.value.reason == "unknown_plugin"


def test_validate_unavailable_plugins():
    with pytest.raises(OrgConflictError) as ei:
        validate_org_plugins({"work": "task_pool"})
    assert ei.value.reason == "plugin_unavailable"
    with pytest.raises(OrgConflictError) as ei:
        validate_org_plugins({"work": "paperclip"})
    assert ei.value.reason == "plugin_unavailable"


def test_validate_knowledge_plugins():
    validate_org_plugins(normalize_org_plugins({"knowledge": "noop"}))
    validate_org_plugins(normalize_org_plugins({"knowledge": "git"}))
    with pytest.raises(OrgConflictError) as ei:
        validate_org_plugins(normalize_org_plugins({"knowledge": "llm_wiki"}))
    assert ei.value.reason == "plugin_unavailable"
    with pytest.raises(OrgConflictError) as ei:
        validate_org_plugins(normalize_org_plugins({"knowledge": "mem0"}))
    assert ei.value.reason == "unknown_plugin"


def test_resolve_builtin_and_aliases():
    repo = MagicMock()
    assert isinstance(resolve_work_pattern("builtin_work", repo), BuiltinWorkPattern)
    assert isinstance(resolve_work_pattern("minimal", repo), BuiltinWorkPattern)
    with pytest.raises(OrgConflictError) as ei:
        resolve_work_pattern("task_pool", repo)
    assert ei.value.reason == "plugin_unavailable"
    with pytest.raises(OrgConflictError) as ei:
        resolve_work_pattern("nope", repo)
    assert ei.value.reason == "unknown_plugin"
