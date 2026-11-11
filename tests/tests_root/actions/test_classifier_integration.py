"""Intégration classifieur flag off/on : comptes d'appels, pas de double classif,
chemin nominal = 0 appel, confidentialité (§17, §18)."""
from __future__ import annotations

import uuid

import pytest

from apps.actions import classifier as clf
from apps.actions import wiring
from apps.chat.services import chat_service as cs

pytestmark = pytest.mark.django_db


class _Req:
    data: dict = {}
    headers: dict = {}


def _vd(q):
    return {"rag_space_id": uuid.uuid4(), "question": q, "thread_id": None}


@pytest.fixture
def spies(monkeypatch, settings):
    settings.ACTION_AUDIT_ENABLED = False
    counts = {"classify": 0, "generation": 0, "retrieval": 0}
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(
        cs, "_run_retrieval",
        lambda **k: counts.__setitem__("retrieval", counts["retrieval"] + 1) or
        {"status": "ok", "sources": [], "prompt": "P", "log_id": None, "answer_kind": "ok"},
    )
    monkeypatch.setattr(
        cs, "generate_text",
        lambda **k: counts.__setitem__("generation", counts["generation"] + 1) or
        {"text": "R", "provider": "s", "model": "m"},
    )

    # Le classifieur (mocké) compte ses appels et renvoie une classification.
    def fake_classify(text, candidate_count=0):
        counts["classify"] += 1
        return {"intent": "search.list_documents", "score": 0.9, "parameters": {}}

    monkeypatch.setattr(clf, "classify_action", fake_classify)
    return counts, settings


def _enable(settings):
    settings.ACTIONS_LLM_CLASSIFIER_ENABLED = True


# ---- chemin nominal : 0 appel classifieur (flag on) -----------------------
def test_nominal_question_zero_classifier_calls(spies):
    counts, settings = spies
    _enable(settings)
    wiring.run_chat_sync(request=_Req(), user=None, rag_space=None,
                         validated_data=_vd("Quelle est la date du document ?"))
    assert counts["classify"] == 0     # aucune classification sur le nominal
    assert counts["generation"] == 1   # 1 seule génération (RAG)


# ---- flag OFF vs ON sur une question nominale : identique ------------------
def test_flag_off_equals_on_for_nominal(spies):
    counts, settings = spies
    q = "Quelle est la durée du contrat ?"
    settings.ACTIONS_LLM_CLASSIFIER_ENABLED = False
    off = wiring.run_chat_sync(request=_Req(), user=None, rag_space=None, validated_data=_vd(q))
    counts["classify"] = counts["generation"] = counts["retrieval"] = 0
    settings.ACTIONS_LLM_CLASSIFIER_ENABLED = True
    on = wiring.run_chat_sync(request=_Req(), user=None, rag_space=None, validated_data=_vd(q))
    assert off["status"] == on["status"]
    assert counts["classify"] == 0


# ---- indice listing/summarize non tranché -> 1 classification -------------
def test_hint_triggers_single_classification(spies, monkeypatch):
    counts, settings = spies
    _enable(settings)
    from api.v1.chat import views as cv
    monkeypatch.setattr(cv, "_extract_term_for_listing", lambda q: "Nordexia")
    monkeypatch.setattr(cv, "_run_listing_search", lambda **k: [])
    monkeypatch.setattr(cv, "_format_listing_answer", lambda t, h: "L")

    out = wiring.run_chat_sync(request=_Req(), user=None, rag_space=None,
                               validated_data=_vd("Peux-tu lister les dossiers ?"))
    assert out == {"answer": "L", "sources": [], "log_id": None}  # escaladé -> listing
    assert counts["classify"] == 1   # exactement UNE classification
    assert counts["generation"] == 0  # pas de RAG


# ---- committed rule -> pas de classification ------------------------------
def test_committed_summary_no_classification(spies):
    counts, settings = spies
    _enable(settings)
    out = wiring.run_chat_sync(request=_Req(), user=None, rag_space=None,
                               validated_data=_vd("Résume le document Rapport.pdf"))
    assert out["status"] == "clarification"   # summary committé (rag_space None -> clarif)
    assert counts["classify"] == 0


# ---- confidentialité : la question n'est jamais persistée par l'audit ------
def test_classifier_audit_has_no_question(monkeypatch, settings):
    settings.ACTIONS_LLM_CLASSIFIER_ENABLED = True
    settings.ACTION_AUDIT_ENABLED = True
    secret_q = "Mon token est sk-SECRET-123 ; fais un topo du dossier confidentiel.pdf"
    monkeypatch.setattr(
        clf, "generate_text",
        lambda **k: {"text": '{"intent":"document.summarize","score":0.9,"parameters":{"document_reference":"confidentiel.pdf"}}'},
    )
    clf.classify_action(secret_q, candidate_count=0)

    from apps.audit.models import ActionAuditEvent
    for e in ActionAuditEvent.objects.filter(action_name="action.classify"):
        blob = f"{e.metadata} {e.error_code} {e.detection_method}"
        assert "SECRET" not in blob and "confidentiel.pdf" not in blob and secret_q not in blob
        # seules des métadonnées bornées sont présentes
        assert set(e.metadata).issubset(
            {"classifier_model", "candidate_count", "result_intent", "result_valid", "failure_category"}
        )
