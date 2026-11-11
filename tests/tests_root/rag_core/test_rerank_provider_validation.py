"""Dette D1 — validation du provider de reranking et observabilité du repli.

Le rerank ne reconnaît que ``stub`` et ``cross_encoder``. Avant ce durcissement,
toute autre valeur (``local``, ``crossencoder``…) faisait tourner le stub avec
``fallback_used=False`` : la dégradation était invisible, et le log
(``provider=stub fallback=False``) indistinguable d'un stub légitime.

Ces tests verrouillent les deux volets :
  - la SÉLECTION (quelle implémentation tourne réellement) ;
  - l'OBSERVABILITÉ (la dégradation est-elle signalée et traçable).

Aucun modèle externe n'est chargé : le cross-encoder est doublé.
"""
import logging
import sys
import types

import pytest
from django.core.checks import Error
from django.test import override_settings

from apps.rag_core.checks_rerank import check_rerank_provider
from apps.rag_core.services import rerank as rerank_mod
from apps.rag_core.services.rerank import rerank_chunks_observed


def _chunks():
    return [
        {"chunk_id": "a", "text": "paris est la capitale"},
        {"chunk_id": "b", "text": "capitale de la france paris"},
    ]


@pytest.fixture
def fake_cross_encoder(monkeypatch):
    """Double du cross-encoder : disponible et fonctionnel, sans modèle réel.

    Le rendre DISPONIBLE est essentiel : si le stub tourne quand même, c'est bien
    la sélection qui est en cause, pas une dépendance manquante.
    """
    rerank_mod._CROSS_ENCODER_MODEL = None
    rerank_mod._CROSS_ENCODER_SIGNATURE = None
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(
            CrossEncoder=lambda model_name, device=None: types.SimpleNamespace(
                predict=lambda pairs, batch_size=16: [0.1, 0.9]
            )
        ),
    )


# --------------------------------------------------------------------------- #
# Sélection — les deux valeurs légitimes
# --------------------------------------------------------------------------- #
@override_settings(
    RAG_RERANK_PROVIDER="cross_encoder",
    RAG_RERANK_MODEL_NAME="fake/model",
    RAG_RERANK_DEVICE="cpu",
)
def test_cross_encoder_valide_utilise_le_vrai_reranker(fake_cross_encoder):
    _reranked, info = rerank_chunks_observed(question="q", chunks=_chunks())

    assert info.requested_provider == "cross_encoder"
    assert info.effective_provider == "cross_encoder"
    assert info.fallback_used is False
    assert info.error == ""


@override_settings(RAG_RERANK_PROVIDER="stub")
def test_stub_explicite_nest_pas_un_fallback():
    """Le stub demandé explicitement est un choix, pas une dégradation."""
    _reranked, info = rerank_chunks_observed(question="q", chunks=_chunks())

    assert info.requested_provider == "stub"
    assert info.effective_provider == "stub"
    assert info.fallback_used is False
    assert info.error == ""


# --------------------------------------------------------------------------- #
# Sélection — valeurs invalides (le cœur de D1)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("valeur", ["local", "crossencoder"])
@override_settings(RAG_RERANK_MODEL_NAME="fake/model", RAG_RERANK_DEVICE="cuda")
def test_provider_inconnu_retombe_sur_stub_en_le_signalant(
    valeur, fake_cross_encoder, settings, caplog
):
    """Cross-encoder DISPONIBLE : seule la valeur invalide explique le repli."""
    settings.RAG_RERANK_PROVIDER = valeur

    with caplog.at_level(logging.WARNING, logger=rerank_mod.logger.name):
        _reranked, info = rerank_chunks_observed(question="q", chunks=_chunks())

    assert info.requested_provider == valeur
    assert info.effective_provider == "stub"
    # Le point central : la dégradation est SIGNALÉE.
    assert info.fallback_used is True
    assert valeur in info.error
    assert "inconnu" in info.error

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(valeur in m for m in warnings), "aucun warning ne cite le provider demandé"


# --------------------------------------------------------------------------- #
# Normalisation — casse et espaces restent tolérés
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("valeur", ["Cross_Encoder", "  cross_encoder  ", "CROSS_ENCODER"])
@override_settings(RAG_RERANK_MODEL_NAME="fake/model", RAG_RERANK_DEVICE="cpu")
def test_casse_et_espaces_restent_acceptes(valeur, fake_cross_encoder, settings):
    """Le durcissement ne doit pas rendre la configuration plus fragile."""
    settings.RAG_RERANK_PROVIDER = valeur

    _reranked, info = rerank_chunks_observed(question="q", chunks=_chunks())

    assert info.effective_provider == "cross_encoder"
    assert info.fallback_used is False


# --------------------------------------------------------------------------- #
# Observabilité — le log distingue demandé / effectif
# --------------------------------------------------------------------------- #
@override_settings(RAG_RERANK_PROVIDER="local")
def test_le_log_trace_le_provider_demande_et_effectif(caplog):
    """Sans `requested_provider`, "provider=stub" est indistinguable d'un stub voulu."""
    with caplog.at_level(logging.INFO, logger=rerank_mod.logger.name):
        rerank_chunks_observed(question="q", chunks=_chunks())

    ligne = [m for m in (r.getMessage() for r in caplog.records) if "rerank done" in m]
    assert ligne, "ligne de synthèse du rerank absente"
    assert "requested_provider=local" in ligne[0]
    assert "effective_provider=stub" in ligne[0]


# --------------------------------------------------------------------------- #
# System check — la garde de démarrage
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("valeur", ["local", "crossencoder", "ollama"])
def test_system_check_rouge_si_provider_inconnu_et_rerank_actif(valeur, settings):
    settings.RAG_RERANK_ENABLED = True
    settings.RAG_RERANK_PROVIDER = valeur

    errors = check_rerank_provider(None)

    assert len(errors) == 1
    assert isinstance(errors[0], Error)
    assert errors[0].id == "rag_core.E004"


@pytest.mark.parametrize("valeur", ["stub", "cross_encoder", "Cross_Encoder ", ""])
def test_system_check_vert_pour_les_valeurs_autorisees(valeur, settings):
    """`""` normalise vers `stub` (comme une variable absente) : pas une erreur."""
    settings.RAG_RERANK_ENABLED = True
    settings.RAG_RERANK_PROVIDER = valeur

    assert check_rerank_provider(None) == []


def test_system_check_silencieux_si_reranking_desactive(settings):
    """Reranking éteint : la valeur est inerte, elle ne doit pas bloquer le démarrage."""
    settings.RAG_RERANK_ENABLED = False
    settings.RAG_RERANK_PROVIDER = "local"

    assert check_rerank_provider(None) == []
