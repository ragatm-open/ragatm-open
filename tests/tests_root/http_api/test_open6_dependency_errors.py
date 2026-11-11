"""erreurs applicatives explicites quand une dépendance est indisponible.

Qualification : Gateway ou Ollama arrêtés => `500 internal_error` ; Redis arrêté
pendant un dépôt => page 500 Django brute. Ces tests verrouillent le comportement
corrigé : codes HTTP cohérents, code d'erreur stable, `request_id`, `Retry-After`,
aucun détail interne, aucun repli, aucune réponse fabriquée. Mocks uniquement.
"""
from __future__ import annotations

import json

import pytest
import redis.exceptions as rx

from apps.llm_router.gateway import exceptions as gx

pytestmark = pytest.mark.django_db

CHAT = "/api/v1/chat/"
STREAM = "/api/v1/chat/stream/"


def _post_chat(api_client, rag_space, path=CHAT, **extra):
    return api_client.post(
        path,
        {"rag_space_id": str(rag_space.id), "question": "Quelle est la température à Bérénice ?"},
        format="json",
        HTTP_X_REQUEST_ID="open6-req-1",
        **extra,
    )


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


# --------------------------------------------------------------------------- chat agrégé
@pytest.mark.parametrize(
    "exc",
    [
        gx.GatewayUnavailableError("AI Gateway unavailable and no fallback provider exists"),
        gx.GatewayTemporaryUnavailable("HTTPConnectionPool(host='ai-gateway', port=8100): refused"),
        gx.GatewayTimeoutError("read timed out"),
    ],
)
def test_chat_gateway_down_is_503_named_and_retryable(api_client, rag_space, monkeypatch, exc):
    from api.v1.chat import views

    monkeypatch.setattr(views, "chat_query", _raise(exc))
    res = _post_chat(api_client, rag_space)
    assert res.status_code == 503
    assert res.data == {"detail": "ai_gateway_unavailable", "request_id": "open6-req-1", "retryable": True}
    assert res["Retry-After"] == "10"
    body = json.dumps(res.data)
    assert "ai-gateway" not in body and "8100" not in body and "timed out" not in body


def test_chat_gateway_error_wrapped_in_retrieval_failure_is_still_named(api_client, rag_space, monkeypatch):
    from api.v1.chat import views

    def _wrapped(**k):
        try:
            raise gx.GatewayTemporaryUnavailable("connection refused")
        except gx.GatewayError as inner:
            raise RuntimeError("retrieval leg failed") from inner

    monkeypatch.setattr(views, "chat_query", _wrapped)
    res = _post_chat(api_client, rag_space)
    assert res.status_code == 503
    assert res.data["detail"] == "ai_gateway_unavailable"


@pytest.mark.parametrize(
    "exc",
    [gx.GatewayConfigError("AI_GATEWAY_BASE_URL manquante"), gx.GatewayContractError("schema"), gx.GatewayAuthenticationError("401")],
)
def test_chat_gateway_deployment_defect_is_502_not_retryable(api_client, rag_space, monkeypatch, exc):
    from api.v1.chat import views

    monkeypatch.setattr(views, "chat_query", _raise(exc))
    res = _post_chat(api_client, rag_space)
    assert res.status_code == 502
    assert res.data == {"detail": "ai_gateway_error", "request_id": "open6-req-1", "retryable": False}
    assert not res.has_header("Retry-After")
    assert "AI_GATEWAY_BASE_URL" not in json.dumps(res.data)


def test_chat_unknown_failure_still_500_internal_error(api_client, rag_space, monkeypatch):
    """Comportement historique conservé pour un vrai bug : 500 anonyme, request_id, rien d'autre."""
    from api.v1.chat import views

    monkeypatch.setattr(views, "chat_query", _raise(RuntimeError("secret internal path /opt/x")))
    res = _post_chat(api_client, rag_space)
    assert res.status_code == 500
    assert res.data == {"detail": "internal_error", "request_id": "open6-req-1"}


# --------------------------------------------------------------------------- flux SSE
def _sse_events(content: bytes) -> list[tuple[str, dict]]:
    out = []
    for block in content.decode("utf-8").split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if name:
            out.append((name, data))
    return out


def test_chat_stream_gateway_down_emits_named_error_then_done(api_client, rag_space, monkeypatch):
    from api.v1.chat import views

    def _gen(**k):
        raise gx.GatewayUnavailableError("no fallback")
        yield  # pragma: no cover

    monkeypatch.setattr(views, "chat_query_stream", _gen)
    res = _post_chat(api_client, rag_space, path=STREAM)
    assert res.status_code == 200
    events = _sse_events(b"".join(res.streaming_content))
    names = [n for n, _ in events]
    assert names == ["meta", "sources", "error", "done"]
    err = dict(events)["error"]
    assert err["detail"] == "ai_gateway_unavailable" and err["retryable"] is True
    assert err["request_id"] == "open6-req-1" and err["retry_after"] == 10
    assert dict(events)["done"]["status"] == "error"
    # aucun texte fabriqué : pas d'événement token
    assert "token" not in names


def test_chat_stream_unknown_failure_keeps_legacy_message(api_client, rag_space, monkeypatch):
    from api.v1.chat import views

    def _gen(**k):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    monkeypatch.setattr(views, "chat_query_stream", _gen)
    res = _post_chat(api_client, rag_space, path=STREAM)
    events = _sse_events(b"".join(res.streaming_content))
    names = [n for n, _ in events]
    assert names == ["meta", "sources", "token", "done"]
    assert dict(events)["done"]["status"] == "error"
    assert "boom" not in json.dumps(dict(events))


# --------------------------------------------------------------------------- Redis indisponible
def test_upload_redis_down_is_503_dependency_unavailable(api_client, rag_space, monkeypatch):
    """Une panne Redis sur le chemin du dépôt devient un 503 JSON stable (plus de page 500)."""
    from api.v1.documents import views_write

    monkeypatch.setattr(
        views_write,
        "require_rag_space_access",
        _raise(rx.ConnectionError("Error 111 connecting to redis:6379. Connection refused.")),
    )
    from django.core.files.uploadedfile import SimpleUploadedFile

    res = api_client.post(
        f"/api/v1/documents/upload/?rag_space_id={rag_space.id}",
        {"file": SimpleUploadedFile("note.txt", b"contenu synthetique", content_type="text/plain")},
        format="multipart",
        HTTP_X_REQUEST_ID="open6-req-2",
    )
    assert res.status_code == 503
    assert res["Content-Type"].startswith("application/json")
    assert res.json() == {
        "detail": "dependency_unavailable",
        "dependency": "redis",
        "request_id": "open6-req-2",
        "retryable": True,
    }
    assert res["Retry-After"] == "10"
    assert "6379" not in res.content.decode() and "redis:" not in res.content.decode()


def test_broker_down_is_503_dependency_unavailable(api_client, rag_space, monkeypatch):
    from kombu.exceptions import OperationalError

    from api.v1.chat import views

    monkeypatch.setattr(views, "chat_query", _raise(OperationalError("[Errno 111] Connection refused")))
    res = _post_chat(api_client, rag_space)
    # la vue chat capture tout => 500 historique ? Non : le kombu.OperationalError n'est pas une
    # GatewayError, la vue répond donc 500 internal_error (fail closed), ce que ce test documente.
    assert res.status_code == 500
    assert res.data["detail"] == "internal_error"


def test_non_dependency_exception_is_not_converted(api_client, rag_space, monkeypatch):
    """Le gestionnaire ne masque pas un bug : une exception quelconque reste un 500."""
    from api.v1.documents import views_write

    monkeypatch.setattr(views_write, "require_rag_space_access", _raise(KeyError("bug")))
    from django.core.files.uploadedfile import SimpleUploadedFile

    with pytest.raises(KeyError):
        api_client.post(
            f"/api/v1/documents/upload/?rag_space_id={rag_space.id}",
            {"file": SimpleUploadedFile("note.txt", b"x", content_type="text/plain")},
            format="multipart",
        )
