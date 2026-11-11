"""Calibre la porte d'admission d'un corpus métier, APRÈS l'avoir validée.

    python manage.py calibrate_business_corpus_admission --corpus refvc \\
        --dataset annotations.json                       # plan + métriques, aucune écriture
    python manage.py calibrate_business_corpus_admission --corpus refvc \\
        --dataset annotations.json --output rapport.json --apply

Format du jeu annoté (JSON)
---------------------------
    {
      "questions": [
        {"id": "C01", "category": "corpus",  "label": 1, "text": "..."},
        {"id": "N01", "category": "neg_far", "label": 0, "text": "..."}
      ]
    }

``category`` sert aux plis « une famille retirée » et au décompte des faux positifs par
famille : c'est ce qui distingue un modèle robuste d'un modèle qui a appris la
composition du jeu. L'annotation doit être GELÉE avant toute mesure — annoter après avoir
vu les classements revient à mesurer sa propre intuition.

Ce que la commande refuse
-------------------------
Elle n'écrit jamais sans ``--apply``, et refuse d'écrire une calibration qui n'atteint
pas les critères minimaux (cf. ``services/calibration_training.py``). Un corpus non
calibré est REJETÉ par la porte, donc visible ; un corpus calibré par un modèle médiocre
filtre en silence et à tort — c'est le seul des deux états qui trompe.

Aucune génération LLM : seuls des embeddings de questions sont calculés.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.corpus.services.calibration_training import (
    MIN_ROC_AUC,
    Observation,
    cross_validate,
    category_folds,
    dataset_fingerprint,
    evaluate_criteria,
    fit_logistic,
    group_folds,
    stratified_folds,
)
from apps.rag_core.domain.corpus_admission import (
    CALIBRATION_SCHEMA_VERSION,
    CALIBRATOR_LOGISTIC,
    SIGNAL_CORPUS_VECTOR_TOP10_MEAN,
    SIGNAL_TOP_N,
    admission_signal_value,
)

#: Schéma de référence pour les critères de persistance. Les autres sont RAPPORTÉS, et
#: seul leur pouvoir de classement (ROC-AUC) est exigé : retirer une famille entière de
#: l'entraînement dégrade légitimement le rappel, pas l'ordre.
REFERENCE_SCHEME = "stratified"


class Command(BaseCommand):
    help = (
        "Entraîne et valide la calibration d'admission d'un corpus métier "
        "(signal corpus_vector_top10_mean, calibrateur logistique)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--corpus", required=True, help="slug du corpus métier")
        parser.add_argument("--dataset", required=True, help="fichier JSON annoté")
        parser.add_argument("--calibrator", default=CALIBRATOR_LOGISTIC)
        parser.add_argument("--signal", default=SIGNAL_CORPUS_VECTOR_TOP10_MEAN)
        parser.add_argument("--probability-threshold", type=float, default=0.65)
        parser.add_argument("--vector-top-k", type=int, default=40)
        parser.add_argument("--output", default=None, help="chemin du rapport JSON")
        parser.add_argument("--dry-run", action="store_true", help="défaut : aucune écriture")
        parser.add_argument("--apply", action="store_true", help="persiste la calibration")

    # -- exécution -----------------------------------------------------------
    def handle(self, *args, **opts) -> None:
        if opts["calibrator"] != CALIBRATOR_LOGISTIC:
            raise CommandError(
                f"calibrateur inconnu : {opts['calibrator']!r} "
                f"(seul {CALIBRATOR_LOGISTIC!r} est implémenté en production)"
            )
        if opts["signal"] != SIGNAL_CORPUS_VECTOR_TOP10_MEAN:
            raise CommandError(f"signal inconnu : {opts['signal']!r}")
        threshold = float(opts["probability_threshold"])
        if not (0.0 <= threshold <= 1.0) or not math.isfinite(threshold):
            raise CommandError(f"--probability-threshold hors [0, 1] : {threshold}")

        corpus, provider, model, dimension = self._resolve_corpus(opts["corpus"])
        questions = self._load_dataset(opts["dataset"])
        self.stdout.write(
            f"Corpus {corpus.slug} — embeddings {provider}/{model} (dim {dimension}), "
            f"{len(questions)} questions annotées."
        )

        observations = self._measure(
            corpus=corpus, provider=provider, model=model, dimension=dimension,
            questions=questions, vector_top_k=int(opts["vector_top_k"]),
        )
        report = self._validate(observations, threshold=threshold)
        report.update(
            corpus_slug=corpus.slug,
            corpus_id=str(corpus.id),
            signal=SIGNAL_CORPUS_VECTOR_TOP10_MEAN,
            calibrator_type=CALIBRATOR_LOGISTIC,
            embedding_provider=provider,
            embedding_model=model,
            embedding_dimension=dimension,
            probability_threshold=threshold,
        )
        self._print_report(report)

        if opts["output"]:
            Path(opts["output"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
            self.stdout.write(f"Rapport écrit : {opts['output']}")

        verdict = report["criteria"]
        if not verdict["passed"]:
            raise CommandError(
                "REFUS de persister : critères minimaux non atteints — "
                + " ; ".join(verdict["failures"])
            )

        if not opts["apply"]:
            self.stdout.write(
                "Aucune écriture (mode plan). Relancer avec --apply pour persister."
            )
            return
        self._persist(corpus, report, provider=provider, model=model, dimension=dimension)

    # -- étapes --------------------------------------------------------------
    def _resolve_corpus(self, slug: str):
        from apps.corpus.models import DomainCorpus, DomainCorpusChunk
        from apps.corpus.services.ingest import resolve_corpus_embedding

        corpus = DomainCorpus.objects.filter(slug=slug).first()
        if corpus is None:
            raise CommandError(f"corpus introuvable : {slug!r}")
        try:
            provider, model = resolve_corpus_embedding(corpus)
        except ValueError as exc:
            # Provenance des vecteurs non déclarée : le corpus n'a pas d'échelle connue,
            # donc rien à calibrer. Même refus qu'à l'indexation et au retrieval.
            raise CommandError(f"{exc} (voir set_corpus_embedding_model)") from exc
        if not provider or not model:
            raise CommandError(
                f"corpus {slug!r} sans provider/modèle d'embeddings explicite : "
                "calibrer une échelle inconnue n'aurait aucun sens "
                "(voir set_corpus_embedding_model)."
            )
        chunk = (
            DomainCorpusChunk.objects.filter(corpus=corpus, embedding__isnull=False)
            .order_by("id")
            .first()
        )
        if chunk is None:
            raise CommandError(f"corpus {slug!r} sans chunk vectorisé : rien à calibrer.")
        return corpus, provider, model, len(chunk.embedding)

    def _load_dataset(self, path: str) -> list[dict]:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise CommandError(f"jeu annoté illisible : {exc}") from exc
        except ValueError as exc:
            raise CommandError(f"jeu annoté invalide (JSON) : {exc}") from exc
        questions = payload.get("questions") if isinstance(payload, dict) else payload
        if not isinstance(questions, list) or not questions:
            raise CommandError("jeu annoté vide ou mal formé (clé `questions` attendue).")
        seen: set[str] = set()
        for q in questions:
            for key in ("id", "category", "label", "text"):
                if key not in q:
                    raise CommandError(f"question incomplète (clé {key!r} absente) : {q.get('id')!r}")
            if int(q["label"]) not in (0, 1):
                raise CommandError(f"label non binaire pour {q['id']!r}")
            if q["id"] in seen:
                raise CommandError(f"identifiant de question dupliqué : {q['id']!r}")
            seen.add(q["id"])
        return questions

    def _measure(self, *, corpus, provider, model, dimension, questions, vector_top_k):
        """Calcule le signal de chaque question. Embeddings seulement, aucune génération."""
        from apps.corpus.services.retrieval import search_corpus_chunks_vector
        from apps.llm_router.egress import domain_corpus_context
        from apps.llm_router.embeddings import embed_texts

        top_k = max(SIGNAL_TOP_N, int(vector_top_k))
        _calibration_ctx = domain_corpus_context(
            corpus_id=getattr(corpus, "id", ""), provider=provider,
            operation="embedding.query",
        )
        observations: list[Observation] = []
        for index, q in enumerate(questions, start=1):
            embedding = embed_texts(
                [q["text"]], dim=dimension, provider=provider, model=model,
                context=_calibration_ctx,
            )[0]
            candidates = search_corpus_chunks_vector(
                corpus_ids=[str(corpus.id)], query_embedding=embedding, top_k=top_k,
            )
            signal = admission_signal_value(candidates)
            if signal is None:
                raise CommandError(
                    f"question {q['id']!r} : aucun candidat vectoriel — le corpus est-il "
                    "vectorisé et le modèle correct ?"
                )
            documents = [
                (c.get("source") or {}).get("document_id")
                for c in candidates[:SIGNAL_TOP_N]
            ]
            documents = [d for d in documents if d]
            observations.append(
                Observation(
                    question_id=str(q["id"]),
                    category=str(q["category"]),
                    label=int(q["label"]),
                    signal=float(signal),
                    text_digest=hashlib.sha256(q["text"].encode("utf-8")).hexdigest()[:16],
                    dominant_document_id=(
                        max(set(documents), key=documents.count) if documents else None
                    ),
                )
            )
            if index % 10 == 0:
                self.stdout.write(f"  {index}/{len(questions)} questions mesurées")
        return observations

    def _validate(self, observations, *, threshold: float) -> dict:
        schemes = {
            REFERENCE_SCHEME: stratified_folds(observations),
            "group": group_folds(observations),
            "leave_one_category_out": category_folds(observations),
        }
        validation = {}
        for name, folds in schemes.items():
            try:
                validation[name] = cross_validate(
                    observations, folds, probability_threshold=threshold,
                )
            except ValueError as exc:
                # Un schéma peut être INAPPLICABLE au jeu fourni — typiquement
                # « une famille retirée » quand tous les positifs tiennent dans une seule
                # catégorie. Ce n'est pas une erreur d'exécution, c'est une limite du jeu :
                # on la NOMME, et elle empêchera la persistance.
                validation[name] = {"evaluable": False, "reason": str(exc)}

        verdict = evaluate_criteria(observations, validation[REFERENCE_SCHEME])
        for name, metrics in validation.items():
            if name == REFERENCE_SCHEME:
                continue
            if not metrics.get("evaluable", True):
                verdict.require(False, f"schéma {name} non évaluable : {metrics['reason']}")
                continue
            auc = metrics.get("roc_auc")
            verdict.require(
                isinstance(auc, (int, float)) and math.isfinite(auc) and auc >= MIN_ROC_AUC,
                f"ROC-AUC {auc} < {MIN_ROC_AUC} sur le schéma {name}",
            )

        final = fit_logistic([o.signal for o in observations], [o.label for o in observations])
        positives = sum(1 for o in observations if o.label == 1)
        return {
            "coefficients": {"intercept": final.intercept, "top10_mean": final.coefficient},
            "training": {
                "sample_count": len(observations),
                "positive_count": positives,
                "negative_count": len(observations) - positives,
                "negative_families": sorted({o.category for o in observations if o.label == 0}),
                "dataset_hash": dataset_fingerprint(observations),
            },
            "validation": {
                name: {k: v for k, v in m.items() if k != "out_of_fold_probabilities"}
                for name, m in validation.items()
            },
            "out_of_fold_probabilities": {
                name: m.get("out_of_fold_probabilities", {}) for name, m in validation.items()
            },
            "criteria": {"passed": verdict.passed, "failures": verdict.failures},
        }

    def _print_report(self, report: dict) -> None:
        training = report["training"]
        self.stdout.write(
            f"Jeu : {training['sample_count']} questions "
            f"({training['positive_count']}+ / {training['negative_count']}−), "
            f"familles négatives {training['negative_families']}, "
            f"empreinte {training['dataset_hash'][:12]}…"
        )
        header = (
            f"{'schéma':<24}{'ROC-AUC':>9}{'PR-AUC':>9}{'Brier':>8}{'préc.':>8}"
            f"{'rappel':>8}{'spéc.':>8}{'équil.':>8}{'FP':>5}{'FN':>5}"
        )
        self.stdout.write(header)
        for name, m in report["validation"].items():
            if not m.get("evaluable", True):
                self.stdout.write(f"{name:<24}   NON ÉVALUABLE — {m['reason']}")
                continue
            self.stdout.write(
                f"{name:<24}{m['roc_auc']:>9.3f}{m['pr_auc']:>9.3f}{m['brier']:>8.3f}"
                f"{m['precision']:>8.3f}{m['recall']:>8.3f}{m['specificity']:>8.3f}"
                f"{m['balanced_accuracy']:>8.3f}{m['false_positives']:>5}"
                f"{m['false_negatives']:>5}"
            )
            st = m.get("signal_threshold")
            if st:
                self.stdout.write(
                    f"    seuil équivalent en unité de signal : moyenne {st['mean']:.4f} "
                    f"σ {st['stddev']:.4f} étendue [{st['min']:.4f} ; {st['max']:.4f}]"
                )
            if m.get("false_positives_by_category"):
                self.stdout.write(
                    f"    faux positifs par famille : {m['false_positives_by_category']}"
                )
        coefficients = report["coefficients"]
        self.stdout.write(
            f"Calibrateur final : p = sigmoid({coefficients['intercept']:.4f} + "
            f"{coefficients['top10_mean']:.4f} × top10_mean)"
        )

    def _persist(self, corpus, report: dict, *, provider, model, dimension) -> None:
        from apps.corpus.models import DomainCorpusAdmissionCalibration

        with transaction.atomic():
            previous = list(
                DomainCorpusAdmissionCalibration.objects.select_for_update()
                .filter(corpus=corpus).order_by("-version")
            )
            next_version = (previous[0].version + 1) if previous else 1
            # Désactivation AVANT insertion : la contrainte n'autorise qu'une active.
            DomainCorpusAdmissionCalibration.objects.filter(
                corpus=corpus, is_active=True
            ).update(is_active=False)
            row = DomainCorpusAdmissionCalibration.objects.create(
                corpus=corpus,
                version=next_version,
                is_active=True,
                signal=SIGNAL_CORPUS_VECTOR_TOP10_MEAN,
                calibrator_type=CALIBRATOR_LOGISTIC,
                embedding_provider=provider,
                embedding_model=model,
                embedding_dimension=int(dimension),
                coefficients=report["coefficients"],
                training=report["training"],
                validation=report["validation"],
                dataset_hash=report["training"]["dataset_hash"],
            )
        self.stdout.write(
            f"Persisté : calibration v{row.version} (format {CALIBRATION_SCHEMA_VERSION}) "
            f"pour {corpus.slug}. Les versions précédentes sont conservées, désactivées."
        )
        self.stdout.write(
            "Rollback : réactiver la version précédente "
            "(aucune réindexation, aucune perte de données)."
        )
