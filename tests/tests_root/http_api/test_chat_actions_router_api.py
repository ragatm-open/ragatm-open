"""Tests API DIRECTS (sans frontend) du routeur d'actions — lot 3b.

Prouve que le backend est l'autorité pour le résumé (POST /api/v1/chat/ direct) et
que le contrat d'erreur listing (``listing_route_failed``) est préservé sous flag.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from apps.rag_core.models import Document

pytestmark = pytest.mark.django_db


@dataclass
class _FakeRes:
    status: str = "ready"
    summary: str = "RÉSUMÉ DIRECT"
    summary_id: str | None = "sid"
    log_id: str | None = None
    mode: str = "bullets"
    scope: str = "document"
    page: int | None = None
    language: str = "fr"
    truncated: bool = False
    provider: str | None = "stub"
    model: str | None = "m"
    tokens: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False
    generated_at = None
    error: str = ""


@pytest.fixture
def flag_on(settings):
    settings.ACTIONS_ROUTER_ENABLED = True
    return settings


def _ready_doc(rag_space, filename):
    return Document.objects.create(rag_space=rag_space, filename=filename, status="READY")


def _fake_res(status="ready", summary="RÉSUMÉ DIRECT"):
    return _FakeRes(status=status, summary=summary)


# ---- Résumé exécuté directement par le backend (frontend non requis) -------
def test_chat_summary_by_filename_executes(api_client, rag_space, flag_on, monkeypatch):
    _ready_doc(rag_space, "Rapport.pdf")
    import apps.rag_core.services.summary_service as ss
    monkeypatch.setattr(ss, "summarize_document", lambda **k: _fake_res())

    res = api_client.post(
        "/api/v1/chat/",
        {"rag_space_id": str(rag_space.id), "question": "Résume le document Rapport.pdf"},
        format="json",
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["answer_kind"] == "summary"
    assert body["answer"] == "RÉSUMÉ DIRECT"


def test_chat_summary_ambiguous_clarifies(api_client, rag_space, flag_on):
    # Aucun document -> cible introuvable -> clarification (aucune génération).
    res = api_client.post(
        "/api/v1/chat/",
        {"rag_space_id": str(rag_space.id), "question": "Résume le document Inconnu.pdf"},
        format="json",
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "clarification"
    assert body["answer_kind"] == "clarification"


def test_chat_summary_other_ragspace_no_leak(api_client, rag_space, flag_on, workspace, monkeypatch):
    from apps.rag_core.models import RagSpace
    other = RagSpace.objects.create(name="other", workspace=workspace)
    _ready_doc(other, "Confidentiel.pdf")
    import apps.rag_core.services.summary_service as ss
    called = {"n": 0}
    monkeypatch.setattr(ss, "summarize_document", lambda **k: called.__setitem__("n", 1) or _fake_res())

    res = api_client.post(
        "/api/v1/chat/",
        {"rag_space_id": str(rag_space.id), "question": "Résume le document Confidentiel.pdf"},
        format="json",
    )
    body = res.json()
    assert body["status"] == "clarification"   # jamais résolu hors de l'espace autorisé
    assert called["n"] == 0                     # aucune génération
    assert "Confidentiel" not in body["answer"] or "trouvé" in body["answer"].lower()


# ---- Contrat d'erreur listing préservé (flag off == flag on) --------------
def _force_listing_failure(monkeypatch):
    from api.v1.chat import views as cv

    monkeypatch.setattr(cv, "_looks_like_listing_query", lambda q: True)
    monkeypatch.setattr(cv, "_extract_term_for_listing", lambda q: "Nordexia")

    def boom(**k):
        raise RuntimeError("SELECT ... boom (interne, ne doit pas fuiter)")

    monkeypatch.setattr(cv, "_run_listing_search", boom)


def test_listing_error_contract_flag_off(api_client, rag_space, monkeypatch):
    _force_listing_failure(monkeypatch)
    res = api_client.post(
        "/api/v1/chat/",
        {"rag_space_id": str(rag_space.id), "question": "Liste les articles sur Nordexia"},
        format="json",
    )
    assert res.status_code == 500
    body = res.json()
    assert body["detail"] == "listing_route_failed"
    assert "boom" not in json.dumps(body)  # pas de fuite interne / SQL


def test_listing_error_contract_flag_on_parity(api_client, rag_space, flag_on, monkeypatch):
    _force_listing_failure(monkeypatch)
    res = api_client.post(
        "/api/v1/chat/",
        {"rag_space_id": str(rag_space.id), "question": "Liste les articles sur Nordexia"},
        format="json",
    )
    assert res.status_code == 500
    body = res.json()
    assert body["detail"] == "listing_route_failed"  # MÊME code que flag off
    assert "boom" not in json.dumps(body)


# ---- SSE direct : résumé disponible --------------------------------------
def test_chat_stream_summary_ready(api_client, rag_space, flag_on, monkeypatch):
    _ready_doc(rag_space, "Note.pdf")
    import apps.rag_core.services.summary_service as ss
    monkeypatch.setattr(ss, "summarize_document", lambda **k: _fake_res(summary="Synthèse SSE."))

    res = api_client.post(
        "/api/v1/chat/stream/",
        {"rag_space_id": str(rag_space.id), "question": "Résume Note.pdf"},
        format="json",
    )
    assert res.status_code == 200
    raw = b"".join(res.streaming_content).decode("utf-8")
    assert "event: meta" in raw and "event: sources" in raw
    assert "event: token" in raw and "Synthèse SSE." in raw
    assert "event: done" in raw and '"answer_kind": "summary"' in raw
