"""Politique de langue de réponse IA (P0).

Couvre : le résolveur commun (détection + chaîne de résolution + gate flag), les
variantes FR/EN des prompts (base + préambules ok/partial/low_confidence/explain),
la NON-RÉGRESSION (cible fr/None ⇒ prompt français à l'octet près), le fallback,
et les cas A→G de la mission (au niveau POLITIQUE : la locale d'interface n'est
jamais un intrant).
"""
import pytest

from apps.rag_core.services.answer_language import (
    DIRECTIVE_EN,
    detect_question_language,
    resolve_answer_language,
    resolve_target,
)
from apps.rag_core.domain.retrieval import build_prompt
from apps.rag_core.services.answer_language import policy_enabled  # noqa: F401
from apps.rag_core.services.query.contracts import (
    PreparedQuery,
    QueryExecutionContext,
    QueryRequest,
    RetrievalPolicy,
)
from apps.rag_core.services.query.prompt_builder import build_prompt_stage
from apps.rag_core.services.query.trace import RetrievalTraceBuilder
from apps.chat.services.chat_fallback import build_general_fallback_prompt, fallback_disclaimer


OBSERVED_EN = (
    "What significant reconciliation discrepancies were identified for Tessaly and Valteria "
    "at the December 2025 close and at the most recent available close?"
)
OBSERVED_FR = "Quels écarts de rapprochement ont été identifiés pour Tessaly à la clôture ?"

# Titres FR (préambule low_confidence) — signature de la structure française.
FR_TITLES = "Informations pertinentes trouvées"
EN_TITLES = "Relevant information found"


# --------------------------------------------------------------------------- #
# Détection
# --------------------------------------------------------------------------- #
def test_detect_english_question():
    assert detect_question_language(OBSERVED_EN) == "en"


def test_detect_french_question():
    assert detect_question_language(OBSERVED_FR) == "fr"


@pytest.mark.parametrize("q", ["", "   ", "12345", "TSL-2025", "42 %", "AB1234"])
def test_detect_indeterminable_returns_none(q):
    assert detect_question_language(q) is None


# --------------------------------------------------------------------------- #
# Résolveur (indépendant du flag / de la locale UI)
# --------------------------------------------------------------------------- #
def test_forced_pref_wins_over_detection():
    assert resolve_answer_language(pref="fr", question=OBSERVED_EN) == "fr"
    assert resolve_answer_language(pref="en", question=OBSERVED_FR) == "en"


def test_auto_follows_question_language():
    assert resolve_answer_language(pref="auto", question=OBSERVED_EN) == "en"
    assert resolve_answer_language(pref="auto", question=OBSERVED_FR) == "fr"


def test_auto_indeterminable_falls_back_to_thread_then_fr():
    # Question indéterminable → langue précédente du thread.
    assert resolve_answer_language(pref="auto", question="42", thread_prev_language="en") == "en"
    # Pas de thread → français (repli historique).
    assert resolve_answer_language(pref="auto", question="42", thread_prev_language=None) == "fr"


def test_unknown_pref_treated_as_auto():
    assert resolve_answer_language(pref="de", question=OBSERVED_EN) == "en"
    assert resolve_answer_language(pref=None, question=OBSERVED_FR) == "fr"


# --------------------------------------------------------------------------- #
# Gate plateforme (flag)
# --------------------------------------------------------------------------- #
def test_resolve_target_disabled_returns_none(settings):
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = False
    assert resolve_target(pref="en", question=OBSERVED_EN) is None
    assert resolve_target(pref="auto", question=OBSERVED_EN) is None


def test_resolve_target_enabled(settings):
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = True
    assert resolve_target(pref="auto", question=OBSERVED_EN) == "en"
    assert resolve_target(pref="auto", question=OBSERVED_FR) == "fr"
    assert resolve_target(pref="fr", question=OBSERVED_EN) == "fr"
    assert resolve_target(pref="en", question=OBSERVED_FR) == "en"


# --------------------------------------------------------------------------- #
# Cas A→G de la mission (niveau POLITIQUE : la locale UI n'est PAS un intrant)
# --------------------------------------------------------------------------- #
def test_cases_A_to_G(settings):
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = True
    r = resolve_answer_language
    # A : UI en, question en, nouvelle conversation → en
    assert r(pref="auto", question=OBSERVED_EN) == "en"
    # B : UI fr, question en → en (indépendant de l'UI)
    assert r(pref="auto", question=OBSERVED_EN) == "en"
    # C : UI en, question fr → fr
    assert r(pref="auto", question=OBSERVED_FR) == "fr"
    # D : conversation commencée en fr, puis question en → en (suit la question)
    assert r(pref="auto", question=OBSERVED_EN, thread_prev_language="fr") == "en"
    # E : conversation commencée en en, puis question fr → fr
    assert r(pref="auto", question=OBSERVED_FR, thread_prev_language="en") == "fr"
    # F : documents fr, question en → en (la langue des docs n'est pas un intrant)
    assert r(pref="auto", question=OBSERVED_EN) == "en"
    # G : documents en, question fr → fr
    assert r(pref="auto", question=OBSERVED_FR) == "fr"


# --------------------------------------------------------------------------- #
# build_prompt (base) — FR inchangé / EN localisé
# --------------------------------------------------------------------------- #
_SEL = [{"chunk_id": "a", "score": 0.9, "text": "Body.", "source": {"label": "Doc"}}]


def test_build_prompt_french_default_unchanged():
    p_none = build_prompt("Q ?", _SEL, answer_language=None)
    p_fr = build_prompt("Q ?", _SEL, answer_language="fr")
    assert p_none == p_fr  # fr et None empruntent le MÊME chemin français
    assert "FORMAT DE RÉPONSE" in p_none
    assert "Tu es un assistant RAG." in p_none
    assert DIRECTIVE_EN not in p_none


def test_build_prompt_english_variant():
    p_en = build_prompt("Q?", _SEL, answer_language="en")
    assert p_en.startswith(DIRECTIVE_EN)
    assert "RESPONSE FORMAT" in p_en
    assert "You are a RAG assistant." in p_en
    assert "FORMAT DE RÉPONSE" not in p_en


# --------------------------------------------------------------------------- #
# build_prompt_stage — préambules FR/EN + non-régression
# --------------------------------------------------------------------------- #
def _ctx(*, question="q", answer_language=None, explanatory=False, news=False):
    req = QueryRequest.from_legacy_args(
        rag_space_id="rs", question=question, rag_space=None, answer_language=answer_language
    )
    tb = RetrievalTraceBuilder(entrypoint="internal", request_id="rid")
    ctx = QueryExecutionContext.create(req, started_at=0.0, trace=tb)
    ctx.prepared = PreparedQuery.from_resolved(
        original_question=question, queries_for_search=[question],
        primary_retrieval_query=question, inventory=False,
        explanatory=explanatory, news=news, has_temporal_intent=False,
    )
    ctx.policy = RetrievalPolicy(
        fts_required=True, hybrid_enabled=False, vector_available=True,
        temporal_enabled=False, rerank_enabled=False, diversity_enabled=False,
        min_sources=1, min_text_rank=0.0, min_score=0.0, top_k=4, vector_top_k=20,
        candidate_k=4, lexical_weight=1.0,
        rerank_trigger_min_candidates=1, rerank_top_k=4,
    )
    return ctx


def _sel():
    return [{"chunk_id": "a", "score": 0.9, "text": "Reconciliation body."}]


def test_lowconf_french_unchanged_when_target_none():
    res = build_prompt_stage(_ctx(answer_language=None), selected=_sel(),
                             low_confidence=True, strict_refusal=False)
    assert FR_TITLES in res.prompt
    assert EN_TITLES not in res.prompt
    assert DIRECTIVE_EN not in res.prompt


def test_lowconf_french_and_none_are_byte_identical():
    p_none = build_prompt_stage(_ctx(answer_language=None), selected=_sel(),
                                low_confidence=True, strict_refusal=False).prompt
    p_fr = build_prompt_stage(_ctx(answer_language="fr"), selected=_sel(),
                              low_confidence=True, strict_refusal=False).prompt
    assert p_none == p_fr  # non-régression : fr == comportement historique


def test_lowconf_english_titles_and_directive():
    res = build_prompt_stage(_ctx(answer_language="en"), selected=_sel(),
                             low_confidence=True, strict_refusal=False)
    assert EN_TITLES in res.prompt
    assert FR_TITLES not in res.prompt
    assert DIRECTIVE_EN in res.prompt
    assert res.answer_kind == "low_confidence"  # la POLITIQUE de confiance est intacte


def test_partial_english_structure():
    res = build_prompt_stage(_ctx(answer_language="en"), selected=_sel(),
                             low_confidence=False, strict_refusal=True)
    assert "Uncertainties / what is missing" in res.prompt
    assert "Zones d'incertitude" not in res.prompt
    assert res.answer_kind == "partial"


def test_explain_english_structure():
    res = build_prompt_stage(_ctx(answer_language="en", explanatory=True), selected=_sel(),
                             low_confidence=False, strict_refusal=False)
    assert DIRECTIVE_EN in res.prompt
    assert res.answer_kind == "explain"


# --------------------------------------------------------------------------- #
# Fallback (hors sources)
# --------------------------------------------------------------------------- #
def test_fallback_prompt_and_disclaimer_localised():
    assert "Respond in English." in build_general_fallback_prompt("Q?", answer_language="en")
    assert "Réponds en français." in build_general_fallback_prompt("Q ?", answer_language=None)
    assert "outside the RagSpace" in fallback_disclaimer("en")
    assert "hors documents" in fallback_disclaimer(None)
