"""Qualification — `X-Request-ID` à la frontière HTTP du backend (middleware en conditions réelles).

- un identifiant conforme fourni par le proxy Next est conservé et renvoyé ;
- un identifiant non conforme est remplacé (jamais relayé) et le client reçoit l'identifiant effectif ;
- l'identifiant renvoyé dans les erreurs applicatives (503 `ai_gateway_unavailable`) est celui
  transmis à la Gateway par le client de génération (même valeur, même grammaire).
"""
from __future__ import annotations

import pytest

from apps.llm_router.gateway import exceptions as gx

pytestmark = pytest.mark.django_db

CHAT = "/api/v1/chat/"


def test_conforming_request_id_round_trips(api_client, rag_space, monkeypatch):
    from api.v1.chat import views

    def _raise(*a, **k):
        raise gx.GatewayTemporaryUnavailable("refused")

    monkeypatch.setattr(views, "chat_query", _raise)
    res = api_client.post(CHAT, {"rag_space_id": str(rag_space.id), "question": "q ?"},
                          format="json", HTTP_X_REQUEST_ID="tr.http:1-a")
    assert res.status_code == 503
    assert res["X-Request-ID"] == "tr.http:1-a"
    assert res.data["request_id"] == "tr.http:1-a"


def test_non_conforming_request_id_is_replaced_not_relayed(api_client, rag_space, monkeypatch):
    from api.v1.chat import views

    seen = {}

    def _capture(*a, **k):
        from config.middleware.request_id import get_current_request_id
        seen["current"] = get_current_request_id()
        raise gx.GatewayTemporaryUnavailable("refused")

    monkeypatch.setattr(views, "chat_query", _capture)
    bad = "bad id with spaces"
    res = api_client.post(CHAT, {"rag_space_id": str(rag_space.id), "question": "q ?"},
                          format="json", HTTP_X_REQUEST_ID=bad)
    assert res.status_code == 503
    rid = res["X-Request-ID"]
    assert rid != bad and len(rid) == 36
    assert res.data["request_id"] == rid
    assert seen["current"] == rid  # l'identifiant effectif est celui exposé aux clients Gateway
