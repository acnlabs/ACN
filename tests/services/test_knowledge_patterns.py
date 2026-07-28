"""Unit tests for Org knowledge plugins (K3)."""

from __future__ import annotations

import pytest

from acn.core.exceptions import OrgConflictError
from acn.services.knowledge_patterns import (
    GitSidecarKnowledge,
    NoopKnowledge,
    resolve_knowledge_plugin,
)
from acn.services.knowledge_patterns.resolve import knowledge_enabled


def test_resolve_noop_and_git():
    noop = resolve_knowledge_plugin("noop")
    assert isinstance(noop, NoopKnowledge)
    assert noop.enabled() is False
    assert noop.default_refs("org_x") == []

    git = resolve_knowledge_plugin("git")
    assert isinstance(git, GitSidecarKnowledge)
    assert git.enabled() is True
    assert git.default_refs("org_x") == [
        {"uri": "orgkb://org_x/charter.md", "title": "charter.md"}
    ]


def test_resolve_unknown_knowledge_plugin():
    with pytest.raises(OrgConflictError) as ei:
        resolve_knowledge_plugin("mem0")
    assert ei.value.reason == "unknown_plugin"


def test_knowledge_enabled_helper():
    assert knowledge_enabled(None) is False
    assert knowledge_enabled({"knowledge": "noop"}) is False
    assert knowledge_enabled({"knowledge": "git"}) is True
    assert knowledge_enabled({"knowledge": "weird"}) is False
