"""Invariants de confidentialité du dispositif de feedback.

Ces tests ne décrivent pas des fonctionnalités : ils décrivent ce qui ne doit
JAMAIS arriver. Ils sont volontairement redondants avec les tests fonctionnels —
une garantie de confidentialité qui n'existe qu'en tant qu'effet de bord d'un
test fonctionnel disparaît le jour où ce test change.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.audit.models import ActionAuditEvent
from apps.feedback.diagnostics import buffer
from apps.feedback.export import package, redaction
from apps.feedback.models import AnswerFeedback
from apps.feedback.permissions import (
    can_approve_export,
    can_download_package,
    can_view_diagnostic_content,
    can_view_feedback_metadata,
)
from apps.feedback.services.create_feedback import create_feedback
from apps.feedback.services.export_service import approve_export, generate_package
from apps.feedback.services.transitions import transition
from apps.feedback.tasks import capture_feedback_snapshot_task
from apps.rag_core.services.query.orchestrator import query_rag

pytestmark = pytest.mark.django_db

TEST_KEY = "U4DzV1ONux1P7TEAq1XUUbqcAX_8P7ReJ-lDAZXGqaI="
SECRET_SENTENCE = "Nordexia Conseil accompagne des collectivités territoriales."


@pytest.fixture
def corpus(rag_space, ingest_text):
    ingest_text(rag_space, SECRET_SENTENCE, "Rapport_CONFIDENTIEL_2026.txt")
    return rag_space


# ---------------------------------------------------------------------------
# 1. Redis ne contient aucun texte
# ---------------------------------------------------------------------------
@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_no_chunk_text_ever_reaches_the_buffer(corpus, user):
    result = query_rag(
        rag_space_id=corpus.id,
        question="what is Nordexia Conseil?",
        user=user,
        rag_space=corpus,
        entrypoint="chat_sync",
    )
    raw = cache.get(buffer.buffer_key(result["log_id"]))
    assert raw

    for forbidden in (
        SECRET_SENTENCE,
        "Nordexia",
        "fondations",
        "Rapport_CONFIDENTIEL_2026.txt",
        ".txt",
        "/media",
        "/home",
    ):
        assert forbidden not in raw, f"le tampon ne doit JAMAIS contenir {forbidden!r}"

    # Ce qu'il contient : des identifiants et des nombres.
    parsed = json.loads(raw)
    for candidate in parsed["candidates"]:
        assert set(candidate) <= {
            "chunk_id",
            "document_id",
            "leg",
            "rank_fts",
            "rank_vector",
            "rank_fused",
            "score",
            "fusion_score",
            "rerank_score",
            "selected",
            "excluded_by",
            "structural_penalty_applied",
        }


# ---------------------------------------------------------------------------
# 2. Les logs applicatifs ne portent aucun contenu
# ---------------------------------------------------------------------------
@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_application_logs_carry_no_content(corpus, user, caplog):
    caplog.set_level(logging.DEBUG)

    result = query_rag(
        rag_space_id=corpus.id,
        question="what is Nordexia Conseil?",
        user=user,
        rag_space=corpus,
        entrypoint="chat_sync",
    )
    from apps.audit.models import RagQueryLog

    log = RagQueryLog.objects.get(id=result["log_id"])
    feedback = create_feedback(
        user=user,
        log=log,
        rating=AnswerFeedback.RATING_DOWN,
        kind=AnswerFeedback.KIND_INFO_IN_DOCS,
        comment="secret: mon mot de passe est hunter2hunter2",
        expected_term="Nordexia Conseil",
    )
    capture_feedback_snapshot_task(str(feedback.id))

    emitted = "\n".join(
        r.getMessage()
        for r in caplog.records
        if r.name.startswith(("apps.feedback", "apps.rag_core.services.query"))
    )
    for forbidden in (SECRET_SENTENCE, "hunter2hunter2", "Rapport_CONFIDENTIEL_2026.txt"):
        assert forbidden not in emitted, f"log applicatif pollué par {forbidden!r}"


def test_logged_events_use_the_declared_vocabulary(caplog, user, query_log):
    """Les événements structurés sont ceux annoncés, et rien de plus bavard."""
    caplog.set_level(logging.INFO)
    feedback = create_feedback(user=user, log=query_log, rating=AnswerFeedback.RATING_DOWN)
    capture_feedback_snapshot_task(str(feedback.id))

    messages = [r.getMessage() for r in caplog.records if r.name.startswith("apps.feedback")]
    joined = " ".join(messages)
    assert "feedback_created" in joined
    assert any(
        marker in joined
        for marker in ("feedback_snapshot_created", "feedback_snapshot_partial", "feedback_buffer_miss")
    )


# ---------------------------------------------------------------------------
# 3. Le paquet exporté
# ---------------------------------------------------------------------------
@pytest.fixture
def exported_package(db, user, query_log, tmp_path):
    from django.contrib.auth import get_user_model

    su = get_user_model().objects.create_user(
        username="root2", password="pw", email="r@e.com", is_superuser=True, is_staff=True
    )
    feedback = create_feedback(
        user=user,
        log=query_log,
        rating=AnswerFeedback.RATING_DOWN,
        kind=AnswerFeedback.KIND_INFO_IN_DOCS,
        comment="contactez-moi sur <auteur>@example.com, clé sk-abcdefghijklmnopqrstuvwx",
        expected_document="Rapport_CONFIDENTIEL_2026.txt",
    )
    capture_feedback_snapshot_task(str(feedback.id))
    transition(feedback, target=AnswerFeedback.STATUS_QUALIFIED, actor=su)
    with override_settings(
        FEEDBACK_INCIDENT_EXPORT_ENABLED=True,
        FEEDBACK_INCIDENT_EXPORT_KEY=TEST_KEY,
        FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT="staging",
    ):
        approve_export(feedback, level_value=1, user=su, justification="")
        result = generate_package(feedback, level_value=1, user=su)
    return result


def test_package_contains_no_user_comment_no_key_no_path(exported_package):
    files = package.read_package(exported_package["payload"])
    blob = b"".join(files.values()).decode("utf-8", errors="ignore")

    for forbidden in (
        "<auteur>@example.com",          # donnée personnelle
        "sk-abcdefghijklmnopqrstuvwx",  # secret
        "contactez-moi",                # commentaire libre
        "/home/",                       # chemin de stockage
        "Rapport_CONFIDENTIEL_2026",    # nom de fichier en clair
    ):
        assert forbidden not in blob, f"le paquet ne doit pas contenir {forbidden!r}"


def test_package_never_carries_absolute_paths(exported_package):
    files = package.read_package(exported_package["payload"])
    for name, blob in files.items():
        text = blob.decode("utf-8", errors="ignore")
        assert "<racine du dépôt>" not in text
        assert "/var/lib" not in text
        # Les chemins DANS l'archive sont relatifs.
        assert not name.startswith("/")


# ---------------------------------------------------------------------------
# 4. Audit sans contenu
# ---------------------------------------------------------------------------
def test_audit_events_never_carry_content(exported_package):
    for event in ActionAuditEvent.objects.all():
        blob = json.dumps(event.metadata, default=str, ensure_ascii=False)
        for forbidden in (
            "Nordexia Conseil",
            "<auteur>@example.com",
            "sk-abcdefghijklmnopqrstuvwx",
            "contactez-moi",
        ):
            assert forbidden not in blob


# ---------------------------------------------------------------------------
# 5. Rôles
# ---------------------------------------------------------------------------
def test_role_matrix(db, user, other_user, query_log, workspace):
    from django.contrib.auth import get_user_model
    from apps.workspaces.models import Membership

    su = get_user_model().objects.create_user(
        username="root3", password="pw", email="r3@e.com", is_superuser=True, is_staff=True
    )
    feedback = create_feedback(user=user, log=query_log, rating=AnswerFeedback.RATING_DOWN)

    # Propriétaire du signal : voit son propre feedback, rien du diagnostic.
    assert can_view_feedback_metadata(user, feedback) is True
    assert can_view_diagnostic_content(user) is False
    assert can_approve_export(user, level=1) is False
    assert can_download_package(user) is False

    # Utilisateur tiers sans lien : rien.
    assert can_view_feedback_metadata(other_user, feedback) is False

    # Admin de workspace : métadonnées de son périmètre, JAMAIS les extraits.
    Membership.objects.update_or_create(
        workspace=workspace, user=other_user, defaults={"role": Membership.ROLE_ADMIN}
    )
    assert can_view_feedback_metadata(other_user, feedback) is True
    assert can_view_diagnostic_content(other_user) is False
    assert can_approve_export(other_user, level=1) is False

    # Superuser : tout, y compris le niveau 3 et le téléchargement.
    assert can_view_diagnostic_content(su) is True
    assert can_approve_export(su, level=3) is True
    assert can_download_package(su) is True


def test_cross_workspace_access_is_denied(db, user, query_log):
    """Un admin d'un AUTRE workspace ne voit pas l'incident."""
    from django.contrib.auth import get_user_model
    from apps.workspaces.models import Membership, Workspace

    stranger = get_user_model().objects.create_user(
        username="etranger", password="pw", email="e@e.com"
    )
    other_ws = Workspace.objects.create(name="Autre")
    Membership.objects.create(workspace=other_ws, user=stranger, role=Membership.ROLE_OWNER)

    feedback = create_feedback(user=user, log=query_log, rating=AnswerFeedback.RATING_DOWN)
    assert can_view_feedback_metadata(stranger, feedback) is False


# ---------------------------------------------------------------------------
# 6. Aucun chemin d'export automatique dans tout le paquet applicatif
# ---------------------------------------------------------------------------
def test_no_module_triggers_an_export_outside_the_export_service():
    """Recherche STRUCTURELLE : seul le service d'export appelle la génération.

    Un test de comportement ne suffirait pas ici — il ne couvrirait que les
    chemins qu'on a pensé à écrire. Celui-ci échoue si un futur module appelle
    ``build_package`` ailleurs que dans le service d'export.
    """
    root = Path(__file__).resolve().parents[3] / "backend" / "apps" / "feedback"
    allowed = {"export/package.py", "services/export_service.py"}

    offenders = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        if rel in allowed or rel.startswith("management/commands/export_rag_incident"):
            continue
        text = path.read_text(encoding="utf-8")
        if "build_package(" in text or "generate_package(" in text:
            offenders.append(rel)

    assert offenders == [], (
        f"Ces modules déclenchent un export hors du service dédié : {offenders}"
    )


def test_capture_task_has_no_export_capability():
    """La tâche déclenchée par le CLIC ne doit connaître aucun code d'export."""
    root = Path(__file__).resolve().parents[3] / "backend" / "apps" / "feedback"
    text = (root / "tasks.py").read_text(encoding="utf-8")
    for forbidden in ("from apps.feedback.export", "build_package", "generate_package"):
        assert forbidden not in text


# ---------------------------------------------------------------------------
# 7. Pseudonymisation
# ---------------------------------------------------------------------------
def test_no_nominative_identifier_is_stored(db, user, query_log):
    user.username = "<auteur>.Exemple"
    user.email = "<auteur>@example.com"
    user.save(update_fields=["username", "email"])

    feedback = create_feedback(user=user, log=query_log, rating=AnswerFeedback.RATING_DOWN)
    blob = json.dumps(
        {
            "pseudonym": feedback.user_pseudonym,
            "workspace": feedback.workspace_id_hist,
            "fingerprint": feedback.dedup_fingerprint,
        }
    )
    assert "<auteur>" not in blob
    assert "@example.com" not in blob
    assert len(feedback.user_pseudonym) == 64


def test_pseudonym_is_stable_and_salted(db, user, query_log, settings):
    from apps.feedback.services.identity import pseudonymize_user

    settings.FEEDBACK_PSEUDONYM_SALT = "sel-a"
    first = pseudonymize_user(user.pk)
    assert first == pseudonymize_user(user.pk), "le pseudonyme doit être STABLE"

    settings.FEEDBACK_PSEUDONYM_SALT = "sel-b"
    assert pseudonymize_user(user.pk) != first, "changer de sel doit changer le pseudonyme"


def test_redaction_never_returns_the_secret_value():
    """La détection renvoie des CATÉGORIES, jamais la valeur trouvée."""
    found = redaction.detect_secrets({"k": "sk-abcdefghijklmnopqrstuvwx"})
    assert found == ["openai_key"]
    assert "abcdefghijklmnop" not in json.dumps(found)
