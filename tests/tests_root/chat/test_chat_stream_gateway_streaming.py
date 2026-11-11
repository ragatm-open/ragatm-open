"""Chat SSE avec le streaming natif de l'AI Gateway (AI_GATEWAY_STREAMING_ENABLED).

Trois exigences d'exploitation sont verrouillées ici :

* **parité de génération** entre les deux régimes (pseudo-stream et flux natif) :
  mêmes modèle, options et ``think`` — sans quoi une bascule de drapeau
  changerait la réponse, et un ``num_ctx`` différent ferait recharger le runner ;
* **persistance non multipliée** : un fragment n'est pas une écriture ; une
  requête de chat reste un message assistant et une mise à jour de journal ;
* **réponse tronquée annoncée** : une rupture après le premier fragment conserve
  le texte déjà affiché et le marque ``partial``, au lieu de le perdre.

Mocks uniquement, aucun réseau.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.conf import settings

from apps.chat.services import chat_service as cs
from apps.llm_router.gateway import GatewayStreamError


def _patch_common(monkeypatch, *, calls: dict) -> None:
    monkeypatch.setattr(cs, "normalize_llm_overrides", lambda space: ("ollama", "llama3.1:8b"))
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(cs, "mode_b_enabled", lambda: False)
    monkeypatch.setattr(cs, "_conversation_context_payload", lambda **k: "")
    monkeypatch.setattr(
        cs, "_run_retrieval",
        lambda **k: {"status": "ok", "prompt": "PROMPT", "sources": [],
                     "log_id": "log-1", "answer_kind": "ok"},
    )
    calls.setdefault("log_updates", [])
    calls.setdefault("messages", [])
    monkeypatch.setattr(
        cs, "maybe_update_log",
        lambda log_id, **k: calls["log_updates"].append(k),
    )
    monkeypatch.setattr(
        cs, "append_message",
        lambda **k: calls["messages"].append(k),
    )
    monkeypatch.setattr(cs, "_gateway_enabled", lambda: True)


def _events(**kw):
    rag_space = SimpleNamespace(
        llm_provider="ollama", llm_model="llama3.1:8b", allow_external_fallback=False
    )
    return list(
        cs.chat_query_stream(
            rag_space_id="rs", question="explique Tessaly", rag_space=rag_space, **kw
        )
    )


def _stream(*deltas, boom: Exception | None = None, after: int | None = None):
    """Fabrique un ``generate_text_stream`` de test."""

    def fake(**kw):
        fake.captured = kw  # type: ignore[attr-defined]
        for i, d in enumerate(deltas):
            if boom is not None and after is not None and i >= after:
                raise boom
            yield {"type": "delta", "delta": d}
        if boom is not None:
            raise boom
        yield {"type": "result", "provider": "ai_gateway", "model": "qwen3:30b-instruct",
               "usage": {"total_tokens": 15}, "tokens": 15}

    fake.captured = {}  # type: ignore[attr-defined]
    return fake


@pytest.mark.django_db
def test_flux_natif_emet_les_fragments_puis_done(monkeypatch):
    calls: dict = {}
    _patch_common(monkeypatch, calls=calls)
    monkeypatch.setattr(cs, "_gateway_streaming_enabled", lambda: True)
    stream = _stream("Bonjour", " le", " monde")
    monkeypatch.setattr(cs, "generate_text_stream", stream)
    monkeypatch.setattr(
        cs, "generate_text",
        lambda **k: (_ for _ in ()).throw(AssertionError("le flux natif ne doit pas générer en bloc")),
    )

    events = _events()
    tokens = [e["data"]["delta"] for e in events if e["event"] == "token"]
    assert tokens == ["Bonjour", " le", " monde"]

    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["status"] == "ok"
    assert done["data"]["partial"] is False
    assert done["data"]["termination"] == "done"
    assert "Bonjour le monde" in done["data"]["answer"]
    assert done["data"]["model"] == "qwen3:30b-instruct"


@pytest.mark.django_db
def test_parite_des_reglages_de_generation_entre_les_deux_regimes(monkeypatch):
    """Bascule du drapeau => même modèle, mêmes options, même ``think``."""
    # Régime flux natif
    calls: dict = {}
    _patch_common(monkeypatch, calls=calls)
    monkeypatch.setattr(cs, "_gateway_streaming_enabled", lambda: True)
    stream = _stream("ok")
    monkeypatch.setattr(cs, "generate_text_stream", stream)
    _events()
    streamed = dict(stream.captured)

    # Régime pseudo-stream (drapeau à 0)
    captured_sync: dict = {}
    monkeypatch.setattr(cs, "_gateway_streaming_enabled", lambda: False)
    monkeypatch.setattr(
        cs, "generate_text",
        lambda **k: captured_sync.update(k) or {
            "text": "ok", "provider": "ai_gateway", "model": k["model"],
            "usage": None, "tokens": 1,
        },
    )
    _events()

    for champ in ("prompt", "provider", "model", "options", "think"):
        assert streamed[champ] == captured_sync[champ], champ
    # Les valeurs sont LUES aux settings : recopier un littéral ferait échouer ce
    # test à chaque réglage de budget, sans rien apprendre sur la parité — qui est
    # la propriété testée ici.
    assert streamed["options"] == {
        "temperature": settings.RAG_FACTUAL_TEMPERATURE,
        "num_ctx": settings.RAG_FACTUAL_NUM_CTX,
        "num_predict": settings.RAG_FACTUAL_NUM_PREDICT,
    }


@pytest.mark.django_db
def test_aucune_ecriture_par_fragment(monkeypatch):
    """20 fragments => toujours une seule mise à jour de journal et un message."""
    calls: dict = {}
    _patch_common(monkeypatch, calls=calls)
    monkeypatch.setattr(cs, "_gateway_streaming_enabled", lambda: True)
    monkeypatch.setattr(cs, "generate_text_stream", _stream(*[f"f{i} " for i in range(20)]))

    thread = SimpleNamespace(id="11111111-1111-1111-1111-111111111111")
    events = _events(thread_id=str(thread.id))
    assert len([e for e in events if e["event"] == "token"]) == 20
    # Une seule mise à jour finale du journal de requête (jamais une par token).
    assert len(calls["log_updates"]) == 1
    assert "answer" in calls["log_updates"][0]


@pytest.mark.django_db
def test_rupture_apres_un_fragment_marque_la_reponse_partielle(monkeypatch):
    calls: dict = {}
    _patch_common(monkeypatch, calls=calls)
    monkeypatch.setattr(cs, "_gateway_streaming_enabled", lambda: True)
    monkeypatch.setattr(
        cs, "generate_text_stream",
        _stream("Bonjour", " le", boom=GatewayStreamError("coupure"), after=2),
    )

    events = _events()
    kinds = [e["event"] for e in events]
    assert "error" not in kinds, "le texte déjà affiché ne doit pas être remplacé par une erreur"
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["partial"] is True
    assert done["data"]["termination"] == "stream_error"
    assert done["data"]["status"] == "partial"
    assert "Bonjour le" in done["data"]["answer"]
    # Le journal porte l'incident sans perdre le texte produit.
    assert calls["log_updates"][0]["status"] == "error"
    assert calls["log_updates"][0]["error_stage"] == "llm"
    assert "Bonjour le" in calls["log_updates"][0]["answer"]


@pytest.mark.django_db
def test_rupture_avant_tout_fragment_reste_une_erreur_llm(monkeypatch):
    """Aucun texte affiché : comportement historique inchangé."""
    calls: dict = {}
    _patch_common(monkeypatch, calls=calls)
    monkeypatch.setattr(cs, "_gateway_streaming_enabled", lambda: True)
    monkeypatch.setattr(
        cs, "generate_text_stream",
        _stream(boom=cs.LLMError("panne"), after=None),
    )

    events = _events()
    assert [e["event"] for e in events][-2:] == ["error", "done"]
    err = next(e for e in events if e["event"] == "error")
    assert err["data"]["detail"] == "llm_error"


@pytest.mark.django_db
def test_drapeau_a_zero_conserve_le_pseudo_stream(monkeypatch):
    """Repli d'exploitation : le chemin historique est restauré à l'identique."""
    calls: dict = {}
    _patch_common(monkeypatch, calls=calls)
    monkeypatch.setattr(cs, "_gateway_streaming_enabled", lambda: False)
    monkeypatch.setattr(
        cs, "generate_text_stream",
        lambda **k: (_ for _ in ()).throw(AssertionError("flux natif interdit quand le drapeau est à 0")),
    )
    monkeypatch.setattr(
        cs, "generate_text",
        lambda **k: {"text": "Tessaly est une coopérative.", "provider": "ai_gateway",
                     "model": "llama3.1:8b", "usage": None, "tokens": 4},
    )

    events = _events()
    tokens = [e["data"]["delta"] for e in events if e["event"] == "token"]
    assert tokens == ["Tessaly est une coopérative."]
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["partial"] is False
    assert done["data"]["termination"] == "done"
