"""Tests unitaires du service de suggestions de reformulation (mocks uniquement).

Aucun appel réseau / Gateway : ``generate_text`` est systématiquement moqué.
Couvre : sortie valide, troncature à 3, dédup, égalité à la question, chaîne
vide, JSON invalide, sortie vide, langue FR/EN, provider inchangé, robustesse
(jamais d'exception), question vide, flag désactivé, aucun repli cloud implicite.
"""
from __future__ import annotations

import json

import pytest

from apps.chat.services import rephrasing_suggestions as rs


@pytest.fixture(autouse=True)
def _enabled(settings):
    # Ces tests exercent la LOGIQUE de génération : le flag (OFF par défaut) est
    # activé. Le test dédié au flag désactivé l'override explicitement.
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = True


def _mock_generate(monkeypatch, text, capture: dict | None = None, raises: Exception | None = None):
    def fake(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if raises is not None:
            raise raises
        return {"text": text}

    monkeypatch.setattr(rs, "generate_text", fake)


def _valid(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- sortie valide
def test_valid_json_returns_suggestions(monkeypatch):
    _mock_generate(
        monkeypatch,
        _valid({"suggestions": ["Quels risques de retard sont mentionnés ?",
                                "Le contrat précise-t-il des pénalités ?"]}),
    )
    out = rs.generate_rephrasing_suggestions(question="Montant de la pénalité ?", provider="ollama", model="m")
    assert out == [
        "Quels risques de retard sont mentionnés ?",
        "Le contrat précise-t-il des pénalités ?",
    ]


def test_too_long_is_truncated_to_three(monkeypatch):
    _mock_generate(
        monkeypatch,
        _valid({"suggestions": ["Q1 ?", "Q2 ?", "Q3 ?", "Q4 ?", "Q5 ?"]}),
    )
    out = rs.generate_rephrasing_suggestions(question="Question initiale ?", provider="ollama", model="m")
    assert out == ["Q1 ?", "Q2 ?", "Q3 ?"]
    assert len(out) <= 3


def test_exact_duplicates_removed(monkeypatch):
    _mock_generate(
        monkeypatch,
        _valid({"suggestions": ["Quelle est la date de livraison ?",
                                "quelle est la date de livraison ?",
                                "Quels sont les jalons ?"]}),
    )
    out = rs.generate_rephrasing_suggestions(question="La date ?", provider="ollama", model="m")
    assert out == ["Quelle est la date de livraison ?", "Quels sont les jalons ?"]


def test_suggestion_equal_to_question_removed(monkeypatch):
    _mock_generate(
        monkeypatch,
        _valid({"suggestions": ["Quel est le montant de la pénalité ?",
                                "Quels retards sont documentés ?"]}),
    )
    out = rs.generate_rephrasing_suggestions(
        question="Quel est le montant de la pénalité ?", provider="ollama", model="m"
    )
    assert out == ["Quels retards sont documentés ?"]


def test_empty_string_removed(monkeypatch):
    _mock_generate(
        monkeypatch,
        _valid({"suggestions": ["", "   ", "Quels risques sont identifiés ?"]}),
    )
    out = rs.generate_rephrasing_suggestions(question="La date ?", provider="ollama", model="m")
    assert out == ["Quels risques sont identifiés ?"]


def test_non_string_items_ignored(monkeypatch):
    _mock_generate(
        monkeypatch,
        _valid({"suggestions": [123, None, {"x": 1}, "Quels jalons sont prévus ?"]}),
    )
    out = rs.generate_rephrasing_suggestions(question="La date ?", provider="ollama", model="m")
    assert out == ["Quels jalons sont prévus ?"]


def test_json_wrapped_in_code_fence(monkeypatch):
    _mock_generate(
        monkeypatch,
        "```json\n{\"suggestions\": [\"Quelle échéance ?\"]}\n```",
    )
    out = rs.generate_rephrasing_suggestions(question="La date ?", provider="ollama", model="m")
    assert out == ["Quelle échéance ?"]


def test_json_with_surrounding_prose(monkeypatch):
    _mock_generate(
        monkeypatch,
        "Voici mes propositions : {\"suggestions\": [\"Quel jalon ?\"]} merci.",
    )
    out = rs.generate_rephrasing_suggestions(question="La date ?", provider="ollama", model="m")
    assert out == ["Quel jalon ?"]


# --------------------------------------------------- invalide / vide => fallback
def test_invalid_json_returns_deterministic_fallback(monkeypatch):
    _mock_generate(monkeypatch, "ceci n'est pas du json")
    out = rs.generate_rephrasing_suggestions(question="Quel montant du retard ?", provider="ollama", model="m")
    assert 1 <= len(out) <= 3
    assert all(isinstance(s, str) and s.strip() for s in out)


def test_empty_output_returns_deterministic_fallback(monkeypatch):
    _mock_generate(monkeypatch, "")
    out = rs.generate_rephrasing_suggestions(question="Quel montant du retard ?", provider="ollama", model="m")
    assert 1 <= len(out) <= 3


def test_missing_suggestions_key_returns_fallback(monkeypatch):
    _mock_generate(monkeypatch, _valid({"other": []}))
    out = rs.generate_rephrasing_suggestions(question="Quel montant ?", provider="ollama", model="m")
    assert 1 <= len(out) <= 3


def test_generation_error_never_raises_and_falls_back(monkeypatch):
    _mock_generate(monkeypatch, "", raises=RuntimeError("gateway down"))
    out = rs.generate_rephrasing_suggestions(question="Quel montant du retard ?", provider="ollama", model="m")
    assert 1 <= len(out) <= 3  # fallback déterministe, aucune exception


# ---------------------------------------------------------------- garde-fous
def test_blank_question_returns_empty(monkeypatch):
    _mock_generate(monkeypatch, _valid({"suggestions": ["X ?"]}))
    assert rs.generate_rephrasing_suggestions(question="   ", provider="ollama", model="m") == []


def test_disabled_flag_returns_empty(monkeypatch, settings):
    settings.RAG_REPHRASING_SUGGESTIONS_ENABLED = False
    _mock_generate(monkeypatch, _valid({"suggestions": ["X ?"]}))
    assert rs.generate_rephrasing_suggestions(question="Quel montant ?", provider="ollama", model="m") == []


# ---------------------------------------------------- provider inchangé / policy
def test_provider_and_model_unchanged_no_cloud_fallback(monkeypatch):
    cap: dict = {}
    _mock_generate(monkeypatch, _valid({"suggestions": ["Q ?"]}), capture=cap)
    rs.generate_rephrasing_suggestions(question="Quel montant ?", provider="ollama", model="qwen3:14b")
    assert cap["provider"] == "ollama"
    assert cap["model"] == "qwen3:14b"
    # Lot 1 : aucun repli externe possible pour une simple suggestion — refus
    # structurel porté par le contexte, valable aussi Gateway désactivée.
    assert cap["context"].external_egress_allowed is False
    assert cap["context"].policy.policy_source == "system-local-only"
    # Budget borné + température basse.
    assert cap["options"]["temperature"] <= 0.2
    assert cap["options"]["num_predict"] <= 256
    # Timeout dédié court + AUCUN retry réponse vide (traitement borné).
    assert cap["retry_on_empty"] is False
    assert 0 < cap["timeout_s"] <= 5


# ------------------------------------------------------------------- langue
def test_language_detection_fr():
    assert rs.detect_language("Quel est le montant de la pénalité ?") == "fr"


def test_language_detection_en():
    assert rs.detect_language("What is the penalty amount for the delay?") == "en"


def test_deterministic_fallback_language_fr(monkeypatch):
    _mock_generate(monkeypatch, "invalide")
    out = rs.generate_rephrasing_suggestions(question="Quel est le montant du retard ?", provider="ollama", model="m")
    joined = " ".join(out).lower()
    assert any(tok in joined for tok in ("quel", "porte", "décomposer", "précis"))


def test_deterministic_fallback_language_en(monkeypatch):
    _mock_generate(monkeypatch, "invalid")
    out = rs.generate_rephrasing_suggestions(question="What is the cost of the delay?", provider="ollama", model="m")
    joined = " ".join(out).lower()
    assert any(tok in joined for tok in ("which", "could", "break", "specific"))


def test_never_more_than_three(monkeypatch):
    _mock_generate(monkeypatch, _valid({"suggestions": [f"Q{i} ?" for i in range(10)]}))
    out = rs.generate_rephrasing_suggestions(question="Question ?", provider="ollama", model="m")
    assert len(out) <= 3
