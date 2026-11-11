"""Parité de la garde news avec la source, et sécurité du seam LLM."""
from __future__ import annotations

import pytest

from apps.actions.matchers import _NEWS_HINTS_NORM
from apps.actions.normalize import normalize
from apps.actions.router import ActionRouter

_CORPUS = [
    "Quoi de neuf sur Nordexia ?",
    "Actualités récentes Valteria",
    "Dernières nouvelles",
    "Mise à jour du dossier",
    "Quelles sont les news ?",
    "updates on Alpha",
    "Quelle est la durée du contrat ?",  # négatif
    "Résume le document",                # négatif
    "Liste les articles",                # négatif
]


def test_news_gate_is_a_superset_of_source():
    """Non-régression : tout ce que la garde actuelle capte, la nouvelle le capte.

    La nouvelle garde est *plus tolérante* aux accents (elle corrige des bugs
    latents de la source : ``dernier(?:es)?``/``récent(?:es)?`` ne matchent pas
    « dernières »/« récentes » accentués). On exige donc ``src => mine``, pas
    l'égalité stricte.
    """
    try:
        from apps.chat.services.intents.news import _NEWS_HINTS
    except Exception:  # pragma: no cover - env sans Django complet
        pytest.skip("intents.news indisponible dans cet environnement")

    for phrase in _CORPUS:
        src = bool(_NEWS_HINTS.search(phrase))
        mine = bool(_NEWS_HINTS_NORM.search(normalize(phrase)))
        if src:
            assert mine, f"régression garde news sur {phrase!r}: src=True mine=False"
        # négatifs francs restent négatifs
        if phrase in ("Quelle est la durée du contrat ?", "Résume le document", "Liste les articles"):
            assert not mine, f"faux positif garde news sur {phrase!r}"


def test_llm_seam_disabled_by_default():
    r = ActionRouter()
    assert r._llm is None


def test_classifier_escalates_only_on_hint_and_returns_dict():
    # Le classifieur ne propose (dict validé) que sur formulation à indice résumé/
    # listing non tranché ; il ne peut escalader que vers listing/summarize.
    def fake(norm, allowed, candidate_count=0):
        assert "search.list_documents" in allowed and "document.delete" not in allowed
        return {"intent": "search.list_documents", "score": 0.88, "parameters": {}}

    r = ActionRouter(llm_classifier=fake)
    plan = r.route("Peux-tu lister les dossiers ?")  # « lister » : indice, regex stricte ne commit pas
    assert plan.action == "search.list_documents"
    assert plan.detection_method == "llm"


def test_classifier_not_called_on_nominal_question():
    # Question factuelle nominale : aucun indice -> classifieur JAMAIS appelé.
    called = {"n": 0}

    def fake(norm, allowed, candidate_count=0):
        called["n"] += 1
        return {"intent": "search.answer", "score": 1.0}

    r = ActionRouter(llm_classifier=fake)
    plan = r.route("Quelle est la date du document ?")
    assert plan.action == "search.answer"
    assert called["n"] == 0


def test_classifier_never_overrides_committed_rule():
    called = {"n": 0}

    def fake(norm, allowed, candidate_count=0):  # pragma: no cover
        called["n"] += 1
        return {"intent": "document.summarize", "score": 1.0}

    r = ActionRouter(llm_classifier=fake)
    plan = r.route("Résume le document Rapport.pdf")
    assert plan.action == "document.summarize"
    assert called["n"] == 0  # règle committée -> LLM non consulté


def test_classifier_none_intent_falls_back_to_answer():
    r = ActionRouter(llm_classifier=lambda n, a, c=0: {"intent": "none", "score": 0.9})
    assert r.route("Fais un topo du dossier").action == "search.answer"


def test_classifier_incoherent_or_mutation_yields_clarification_never_execute():
    # Intention non escaladable alors qu'un indice d'action est présent -> clarification.
    # Jamais une action mutante, jamais une exécution silencieuse.
    for intent in ("document.delete", "search.news", "configuration.update", "search.answer", "bogus"):
        r = ActionRouter(llm_classifier=lambda n, a, c=0, _i=intent: {"intent": _i, "score": 0.99})
        plan = r.route("Fais un topo du dossier")
        assert plan.action == "clarification"
        assert plan.needs_clarification is True
        # jamais une mutation ni une action exécutable arbitraire
        assert plan.action not in {"document.delete", "document.upload", "document.reindex", "configuration.update"}


def test_classifier_unavailable_yields_clarification():
    r = ActionRouter(llm_classifier=lambda n, a, c=0: None)  # timeout/JSON invalide
    plan = r.route("Fais un topo du dossier")
    assert plan.action == "clarification" and plan.parameters.get("reason") == "classifier_unavailable"
