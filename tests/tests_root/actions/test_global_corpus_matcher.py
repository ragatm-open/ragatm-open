"""L3 — matcher de demande globale : positifs, anti-faux-positifs, priorité."""
import pytest

from apps.actions.matchers import (
    FullAnalysisMatcher,
    GlobalCorpusMatcher,
    QuickConfirmMatcher,
)
from apps.actions.normalize import normalize
from apps.actions.registry import ACTION_REGISTRY, allowed_actions_for_llm
from apps.actions.router import ActionRouter

from .fixtures_corpus_global import (
    FULL_COMMANDS,
    GLOBAL_NEGATIVES,
    GLOBAL_POSITIVES,
    PRESERVED_INTENTS,
    QUICK_COMMANDS,
)

_GLOBAL = GlobalCorpusMatcher()
_QUICK = QuickConfirmMatcher()
_FULL = FullAnalysisMatcher()


def _match(matcher, text: str):
    return matcher.match(norm=normalize(text), original=text)


# --------------------------------------------------------------------------- #
# M1 — positifs
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", GLOBAL_POSITIVES)
def test_global_positives(text):
    mr = _match(_GLOBAL, text)
    assert mr is not None, f"demande globale non détectée : {text!r}"
    assert mr.action == "corpus.global_request"
    assert mr.detection_method in ("rule", "explicit_command")


@pytest.mark.parametrize("text", GLOBAL_POSITIVES)
def test_router_routes_global_positives(text):
    assert ActionRouter().route(text).action == "corpus.global_request"


# --------------------------------------------------------------------------- #
# M2 — anti-faux-positifs
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", GLOBAL_NEGATIVES)
def test_global_negatives(text):
    assert _match(_GLOBAL, text) is None, f"faux positif : {text!r}"


@pytest.mark.parametrize("text", GLOBAL_NEGATIVES)
def test_router_never_routes_negatives_to_global(text):
    assert ActionRouter().route(text).action != "corpus.global_request"


def test_the_word_all_alone_is_never_enough():
    """« tous » doit porter sur un collectif DOCUMENTAIRE, jamais sur autre chose."""
    for text in (
        "quels sont tous les risques",
        "cite tous les montants",
        "tous les points importants",
        "analyse toutes les clauses",
    ):
        assert _match(_GLOBAL, text) is None, text


# --------------------------------------------------------------------------- #
# M3 — priorité / non-régression des intentions existantes
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", PRESERVED_INTENTS)
def test_router_priority_preserves_existing_intents(text, expected):
    assert ActionRouter().route(text).action == expected


def test_summarize_all_documents_is_global_not_single_document():
    """La régression que ce lot supprime : « résume tous les documents » partait
    en résumé mono-document (donc en RAG top-k) au lieu d'être vu comme global."""
    assert ActionRouter().route("résume tous les documents").action == "corpus.global_request"
    assert ActionRouter().route("résume Rapport.pdf").action == "document.summarize"


def test_negation_is_not_suppressed_by_the_generic_guard():
    """« NE te limite PAS aux sources » est une négation portant sur un déclencheur.
    Sans l'exemption de garde-fou, elle serait rétrogradée vers search.answer."""
    plan = ActionRouter().route("ne te limite pas aux sources les plus pertinentes")
    assert plan.action == "corpus.global_request"


# --------------------------------------------------------------------------- #
# Confirmations contextuelles
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", QUICK_COMMANDS)
def test_quick_confirm_commands(text):
    mr = _match(_QUICK, text)
    assert mr is not None and mr.action == "corpus.quick_confirm"
    assert mr.detection_method == "explicit_command"


@pytest.mark.parametrize("text", FULL_COMMANDS)
def test_full_analysis_commands(text):
    mr = _match(_FULL, text)
    assert mr is not None and mr.action == "corpus.full_analysis"


def test_confirmations_require_an_exact_command():
    """Égalité stricte : « rapide » noyé dans une phrase ne confirme rien."""
    for text in (
        "donne-moi une réponse rapide sur le contrat",
        "je veux un résumé rapide de ce document",
        "quelle est la réponse rapide à cette question ?",
    ):
        assert _match(_QUICK, text) is None, text


def test_confirmations_are_not_in_the_router_priority():
    """Hors contexte, « réponse rapide » reste une question ordinaire."""
    assert ActionRouter().route("réponse rapide").action == "search.answer"
    assert ActionRouter().route("analyse complète").action == "search.answer"


# --------------------------------------------------------------------------- #
# Registre : jamais proposé au classifieur LLM
# --------------------------------------------------------------------------- #

def test_corpus_actions_are_registered_read_only_and_non_destructive():
    for name in ("corpus.global_request", "corpus.quick_confirm", "corpus.full_analysis"):
        d = ACTION_REGISTRY.get(name)
        assert d is not None, name
        assert d.destructive is False
        assert d.permission.scope == "rag_space"
        assert d.permission.level == "read"
        assert d.llm_proposable is False


def test_corpus_actions_are_never_offered_to_the_llm_classifier():
    """Une erreur de classification coûterait ici une analyse de tout le corpus."""
    allowed = allowed_actions_for_llm()
    assert "corpus.global_request" not in allowed
    assert "corpus.quick_confirm" not in allowed
    assert "corpus.full_analysis" not in allowed
    # La liste fermée historique reste EXACTEMENT la même.
    assert set(allowed) == {
        "search.answer",
        "search.list_documents",
        "search.news",
        "document.summarize",
        "none",
    }
