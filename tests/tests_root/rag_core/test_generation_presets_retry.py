"""Tests du lot robustesse génération : presets par tâche, gestion qwen3, retry
réponse vide. Mocks uniquement — aucun appel à le serveur IA.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from apps.llm_router import model_utils as mu
from apps.llm_router import router
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.router import LLMError
from apps.llm_router.gateway import GatewayTimeoutError


# =========================================================== model_utils (purs)

def test_is_qwen3_family():
    assert mu.is_qwen3("qwen3:14b")
    assert mu.is_qwen3("QWEN3:30B-INSTRUCT")
    assert mu.is_qwen3("registry.local/qwen3:14b")
    assert not mu.is_qwen3("qwen2.5:14b")
    assert not mu.is_qwen3("llama3.1:8b")
    assert not mu.is_qwen3("")


def test_is_qwen3_14b_only_14b():
    assert mu.is_qwen3_14b("qwen3:14b")
    assert mu.is_qwen3_14b("qwen3:14b-instruct")
    assert mu.is_qwen3_14b("org/qwen3:14b-q4_0")
    assert not mu.is_qwen3_14b("qwen3:30b-instruct")
    assert not mu.is_qwen3_14b("qwen3:8b")
    assert not mu.is_qwen3_14b("qwen3:140b")  # ne confond pas 140b avec 14b
    assert not mu.is_qwen3_14b("llama3.1:8b")


def test_gen_options_omits_none():
    assert mu.gen_options(temperature=0.1, num_ctx=8192, num_predict=768) == {
        "temperature": 0.1, "num_ctx": 8192, "num_predict": 768,
    }
    assert mu.gen_options(temperature=0.0) == {"temperature": 0.0}
    assert mu.gen_options() == {}


def test_resolve_options_explicit_wins():
    preset = {"temperature": 0.1, "num_predict": 768}
    merged = mu.resolve_options(preset, {"num_predict": 2048})
    assert merged == {"temperature": 0.1, "num_predict": 2048}
    # None dans explicit n'écrase pas
    assert mu.resolve_options(preset, {"num_predict": None}) == preset


def test_think_for_focused_task():
    # qwen3:14b + tâche ciblée => False
    assert mu.think_for_focused_task("qwen3:14b", task="factual") is False
    assert mu.think_for_focused_task("qwen3:14b", task="summary") is False
    assert mu.think_for_focused_task("qwen3:14b", task="extraction") is False
    # qwen3:14b hors tâche ciblée => None (pas de forçage)
    assert mu.think_for_focused_task("qwen3:14b", task="report") is None
    # autres modèles => None
    assert mu.think_for_focused_task("qwen3:30b-instruct", task="factual") is None
    assert mu.think_for_focused_task("llama3.1:8b", task="factual") is None


# =========================================================== retry réponse vide

@pytest.fixture
def gateway_enabled(monkeypatch):
    monkeypatch.setattr(router, "_gateway_enabled", lambda: True)


def _seq_gateway(responses, calls):
    """Fabrique un _generate_via_gateway qui renvoie/raise selon une séquence."""
    def _fn(*, prompt, provider, model, options=None, think=None, request_id=None):
        # `request_id` (qualification) : corrélation relayée en en-tête par le client ; sans
        # incidence sur les options de génération vérifiées ici.
        calls.append({"prompt": prompt, "provider": provider, "model": model,
                      "options": options, "think": think, "request_id": request_id})
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    return _fn


def test_empty_then_valid_retries_once(gateway_enabled, monkeypatch):
    calls: list = []
    responses = [
        {"text": "   ", "model": "qwen3:14b", "tokens": 0},          # 1er vide
        {"text": "réponse", "model": "qwen3:14b", "tokens": 5},      # retry OK
    ]
    monkeypatch.setattr(router, "_generate_via_gateway", _seq_gateway(responses, calls))
    out = router.generate_text(prompt="P", provider="ollama", model="qwen3:14b",
                               options={"num_predict": 768})
    assert out["text"] == "réponse"
    assert len(calls) == 2                       # un seul retry
    assert calls[1]["think"] is False            # retry force think=false
    assert calls[1]["options"]["num_predict"] == 1024   # PLANCHER : max(768, 1024)
    assert calls[1]["prompt"] == "P" and calls[1]["model"] == "qwen3:14b"  # inchangés


def test_two_empties_raise_typed_error(gateway_enabled, monkeypatch):
    calls: list = []
    responses = [{"text": ""}, {"text": "  "}]
    monkeypatch.setattr(router, "_generate_via_gateway", _seq_gateway(responses, calls))
    with pytest.raises(LLMError):
        router.generate_text(prompt="P", provider="ollama", model="m")
    assert len(calls) == 2                        # exactement un retry, pas plus


@pytest.mark.parametrize("initial, expected", [(3072, 3072), (4096, 4096), (2048, 2048)])
def test_le_budget_explicite_de_l_appelant_n_est_jamais_abaisse(gateway_enabled, monkeypatch, initial, expected):
    """Le plafond 2048 a été retiré : il abaissait un budget que l'appelant avait choisi.

    Valeurs RÉALISTES : elles sont toutes acceptables par le profil Gateway `planner-bounded`
    (maximum 4096). C'est ce qui rend le scénario possible — le retry sur réponse vide ne suit
    qu'un HTTP 200, donc un budget que la Gateway aurait refusé n'atteindrait jamais ce chemin.
    """
    calls: list = []
    responses = [{"text": ""}, {"text": "ok"}]
    monkeypatch.setattr(router, "_generate_via_gateway", _seq_gateway(responses, calls))
    router.generate_text(prompt="P", model="m", options={"num_predict": initial})
    assert calls[0]["options"]["num_predict"] == initial
    assert calls[1]["options"]["num_predict"] == expected


@pytest.mark.parametrize("initial, expected", [(None, 1024), (70, 1024), (128, 1024), (512, 1024), (1024, 1024)])
def test_le_plancher_1024_releve_toujours_les_petits_budgets(gateway_enabled, monkeypatch, initial, expected):
    """Partie HISTORIQUEMENT justifiée du mécanisme, inchangée : une réponse vide vient souvent
    d'un budget trop court pour produire quoi que ce soit après le raisonnement interne. 70 et 128
    sont les budgets réels de `build_admission_dataset` et de la réécriture de requête."""
    calls: list = []
    responses = [{"text": ""}, {"text": "ok"}]
    monkeypatch.setattr(router, "_generate_via_gateway", _seq_gateway(responses, calls))
    opts = None if initial is None else {"num_predict": initial}
    router.generate_text(prompt="P", model="m", options=opts)
    assert calls[1]["options"]["num_predict"] == expected


def test_la_fonction_pure_du_budget_de_retry(gateway_enabled):
    """Test INTERNE de la fonction, hors simulation du chemin Gateway : il couvre aussi des
    valeurs que la Gateway refuserait en amont, pour que la règle elle-même soit sans trou."""
    f = router._empty_retry_num_predict
    assert [f(None), f({}), f({"num_predict": 0})] == [1024, 1024, 1024]
    assert [f({"num_predict": n}) for n in (70, 128, 512, 1024)] == [1024, 1024, 1024, 1024]
    assert [f({"num_predict": n}) for n in (2048, 3072, 4096)] == [2048, 3072, 4096]
    assert f({"num_predict": 5000}) == 5000, "plus aucun plafond côté routeur"
    assert f({"num_predict": True}) == 1024, "un booléen n'est pas un budget"
    assert f({"num_predict": "3072"}) == 1024, "une chaîne n'est pas un budget"


def test_no_retry_when_first_response_valid(gateway_enabled, monkeypatch):
    calls: list = []
    responses = [{"text": "direct", "model": "m", "tokens": 1}]
    monkeypatch.setattr(router, "_generate_via_gateway", _seq_gateway(responses, calls))
    out = router.generate_text(prompt="P", model="m")
    assert out["text"] == "direct" and len(calls) == 1


def test_no_retry_on_gateway_error_raises_controlled_error(gateway_enabled, monkeypatch):
    """Édition Open : repli activé mais aucun provider de repli n'existe (`none`) —
    l'erreur Gateway remonte typée, sans second essai « réponse vide »."""
    from apps.llm_router.gateway import GatewayUnavailableError

    calls: list = []
    responses = [GatewayTimeoutError("timeout")]
    monkeypatch.setattr(router, "_generate_via_gateway", _seq_gateway(responses, calls))
    monkeypatch.setattr(router, "_gateway_fallback_enabled", lambda: True)
    monkeypatch.setattr(router, "_gateway_fallback_provider", lambda: "none")
    monkeypatch.setattr(
        router, "_generate_text_direct",
        lambda **k: (_ for _ in ()).throw(AssertionError("aucun chemin direct ne doit servir")),
    )
    with pytest.raises(GatewayUnavailableError):
        router.generate_text(prompt="P", provider="ollama", model="m")
    assert len(calls) == 1        # aucun retry "réponse vide" sur une erreur Gateway


def test_empty_retry_logs_no_sensitive_content(gateway_enabled, monkeypatch, caplog):
    calls: list = []
    secret = "PROMPT-SECRET-1234 documents confidentiels"
    responses = [{"text": ""}, {"text": ""}]
    monkeypatch.setattr(router, "_generate_via_gateway", _seq_gateway(responses, calls))
    with caplog.at_level(logging.WARNING, logger="apps.llm_router.router"):
        with pytest.raises(LLMError):
            router.generate_text(prompt=secret, model="qwen3:14b", options={"num_predict": 768})
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "gateway_empty_response" in joined
    assert "PROMPT-SECRET" not in joined and "confidentiels" not in joined


# =========================================================== câblage presets

def test_rewrite_preset_and_think(monkeypatch, settings):
    from apps.rag_core.services.query import rewrite

    settings.RAG_REWRITE_TEMPERATURE = 0.0
    settings.RAG_REWRITE_NUM_CTX = 2048
    settings.RAG_REWRITE_NUM_PREDICT = 128
    monkeypatch.setattr(rewrite, "REWRITE_MODEL", "qwen3:14b")

    captured = {}

    def fake_generate_text(**kw):
        captured.update(kw)
        return {"text": "requete courte"}

    monkeypatch.setattr(rewrite, "generate_text", fake_generate_text)
    rewrite.rewrite_query_with_llm("Explique-moi le contrat Alpha s'il te plaît")
    assert captured["options"] == {"temperature": 0.0, "num_ctx": 2048, "num_predict": 128}
    assert captured["think"] is False           # qwen3:14b + extraction


def test_rewrite_think_none_for_non_qwen14b(monkeypatch, settings):
    from apps.rag_core.services.query import rewrite
    monkeypatch.setattr(rewrite, "REWRITE_MODEL", "llama3.1:8b")
    captured = {}
    monkeypatch.setattr(rewrite, "generate_text", lambda **kw: captured.update(kw) or {"text": "x"})
    rewrite.rewrite_query_with_llm("explique X")
    assert captured["think"] is None


def test_summary_report_preset_and_think(monkeypatch, settings):
    from apps.rag_core.services import summary_service as ss

    settings.RAG_REPORT_TEMPERATURE = 0.1
    settings.RAG_REPORT_NUM_CTX = 8192
    settings.RAG_REPORT_NUM_PREDICT = 2048

    monkeypatch.setattr(ss, "normalize_llm_overrides", lambda space: ("ollama", "qwen3:14b"))
    monkeypatch.setattr(ss, "_build_prompt", lambda **k: "PROMPT")

    captured = {}

    def fake_generate_text(*, prompt, provider, model, options=None, think=None, **kwargs):
        captured.update(options=options, think=think, model=model, **kwargs)
        return {"text": "résumé", "provider": "ai_gateway", "model": model, "usage": None, "tokens": 3}

    monkeypatch.setattr(ss, "generate_text", fake_generate_text)

    doc = SimpleNamespace(rag_space=SimpleNamespace(llm_provider="ollama", llm_model="qwen3:14b"))
    chunks = [SimpleNamespace(text="contenu du document", page_number=None)]
    ss._perform_generation(doc, mode="bullets", scope="doc", page=None, language="fr", chunks=chunks)
    assert captured["options"] == {"temperature": 0.1, "num_ctx": 8192, "num_predict": 2048}
    assert captured["think"] is False           # qwen3:14b + summary


def test_chat_factual_preset_and_think(monkeypatch, settings):
    from apps.chat.services import chat_service as cs

    settings.RAG_FACTUAL_TEMPERATURE = 0.1
    settings.RAG_FACTUAL_NUM_CTX = 8192
    settings.RAG_FACTUAL_NUM_PREDICT = 768

    monkeypatch.setattr(cs, "normalize_llm_overrides", lambda space: ("ollama", "qwen3:30b-instruct"))
    monkeypatch.setattr(cs, "llm_route_news_or_none", lambda **k: None)
    monkeypatch.setattr(cs, "mode_b_enabled", lambda: False)
    monkeypatch.setattr(cs, "_conversation_context_payload", lambda **k: "")
    monkeypatch.setattr(cs, "maybe_update_log", lambda *a, **k: None)
    monkeypatch.setattr(
        cs, "_run_retrieval",
        lambda **k: {"status": "ok", "prompt": "PROMPT", "sources": [], "log_id": None, "answer_kind": "ok"},
    )

    captured = {}

    def fake_generate_text(**kw):
        captured.update(kw)
        return {"text": "réponse", "provider": "ai_gateway", "model": kw.get("model"), "usage": None, "tokens": 2}

    monkeypatch.setattr(cs, "generate_text", fake_generate_text)

    rag_space = SimpleNamespace(llm_provider="ollama", llm_model="qwen3:30b-instruct", allow_external_fallback=False)
    out = cs.chat_query(rag_space_id="rs", question="Quelle est la durée ?", rag_space=rag_space)
    assert out["status"] == "ok"
    assert captured["options"] == {"temperature": 0.1, "num_ctx": 8192, "num_predict": 768}
    assert captured["think"] is None            # qwen3:30b n'est PAS forcé à false


def test_generate_text_without_options_parity(gateway_enabled, monkeypatch):
    """Non-régression : sans options, aucune option de preset injectée par le routeur."""
    calls: list = []
    monkeypatch.setattr(router, "_generate_via_gateway",
                        _seq_gateway([{"text": "ok", "model": "m"}], calls))
    router.generate_text(prompt="P", model="m")
    assert calls[0]["options"] is None and calls[0]["think"] is None
