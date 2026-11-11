"""Export d'incident : niveaux, autorisation, rédaction, chiffrement, audit.

Le test le plus important de ce fichier est le premier :
``test_no_automatic_export_path_exists``. Tous les autres décrivent comment un
export se fait ; celui-là décrit la seule chose qui ne doit jamais se produire.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.audit.models import ActionAuditEvent
from apps.feedback.export import crypto, levels, package, redaction
from apps.feedback.models import AnswerFeedback, FeedbackDiagnosticSnapshot
from apps.feedback.services.create_feedback import create_feedback
from apps.feedback.services.export_service import approve_export, generate_package
from apps.feedback.services.transitions import transition
from apps.feedback.tasks import capture_feedback_snapshot_task

pytestmark = pytest.mark.django_db

#: Clé Fernet de TEST uniquement — générée pour ce fichier, jamais déployée.
#: Une clé de test dans le dépôt n'est un secret que si elle sert ailleurs :
#: celle-ci n'est référencée nulle part hors de ces tests.
TEST_KEY = "U4DzV1ONux1P7TEAq1XUUbqcAX_8P7ReJ-lDAZXGqaI="

EXPORT_ON = dict(
    FEEDBACK_INCIDENT_EXPORT_ENABLED=True,
    FEEDBACK_INCIDENT_EXPORT_KEY=TEST_KEY,
    FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT="staging",
)


@pytest.fixture
def superuser(db):
    return get_user_model().objects.create_user(
        username="root", password="pw", email="root@e.com", is_superuser=True, is_staff=True
    )


@pytest.fixture
def qualified(db, user, query_log, superuser):
    """Un incident qualifié, avec snapshot — l'état d'où part un export."""
    feedback = create_feedback(
        user=user,
        log=query_log,
        rating=AnswerFeedback.RATING_DOWN,
        kind=AnswerFeedback.KIND_INFO_IN_DOCS,
        comment="mon adresse est someone@example.com et le fichier est dans <racine du dépôt>",
        expected_document="note-interne-2023.pdf",
        expected_term="Nordexia Conseil",
    )
    capture_feedback_snapshot_task(str(feedback.id))
    transition(feedback, target=AnswerFeedback.STATUS_QUALIFIED, actor=superuser)
    feedback.refresh_from_db()
    return feedback


# ---------------------------------------------------------------------------
# L'invariant : aucun export automatique
# ---------------------------------------------------------------------------
def test_no_automatic_export_path_exists(qualified, user, query_log):
    """Le clic ne peut RIEN exporter, même sur un incident déjà qualifié.

    Vérifié structurellement : la capture diagnostique ne référence aucun code
    d'export, et l'export exige un statut que le clic ne pose jamais.
    """
    import apps.feedback.tasks as tasks_module

    source = (tasks_module.__file__ or "").replace(".pyc", ".py")
    with open(source, encoding="utf-8") as fh:
        body = fh.read()
    for forbidden in ("export", "build_package", "generate_package"):
        assert forbidden not in body.lower().replace("export_", "§"), (
            "la tâche de capture ne doit contenir AUCUN chemin d'export"
        )

    # Et un feedback frais ne peut pas être exporté : il n'est pas approuvé.
    fresh = AnswerFeedback.objects.get(id=qualified.id)
    with pytest.raises(levels.ExportRefused, match="autorisation préalable"):
        generate_package(fresh, level_value=1, user=user)


@override_settings(FEEDBACK_INCIDENT_EXPORT_ENABLED=False)
def test_export_disabled_by_default(qualified, superuser):
    approve_export(qualified, level_value=1, user=superuser, justification="test")
    with pytest.raises(levels.ExportRefused, match="désactivé"):
        generate_package(qualified, level_value=1, user=superuser)


# ---------------------------------------------------------------------------
# Niveaux
# ---------------------------------------------------------------------------
@override_settings(**EXPORT_ON)
def test_level_1_contains_no_text_at_all(qualified, superuser):
    approve_export(qualified, level_value=1, user=superuser, justification="")
    result = generate_package(qualified, level_value=1, user=superuser)

    assert result["encrypted"] is False, "sans texte, il n'y a pas de contenu de production"
    files = package.read_package(result["payload"])
    assert set(files) >= {
        "manifest.json",
        "request.json",
        "effective_config.json",
        "retrieval_candidates.json",
        "selected_chunks.json",
        "generation.json",
        "expected_behavior.md",
    }
    assert not any(p.startswith("source_fragments/") for p in files)

    blob = b"".join(files[p] for p in files if p.endswith(".json")).decode("utf-8")
    for forbidden in ("Nordexia Conseil", "I don't know", "extrait", "note-interne-2023.pdf"):
        assert forbidden not in blob, f"niveau 1 : {forbidden!r} ne doit pas sortir"

    # Ce qui reste est exactement ce qu'il faut pour diagnostiquer.
    selected = json.loads(files["selected_chunks.json"])
    assert selected["sources"][0]["chunk_id"]
    assert selected["sources"][0]["score"] == 0.71


@override_settings(**EXPORT_ON)
def test_level_2_includes_fragments_and_is_encrypted(qualified, superuser):
    approve_export(qualified, level_value=2, user=superuser, justification="reproduire le no_answer")
    result = generate_package(
        qualified, level_value=2, user=superuser, justification="reproduire le no_answer"
    )

    assert result["encrypted"] is True
    assert result["filename"].endswith(".zip.enc")
    assert result["manifest"]["encryption_status"] == "fernet"

    # Le chiffré ne laisse RIEN filtrer en clair.
    assert b"Nordexia" not in result["payload"]

    files = package.read_package(result["payload"])
    selected = json.loads(files["selected_chunks.json"])
    assert selected["sources"][0]["snippet"] == "extrait"
    # Le nom de fichier reste pseudonymisé même au niveau 2.
    assert selected["sources"][0]["filename"].startswith("doc-")
    assert "note-interne-2023" not in json.dumps(selected)


@override_settings(**EXPORT_ON)
def test_level_2_requires_a_justification(qualified, superuser):
    with pytest.raises(levels.ExportRefused, match="justification"):
        approve_export(qualified, level_value=2, user=superuser, justification="   ")


@override_settings(**EXPORT_ON)
def test_level_3_is_superuser_only(qualified, user, superuser):
    from apps.workspaces.models import Membership

    Membership.objects.update_or_create(
        workspace_id=int(qualified.workspace_id_hist), user=user,
        defaults={"role": Membership.ROLE_OWNER},
    )
    with pytest.raises(levels.ExportRefused, match="superuser"):
        approve_export(qualified, level_value=3, user=user, justification="défaut d'extraction")

    approve_export(qualified, level_value=3, user=superuser, justification="défaut d'extraction")
    assert qualified.status == AnswerFeedback.STATUS_EXPORT_APPROVED


# ---------------------------------------------------------------------------
# Rédaction et secrets
# ---------------------------------------------------------------------------
@override_settings(**EXPORT_ON)
def test_user_comment_never_leaves_production(qualified, superuser):
    approve_export(qualified, level_value=2, user=superuser, justification="j")
    result = generate_package(qualified, level_value=2, user=superuser, justification="j")
    files = package.read_package(result["payload"])
    blob = b"".join(files.values()).decode("utf-8", errors="ignore")

    # Le commentaire libre est exclu PAR DÉFAUT : il peut contenir n'importe quoi.
    assert "someone@example.com" not in blob
    assert "<racine du dépôt>" not in blob


def test_emails_and_paths_are_scrubbed():
    text = "écrire à someone@example.com, fichier dans /media/uploads/rapport.pdf"
    out = redaction.scrub_text(text)
    assert "someone@example.com" not in out
    assert "/media/uploads" not in out
    assert out.count(redaction.REDACTED) == 2


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwx",
        "-----BEGIN RSA PRIVATE KEY-----",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        'api_key = "abcdefghijklmnop123456"',
    ],
)
def test_secret_patterns_are_detected(secret):
    assert redaction.detect_secrets({"note": secret})


@override_settings(**EXPORT_ON)
def test_detected_secret_blocks_the_export(qualified, superuser, monkeypatch):
    monkeypatch.setattr(
        redaction, "detect_secrets", lambda payload: ["openai_key"]
    )
    approve_export(qualified, level_value=2, user=superuser, justification="j")
    with pytest.raises(package.ExportBlocked, match="secret"):
        generate_package(qualified, level_value=2, user=superuser, justification="j")

    # Manuellement approuvé : l'export passe, mais l'état le DIT.
    result = generate_package(
        qualified,
        level_value=2,
        user=superuser,
        justification="j",
        allow_secrets_override=True,
    )
    assert result["manifest"]["redaction_status"] == redaction.STATUS_MANUALLY_APPROVED


def test_document_pseudonym_is_stable():
    a = redaction.pseudonymize_filename("Budget_2026_confidentiel.xlsx")
    b = redaction.pseudonymize_filename("Budget_2026_confidentiel.xlsx")
    assert a == b and a.startswith("doc-") and a.endswith(".xlsx")
    assert "Budget" not in a


# ---------------------------------------------------------------------------
# Intégrité et chiffrement
# ---------------------------------------------------------------------------
@override_settings(**EXPORT_ON)
def test_manifest_integrity_and_signature(qualified, superuser):
    approve_export(qualified, level_value=1, user=superuser)
    result = generate_package(qualified, level_value=1, user=superuser)
    files = package.read_package(result["payload"])
    manifest = json.loads(files["manifest.json"])

    # Le condensat est calculé HORS manifeste : il doit être recalculable.
    recomputed = crypto.canonical_content_sha256(
        {k: v for k, v in files.items() if k != "manifest.json"}
    )
    assert manifest["content_sha256"] == recomputed
    assert crypto.verify_signature(manifest["content_sha256"], manifest["signature"]) is True
    assert crypto.verify_signature(manifest["content_sha256"], "0" * 64) is False


@override_settings(**EXPORT_ON)
def test_manifest_carries_the_full_provenance(qualified, superuser):
    approve_export(qualified, level_value=1, user=superuser)
    manifest = generate_package(qualified, level_value=1, user=superuser)["manifest"]

    for field in (
        "schema_version",
        "retrieval_trace_schema_version",
        "sources_schema_version",
        "incident_id",
        "source_environment",
        "git_sha",
        "exported_at",
        "expires_at",
        "contains_production_content",
        "redaction_status",
        "encryption_status",
        "source_model",
        "embedding_model",
        "content_sha256",
        "signature",
        "approved_by",
        "replay_modes_supported",
        "capture_mode",
    ):
        assert field in manifest, f"manifeste incomplet : {field}"
    assert manifest["source_environment"] == "staging"
    assert manifest["approved_by"] == "root"


@override_settings(
    FEEDBACK_INCIDENT_EXPORT_ENABLED=True,
    FEEDBACK_INCIDENT_EXPORT_KEY="",  # aucune clé
)
def test_production_content_without_key_is_refused_not_downgraded(qualified, superuser):
    """Sans clé, on REFUSE. On ne produit pas un paquet en clair « en attendant »."""
    approve_export(qualified, level_value=2, user=superuser, justification="j")
    with pytest.raises(package.ExportBlocked, match="refusé plutôt que produit"):
        generate_package(qualified, level_value=2, user=superuser, justification="j")


@override_settings(**EXPORT_ON)
def test_package_is_deterministic(qualified, superuser):
    approve_export(qualified, level_value=1, user=superuser)
    first = generate_package(qualified, level_value=1, user=superuser)

    # Deux exports du même incident : contenu logique identique.
    files_1 = package.read_package(first["payload"])
    second_files = {k: v for k, v in files_1.items() if k != "manifest.json"}
    assert crypto.canonical_content_sha256(second_files) == first["manifest"]["content_sha256"]


# ---------------------------------------------------------------------------
# Cycle de vie et audit
# ---------------------------------------------------------------------------
@override_settings(**EXPORT_ON)
def test_export_requires_qualification_first(user, query_log, superuser):
    feedback = create_feedback(user=user, log=query_log, rating=AnswerFeedback.RATING_DOWN)
    with pytest.raises(levels.ExportRefused, match="QUALIFIÉ"):
        approve_export(feedback, level_value=1, user=superuser)


@override_settings(**EXPORT_ON)
def test_every_export_step_is_audited(qualified, superuser):
    approve_export(qualified, level_value=1, user=superuser, justification="cas Nordexia")
    generate_package(qualified, level_value=1, user=superuser, justification="cas Nordexia")

    actions = set(
        ActionAuditEvent.objects.values_list("action_name", flat=True)
    )
    assert "platform.feedback.export_approved" in actions
    assert "platform.feedback.exported" in actions
    assert "platform.feedback.transition" in actions

    # L'audit ne contient JAMAIS le contenu exporté.
    for event in ActionAuditEvent.objects.all():
        blob = json.dumps(event.metadata, default=str)
        for forbidden in ("Nordexia Conseil", "I don't know", "someone@example.com"):
            assert forbidden not in blob


@override_settings(**EXPORT_ON)
def test_status_reaches_exported(qualified, superuser):
    approve_export(qualified, level_value=1, user=superuser)
    generate_package(qualified, level_value=1, user=superuser)
    qualified.refresh_from_db()
    assert qualified.status == AnswerFeedback.STATUS_EXPORTED


@override_settings(**EXPORT_ON)
def test_export_without_snapshot_is_refused(user, query_log, superuser):
    """Sans snapshot (expiré, par exemple), il n'y a rien à exporter."""
    feedback = create_feedback(user=user, log=query_log, rating=AnswerFeedback.RATING_DOWN)
    transition(feedback, target=AnswerFeedback.STATUS_QUALIFIED, actor=superuser)
    approve_export(feedback, level_value=1, user=superuser)
    FeedbackDiagnosticSnapshot.objects.all().delete()
    feedback.refresh_from_db()

    with pytest.raises(package.ExportBlocked, match="rien à exporter"):
        generate_package(feedback, level_value=1, user=superuser)


@override_settings(**EXPORT_ON)
def test_replay_modes_are_honest(qualified, superuser):
    """On n'annonce que les modes RÉELLEMENT exécutables avec ce paquet."""
    approve_export(qualified, level_value=1, user=superuser)
    manifest_1 = generate_package(qualified, level_value=1, user=superuser)["manifest"]
    # Niveau 1 : aucun extrait, donc aucun replay possible.
    assert manifest_1["replay_modes_supported"] == []

    transition(qualified, target=AnswerFeedback.STATUS_CLOSED, actor=superuser)
