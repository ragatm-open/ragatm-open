"""Garde-fous d'exploitation — les angles morts qui ne se voient qu'en production.

Ces trois sujets ont une propriété commune : ils ne cassent rien immédiatement.
Ils dégradent la capacité d'analyse *plus tard*, quand l'incident est déjà là et
qu'il est trop tard pour reconstituer l'information. D'où des avertissements au
démarrage plutôt qu'une découverte a posteriori.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.test import override_settings

from apps.feedback.checks import (
    ID_GIT_SHA_MISSING,
    ID_SALT_FALLBACK,
    check_feedback_provenance,
)
from apps.feedback.diagnostics import capture

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# 1. Provenance des incidents (APP_GIT_SHA)
# ---------------------------------------------------------------------------
@override_settings(DJANGO_ENV="dev", APP_GIT_SHA="", FEEDBACK_PSEUDONYM_SALT="")
def test_no_warning_in_development():
    """En dev le repli EST le comportement voulu : il n'a pas à faire de bruit."""
    assert check_feedback_provenance(None) == []


@override_settings(DJANGO_ENV="staging", APP_GIT_SHA="", FEEDBACK_PSEUDONYM_SALT="")
def test_deployed_environment_warns_on_both():
    ids = {issue.id for issue in check_feedback_provenance(None)}
    assert ids == {ID_GIT_SHA_MISSING, ID_SALT_FALLBACK}


@override_settings(DJANGO_ENV="prod", APP_GIT_SHA="deadbeef", FEEDBACK_PSEUDONYM_SALT="sel")
def test_no_warning_once_both_are_set():
    assert check_feedback_provenance(None) == []


@override_settings(DJANGO_ENV="prod", APP_GIT_SHA="", FEEDBACK_PSEUDONYM_SALT="sel")
def test_git_sha_alone_is_reported():
    issues = check_feedback_provenance(None)
    assert len(issues) == 1 and issues[0].id == ID_GIT_SHA_MISSING
    # Le message doit dire QUOI faire, pas seulement ce qui manque.
    assert "git rev-parse HEAD" in issues[0].hint


def test_deploy_compose_injects_the_revision():
    """Le compose déployé doit transmettre APP_GIT_SHA au conteneur.

    Sans cela, une image construite sans `.git` estampillerait tous les
    incidents d'une révision vide — et « corrigé depuis quelle version ? »
    resterait sans réponse.
    """
    root = Path(__file__).resolve().parents[3]
    for name in (
        "docker-compose.staging.yml",
        "docker-compose.prod.yml",
        "docker-compose.staging.le serveur applicatif.yml",
        "docker-compose.prod.le serveur applicatif.yml",
    ):
        path = root / "deploy" / "docker" / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "APP_GIT_SHA" in text, f"{name} ne transmet pas APP_GIT_SHA"


@override_settings(APP_GIT_SHA="")
def test_git_sha_fallback_never_raises(monkeypatch):
    """Sans dépôt git (image `git archive`), le repli échoue SILENCIEUSEMENT."""
    import subprocess

    from apps.feedback.services import context

    monkeypatch.setattr(context, "_CACHED", None)

    def _no_git(*a, **kw):
        raise FileNotFoundError("git introuvable")

    monkeypatch.setattr(subprocess, "run", _no_git)
    assert context.current_git_sha() == ""  # vide, mais aucune exception


# ---------------------------------------------------------------------------
# 2. Frontière d'observation transportée AVEC la donnée
# ---------------------------------------------------------------------------
def test_trace_declares_where_the_pool_was_observed():
    """« Absent de la trace » ne doit jamais pouvoir se lire « jamais candidat »."""
    trace = capture.build_candidate_trace(
        pool=[{"chunk_id": "a", "score": 0.5}],
        selected_pre_rerank=[],
        selected_post_rerank=[],
        selected_final=[],
        top_k=1,
        rerank_applied=False,
        diversity_applied=False,
    )
    payload = capture.build_trace_payload(
        log_id="l", created_at="", embedding_model="m",
        candidate_trace=trace,
        selection=capture.build_selection(
            selected_final=[], answerability_policy="no_answer",
            thresholds={}, budgets={},
        ),
    )
    assert payload["observation_boundary"] == "post_fusion"
    wording = payload["absent_candidate_means"]
    assert "post-fusion" in wording
    # La formulation interdite ne doit apparaître nulle part.
    assert "jamais candidat" not in wording.lower()
    # Et elle voyage avec la donnée, donc jusque dans le paquet d'incident.
    assert "post_fusion" in json.dumps(payload)


def test_replay_report_states_the_reading_rule(capsys):
    """La commande de replay doit expliciter la lecture d'une absence.

    On exerce le RENDU réel plutôt que le code source : c'est le message vu par
    la personne qui analyse l'incident qui doit être juste, pas le fichier.
    """
    from django.core.management.base import OutputWrapper

    from apps.feedback.management.commands.replay_rag_incident import Command

    command = Command()
    command.stdout = OutputWrapper(__import__("sys").stdout)
    command._render(
        {
            "incident_id": "abc",
            "mode": "retrieval",
            "deterministic": True,
            "source_capture_is_exact": True,
            "differences": {"missing_candidates": ["c1"], "selection_changes": {}},
        }
    )
    rendered = capsys.readouterr().out

    assert "POST-FUSION" in rendered
    assert "n'est pas observable à ce point de capture" in rendered
    # La conclusion que la trace ne soutient PAS ne doit jamais être suggérée.
    assert "jamais candidat" not in rendered.lower()


# ---------------------------------------------------------------------------
# 3. Pseudonymisation : le couplage au SECRET_KEY doit être visible
# ---------------------------------------------------------------------------
@override_settings(FEEDBACK_PSEUDONYM_SALT="", SECRET_KEY="cle-django-1")
def test_pseudonym_falls_back_to_secret_key(user):
    from apps.feedback.services.identity import pseudonymize_user

    first = pseudonymize_user(user.pk)
    assert len(first) == 64

    with override_settings(SECRET_KEY="cle-django-2"):
        # C'est précisément le risque signalé par feedback.W002 : une rotation
        # de SECRET_KEY casse le regroupement historique, sans erreur ni trace.
        assert pseudonymize_user(user.pk) != first


@override_settings(FEEDBACK_PSEUDONYM_SALT="sel-dedie", SECRET_KEY="cle-django-1")
def test_dedicated_salt_isolates_from_secret_key_rotation(user):
    from apps.feedback.services.identity import pseudonymize_user

    first = pseudonymize_user(user.pk)
    with override_settings(SECRET_KEY="cle-django-2"):
        assert pseudonymize_user(user.pk) == first, (
            "un sel dédié doit rendre les pseudonymes insensibles à la rotation "
            "de la clé Django"
        )
