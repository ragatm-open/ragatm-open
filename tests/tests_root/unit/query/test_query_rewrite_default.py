"""Réécriture LLM de la question : désactivée par défaut, et pourquoi.

Mesurée en A/B le 30/07/2026 sur 100 questions, même code, même base, même configuration.
Elle ne se déclenchait jamais avant l'arrivée de la détection d'intention ; activée sur
27 questions sur 100, elle n'a produit aucun gain mesurable et a doublé le P95.

Ce fichier verrouille le défaut et la propriété qui rend la mesure interprétable : la
réécriture ne s'applique qu'en mode explicatif, et la comparaison déterministe lui passe
devant.
"""
from __future__ import annotations

import importlib
import os
from unittest import mock

import pytest

from apps.rag_core.services.query import rewrite as rw


def _reload_with(**env):
    """Recharge le module : les drapeaux sont lus par `os.getenv` À L'IMPORT."""
    with mock.patch.dict(os.environ, env, clear=False):
        return importlib.reload(rw)


@pytest.fixture(autouse=True)
def _restore_module():
    yield
    importlib.reload(rw)


def test_rewrite_is_disabled_by_default():
    """Le défaut de CODE, pas seulement celui d'un `.env`.

    Aucune source indispensable récupérée, décisions d'admission identiques, et
    P95 +102,6 % : le défaut sûr est OFF.
    """
    env = {k: v for k, v in os.environ.items() if k != "RAG_QUERY_REWRITE_ENABLED"}
    with mock.patch.dict(os.environ, env, clear=True):
        module = importlib.reload(rw)
        assert module.QUERY_REWRITE_ENABLED is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_rewrite_can_still_be_enabled_explicitly(value):
    module = _reload_with(RAG_QUERY_REWRITE_ENABLED=value)
    assert module.QUERY_REWRITE_ENABLED is True


@pytest.mark.parametrize("value", ["0", "false", "False", ""])
def test_falsy_values_keep_it_disabled(value):
    module = _reload_with(RAG_QUERY_REWRITE_ENABLED=value)
    assert module.QUERY_REWRITE_ENABLED is False


def test_disabled_rewrite_yields_a_single_search_query():
    """Un seul plan de recherche : c'est ce qui supprime les deux passes coûteuses."""
    module = _reload_with(RAG_QUERY_REWRITE_ENABLED="0")
    queries = module.build_queries_for_search(
        question="Why do venture capital funds accept a high failure rate?",
        explanatory_mode=True,
    )
    assert queries == ["Why do venture capital funds accept a high failure rate?"]


def test_keywordize_is_gated_by_the_same_flag():
    """La requête keywordisée est le poste le plus coûteux : elle tombe avec la réécriture.

    Son `ts_rank` — une disjonction large sur 8 713 chunks — coûtait 2,7 s côté privé.
    La désactiver avec la réécriture est donc l'essentiel du gain de latence.
    """
    module = _reload_with(RAG_QUERY_REWRITE_ENABLED="0", RAG_KEYWORDIZE_ENABLED="1")
    queries = module.build_queries_for_search(
        question="How does dilution affect expected returns?", explanatory_mode=True
    )
    assert len(queries) == 1


def test_non_explanatory_question_never_triggers_a_rewrite():
    module = _reload_with(RAG_QUERY_REWRITE_ENABLED="1")
    queries = module.build_queries_for_search(
        question="Quel est le montant du contrat ?", explanatory_mode=False
    )
    assert queries == ["Quel est le montant du contrat ?"]


def test_comparative_expansion_is_deterministic_and_bypasses_the_llm():
    """L'expansion comparative ne passe pas par le LLM : elle n'est pas concernée."""
    module = _reload_with(RAG_QUERY_REWRITE_ENABLED="1")
    called = []
    with mock.patch.object(module, "rewrite_query_with_llm",
                           side_effect=lambda *a, **k: called.append(1) or ""):
        queries = module.build_queries_for_search(
            question="Différences entre le télétravail et un bureau partagé ?",
            explanatory_mode=True,
            comparative_objects=("télétravail", "bureau partagé"),
        )
    assert called == [], "aucun appel LLM sur le chemin comparatif"
    assert len(queries) >= 1
