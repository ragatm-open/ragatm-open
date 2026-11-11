"""Expansion de requête comparative — déterministe, sans appel LLM.

Mesuré avant correctif : une question comparative déclenchait le mode analytique, donc
une reformulation LLM — +1,8 s médian sur le retrieval seul (jusqu'à ×25 observé), pour
une requête non déterministe qui rebattait le top-k sans gain de couverture.

Une comparaison EXPLICITE porte déjà ses deux objets : il n'y a rien à deviner.
"""
import pytest

from apps.rag_core.services.query import rewrite
from apps.rag_core.services.query.heuristics import detect_comparative_intent


@pytest.fixture(autouse=True)
def _rewrite_enabled(monkeypatch):
    """Force le drapeau de réécriture, que l'environnement le pose ou non.

    `rewrite.QUERY_REWRITE_ENABLED` est résolu **à l'import** depuis
    `RAG_QUERY_REWRITE_ENABLED`, dont le défaut est `"0"`. Sans ce forçage, ces tests
    ne passaient que sur un poste dont le `.env` pose la variable à 1 : ailleurs — en
    CI notamment — le rewriter n'était jamais appelé et les assertions « zéro appel »
    passaient à vide, sans rien démontrer.

    Le forçage rend chaque test de ce fichier significatif : `test_comparaison_
    explicite_n_appelle_pas_le_llm` prouve désormais que le chemin comparatif évite le
    LLM alors qu'il POURRAIT l'appeler, ce qui est exactement la propriété visée.
    """
    monkeypatch.setattr(rewrite, "QUERY_REWRITE_ENABLED", True)


@pytest.fixture
def llm_spy(monkeypatch):
    """Compte les appels au rewriter LLM (et échoue si l'un d'eux passe malgré tout)."""
    calls: list[str] = []

    def _spy(question, **kwargs):
        calls.append(question)
        return "requete reformulee"

    monkeypatch.setattr(rewrite, "rewrite_query_with_llm", _spy)
    return calls


def _objects(question):
    return detect_comparative_intent(question).objects


# --- Comparaison explicite : ZÉRO appel LLM ---------------------------------
def test_comparaison_explicite_n_appelle_pas_le_llm(llm_spy):
    q = "Quelles différences entre le système A et le système B ?"
    queries = rewrite.build_queries_for_search(
        question=q, explanatory_mode=True, comparative_objects=_objects(q)
    )
    assert llm_spy == []
    assert queries[0] == q
    # Une requête PORTANT LES DEUX objets : c'est elle qui remonte les passages
    # comparables. Deux requêtes séparées ont été mesurées et dégradaient la couverture.
    assert queries[1] == "système A système B"


def test_expansion_deterministe_est_reproductible(llm_spy):
    q = "Compare le dispositif X avec le dispositif Y."
    first = rewrite.build_queries_for_search(
        question=q, explanatory_mode=True, comparative_objects=_objects(q)
    )
    second = rewrite.build_queries_for_search(
        question=q, explanatory_mode=True, comparative_objects=_objects(q)
    )
    assert first == second
    assert llm_spy == []


def test_expansion_sans_objet_exploitable_reste_la_question(llm_spy):
    queries = rewrite.build_comparative_queries("Question ?", ("", "   "))
    assert queries == ["Question ?"]
    assert llm_spy == []


def test_expansion_respecte_le_plafond_de_requetes(llm_spy):
    q = "Quelles différences entre le système A et le système B ?"
    queries = rewrite.build_queries_for_search(
        question=q, explanatory_mode=True, comparative_objects=_objects(q)
    )
    assert 0 < len(queries) <= rewrite.MAX_SEARCH_QUERIES
    assert len(queries) == len(set(q.lower() for q in queries))  # ni doublon ni vide


def test_expansion_n_invente_aucun_terme(llm_spy):
    """Aucun mot qui ne vienne de la question : pas d'acronyme développé, pas de traduction."""
    q = "Quelles différences entre le référentiel ABC et le référentiel XYZ ?"
    queries = rewrite.build_queries_for_search(
        question=q, explanatory_mode=True, comparative_objects=_objects(q)
    )
    source_words = set(q.lower().replace("?", " ").split())
    for produced in queries[1:]:
        assert set(produced.lower().split()) <= source_words


# --- Chemins non comparatifs : comportement historique INCHANGÉ -------------
def test_question_explicative_conserve_le_rewrite_llm(llm_spy):
    queries = rewrite.build_queries_for_search(
        question="Explique le dispositif de contrôle interne", explanatory_mode=True
    )
    assert len(llm_spy) == 1
    assert "requete reformulee" in queries


def test_question_standard_ne_declenche_aucun_rewrite(llm_spy):
    queries = rewrite.build_queries_for_search(
        question="Quelle est la fréquence du reporting ?", explanatory_mode=False
    )
    assert llm_spy == []
    assert queries == ["Quelle est la fréquence du reporting ?"]


# --- Robustesse du chemin explicatif (repli inchangé) -----------------------
def test_rewrite_en_echec_retombe_sur_la_question(monkeypatch):
    """Timeout / erreur provider : la question originale est utilisée, sans erreur."""
    monkeypatch.setattr(rewrite, "rewrite_query_with_llm", lambda q, **k: "")
    queries = rewrite.build_queries_for_search(
        question="Explique le dispositif de contrôle interne", explanatory_mode=True
    )
    assert queries[0] == "Explique le dispositif de contrôle interne"


def test_rewrite_qui_leve_est_neutralise_en_amont(monkeypatch):
    """`rewrite_query_with_llm` absorbe déjà ses exceptions : le contrat reste sûr."""
    from apps.llm_router.router import LLMError

    def _boom(*a, **k):
        raise LLMError("provider down")

    monkeypatch.setattr(rewrite, "generate_text", _boom)
    assert rewrite.rewrite_query_with_llm("Explique le dispositif") == ""


def test_question_vide():
    assert rewrite.build_queries_for_search(question="   ", explanatory_mode=True) == []
