"""Résumé backend exécutable (lot 3b) — résolution + exécution + permissions.

``summarize_document`` est mocké (pas de LLM réel) ; la RÉSOLUTION s'appuie sur de
vrais ``Document`` en base pour prouver l'isolation RagSpace / anti-énumération et
l'indépendance vis-à-vis du frontend.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from apps.actions import wiring
from apps.actions.plan import ActionPlan
from apps.actions.resolvers import resolve_summary_target

pytestmark = pytest.mark.django_db


@dataclass
class _FakeRes:
    status: str
    summary: str = "RÉSUMÉ"
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
def space(db):
    from django.contrib.auth import get_user_model

    from apps.rag_core.models import RagSpace
    from apps.workspaces.models import Membership, Workspace

    user = get_user_model().objects.create_user(username="su", password="pw")
    ws = Workspace.objects.create(name="W")
    Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_MEMBER)
    rs = RagSpace.objects.create(name="S", workspace=ws)
    return user, rs


def _doc(rag_space, filename, status="READY"):
    from apps.rag_core.models import Document

    return Document.objects.create(rag_space=rag_space, filename=filename, status=status)


def _plan_filename(name):
    return ActionPlan(action="document.summarize", detection_method="rule",
                      parameters={"ref_kind": "filename", "document_reference": name})


# ---- résolution ----------------------------------------------------------
def test_resolve_by_filename_unique(space):
    user, rs = space
    _doc(rs, "Rapport_AXA_2024.pdf")
    t = resolve_summary_target(user=user, rag_space=rs, thread_id=None, plan=_plan_filename("Rapport_AXA_2024.pdf"))
    assert t.status == "resolved" and t.document.filename == "Rapport_AXA_2024.pdf"


def test_resolve_by_filename_ambiguous_clarifies(space):
    user, rs = space
    _doc(rs, "Rapport_A.pdf")
    _doc(rs, "Rapport_B.pdf")
    t = resolve_summary_target(user=user, rag_space=rs, thread_id=None, plan=_plan_filename("Rapport"))
    assert t.status == "clarification" and "Plusieurs" in t.message


def test_resolve_not_found_no_leak(space):
    user, rs = space
    t = resolve_summary_target(user=user, rag_space=rs, thread_id=None, plan=_plan_filename("Inexistant.pdf"))
    assert t.status == "clarification"


def test_resolve_isolation_other_ragspace(space):
    """Un document d'un AUTRE RagSpace n'est jamais résolu (anti-énumération)."""
    user, rs = space
    from apps.rag_core.models import RagSpace

    other = RagSpace.objects.create(name="OTHER", workspace=rs.workspace)
    _doc(other, "Secret_Autre_Espace.pdf")
    t = resolve_summary_target(user=user, rag_space=rs, thread_id=None, plan=_plan_filename("Secret_Autre_Espace.pdf"))
    assert t.status == "clarification"  # introuvable dans l'espace autorisé


# ---- exécution (summarize_document mocké) --------------------------------
def _patch_summarize(monkeypatch, res):
    import apps.rag_core.services.summary_service as ss

    calls = {"n": 0}

    def fake(**kwargs):
        calls["n"] += 1
        return res

    monkeypatch.setattr(ss, "summarize_document", fake)
    # la ré-validation de permission passe (doc dans l'espace autorisé)
    import api.v1.documents.views_common as vc
    monkeypatch.setattr(vc, "load_accessible_document", lambda *a, **k: True)
    return calls


def test_execute_summary_ready_sync(space, monkeypatch):
    user, rs = space
    _doc(rs, "Contrat.pdf")
    calls = _patch_summarize(monkeypatch, _FakeRes(status="ready", summary="Voici le résumé."))

    out = wiring.run_chat_sync(
        request=type("R", (), {"data": {}, "headers": {}})(),
        user=user, rag_space=rs,
        validated_data={"rag_space_id": uuid.uuid4(), "question": "Résume le document Contrat.pdf", "thread_id": None},
    )
    assert out["status"] == "ok"
    assert out["answer_kind"] == "summary"
    assert out["answer"] == "Voici le résumé."
    assert out["summary"]["status"] == "ready"  # contrat historique embarqué
    assert calls["n"] == 1  # summarize_document appelé une fois


def test_execute_summary_pending_sync(space, monkeypatch):
    user, rs = space
    _doc(rs, "Gros.pdf")
    _patch_summarize(monkeypatch, _FakeRes(status="pending", summary=""))

    out = wiring.run_chat_sync(
        request=type("R", (), {"data": {}, "headers": {}})(),
        user=user, rag_space=rs,
        validated_data={"rag_space_id": uuid.uuid4(), "question": "Résume le document Gros.pdf", "thread_id": None},
    )
    assert out["status"] == "pending"
    assert out["summary"]["status"] == "pending"  # le client peut poller GET summary


def test_execute_summary_ready_stream(space, monkeypatch):
    user, rs = space
    _doc(rs, "Note.pdf")
    _patch_summarize(monkeypatch, _FakeRes(status="ready", summary="Synthèse."))

    events = list(wiring.run_chat_stream(
        request=type("R", (), {"data": {}, "headers": {}})(),
        user=user, rag_space=rs, request_id="r",
        validated_data={"rag_space_id": uuid.uuid4(), "question": "Résume Note.pdf", "thread_id": None},
    ))
    assert [e["event"] for e in events] == ["meta", "sources", "token", "done"]
    assert events[2]["data"]["delta"] == "Synthèse."
    assert events[3]["data"]["status"] == "ok"
    assert events[3]["data"]["answer_kind"] == "summary"


def test_summary_not_found_yields_clarification_no_summarize_call(space, monkeypatch):
    user, rs = space
    calls = _patch_summarize(monkeypatch, _FakeRes(status="ready"))
    out = wiring.run_chat_sync(
        request=type("R", (), {"data": {}, "headers": {}})(),
        user=user, rag_space=rs,
        validated_data={"rag_space_id": uuid.uuid4(), "question": "Résume Absent.pdf", "thread_id": None},
    )
    assert out["status"] == "clarification"
    assert calls["n"] == 0  # aucun handler de génération pour une cible absente
