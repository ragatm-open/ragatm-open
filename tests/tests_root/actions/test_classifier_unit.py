"""Classificateur LLM : schéma strict, bornes, timeout, local-only (§16, §9)."""
from __future__ import annotations

import pytest

from apps.actions import classifier
from apps.actions.classifier import classify_action
from apps.llm_router.router import LLMError


@pytest.fixture(autouse=True)
def _enabled(settings):
    settings.ACTIONS_LLM_CLASSIFIER_ENABLED = True
    settings.ACTION_AUDIT_ENABLED = False  # pas de DB pour ces tests unitaires
    return settings


def _mock(monkeypatch, text):
    monkeypatch.setattr(classifier, "generate_text", lambda **k: {"text": text})


def test_valid_json(monkeypatch):
    _mock(monkeypatch, '{"intent":"search.list_documents","score":0.9,"parameters":{},"missing_parameters":[],"alternatives":[]}')
    out = classify_action("Peux-tu lister les dossiers ?")
    assert out["intent"] == "search.list_documents" and out["score"] == 0.9


def test_fenced_json_accepted(monkeypatch):
    # Les modèles (ex. mistral) encapsulent en ```json … ``` : on retire la clôture
    # (objet unique non ambigu), sans extraire un {...} d'un texte libre.
    _mock(monkeypatch, '```json\n{\n  "intent": "document.summarize",\n  "score": 0.9\n}\n```')
    out = classify_action("Fais un topo du dossier")
    assert out["intent"] == "document.summarize"


def test_two_objects_in_fence_rejected(monkeypatch):
    _mock(monkeypatch, '```json\n{"intent":"none","score":0.1}{"intent":"none","score":0.2}\n```')
    assert classify_action("Fais un topo") is None


def test_disabled_returns_none(settings, monkeypatch):
    settings.ACTIONS_LLM_CLASSIFIER_ENABLED = False
    called = {"n": 0}
    monkeypatch.setattr(classifier, "generate_text", lambda **k: called.__setitem__("n", 1))
    assert classify_action("x") is None
    assert called["n"] == 0  # aucun appel LLM si désactivé


@pytest.mark.parametrize(
    "text",
    [
        "pas du json",
        "",
        "Voici: {\"intent\":\"none\",\"score\":0.1}",   # texte autour -> refusé (pas d'extraction)
        '{"intent":"none","score":0.1} {"intent":"none","score":0.2}',  # deux objets
        '{"intent":"none","score":0.1,"evil":1}',        # champ inconnu
        '{"intent":"document.delete","score":0.9}',      # intention hors allowlist
        '{"intent":"search.answer","score":1.5}',        # score hors bornes
        '{"intent":"search.answer"}',                    # score manquant
        '{"score":0.5}',                                 # intent manquant
        '[]',                                            # pas un objet
    ],
)
def test_invalid_outputs_rejected(monkeypatch, text):
    _mock(monkeypatch, text)
    assert classify_action("Fais un topo du dossier") is None


def test_provider_error_returns_none_no_external(monkeypatch):
    captured = {}

    def boom(**kw):
        captured.update(kw)
        raise LLMError("local provider down")

    monkeypatch.setattr(classifier, "generate_text", boom)
    assert classify_action("Fais un topo du dossier") is None
    # un seul appel, LOCAL only (jamais de repli externe)
    assert captured["context"].external_egress_allowed is False
    assert captured["retry_on_empty"] is False


def test_exception_returns_none(monkeypatch):
    monkeypatch.setattr(classifier, "generate_text", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert classify_action("Fais un topo") is None


def test_local_only_params_forwarded(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        classifier, "generate_text",
        lambda **kw: captured.update(kw) or {"text": '{"intent":"none","score":0.1}'},
    )
    classify_action("Fais un condensé du dossier")
    assert captured["context"].external_egress_allowed is False
    assert captured["retry_on_empty"] is False
    assert captured["timeout_s"] == pytest.approx(5.0)
    assert captured["think"] is False
    # LOCAL STRICT : l'interdiction est portée par un contexte système dédié, dont
    # le refus est STRUCTUREL — il ne dépend ni du provider épinglé, ni de l'état
    # de la Gateway (l'ancien montage se contournait avec AI_GATEWAY_ENABLED=0).
    assert captured["context"].policy.policy_source == "system-local-only"


def test_model_allowlist_blocks_unlisted(settings, monkeypatch):
    settings.ACTIONS_LLM_CLASSIFIER_MODEL = "rogue-model"
    settings.ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS = ["small-classifier"]
    called = {"n": 0}
    monkeypatch.setattr(classifier, "generate_text", lambda **k: called.__setitem__("n", 1))
    assert classify_action("Fais un topo") is None
    assert called["n"] == 0  # modèle hors allowlist -> aucun appel
