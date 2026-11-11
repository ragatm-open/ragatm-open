"""Commande de calibration — ce qu'elle refuse d'écrire.

Une calibration décide de la participation ENTIÈRE d'une source. La commande doit donc
se comporter comme un garde-fou, pas comme un utilitaire : rien sans `--apply`, rien
sous les critères, rien sur une identité douteuse.

Le leg vectoriel est remplacé par un double déterministe : ce fichier teste la POLITIQUE
d'écriture, pas la qualité d'un provider d'embeddings de test.
"""
from __future__ import annotations

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.corpus.models import (
    BusinessDomain,
    DomainCorpus,
    DomainCorpusAdmissionCalibration,
)
from apps.corpus.services.calibration_training import (
    Observation,
    cross_validate,
    dataset_fingerprint,
    evaluate_criteria,
    fit_logistic,
    stratified_folds,
)
from apps.corpus.services.ingest import ingest_corpus_document_from_upload

pytestmark = pytest.mark.django_db

NEGATIVE_FAMILIES = ["neg_far", "neg_built", "neg_fin", "neg_entr", "neg_lex"]


@pytest.fixture
def corpus(db):
    domain = BusinessDomain.objects.create(slug="vc-cmd", name="VC")
    corpus = DomainCorpus.objects.create(
        domain=domain, slug="vc-cmd", name="VC",
        embedding_provider="ai_gateway", embedding_model="legacy-768",
    )
    ingest_corpus_document_from_upload(
        corpus=corpus, filename="vc.txt", content_type="text/plain",
        file_bytes=b"Venture capital is equity capital provided to unlisted companies.",
    )
    return corpus


#: Les positifs sont répartis sur DEUX catégories : un jeu dont tous les positifs
#: tiennent dans une seule famille ne peut pas démontrer que le modèle n'a pas appris la
#: composition du jeu — la commande le refuse, et c'est testé plus bas.
POSITIVE_FAMILIES = ["corpus", "mixed"]


def _dataset(tmp_path, *, positives=25, negatives=25, families=NEGATIVE_FAMILIES,
             positive_families=POSITIVE_FAMILIES):
    questions = [
        {"id": f"P{i:02d}", "category": positive_families[i % len(positive_families)],
         "label": 1, "text": f"venture capital {i}"}
        for i in range(positives)
    ] + [
        {
            "id": f"N{i:02d}",
            "category": families[i % len(families)],
            "label": 0,
            "text": f"unrelated topic {i}",
        }
        for i in range(negatives)
    ]
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps({"questions": questions}), encoding="utf-8")
    return path


def _rows(score, *, document_id="doc0", corpus_id="c", count=10):
    return [
        {
            "chunk_id": f"ch{i}", "score": score, "source_type": "domain_corpus",
            "source": {"source_type": "domain_corpus", "corpus_id": corpus_id,
                       "document_id": document_id},
        }
        for i in range(count)
    ]


@pytest.fixture
def scored_by_question(monkeypatch):
    """Le double renvoie un score fonction de la question — via `embed_texts` espionné."""
    from apps.corpus.services import retrieval as corpus_retrieval
    from apps.llm_router import embeddings as embeddings_mod

    state = {"question": "", "high": 0.80, "low": 0.55, "jitter": 0.0}
    real_embed = embeddings_mod.embed_texts

    def spy_embed(texts, **kwargs):
        state["question"] = texts[0] if texts else ""
        return real_embed(texts, **kwargs)

    def fake_vector(*, corpus_ids, query_embedding, top_k):
        positive = "venture" in state["question"]
        base = state["high"] if positive else state["low"]
        index = int("".join(ch for ch in state["question"] if ch.isdigit()) or 0)
        # Le document dominant VARIE d'une question à l'autre : sinon le découpage par
        # groupe n'aurait qu'un seul groupe, et ne validerait rien.
        return _rows(
            base + (index % 5) * state["jitter"], document_id=f"doc{index % 6}"
        )

    monkeypatch.setattr(embeddings_mod, "embed_texts", spy_embed)
    monkeypatch.setattr(corpus_retrieval, "search_corpus_chunks_vector", fake_vector)
    return state


# ===========================================================================
# 31-32. Plan par défaut, écriture explicite
# ===========================================================================
def test_dry_run_writes_nothing(corpus, tmp_path, scored_by_question):
    call_command(
        "calibrate_business_corpus_admission",
        "--corpus", corpus.slug, "--dataset", str(_dataset(tmp_path)),
    )
    assert DomainCorpusAdmissionCalibration.objects.count() == 0


def test_explicit_dry_run_flag_writes_nothing(corpus, tmp_path, scored_by_question):
    call_command(
        "calibrate_business_corpus_admission",
        "--corpus", corpus.slug, "--dataset", str(_dataset(tmp_path)), "--dry-run",
    )
    assert DomainCorpusAdmissionCalibration.objects.count() == 0


def test_apply_persists_an_active_calibration(corpus, tmp_path, scored_by_question):
    call_command(
        "calibrate_business_corpus_admission",
        "--corpus", corpus.slug, "--dataset", str(_dataset(tmp_path)), "--apply",
    )
    row = DomainCorpusAdmissionCalibration.objects.get()
    assert row.is_active is True and row.version == 1
    assert row.signal == "corpus_vector_top10_mean"
    assert row.calibrator_type == "logistic"
    assert row.embedding_provider == "ai_gateway"
    assert row.embedding_model == "legacy-768"
    assert set(row.coefficients) == {"intercept", "top10_mean"}


# ===========================================================================
# 33-34. Refus
# ===========================================================================
def test_dataset_with_too_few_observations_is_refused(corpus, tmp_path, scored_by_question):
    dataset = _dataset(tmp_path, positives=10, negatives=10)
    with pytest.raises(CommandError, match="critères minimaux"):
        call_command(
            "calibrate_business_corpus_admission",
            "--corpus", corpus.slug, "--dataset", str(dataset), "--apply",
        )
    assert DomainCorpusAdmissionCalibration.objects.count() == 0


def test_dataset_with_too_few_negative_families_is_refused(corpus, tmp_path, scored_by_question):
    dataset = _dataset(tmp_path, families=["neg_far", "neg_built"])
    with pytest.raises(CommandError, match="familles négatives"):
        call_command(
            "calibrate_business_corpus_admission",
            "--corpus", corpus.slug, "--dataset", str(dataset), "--apply",
        )


def test_dataset_whose_positives_share_a_single_category_is_refused(
    corpus, tmp_path, scored_by_question
):
    """« Une famille retirée » devient inapplicable : la limite est nommée, pas ignorée.

    Un jeu dont tous les positifs tiennent dans une seule catégorie ne peut pas
    démontrer que le modèle n'a pas appris la composition du jeu.
    """
    dataset = _dataset(tmp_path, positive_families=["corpus"])
    with pytest.raises(CommandError, match="non évaluable"):
        call_command("calibrate_business_corpus_admission", "--corpus", corpus.slug,
                     "--dataset", str(dataset), "--apply")
    assert DomainCorpusAdmissionCalibration.objects.count() == 0


def test_insufficient_metrics_are_refused(corpus, tmp_path, scored_by_question):
    """Signal non discriminant : la commande refuse, plutôt que de filtrer au hasard."""
    scored_by_question["high"] = 0.70
    scored_by_question["low"] = 0.70          # positifs et négatifs indiscernables
    with pytest.raises(CommandError, match="critères minimaux"):
        call_command(
            "calibrate_business_corpus_admission",
            "--corpus", corpus.slug, "--dataset", str(_dataset(tmp_path)), "--apply",
        )
    assert DomainCorpusAdmissionCalibration.objects.count() == 0


def test_unknown_calibrator_and_signal_are_refused(corpus, tmp_path, scored_by_question):
    dataset = str(_dataset(tmp_path))
    with pytest.raises(CommandError, match="calibrateur inconnu"):
        call_command("calibrate_business_corpus_admission", "--corpus", corpus.slug,
                     "--dataset", dataset, "--calibrator", "isotonic")
    with pytest.raises(CommandError, match="signal inconnu"):
        call_command("calibrate_business_corpus_admission", "--corpus", corpus.slug,
                     "--dataset", dataset, "--signal", "corpus_fts_top10_mean")


def test_corpus_without_explicit_embedding_model_is_refused(corpus, tmp_path, scored_by_question):
    DomainCorpus.objects.filter(id=corpus.id).update(embedding_model="")
    with pytest.raises(CommandError):
        call_command("calibrate_business_corpus_admission", "--corpus", corpus.slug,
                     "--dataset", str(_dataset(tmp_path)))


@pytest.mark.parametrize(
    "questions, message",
    [
        ([{"id": "A", "category": "corpus", "label": 1}], "clé 'text'"),
        ([{"id": "A", "category": "corpus", "label": 2, "text": "x"}], "label non binaire"),
        (
            [{"id": "A", "category": "corpus", "label": 1, "text": "x"},
             {"id": "A", "category": "corpus", "label": 0, "text": "y"}],
            "dupliqué",
        ),
    ],
)
def test_malformed_dataset_is_refused(corpus, tmp_path, questions, message):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"questions": questions}), encoding="utf-8")
    with pytest.raises(CommandError, match=message):
        call_command("calibrate_business_corpus_admission", "--corpus", corpus.slug,
                     "--dataset", str(path))


# ===========================================================================
# 35-36. Empreinte et versionnement
# ===========================================================================
def test_dataset_hash_is_persisted_and_reported(corpus, tmp_path, scored_by_question):
    report = tmp_path / "report.json"
    call_command(
        "calibrate_business_corpus_admission", "--corpus", corpus.slug,
        "--dataset", str(_dataset(tmp_path)), "--output", str(report), "--apply",
    )
    row = DomainCorpusAdmissionCalibration.objects.get()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert len(row.dataset_hash) == 64
    assert payload["training"]["dataset_hash"] == row.dataset_hash
    assert row.training["sample_count"] == 50


def test_recalibration_versions_instead_of_overwriting(corpus, tmp_path, scored_by_question):
    """L'historique survit : une décision passée doit rester explicable."""
    dataset = str(_dataset(tmp_path))
    for _ in range(2):
        call_command("calibrate_business_corpus_admission", "--corpus", corpus.slug,
                     "--dataset", dataset, "--apply")
    rows = list(DomainCorpusAdmissionCalibration.objects.order_by("version"))
    assert [r.version for r in rows] == [1, 2]
    assert [r.is_active for r in rows] == [False, True]


def test_report_covers_the_three_cross_validation_schemes(corpus, tmp_path, scored_by_question):
    report = tmp_path / "report.json"
    call_command(
        "calibrate_business_corpus_admission", "--corpus", corpus.slug,
        "--dataset", str(_dataset(tmp_path)), "--output", str(report),
    )
    validation = json.loads(report.read_text(encoding="utf-8"))["validation"]
    assert set(validation) == {"stratified", "group", "leave_one_category_out"}
    for metrics in validation.values():
        for key in ("roc_auc", "pr_auc", "brier", "recall", "specificity", "balanced_accuracy"):
            assert key in metrics


def test_report_contains_no_question_text(corpus, tmp_path, scored_by_question):
    report = tmp_path / "report.json"
    call_command(
        "calibrate_business_corpus_admission", "--corpus", corpus.slug,
        "--dataset", str(_dataset(tmp_path)), "--output", str(report), "--apply",
    )
    payload = report.read_text(encoding="utf-8")
    assert "venture capital 0" not in payload and "unrelated topic 0" not in payload
    stored = DomainCorpusAdmissionCalibration.objects.get()
    serialized = json.dumps([stored.training, stored.validation, stored.coefficients])
    assert "venture" not in serialized and "unrelated" not in serialized


# ===========================================================================
# Le module d'entraînement, sans base de données
# ===========================================================================
def _observations(separation: float, n: int = 60):
    return [
        Observation(
            question_id=f"q{i}", category="corpus" if i % 2 else NEGATIVE_FAMILIES[i % 5],
            label=i % 2, signal=(0.60 + separation) if i % 2 else 0.60,
            text_digest=f"d{i}", dominant_document_id=f"doc{i % 4}",
        )
        for i in range(n)
    ]


def test_out_of_fold_metrics_reward_separation_and_punish_noise():
    separable = cross_validate(
        _observations(0.15), stratified_folds(_observations(0.15)), probability_threshold=0.65
    )
    flat = cross_validate(
        _observations(0.0), stratified_folds(_observations(0.0)), probability_threshold=0.65
    )
    assert separable["roc_auc"] == pytest.approx(1.0)
    assert flat["roc_auc"] == pytest.approx(0.5, abs=0.05)
    assert evaluate_criteria(_observations(0.15), separable).passed is True
    assert evaluate_criteria(_observations(0.0), flat).passed is False


def test_logistic_fit_returns_coefficients_in_the_native_signal_unit():
    """Les coefficients persistés s'appliquent au signal BRUT, sans standardisation."""
    observations = _observations(0.20)
    fit = fit_logistic([o.signal for o in observations], [o.label for o in observations])
    assert fit.probability(0.60) < 0.5 < fit.probability(0.80)
    assert fit.coefficient > 0.0


def test_dataset_fingerprint_is_stable_and_order_independent():
    observations = _observations(0.15)
    assert dataset_fingerprint(observations) == dataset_fingerprint(list(reversed(observations)))


def test_dataset_fingerprint_changes_with_the_labels():
    observations = _observations(0.15)
    flipped = [
        Observation(o.question_id, o.category, 1 - o.label, o.signal, o.text_digest)
        for o in observations
    ]
    assert dataset_fingerprint(observations) != dataset_fingerprint(flipped)
