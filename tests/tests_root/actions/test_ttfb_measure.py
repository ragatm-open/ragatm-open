"""Harnais de mesure SSE instrumenté (lot 3b, §12) — mocks.

Mesure : n_events, TTFB (1er événement), time-to-done, et le NOMBRE d'appels
LLM / retrieval / summarize par cas. Les temps muraux sont négligeables (mocks) :
ce harnais valide l'ORDRE et les COMPTES d'appels, pas la latence live.
"""
from __future__ import annotations

import time
import uuid

import pytest

from apps.actions import wiring
from apps.chat.services import chat_service as cs

pytestmark = pytest.mark.django_db


class _Req:
    data: dict = {}
    headers: dict = {}


def _vd(q):
    return {"rag_space_id": uuid.uuid4(), "question": q, "thread_id": None}


def _measure(gen):
    counts = {"n": 0, "ttfb_ms": None, "ttdone_ms": None}
    t0 = time.perf_counter()
    for evt in gen:
        counts["n"] += 1
        if counts["ttfb_ms"] is None:
            counts["ttfb_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        if evt.get("event") == "done":
            counts["ttdone_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    return counts


@pytest.fixture
def space(db):
    from django.contrib.auth import get_user_model

    from apps.rag_core.models import Document, RagSpace
    from apps.workspaces.models import Membership, Workspace

    user = get_user_model().objects.create_user(username="m", password="pw")
    ws = Workspace.objects.create(name="W")
    Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_MEMBER)
    rs = RagSpace.objects.create(name="S", workspace=ws)
    Document.objects.create(rag_space=rs, filename="Doc.pdf", status="READY")
    return user, rs


def test_ttfb_table(space, monkeypatch, capsys):
    user, rs = space
    calls = {"llm": 0, "retrieval": 0, "summarize": 0}

    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(
        cs, "_run_retrieval",
        lambda **k: calls.__setitem__("retrieval", calls["retrieval"] + 1) or
        {"status": "ok", "sources": [], "prompt": "P", "log_id": None, "answer_kind": "ok"},
    )
    monkeypatch.setattr(cs, "_gateway_enabled", lambda: False)
    monkeypatch.setattr(
        cs, "generate_text_stream",
        lambda **k: calls.__setitem__("llm", calls["llm"] + 1) or
        iter([{"type": "delta", "delta": "R."}, {"type": "result", "provider": "s", "model": "m", "usage": None}]),
    )

    import apps.rag_core.services.summary_service as ss
    monkeypatch.setattr(
        ss, "summarize_document",
        lambda **k: calls.__setitem__("summarize", calls["summarize"] + 1) or _Ready(),
    )
    import api.v1.documents.views_common as vc
    monkeypatch.setattr(vc, "load_accessible_document", lambda *a, **k: True)

    from api.v1.chat import views as cv
    monkeypatch.setattr(cv, "_extract_term_for_listing", lambda q: "Nordexia")
    monkeypatch.setattr(cv, "_run_listing_search", lambda **k: [])
    monkeypatch.setattr(cv, "_format_listing_answer", lambda t, h: "L")
    monkeypatch.setattr(cv, "_ensure_thread_and_persist_messages", lambda **k: type("T", (), {"id": "t"})())

    cases = {
        "rag_standard": "Quelle est la durée du contrat ?",
        "listing": "Liste les articles sur Nordexia",
        "summary_ready": "Résume Doc.pdf",
        "clarification": "Résume-le",
    }

    rows = []
    for name, q in cases.items():
        for k in calls:
            calls[k] = 0
        m = _measure(
            wiring.run_chat_stream(request=_Req(), user=user, rag_space=rs, request_id="r", validated_data=_vd(q))
        )
        rows.append((name, m["n"], m["ttfb_ms"], m["ttdone_ms"], dict(calls)))

    # assertions d'ordre / comptes
    by = {r[0]: r for r in rows}
    assert by["rag_standard"][4] == {"llm": 1, "retrieval": 1, "summarize": 0}
    assert by["listing"][4] == {"llm": 0, "retrieval": 0, "summarize": 0}
    assert by["summary_ready"][4] == {"llm": 0, "retrieval": 0, "summarize": 1}
    assert by["clarification"][4] == {"llm": 0, "retrieval": 0, "summarize": 0}
    for _, n, ttfb, ttdone, _c in rows:
        assert n >= 1 and ttfb is not None and ttdone is not None and ttfb <= ttdone

    print("\n=== SSE instrumenté (mocks) ===")
    print(f"{'cas':<16}{'n_evts':<8}{'ttfb_ms':<10}{'ttdone_ms':<12}{'appels'}")
    for name, n, ttfb, ttdone, c in rows:
        print(f"{name:<16}{n:<8}{ttfb:<10}{ttdone:<12}{c}")
    with capsys.disabled():
        pass


class _Ready:
    status = "ready"
    summary = "Synthèse."
    summary_id = "sid"
    log_id = None
    mode = "bullets"
    scope = "document"
    page = None
    language = "fr"
    truncated = False
    provider = "s"
    model = "m"
    tokens = 0
    prompt_tokens = None
    completion_tokens = None
    cost_usd = None
    cached = False
    generated_at = None
    error = ""
