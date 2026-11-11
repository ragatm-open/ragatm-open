"""Consigne de FORMAT du prompt final : adaptative, jamais contradictoire.

Le gabarit imposait « Donne une réponse courte et directe » à TOUTES les politiques,
y compris `explain`, `compare` et `partial` dont le préambule demande l'inverse. Le
prompt portait donc deux ordres opposés. Mesuré sur le corpus de dev (249 réponses
réelles, même modèle) : médiane de 252 caractères en politique `ok` contre 2 080 en
`explain` ; A/B contrôlé à budget de tokens CONSTANT : ×2,3 sur cette seule ligne.

Ces tests verrouillent les DEUX moitiés du contrat :

  - la profondeur est désormais proportionnée à la demande ;
  - aucun garde-fou (sources, citations, abstention, faible confiance) n'a été relâché
    pour l'obtenir.
"""
import pytest

from apps.rag_core.domain.retrieval import build_prompt
from apps.rag_core.services.query.contracts import (
    PreparedQuery,
    QueryExecutionContext,
    QueryRequest,
    RetrievalPolicy,
)
from apps.rag_core.services.query.prompt_builder import build_prompt_stage
from apps.rag_core.services.query.trace import RetrievalTraceBuilder

#: Formule dont la disparition EST l'objet du lot : une consigne de concision absolue,
#: valable quelle que soit la question posée.
CONCISION_ABSOLUE_FR = "Donne une réponse courte et directe."
CONCISION_ABSOLUE_EN = "Give a short, direct answer."


def _ctx(*, question, explanatory=False, comparative=False, news=False):
    req = QueryRequest.from_legacy_args(rag_space_id="rs", question=question, rag_space=None)
    ctx = QueryExecutionContext.create(
        req, started_at=0.0, trace=RetrievalTraceBuilder(entrypoint="internal", request_id="rid")
    )
    ctx.prepared = PreparedQuery.from_resolved(
        original_question=question, queries_for_search=[question],
        primary_retrieval_query=question, inventory=False,
        explanatory=explanatory, news=news, has_temporal_intent=False,
        comparative=comparative,
    )
    ctx.policy = RetrievalPolicy(
        fts_required=True, hybrid_enabled=False, vector_available=True,
        temporal_enabled=False, rerank_enabled=False, diversity_enabled=False,
        min_sources=1, min_text_rank=0.0, min_score=0.0, top_k=4, vector_top_k=20,
        candidate_k=4, lexical_weight=1.0,
        rerank_trigger_min_candidates=1, rerank_top_k=4,
    )
    return ctx


def _sources():
    return [{"chunk_id": "a", "score": 0.9, "text": "Le contrôle de premier niveau est mensuel."}]


def _stage(question, **modes):
    # Les deux drapeaux d'ÉTAT DES PREUVES sont décidés en amont (answerability) et
    # passés à l'étage, pas au contexte : on les extrait avant de construire le ctx.
    low_confidence = modes.pop("low_confidence", False)
    strict_refusal = modes.pop("strict_refusal", False)
    return build_prompt_stage(
        _ctx(question=question, **modes),
        selected=_sources(),
        low_confidence=low_confidence,
        strict_refusal=strict_refusal,
    )


def _assert_garde_fous_intacts(prompt: str) -> None:
    """Invariants NON NÉGOCIABLES, quelle que soit la politique de format."""
    assert "Réponds uniquement à partir des SOURCES." in prompt
    assert 'dis exactement : "Je ne sais pas."' in prompt
    assert "RÈGLES DE CITATION (IMPORTANT):" in prompt
    assert "Tu ne dois JAMAIS inventer des numéros de sources." in prompt
    assert "Ne complète jamais avec des informations absentes des SOURCES." in prompt


# --- Cas FACTUEL ------------------------------------------------------------
def test_factuel_conserve_une_consigne_de_reponse_directe():
    res = _stage("Quel est le montant du contrat ?")
    assert res.answer_kind == "ok"
    # La concision reste POSSIBLE et explicitement encouragée pour une question simple…
    assert "réponse directe si la question est factuelle" in res.prompt
    # …mais elle n'est plus IMPOSÉE à toutes les questions.
    assert CONCISION_ABSOLUE_FR not in res.prompt
    _assert_garde_fous_intacts(res.prompt)


def test_factuel_n_impose_pas_de_structure_lourde():
    """Non-régression : le but n'est pas de rendre toutes les réponses longues."""
    res = _stage("Quel est le montant du contrat ?")
    # Aucun préambule spécialisé ne s'applique : pas de plan imposé.
    assert "Structure attendue" not in res.prompt
    assert "1) Contexte / objectif" not in res.prompt
    assert "Adapte longueur et structure" in res.prompt


# --- Cas EXPLICATIF ---------------------------------------------------------
def test_explicatif_demande_du_developpement_sans_contradiction():
    res = _stage("Analyse en détail les principaux risques.", explanatory=True)
    assert res.answer_kind == "explain"
    # Le préambule de politique est bien injecté…
    assert "Donne une réponse détaillée et structurée" in res.prompt
    assert "1) Contexte / objectif" in res.prompt
    # …et le gabarit général ne le contredit plus.
    assert CONCISION_ABSOLUE_FR not in res.prompt
    assert "développée et structurée pour une analyse" in res.prompt
    assert "pas quelques puces quand les SOURCES en offrent davantage" in res.prompt
    _assert_garde_fous_intacts(res.prompt)


# --- Cas COMPARATIF ---------------------------------------------------------
def test_comparatif_garde_sa_structure_et_ses_garde_fous():
    res = _stage("Compare les risques entre les deux dispositifs.", comparative=True)
    assert res.answer_kind == "compare"
    assert "Objets comparés et dimensions couvertes" in res.prompt
    assert "Points communs" in res.prompt
    assert "Principales différences" in res.prompt
    assert "Conséquences / impacts" in res.prompt
    assert "Synthèse" in res.prompt
    # Garde-fou anti-comparaison inventée : intact.
    assert "comparaison artificielle" in res.prompt
    assert CONCISION_ABSOLUE_FR not in res.prompt
    _assert_garde_fous_intacts(res.prompt)


def test_comparatif_prime_sur_explicatif():
    """Précédence inchangée : compare > explain."""
    res = _stage("Compare en détail A et B", comparative=True, explanatory=True)
    assert res.answer_kind == "compare"


# --- Politiques de PRUDENCE : jamais contournées ----------------------------
def test_faible_confiance_prime_sur_une_demande_analytique():
    """La correction de profondeur ne doit JAMAIS court-circuiter l'abstention."""
    res = _stage("Analyse en détail les risques.", explanatory=True, low_confidence=True)
    assert res.answer_kind == "low_confidence"
    assert "réponds même si l'information est partielle" in res.prompt
    assert "N'invente rien." in res.prompt
    assert "Limites des informations disponibles" in res.prompt
    # Le préambule analytique ne s'est PAS substitué à celui de prudence.
    assert "Donne une réponse détaillée et structurée" not in res.prompt
    _assert_garde_fous_intacts(res.prompt)


def test_couverture_partielle_prime_sur_une_demande_analytique():
    res = _stage("Analyse en détail les risques.", explanatory=True, strict_refusal=True)
    assert res.answer_kind == "partial"
    assert "explique clairement les limites" in res.prompt
    assert "ce qui manque dans les documents" in res.prompt
    _assert_garde_fous_intacts(res.prompt)


# --- Gabarit brut (FR / EN) -------------------------------------------------
def test_gabarit_par_defaut_ne_porte_plus_de_concision_absolue():
    prompt = build_prompt("Q ?", [{"text": "x", "source": {"filename": "a.txt"}}])
    assert CONCISION_ABSOLUE_FR not in prompt
    assert "FORMAT DE RÉPONSE:" in prompt
    assert 'ajoute une ligne "Sources: [x], [y]"' in prompt


def test_gabarit_anglais_miroir():
    prompt = build_prompt(
        "Q?", [{"text": "x", "source": {"filename": "a.txt"}}], answer_language="en"
    )
    assert CONCISION_ABSOLUE_EN not in prompt
    assert "RESPONSE FORMAT:" in prompt
    assert "Match length and structure to the question" in prompt
    assert "Never fill gaps with information absent from the SOURCES." in prompt
    # Garde-fous anglais intacts.
    assert 'say exactly: "I don\'t know."' in prompt
    assert "CITATION RULES (IMPORTANT):" in prompt


@pytest.mark.parametrize("answer_language", [None, "fr", "en"])
def test_la_consigne_de_format_est_toujours_presente(answer_language):
    prompt = build_prompt("Q ?", [{"text": "x"}], answer_language=answer_language)
    marker = "RESPONSE FORMAT:" if answer_language == "en" else "FORMAT DE RÉPONSE:"
    assert marker in prompt
