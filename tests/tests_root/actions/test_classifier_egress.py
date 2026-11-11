"""Verrouillage de l'absence d'egress externe (§7) + fail-closed allowlist (§8)."""
from __future__ import annotations

import pytest

from apps.llm_router.egress import ExternalEgressDenied

from apps.actions import classifier as clf
from apps.actions.classifier import CLASSIFIER_POLICY, classify_action, get_classifier


@pytest.fixture(autouse=True)
def _base(settings):
    settings.ACTIONS_LLM_CLASSIFIER_ENABLED = True
    settings.ACTION_AUDIT_ENABLED = False
    settings.ACTIONS_LLM_CLASSIFIER_PROVIDER = "local"
    settings.ACTIONS_LLM_CLASSIFIER_MODEL = ""
    settings.ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS = []
    return settings


def test_policy_constant_is_local_only():
    assert CLASSIFIER_POLICY == "local_only"


def test_classifier_pins_local_space_provider(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        clf, "generate_text",
        lambda **kw: captured.update(kw) or {"text": '{"intent":"none","score":0.1}'},
    )
    classify_action("Fais un topo du dossier")
    # Lot 1 : l'invariant d'egress n'est plus obtenu en « épinglant » un
    # space_provider local (montage qui se contournait dès que la Gateway était
    # désactivée) mais par un contexte système STRICTEMENT local, dont le refus
    # est structurel et indépendant du transport.
    ctx = captured["context"]
    assert ctx.external_egress_allowed is False
    assert ctx.policy.policy_source == "system-local-only"
    with pytest.raises(ExternalEgressDenied):
        ctx.ensure_external_allowed("openai")
    assert captured["retry_on_empty"] is False


def test_gateway_fallback_blocks_openai_with_classifier_kwargs(monkeypatch):
    """Avec les kwargs du classifieur, un échec Gateway NE DOIT PAS appeler OpenAI."""
    from apps.llm_router import router as R

    monkeypatch.setattr(R, "_gateway_fallback_enabled", lambda: True)
    monkeypatch.setattr(R, "_gateway_fallback_provider", lambda: "openai")
    called = {"openai": 0}
    monkeypatch.setattr(R, "_generate_text_direct", lambda **k: called.__setitem__("openai", 1))

    with pytest.raises(Exception):
        R._generate_gateway_fallback(
            prompt="x", provider=None, model="qwen3:14b", timeout_s=5,
            gateway_error=RuntimeError("gateway timeout"),
            space_provider="ollama",           # <- épinglé par le classifieur
            allow_external_fallback=False,
        )
    assert called["openai"] == 0  # AUCUN appel OpenAI (egress bloqué)


@pytest.mark.parametrize("exc", [Exception("500"), Exception("timeout"), Exception("connection refused")])
def test_classify_returns_none_on_any_provider_failure(monkeypatch, exc):
    monkeypatch.setattr(clf, "generate_text", lambda **k: (_ for _ in ()).throw(exc))
    assert classify_action("Fais un topo du dossier") is None  # -> clarification en amont


def test_empty_response_returns_none(monkeypatch):
    monkeypatch.setattr(clf, "generate_text", lambda **k: {"text": ""})
    assert classify_action("Fais un condensé") is None


# ---- fail-closed allowlist / local-only (§8) ------------------------------
def test_non_local_provider_disables(settings):
    settings.ACTIONS_LLM_CLASSIFIER_PROVIDER = "openai"
    assert get_classifier() is None
    assert classify_action("Fais un topo") is None


def test_pinned_model_without_allowlist_disables(settings):
    settings.ACTIONS_LLM_CLASSIFIER_MODEL = "qwen3:14b"
    settings.ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS = []
    assert get_classifier() is None  # modèle pinné doit être allowlisté


def test_model_not_in_allowlist_disables(settings):
    settings.ACTIONS_LLM_CLASSIFIER_MODEL = "rogue"
    settings.ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS = ["qwen3:14b"]
    assert get_classifier() is None


def test_allowlisted_model_enabled(settings):
    settings.ACTIONS_LLM_CLASSIFIER_MODEL = "qwen3:14b"
    settings.ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS = ["qwen3:14b"]
    assert get_classifier() is not None
