"""Invariants de sécurité du registre (audit §9, §25)."""
from __future__ import annotations

import pytest

from apps.actions.registry import (
    ACTION_REGISTRY,
    ActionDefinition,
    PermissionSpec,
    allowed_actions_for_llm,
    catalog,
    register,
)

_MUTATIONS = [
    "document.upload",
    "document.reindex",
    "document.delete",
    "ragspace.create",
    "configuration.update",
]


def test_mutations_declared_but_not_nl_triggerable():
    for name in _MUTATIONS:
        d = ACTION_REGISTRY[name]
        assert d.natural_language_enabled is False, name
        assert d.matchers == (), name


def test_destructive_actions_marked():
    assert ACTION_REGISTRY["document.delete"].destructive is True
    assert ACTION_REGISTRY["configuration.update"].destructive is True


def test_delete_is_feature_flagged_off():
    assert ACTION_REGISTRY["document.delete"].feature_flag == "ACTIONS_ALLOW_NL_DELETE"


def test_llm_allowed_actions_are_read_only():
    allowed = allowed_actions_for_llm()
    assert "none" in allowed
    for name in _MUTATIONS:
        assert name not in allowed
    # seules les 4 lectures + none
    assert set(allowed) == {
        "search.answer",
        "search.list_documents",
        "search.news",
        "document.summarize",
        "none",
    }


def test_catalog_nl_only_excludes_mutations():
    names = {c["name"] for c in catalog(nl_only=True)}
    assert names.isdisjoint(set(_MUTATIONS))


def test_register_rejects_nl_disabled_with_matchers():
    from apps.actions.matchers import FallbackMatcher

    with pytest.raises(ValueError):
        register(ActionDefinition(
            name="bad.action",
            description="x", examples=(), schema="search.answer",
            permission=PermissionSpec(scope="rag_space", level="read"),
            destructive=False, execution_mode="sync",
            handler_key="noop", audit_event="noop",
            natural_language_enabled=False,
            matchers=(FallbackMatcher(),),  # incohérent -> doit lever
        ))
