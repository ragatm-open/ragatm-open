"""Métriques du benchmark `tabular_aggregation`.

La métrique principale porte sur le **résultat calculé**, jamais sur la récupération
d'un chunk. Un système peut retrouver la bonne table et se tromper de somme : le cas
est faux. Il peut aussi calculer juste à partir d'un chemin inattendu : le cas est bon.

``Recall@k`` et ``MRR`` restent utilisables en **diagnostic** (le moteur a-t-il seulement
vu les bonnes lignes ?), jamais comme critère de réussite d'un cas d'agrégation — c'est
un NO-GO explicite du protocole (§32).

Module pur : aucun LLM, aucun retrieval, aucun accès base.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.rag_core.benchmark import tabular_aggregation_schema as sch
from apps.rag_core.benchmark import tabular_types as tt

#: Garde-fou contre une division par zéro en erreur relative.
_EPSILON = Decimal("1e-12")


@dataclass(frozen=True)
class Verdict:
    """Résultat d'une comparaison, avec de quoi diagnostiquer un échec."""

    correct: bool
    comparison: str
    detail: str = ""
    absolute_error: Decimal | None = None
    relative_error: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"correct": self.correct, "comparison": self.comparison}
        if self.detail:
            d["detail"] = self.detail
        if self.absolute_error is not None:
            d["absolute_error"] = str(self.absolute_error)
        if self.relative_error is not None:
            d["relative_error"] = str(self.relative_error)
        return d


def _as_decimal(value: Any) -> Decimal | None:
    """Décimal comparable, ou ``None`` si la valeur n'en est pas un.

    Les chaînes sont d'abord lues comme **canoniques** (``0.001`` est une tolérance
    écrite par nous, pas une cellule ambiguë), puis en dernier recours avec l'analyse
    tolérante — pour ne pas pénaliser un système évalué qui répondrait « 1 234,56 ».
    """
    if isinstance(value, str):
        try:
            return tt.parse_canonical_decimal(value)
        except tt.TabularTypeError:
            pass
    try:
        return tt.normalize_decimal(value)
    except (tt.TabularTypeError, InvalidOperation):
        return None


def exact_match(predicted: Any, expected: Any) -> Verdict:
    """Égalité stricte après normalisation textuelle (casse et espaces neutralisés)."""
    ok = tt.fold_for_match(predicted) == tt.fold_for_match(expected)
    return Verdict(correct=ok, comparison=sch.CMP_EXACT,
                   detail="" if ok else f"attendu {expected!r}, obtenu {predicted!r}")


def numeric_exact_match(predicted: Any, expected: Any) -> Verdict:
    """Égalité **décimale** : ``12.50`` et ``12.5`` sont le même montant."""
    p, e = _as_decimal(predicted), _as_decimal(expected)
    if p is None or e is None:
        return Verdict(correct=False, comparison=sch.CMP_EXACT_DECIMAL,
                       detail=f"valeur non numérique (attendu {expected!r}, obtenu {predicted!r})")
    ok = p == e
    return Verdict(correct=ok, comparison=sch.CMP_EXACT_DECIMAL,
                   detail="" if ok else f"attendu {e}, obtenu {p}",
                   absolute_error=abs(p - e))


def numeric_tolerance(predicted: Any, expected: Any, *, tolerance: Any) -> Verdict:
    """Écart absolu borné. À n'employer que si le domaine le justifie (§30)."""
    p, e, tol = _as_decimal(predicted), _as_decimal(expected), _as_decimal(tolerance)
    if p is None or e is None or tol is None:
        return Verdict(correct=False, comparison=sch.CMP_ABSOLUTE_TOLERANCE,
                       detail="valeur ou tolérance non numérique")
    err = abs(p - e)
    return Verdict(correct=err <= tol, comparison=sch.CMP_ABSOLUTE_TOLERANCE,
                   detail="" if err <= tol else f"écart {err} > tolérance {tol}",
                   absolute_error=err)


def relative_error_match(predicted: Any, expected: Any, *, tolerance: Any) -> Verdict:
    """Erreur relative — pertinente sur les ordres de grandeur très différents."""
    p, e, tol = _as_decimal(predicted), _as_decimal(expected), _as_decimal(tolerance)
    if p is None or e is None or tol is None:
        return Verdict(correct=False, comparison=sch.CMP_RELATIVE_TOLERANCE,
                       detail="valeur ou tolérance non numérique")
    denom = max(abs(e), _EPSILON)
    rel = abs(p - e) / denom
    return Verdict(correct=rel <= tol, comparison=sch.CMP_RELATIVE_TOLERANCE,
                   detail="" if rel <= tol else f"erreur relative {rel} > {tol}",
                   absolute_error=abs(p - e), relative_error=rel)


def set_match(predicted: Any, expected: Any) -> Verdict:
    """Égalité d'ensembles : l'ordre ne compte pas, les doublons non plus."""
    p = {tt.fold_for_match(v) for v in _iterable(predicted)}
    e = {tt.fold_for_match(v) for v in _iterable(expected)}
    ok = p == e
    detail = ""
    if not ok:
        detail = f"manquants={sorted(e - p)} · en trop={sorted(p - e)}"
    return Verdict(correct=ok, comparison=sch.CMP_SET, detail=detail)


def ordered_match(predicted: Any, expected: Any) -> Verdict:
    """Égalité de séquences : l'ordre fait partie de la réponse (classements, top-k)."""
    p = [tt.fold_for_match(v) for v in _iterable(predicted)]
    e = [tt.fold_for_match(v) for v in _iterable(expected)]
    ok = p == e
    return Verdict(correct=ok, comparison=sch.CMP_ORDERED,
                   detail="" if ok else f"attendu {e}, obtenu {p}")


def row_identifier_match(predicted: Any, expected: Any) -> Verdict:
    """Comparaison d'un identifiant de ligne (``argmin``/``argmax``).

    Toutes les colonnes d'identité attendues doivent correspondre ; une clé manquante
    est un échec, jamais une correspondance partielle tolérée.
    """
    if not isinstance(predicted, dict) or not isinstance(expected, dict):
        return Verdict(correct=False, comparison=sch.CMP_EXACT,
                       detail="row_identifier attend un mapping colonne -> valeur")
    missing = [k for k in expected if k not in predicted]
    if missing:
        return Verdict(correct=False, comparison=sch.CMP_EXACT,
                       detail=f"colonnes d'identité absentes : {missing}")
    bad = [k for k, v in expected.items()
           if tt.fold_for_match(predicted[k]) != tt.fold_for_match(v)]
    return Verdict(correct=not bad, comparison=sch.CMP_EXACT,
                   detail="" if not bad else f"colonnes divergentes : {bad}")


def evaluate(predicted: Any, expected: sch.ExpectedResult) -> Verdict:
    """Applique la comparaison **déclarée par le cas**, sans en choisir une autre."""
    cmp_ = expected.comparison
    if expected.type == sch.RESULT_ROW_IDENTIFIER:
        return row_identifier_match(predicted, expected.value)
    if cmp_ == sch.CMP_EXACT_DECIMAL:
        return numeric_exact_match(predicted, expected.value)
    if cmp_ == sch.CMP_ABSOLUTE_TOLERANCE:
        return numeric_tolerance(predicted, expected.value, tolerance=expected.tolerance)
    if cmp_ == sch.CMP_RELATIVE_TOLERANCE:
        return relative_error_match(predicted, expected.value, tolerance=expected.tolerance)
    if cmp_ == sch.CMP_SET:
        return set_match(predicted, expected.value)
    if cmp_ == sch.CMP_ORDERED:
        return ordered_match(predicted, expected.value)
    if expected.type == sch.RESULT_INTEGER:
        return numeric_exact_match(predicted, expected.value)
    return exact_match(predicted, expected.value)


def aggregate_scores(verdicts: list[Verdict]) -> dict[str, Any]:
    """Score global : proportion de cas dont le **résultat** est correct."""
    total = len(verdicts)
    correct = sum(1 for v in verdicts if v.correct)
    by_cmp: dict[str, dict[str, int]] = {}
    for v in verdicts:
        slot = by_cmp.setdefault(v.comparison, {"total": 0, "correct": 0})
        slot["total"] += 1
        slot["correct"] += int(v.correct)
    return {
        "cases": total,
        "correct": correct,
        "accuracy": (Decimal(correct) / Decimal(total)) if total else Decimal(0),
        "by_comparison": by_cmp,
    }


def _iterable(value: Any):
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]
