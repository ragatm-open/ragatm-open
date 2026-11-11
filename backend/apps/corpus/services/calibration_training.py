"""Entraînement et validation d'une calibration d'admission — code PUR.

Aucune dépendance Django, aucune I/O, aucune bibliothèque numérique : ce module doit
pouvoir être lu, testé et rejoué à la main. La régression logistique à une variable
tient en quinze lignes de descente de gradient ; l'importer d'une bibliothèque absente
du projet coûterait plus qu'elle ne rapporterait.

Ce qui est validé ici décide si une calibration a le droit d'être persistée. Les seuils
d'acceptation sont donc délibérément exigeants : une calibration médiocre ne dégrade pas
« un peu » le retrieval, elle décide de la participation entière d'une source.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

#: Critères MINIMAUX de persistance. En dessous, la calibration est refusée : mieux vaut
#: un corpus non calibré — donc explicitement rejeté par la porte, et visible — qu'un
#: corpus filtré par un modèle qu'on sait mauvais.
MIN_ROC_AUC = 0.90
MIN_PR_AUC = 0.90
MIN_RECALL = 0.80
MIN_SPECIFICITY = 0.80
MIN_BALANCED_ACCURACY = 0.80
MAX_BRIER = 0.15
MIN_POSITIVES = 20
MIN_NEGATIVES = 20
MIN_NEGATIVE_FAMILIES = 3


@dataclass(frozen=True)
class Observation:
    """Une question annotée et son signal mesuré. Aucun texte n'est conservé."""

    question_id: str
    category: str
    label: int
    signal: float
    #: Empreinte du libellé, pour l'identité du jeu — jamais le libellé lui-même.
    text_digest: str = ""
    #: Document dominant du top-10 vectoriel, pour le découpage par groupe.
    dominant_document_id: str | None = None


def dataset_fingerprint(observations: Sequence[Observation]) -> str:
    """Empreinte STABLE du jeu annoté : identités, catégories, labels, digests.

    Ne contient aucun texte lisible. Deux calibrations portant la même empreinte sont
    comparables ; deux empreintes différentes interdisent de comparer leurs métriques.
    """
    payload = "\n".join(
        f"{o.question_id}|{o.category}|{int(o.label)}|{o.text_digest}"
        for o in sorted(observations, key=lambda o: o.question_id)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Régression logistique à une variable
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LogisticFit:
    """Coefficients dans l'unité NATIVE du signal (pas standardisée).

    La standardisation sert l'optimisation ; la persister obligerait à stocker aussi
    moyenne et écart-type du jeu d'entraînement, et à les réappliquer à l'exécution.
    On dé-standardise ici, une fois : la formule persistée est directement
    ``p = sigmoid(intercept + coefficient × signal)``.
    """

    intercept: float
    coefficient: float

    def probability(self, x: float) -> float:
        z = max(-60.0, min(60.0, self.intercept + self.coefficient * float(x)))
        return 1.0 / (1.0 + math.exp(-z))


def fit_logistic(
    xs: Sequence[float], ys: Sequence[int], *, iterations: int = 20000,
    learning_rate: float = 0.5, l2: float = 0.01,
) -> LogisticFit:
    """Descente de gradient sur le signal standardisé, puis retour à l'unité native.

    La régularisation L2 faible n'est pas cosmétique : à quelques dizaines
    d'observations et une seule variable, un jeu presque séparable pousse le coefficient
    vers l'infini et rend les probabilités binaires — donc le seuil sans effet.
    """
    n = len(xs)
    if n == 0 or len(ys) != n:
        raise ValueError("fit_logistic: jeu vide ou labels désalignés")
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / n
    sd = math.sqrt(var) or 1.0
    z = [(x - mean) / sd for x in xs]

    w = b = 0.0
    for _ in range(iterations):
        gw = gb = 0.0
        for zi, yi in zip(z, ys):
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, w * zi + b))))
            gw += (p - yi) * zi
            gb += p - yi
        w -= learning_rate * (gw / n + l2 * w)
        b -= learning_rate * gb / n
    # p = sigmoid(w·(x-mean)/sd + b) = sigmoid((b - w·mean/sd) + (w/sd)·x)
    return LogisticFit(intercept=b - w * mean / sd, coefficient=w / sd)


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------
def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """AUC par la statistique de Mann-Whitney, avec rangs moyens sur les ex æquo."""
    pairs = sorted(zip(scores, labels))
    ranks = [0.0] * len(pairs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = average
        i = j + 1
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return float("nan")
    rank_sum = sum(ranks[k] for k in range(len(pairs)) if pairs[k][1] == 1)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def pr_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    positives = sum(labels)
    if not positives:
        return float("nan")
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    tp = fp = 0
    previous_recall = 0.0
    area = 0.0
    for i in order:
        if labels[i] == 1:
            tp += 1
        else:
            fp += 1
        recall = tp / positives
        precision = tp / (tp + fp)
        area += (recall - previous_recall) * precision
        previous_recall = recall
    return area


def brier(probabilities: Sequence[float], labels: Sequence[int]) -> float:
    if not labels:
        return float("nan")
    return sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(labels)


def confusion(predictions: Sequence[bool], labels: Sequence[int]) -> dict:
    tp = sum(1 for p, y in zip(predictions, labels) if p and y)
    fp = sum(1 for p, y in zip(predictions, labels) if p and not y)
    tn = sum(1 for p, y in zip(predictions, labels) if not p and not y)
    fn = sum(1 for p, y in zip(predictions, labels) if not p and y)
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    specificity = tn / (tn + fp) if tn + fp else float("nan")
    balanced = (
        (recall + specificity) / 2
        if not (math.isnan(recall) or math.isnan(specificity))
        else float("nan")
    )
    return {
        "true_positives": tp, "false_positives": fp,
        "true_negatives": tn, "false_negatives": fn,
        "precision": precision, "recall": recall,
        "specificity": specificity, "balanced_accuracy": balanced,
    }


# ---------------------------------------------------------------------------
# Découpages
# ---------------------------------------------------------------------------
def stratified_folds(observations: Sequence[Observation], k: int = 5) -> list[list[int]]:
    positives = [i for i, o in enumerate(observations) if o.label == 1]
    negatives = [i for i, o in enumerate(observations) if o.label == 0]
    folds: list[list[int]] = [[] for _ in range(k)]
    for j, i in enumerate(positives):
        folds[j % k].append(i)
    for j, i in enumerate(negatives):
        folds[j % k].append(i)
    return [f for f in folds if f]


def category_folds(observations: Sequence[Observation]) -> list[list[int]]:
    """Un pli par catégorie : le test le plus SÉVÈRE.

    Il retire une famille entière de l'entraînement — un seuil qui survit à cela ne doit
    rien à la composition du jeu.
    """
    groups: dict[str, list[int]] = {}
    for i, o in enumerate(observations):
        groups.setdefault(o.category, []).append(i)
    return [groups[c] for c in sorted(groups)]


def group_folds(observations: Sequence[Observation], k: int = 5) -> list[list[int]]:
    """Plis par document dominant : empêche un même document d'entraîner ET d'évaluer."""
    groups: dict[str, list[int]] = {}
    for i, o in enumerate(observations):
        groups.setdefault(o.dominant_document_id or f"__none__{i}", []).append(i)
    folds: list[list[int]] = [[] for _ in range(k)]
    for members in sorted(groups.values(), key=len, reverse=True):
        folds.sort(key=len)
        folds[0].extend(members)
    return [f for f in folds if f]


# ---------------------------------------------------------------------------
# Validation croisée
# ---------------------------------------------------------------------------
def _signal_threshold(fit: LogisticFit, probability_threshold: float) -> float:
    """Seuil ÉQUIVALENT en unité de signal — pour lire la stabilité, pas pour l'appliquer.

    ``p = t`` ⇔ ``intercept + coefficient·x = logit(t)``. Le produit appliqué reste la
    probabilité ; ce nombre sert seulement à comparer les plis entre eux.
    """
    t = min(1 - 1e-12, max(1e-12, float(probability_threshold)))
    logit = math.log(t / (1 - t))
    if fit.coefficient == 0:
        return float("nan")
    return (logit - fit.intercept) / fit.coefficient


def cross_validate(
    observations: Sequence[Observation],
    folds: Sequence[Sequence[int]],
    *,
    probability_threshold: float,
    fitter: Callable[[Sequence[float], Sequence[int]], LogisticFit] = fit_logistic,
) -> dict:
    """Probabilités HORS PLI, puis métriques agrégées dessus.

    Les métriques ne sont jamais calculées sur le jeu d'entraînement : une régression à
    deux paramètres sur un jeu presque séparable y afficherait un AUC flatteur qui ne
    dit rien de son comportement sur une question nouvelle.
    """
    xs = [o.signal for o in observations]
    ys = [o.label for o in observations]
    out_of_fold: list[float | None] = [None] * len(observations)
    signal_thresholds: list[float] = []

    for fold in folds:
        test = set(fold)
        train = [i for i in range(len(observations)) if i not in test]
        if len({ys[i] for i in train}) < 2:
            # Une classe absente de l'entraînement : ce pli n'apprend rien, on ne
            # fabrique pas de prédiction pour autant.
            continue
        fit = fitter([xs[i] for i in train], [ys[i] for i in train])
        signal_thresholds.append(_signal_threshold(fit, probability_threshold))
        for i in fold:
            out_of_fold[i] = fit.probability(xs[i])

    kept = [i for i in range(len(observations)) if out_of_fold[i] is not None]
    probabilities = [float(out_of_fold[i]) for i in kept]
    labels = [ys[i] for i in kept]
    if len(set(labels)) < 2:
        raise ValueError("validation croisée impossible : une seule classe évaluée")

    metrics = confusion([p >= probability_threshold for p in probabilities], labels)
    metrics.update(
        roc_auc=roc_auc(probabilities, labels),
        pr_auc=pr_auc(probabilities, labels),
        brier=brier(probabilities, labels),
        evaluated_count=len(kept),
        fold_count=len(signal_thresholds),
    )
    finite = [t for t in signal_thresholds if math.isfinite(t)]
    if finite:
        mean = sum(finite) / len(finite)
        metrics["signal_threshold"] = {
            "mean": mean,
            "min": min(finite),
            "max": max(finite),
            "stddev": math.sqrt(sum((t - mean) ** 2 for t in finite) / len(finite)),
        }
    false_positives: dict[str, int] = {}
    for i in kept:
        if out_of_fold[i] >= probability_threshold and ys[i] == 0:
            category = observations[i].category
            false_positives[category] = false_positives.get(category, 0) + 1
    metrics["false_positives_by_category"] = false_positives
    metrics["out_of_fold_probabilities"] = {
        observations[i].question_id: float(out_of_fold[i]) for i in kept
    }
    return metrics


# ---------------------------------------------------------------------------
# Critères de persistance
# ---------------------------------------------------------------------------
@dataclass
class CriteriaVerdict:
    passed: bool = True
    failures: list[str] = field(default_factory=list)

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.passed = False
            self.failures.append(message)


def _at_least(value: float, minimum: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value >= minimum


def evaluate_criteria(observations: Sequence[Observation], metrics: dict) -> CriteriaVerdict:
    """Décide si une calibration a le droit d'être persistée. Le doute REFUSE."""
    verdict = CriteriaVerdict()
    positives = sum(1 for o in observations if o.label == 1)
    negatives = len(observations) - positives
    families = {o.category for o in observations if o.label == 0}

    verdict.require(positives >= MIN_POSITIVES, f"positifs {positives} < {MIN_POSITIVES}")
    verdict.require(negatives >= MIN_NEGATIVES, f"négatifs {negatives} < {MIN_NEGATIVES}")
    verdict.require(
        len(families) >= MIN_NEGATIVE_FAMILIES,
        f"familles négatives {len(families)} < {MIN_NEGATIVE_FAMILIES}",
    )
    verdict.require(_at_least(metrics.get("roc_auc"), MIN_ROC_AUC),
                    f"ROC-AUC {metrics.get('roc_auc')} < {MIN_ROC_AUC}")
    verdict.require(_at_least(metrics.get("pr_auc"), MIN_PR_AUC),
                    f"PR-AUC {metrics.get('pr_auc')} < {MIN_PR_AUC}")
    verdict.require(_at_least(metrics.get("recall"), MIN_RECALL),
                    f"rappel {metrics.get('recall')} < {MIN_RECALL}")
    verdict.require(_at_least(metrics.get("specificity"), MIN_SPECIFICITY),
                    f"spécificité {metrics.get('specificity')} < {MIN_SPECIFICITY}")
    verdict.require(_at_least(metrics.get("balanced_accuracy"), MIN_BALANCED_ACCURACY),
                    f"exactitude équilibrée {metrics.get('balanced_accuracy')} < {MIN_BALANCED_ACCURACY}")
    brier_score = metrics.get("brier")
    verdict.require(
        isinstance(brier_score, (int, float)) and math.isfinite(brier_score)
        and brier_score <= MAX_BRIER,
        f"Brier {brier_score} > {MAX_BRIER}",
    )
    return verdict
