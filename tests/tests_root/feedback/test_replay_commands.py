"""Lot 4 — import, replay, promotion en fixture.

Le cycle complet est exercé de bout en bout : un incident réel est capturé,
exporté, importé, rejoué, puis promu en cas de non-régression. Les refus sont
testés autant que les succès — un import qui accepterait un paquet altéré, ou un
replay qui conclurait sur des embeddings incompatibles, seraient pires que
l'absence d'outil.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.feedback.diagnostics import buffer
from apps.feedback.models import AnswerFeedback
from apps.feedback.services.create_feedback import create_feedback
from apps.feedback.services.export_service import approve_export, generate_package
from apps.feedback.services.transitions import transition
from apps.feedback.tasks import capture_feedback_snapshot_task

pytestmark = pytest.mark.django_db

TEST_KEY = "U4DzV1ONux1P7TEAq1XUUbqcAX_8P7ReJ-lDAZXGqaI="

EXPORT_ON = dict(
    FEEDBACK_INCIDENT_EXPORT_ENABLED=True,
    FEEDBACK_INCIDENT_EXPORT_KEY=TEST_KEY,
    FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT="staging",
)
IMPORT_ON = dict(
    FEEDBACK_INCIDENT_IMPORT_ENABLED=True,
    FEEDBACK_INCIDENT_EXPORT_KEY=TEST_KEY,
    FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT="dev",  # on importe AILLEURS qu'à l'origine
)


def _buffer_trace(log_id: str) -> dict:
    """Trace de candidats telle que le pipeline l'écrit réellement."""
    return {
        "schema_version": 1,
        "log_id": log_id,
        "created_at": "2026-07-28T10:00:00+00:00",
        "embedding_model": "nomic-embed-text",
        "pool_size": 2,
        "truncated": False,
        "candidates": [
            {
                "chunk_id": "11111111-1111-1111-1111-111111111111",
                "document_id": "22222222-2222-2222-2222-222222222222",
                "leg": "hybrid",
                "rank_fused": 1,
                "score": 0.71,
                "selected": True,
                "excluded_by": None,
            },
            {
                "chunk_id": "33333333-3333-3333-3333-333333333333",
                "document_id": "22222222-2222-2222-2222-222222222222",
                "leg": "fts",
                "rank_fused": 2,
                "score": 0.05,
                "selected": False,
                "excluded_by": "diversity_max_per_group",
            },
        ],
        "selection": {
            "selected_chunk_ids": ["11111111-1111-1111-1111-111111111111"],
            "answerability_policy": "no_answer",
            "thresholds": {"min_score": 0.76},
            "budgets": {"top_k": 4},
        },
    }


@pytest.fixture
def superuser(db):
    return get_user_model().objects.create_user(
        username="root", password="pw", email="root@e.com", is_superuser=True, is_staff=True
    )


@pytest.fixture
def exported(db, user, query_log, superuser, tmp_path):
    """Un paquet de niveau 2 réellement produit, prêt à être importé."""
    feedback = create_feedback(
        user=user,
        log=query_log,
        rating=AnswerFeedback.RATING_DOWN,
        kind=AnswerFeedback.KIND_INFO_IN_DOCS,
        expected_document="note-interne-2023.pdf",
        expected_term="Nordexia Conseil",
    )
    # Capture EXACTE : c'est le cas nominal, et le seul qui donne au paquet des
    # candidats de production à comparer lors du replay.
    with override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True):
        buffer.write_trace(query_log.id, _buffer_trace(str(query_log.id)))
        capture_feedback_snapshot_task(str(feedback.id))
    transition(feedback, target=AnswerFeedback.STATUS_QUALIFIED, actor=superuser)

    with override_settings(**EXPORT_ON):
        approve_export(feedback, level_value=2, user=superuser, justification="cas Nordexia")
        result = generate_package(
            feedback, level_value=2, user=superuser, justification="cas Nordexia"
        )

    path = tmp_path / result["filename"]
    path.write_bytes(result["payload"])
    return {"path": path, "feedback": feedback, "manifest": result["manifest"]}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def test_import_is_disabled_by_default(exported, tmp_path):
    with pytest.raises(CommandError, match="désactivé"):
        call_command("import_rag_incident", "--input", str(exported["path"]))


@override_settings(**IMPORT_ON)
def test_import_verifies_and_extracts(exported, tmp_path, capsys):
    out = tmp_path / "imported"
    call_command(
        "import_rag_incident", "--input", str(exported["path"]), "--out", str(out)
    )

    assert (out / "manifest.json").is_file()
    assert (out / "retrieval_candidates.json").is_file()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["incident_id"] == str(exported["feedback"].id)


@override_settings(**IMPORT_ON)
def test_import_deletes_the_plaintext_archive(exported, tmp_path):
    call_command(
        "import_rag_incident",
        "--input", str(exported["path"]),
        "--out", str(tmp_path / "imported"),
        "--delete-plaintext-after-import",
    )
    # Une copie de contenu de production sur un poste de dev doit être éphémère.
    assert not exported["path"].exists()


@override_settings(**IMPORT_ON)
def test_import_refuses_a_tampered_package(exported, tmp_path):
    """Une altération d'un seul octet doit être détectée."""
    from apps.feedback.export import crypto, package

    files = package.read_package(exported["path"].read_bytes())
    files["retrieval_candidates.json"] = b'{"candidates": [{"chunk_id": "falsifie"}]}'
    tampered = tmp_path / "tampered.zip"
    tampered.write_bytes(package._zip(files))

    with pytest.raises(CommandError, match="Intégrité rompue"):
        call_command("import_rag_incident", "--input", str(tampered))


@override_settings(**IMPORT_ON)
def test_import_refuses_an_invalid_signature(exported, tmp_path):
    from apps.feedback.export import crypto, package

    files = package.read_package(exported["path"].read_bytes())
    manifest = json.loads(files["manifest.json"])
    manifest["signature"] = "0" * 64
    files["manifest.json"] = crypto.dumps(manifest)
    forged = tmp_path / "forged.zip"
    forged.write_bytes(package._zip(files))

    with pytest.raises(CommandError, match="Signature INVALIDE"):
        call_command("import_rag_incident", "--input", str(forged))


@override_settings(**IMPORT_ON)
def test_import_refuses_an_unknown_schema_version(exported, tmp_path):
    from apps.feedback.export import crypto, package

    files = package.read_package(exported["path"].read_bytes())
    manifest = json.loads(files["manifest.json"])
    manifest["schema_version"] = 99
    files["manifest.json"] = crypto.dumps(manifest)
    future = tmp_path / "future.zip"
    future.write_bytes(package._zip(files))

    with pytest.raises(CommandError, match="Version de paquet non supportée"):
        call_command("import_rag_incident", "--input", str(future))


@override_settings(**IMPORT_ON)
def test_import_refuses_an_expired_package(exported, tmp_path):
    from apps.feedback.export import crypto, package

    files = package.read_package(exported["path"].read_bytes())
    manifest = json.loads(files["manifest.json"])
    manifest["expires_at"] = "2020-01-01T00:00:00+00:00"
    files["manifest.json"] = crypto.dumps(manifest)
    manifest["content_sha256"] = crypto.canonical_content_sha256(files)
    manifest["signature"] = crypto.sign(manifest["content_sha256"])
    files["manifest.json"] = crypto.dumps(manifest)

    stale = tmp_path / "stale.zip"
    stale.write_bytes(package._zip(files))

    with pytest.raises(CommandError, match="EXPIRÉ"):
        call_command("import_rag_incident", "--input", str(stale))


@override_settings(
    FEEDBACK_INCIDENT_IMPORT_ENABLED=True,
    FEEDBACK_INCIDENT_EXPORT_KEY=TEST_KEY,
    FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT="staging",  # MÊME environnement que l'export
)
def test_import_refuses_its_own_environment(exported):
    with pytest.raises(CommandError, match="environnement courant"):
        call_command("import_rag_incident", "--input", str(exported["path"]))


@override_settings(**{**IMPORT_ON, "EMBEDDINGS_MODEL": "un-modele-totalement-different"})
def test_import_refuses_divergent_embedding_model(exported, tmp_path):
    """Le contrôle qui évite une conclusion FAUSSE, pas seulement une erreur."""
    from apps.feedback.export import crypto, package

    files = package.read_package(exported["path"].read_bytes())
    manifest = json.loads(files["manifest.json"])
    manifest["embedding_model"] = "text-embedding-3-large"
    files["manifest.json"] = crypto.dumps(manifest)
    manifest["content_sha256"] = crypto.canonical_content_sha256(files)
    manifest["signature"] = crypto.sign(manifest["content_sha256"])
    files["manifest.json"] = crypto.dumps(manifest)
    other = tmp_path / "other-embeddings.zip"
    other.write_bytes(package._zip(files))

    with pytest.raises(CommandError, match="Modèle d'embedding DIVERGENT"):
        call_command("import_rag_incident", "--input", str(other))

    # L'échappatoire existe, mais elle est explicite et documentée.
    call_command(
        "import_rag_incident",
        "--input", str(other),
        "--out", str(tmp_path / "forced"),
        "--skip-embedding-check",
    )
    assert (tmp_path / "forced" / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
@pytest.fixture
def imported(exported, tmp_path):
    with override_settings(**IMPORT_ON):
        out = tmp_path / "imported"
        call_command("import_rag_incident", "--input", str(exported["path"]), "--out", str(out))
    return out


@override_settings(**IMPORT_ON)
def test_replay_retrieval_produces_a_comparison_report(imported, rag_space, ingest_text, tmp_path):
    ingest_text(rag_space, "Nordexia Conseil accompagne des collectivités.", "nordexia.txt")

    report_path = tmp_path / "report.json"
    call_command(
        "replay_rag_incident",
        "--incident", str(imported),
        "--mode", "retrieval",
        "--rag-space", str(rag_space.id),
        "--out", str(report_path),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "retrieval"
    assert report["deterministic"] is True
    assert set(report["differences"]) >= {
        "missing_candidates",
        "new_candidates",
        "rank_changes",
        "selection_changes",
    }
    # Le dev a bien trouvé des candidats : c'est ce qui rend la comparaison utile.
    assert report["dev"]["candidate_ids"]
    assert report["production"]["candidate_ids"]


@override_settings(**IMPORT_ON)
def test_replay_retrieval_requires_a_rag_space(imported):
    with pytest.raises(CommandError, match="--rag-space est requis"):
        call_command("replay_rag_incident", "--incident", str(imported), "--mode", "retrieval")


@override_settings(**IMPORT_ON)
def test_replay_prompt_only_requires_the_captured_prompt(imported):
    """Sans prompt capturé, on refuse — on ne reconstruit pas un faux prompt."""
    with pytest.raises(CommandError, match="prompt complet"):
        call_command("replay_rag_incident", "--incident", str(imported), "--mode", "prompt-only")


@override_settings(**IMPORT_ON)
def test_replay_creates_no_business_log(imported, rag_space, ingest_text):
    from apps.audit.models import RagQueryLog

    ingest_text(rag_space, "Nordexia Conseil.", "c.txt")
    before = RagQueryLog.objects.count()

    call_command(
        "replay_rag_incident",
        "--incident", str(imported),
        "--mode", "retrieval",
        "--rag-space", str(rag_space.id),
    )
    assert RagQueryLog.objects.count() == before


# ---------------------------------------------------------------------------
# Promotion en fixture
# ---------------------------------------------------------------------------
def test_promotion_produces_a_benchmark_case(imported, tmp_path):
    out = tmp_path / "fixtures"
    call_command(
        "promote_rag_incident_to_fixture",
        "--incident", str(imported),
        "--output", str(out),
        "--rag-space", "espace-de-replay",
    )

    files = list(out.glob("*.jsonl"))
    assert len(files) == 1
    case = json.loads(files[0].read_text(encoding="utf-8").strip())

    assert case["question"] == "what is Nordexia Conseil?"
    assert case["should_answer"] is True
    assert case["expected_chunk_ids"]
    assert case["metadata"]["incident_id"]
    assert case["metadata"]["capture_mode"]
    assert case["rag_space"] == "espace-de-replay"


def test_promoted_case_is_readable_by_the_existing_benchmark_loader(imported, tmp_path):
    """Le cas produit doit être consommable PAR LE HARNAIS EXISTANT.

    C'est tout l'intérêt de réutiliser le format de `rag_benchmark` plutôt que
    d'en inventer un second : un incident promu entre immédiatement dans la
    mesure de qualité déjà en place.
    """
    from apps.rag_core.benchmark.dataset import parse_jsonl

    out = tmp_path / "cases.jsonl"
    call_command(
        "promote_rag_incident_to_fixture",
        "--incident", str(imported),
        "--output", str(out),
        "--rag-space", "espace-de-replay",
    )
    cases = parse_jsonl(out.read_text(encoding="utf-8").splitlines())
    assert len(cases) == 1
    assert cases[0].question == "what is Nordexia Conseil?"
    assert cases[0].should_answer is True


def test_promotion_does_not_leak_content_by_default(imported, tmp_path):
    out = tmp_path / "cases.jsonl"
    call_command(
        "promote_rag_incident_to_fixture", "--incident", str(imported), "--output", str(out)
    )
    case = json.loads(out.read_text(encoding="utf-8").strip())
    # `expected_answer_contains` viendrait du signalement : il n'est pas versé
    # au dépôt sans autorisation explicite.
    assert "expected_answer_contains" not in case


def test_promotion_with_allow_content_includes_the_expected_term(imported, tmp_path):
    out = tmp_path / "cases.jsonl"
    call_command(
        "promote_rag_incident_to_fixture",
        "--incident", str(imported),
        "--output", str(out),
        "--allow-content",
    )
    case = json.loads(out.read_text(encoding="utf-8").strip())
    assert case["expected_answer_contains"] == ["Nordexia Conseil"]


def test_promotion_refuses_an_incident_without_question(imported, tmp_path):
    request_path = Path(imported) / "request.json"
    request_path.write_text(json.dumps({"language": {}}), encoding="utf-8")

    with pytest.raises(CommandError, match="question est absente"):
        call_command(
            "promote_rag_incident_to_fixture",
            "--incident", str(imported),
            "--output", str(tmp_path / "x.jsonl"),
        )
