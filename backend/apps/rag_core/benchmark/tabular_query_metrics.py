"""Métriques du benchmark `tabular_query`.

Ce que ces métriques cherchent à rendre visible tient en une phrase : **une réponse
fausse coûte plus cher qu'un refus.** Un moteur qui refuse trop est pénible ; un moteur
qui répond juste sur la mauvaise colonne, ou qui invente un total à partir d'une formule
non calculée, est dangereux — parce que rien, dans sa réponse, ne signale l'erreur.

D'où la structure :

* les métriques de **chemin** (document, feuille, table, colonnes, opération, filtres)
  disent *où* le moteur s'est trompé, pas seulement *s'il* s'est trompé ;
* `false_answer_rate` isole le mode d'échec critique — avoir répondu `ok` alors que la
  réponse n'était pas la bonne, refus attendu compris ;
* `clarification_precision` / `clarification_recall` mesurent la qualité du refus comme
  une capacité, pas comme un pis-aller.

Aucune comparaison numérique ne passe par un `float`. Les montants sont comparés en
`Decimal`, avec la tolérance **déclarée par le cas** — jamais une tolérance choisie
après coup pour faire passer une mesure.

Module pur : aucun accès base, aucun LLM, aucune dépendance au moteur tabulaire ni à
`tabular_reference_executor`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

#: Libellés des métriques exigées, dans l'ordre du rapport.
METRIC_LABELS: dict[str, str] = {
    "document_accuracy": "document correct",
    "sheet_accuracy": "feuille correcte",
    "table_accuracy": "table (plage) correcte",
    "column_accuracy": "colonnes correctes",
    "operation_accuracy": "opération correcte",
    "filter_accuracy": "filtres corrects",
    "plan_exact_match": "plan exact (opération + filtres + colonnes)",
    "numeric_accuracy": "valeur numérique correcte",
    "clarification_precision": "précision des demandes de clarification",
    "clarification_recall": "rappel des demandes de clarification",
    "false_answer_rate": "taux de fausses réponses",
    "unsafe_query_rejection_rate": "requêtes hostiles neutralisées",
    "determinism_rate": "déterminisme (5 exécutions identiques)",
    "status_accuracy": "statut correct (ok / refus attendu)",
    "answer_accuracy": "réponse correcte (statut ET valeur)",
}

CLARIFICATION = "clarification_required"


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def canonical(value: Any) -> Any:
    """Forme canonique comparable d'une valeur observée.

    Un `Decimal` est normalisé (`12.50` et `12.5` sont le même montant), une date passe
    par ISO, une liste est canonisée élément par élément. Jamais de `float` : la seule
    chose qu'un `float` apporterait ici est une inégalité au dernier bit.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Traversée d'une bibliothèque qui impose le flottant : on repasse par `repr`,
        # la plus courte écriture qui reconstruit le même double.
        return format(Decimal(repr(value)).normalize(), "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    return str(value)


def as_decimal(value: Any) -> Decimal | None:
    """`Decimal` si la valeur en est un, `None` sinon. Ne devine aucune locale."""
    if value is None or isinstance(value, (list, tuple, dict)):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def fold(text: Any) -> str:
    """Repli de comparaison textuelle : casse et espaces neutralisés, accents gardés.

    Les accents sont conservés à dessein : « payé » et « paye » sont deux en-têtes
    différents, et les confondre masquerait précisément une erreur de résolution.
    """
    return " ".join(str(text or "").strip().lower().split())


def normalize_filters(filters: Any) -> list[tuple[str, str, str]]:
    """Filtres sous forme comparable et ORDONNÉE-indépendante.

    Un plan qui applique les mêmes prédicats dans un autre ordre est le même plan :
    la conjonction est commutative. On trie donc plutôt que d'exiger un ordre.
    """
    out: list[tuple[str, str, str]] = []
    for f in filters or ():
        if not isinstance(f, dict):
            continue
        value = f.get("value")
        if value is None and f.get("values"):
            value = ",".join(str(v) for v in f["values"])
        out.append((fold(f.get("column")), fold(f.get("operator")), fold(value)))
    return sorted(out)


# --------------------------------------------------------------------------- #
# Évaluation d'un cas
# --------------------------------------------------------------------------- #
@dataclass
class CaseEvaluation:
    """Verdict complet d'un cas : chaque dimension mesurée séparément.

    Une dimension vaut `None` quand le cas ne la déclare pas — un cas qui n'attend
    aucune valeur numérique ne doit ni gonfler ni pénaliser `numeric_accuracy`.
    """

    id: str
    category: str
    expected_status: str
    expected_code: str
    observed_status: str
    observed_code: str

    status_ok: bool = False
    value_ok: bool | None = None
    document_ok: bool | None = None
    sheet_ok: bool | None = None
    table_ok: bool | None = None
    columns_ok: bool | None = None
    operation_ok: bool | None = None
    filters_ok: bool | None = None
    numeric_ok: bool | None = None
    safe: bool | None = None
    deterministic: bool = True
    latency_ms: float = 0.0
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def correct(self) -> bool:
        """Un cas est correct si le statut attendu ET la valeur attendue sont obtenus."""
        return self.status_ok and (self.value_ok is not False)

    @property
    def plan_exact(self) -> bool | None:
        parts = [self.operation_ok, self.filters_ok, self.columns_ok]
        if all(p is None for p in parts):
            return None
        return all(p is not False for p in parts) and self.operation_ok is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "expected_status": self.expected_status,
            "expected_code": self.expected_code,
            "observed_status": self.observed_status,
            "observed_code": self.observed_code,
            "correct": self.correct,
            "status_ok": self.status_ok,
            "value_ok": self.value_ok,
            "document_ok": self.document_ok,
            "sheet_ok": self.sheet_ok,
            "table_ok": self.table_ok,
            "columns_ok": self.columns_ok,
            "operation_ok": self.operation_ok,
            "filters_ok": self.filters_ok,
            "numeric_ok": self.numeric_ok,
            "plan_exact": self.plan_exact,
            "safe": self.safe,
            "deterministic": self.deterministic,
            "latency_ms": round(self.latency_ms, 3),
            "reason": self.reason,
            "detail": self.detail,
        }


def compare_value(expected: Any, observed: Any, *, tolerance: Any) -> tuple[bool, str]:
    """Compare une valeur attendue et une valeur observée. Jamais d'égalité flottante.

    Trois régimes, choisis par la FORME de la valeur attendue et non par commodité :
    une liste se compare dans l'ordre (un classement est une réponse ordonnée), un
    nombre en `Decimal` avec la tolérance du cas, le reste textuellement.
    """
    if isinstance(expected, list):
        obs = canonical(observed)
        if not isinstance(obs, list):
            return False, f"séquence attendue, obtenu {obs!r}"
        exp = [fold(v) for v in expected]
        got = [fold(v) for v in obs]
        return (exp == got, "" if exp == got else f"attendu {exp}, obtenu {got}")

    e = as_decimal(expected)
    o = as_decimal(canonical(observed))
    if e is not None and o is not None:
        tol = as_decimal(tolerance) or Decimal(0)
        gap = abs(e - o)
        return (gap <= tol, "" if gap <= tol else f"écart {gap} > tolérance {tol}")

    exp, got = fold(expected), fold(canonical(observed))
    return (exp == got, "" if exp == got else f"attendu {exp!r}, obtenu {got!r}")


def evaluate_case(case: dict[str, Any], observed: dict[str, Any], *,
                  deterministic: bool, latency_ms: float) -> CaseEvaluation:
    """Confronte un cas à son observation. Ne calcule aucune valeur attendue."""
    ev = CaseEvaluation(
        id=case["id"], category=case["category"],
        expected_status=case["expected_status"],
        expected_code=case.get("expected_code") or "",
        observed_status=observed.get("status") or "",
        observed_code=observed.get("code") or "",
        deterministic=deterministic, latency_ms=latency_ms)

    # -- statut et code -----------------------------------------------------
    ev.status_ok = ev.observed_status == ev.expected_status
    if ev.status_ok and ev.expected_code:
        # Un refus n'est correct que s'il refuse pour la BONNE raison : un
        # `column_not_found` là où un `formula_result_unavailable` était attendu
        # signale que le moteur s'est arrêté au mauvais endroit.
        ev.status_ok = ev.observed_code == ev.expected_code
    if not ev.status_ok:
        ev.reason = (f"statut/code attendu {ev.expected_status}"
                     f"{'/' + ev.expected_code if ev.expected_code else ''}, "
                     f"obtenu {ev.observed_status}"
                     f"{'/' + ev.observed_code if ev.observed_code else ''}")

    # -- chemin -------------------------------------------------------------
    if case.get("document"):
        ev.document_ok = fold(observed.get("document")) == fold(case["document"])
    if case.get("expected_sheet"):
        ev.sheet_ok = fold(observed.get("sheet")) == fold(case["expected_sheet"])
    if case.get("expected_table"):
        ev.table_ok = fold(observed.get("table")) == fold(case["expected_table"])
    if case.get("expected_columns"):
        exp = sorted(fold(c) for c in case["expected_columns"])
        got = sorted(fold(c) for c in observed.get("columns") or ())
        ev.columns_ok = exp == got
        ev.detail["columns"] = {"expected": exp, "observed": got}
    if case.get("expected_operation"):
        ev.operation_ok = fold(observed.get("operation")) == fold(case["expected_operation"])
        # Les filtres ne se mesurent que là où un plan était attendu : sur un cas dont
        # la bonne issue est un refus, « zéro filtre » ne veut rien dire.
        exp_f = normalize_filters(case.get("expected_filters"))
        got_f = normalize_filters(observed.get("filters"))
        ev.filters_ok = exp_f == got_f
        if not ev.filters_ok:
            ev.detail["filters"] = {"expected": exp_f, "observed": got_f}

    # -- valeur -------------------------------------------------------------
    if case["expected_status"] == "ok" and case.get("expected_value") is not None:
        ok, why = compare_value(case["expected_value"], observed.get("value"),
                                tolerance=case.get("tolerance") or "0")
        ev.value_ok = ok and ev.observed_status == "ok"
        if not ev.value_ok:
            ev.reason = ev.reason or (why or "aucune valeur produite")
        if as_decimal(case["expected_value"]) is not None:
            ev.numeric_ok = ev.value_ok

    # -- sécurité -----------------------------------------------------------
    if case["category"] == "security":
        ev.safe = _is_safe(observed) and ev.status_ok
        if not ev.safe and not ev.reason:
            ev.reason = "la charge hostile n'a pas été neutralisée comme attendu"

    return ev


def _is_safe(observed: dict[str, Any]) -> bool:
    """Aucune charge n'a atteint le texte SQL, et une seule instruction a été produite.

    C'est la propriété structurelle du compilateur : les valeurs voyagent en paramètres,
    les colonnes deviennent des alias positionnels. On la vérifie plutôt que de la
    supposer — une propriété non testée n'est pas une propriété.
    """
    sql = observed.get("sql") or ""
    if not sql:
        return True  # refusé avant toute compilation : rien n'a pu s'exécuter
    if ";" in sql:
        return False
    return all(str(p) not in sql for p in observed.get("params") or ())


# --------------------------------------------------------------------------- #
# Agrégation
# --------------------------------------------------------------------------- #
def _rate(hits: int, support: int) -> dict[str, Any]:
    """Une métrique sans base n'est pas nulle : elle est absente. D'où `None`."""
    return {"value": (hits / support) if support else None,
            "hits": hits, "support": support}


def _tally(evaluations: list[CaseEvaluation], attr: str) -> dict[str, Any]:
    considered = [e for e in evaluations if getattr(e, attr) is not None]
    return _rate(sum(1 for e in considered if getattr(e, attr)), len(considered))


def percentile(values: list[float], q: float) -> float:
    """Percentile par rang, sans interpolation : reproductible sur petits effectifs."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def aggregate(evaluations: list[CaseEvaluation]) -> dict[str, Any]:
    """Synthèse. Chaque métrique porte sa base : un taux sans effectif ne se lit pas."""
    total = len(evaluations)

    # Clarification : classe positive = « le moteur devait demander à l'utilisateur ».
    tp = sum(1 for e in evaluations
             if e.expected_status == CLARIFICATION and e.observed_status == CLARIFICATION)
    fp = sum(1 for e in evaluations
             if e.expected_status != CLARIFICATION and e.observed_status == CLARIFICATION)
    fn = sum(1 for e in evaluations
             if e.expected_status == CLARIFICATION and e.observed_status != CLARIFICATION)

    # Fausse réponse : le moteur a dit « ok » et il avait tort — soit qu'un refus était
    # attendu, soit que le nombre rendu n'est pas le bon. C'est le mode d'échec que la
    # spécification classe critique, parce qu'il est invisible pour l'utilisateur.
    false_answers = sum(1 for e in evaluations
                        if e.observed_status == "ok" and not e.correct)

    plan_exact = [e for e in evaluations if e.plan_exact is not None]

    metrics = {
        "document_accuracy": _tally(evaluations, "document_ok"),
        "sheet_accuracy": _tally(evaluations, "sheet_ok"),
        "table_accuracy": _tally(evaluations, "table_ok"),
        "column_accuracy": _tally(evaluations, "columns_ok"),
        "operation_accuracy": _tally(evaluations, "operation_ok"),
        "filter_accuracy": _tally(evaluations, "filters_ok"),
        "plan_exact_match": _rate(sum(1 for e in plan_exact if e.plan_exact),
                                  len(plan_exact)),
        "numeric_accuracy": _tally(evaluations, "numeric_ok"),
        "clarification_precision": _rate(tp, tp + fp),
        "clarification_recall": _rate(tp, tp + fn),
        "false_answer_rate": _rate(false_answers, total),
        "unsafe_query_rejection_rate": _tally(evaluations, "safe"),
        "determinism_rate": _rate(sum(1 for e in evaluations if e.deterministic), total),
        "status_accuracy": _rate(sum(1 for e in evaluations if e.status_ok), total),
        "answer_accuracy": _rate(sum(1 for e in evaluations if e.correct), total),
    }

    by_category: dict[str, Any] = {}
    for e in evaluations:
        block = by_category.setdefault(
            e.category, {"cases": 0, "status_hits": 0, "answer_hits": 0})
        block["cases"] += 1
        block["status_hits"] += int(e.status_ok)
        block["answer_hits"] += int(e.correct)
    for block in by_category.values():
        block["status_accuracy"] = block["status_hits"] / block["cases"]
        block["answer_accuracy"] = block["answer_hits"] / block["cases"]

    latencies = [e.latency_ms for e in evaluations]
    return {
        "cases": total,
        "metrics": metrics,
        "by_category": by_category,
        "latency_ms": {"p50": percentile(latencies, 0.50),
                       "p95": percentile(latencies, 0.95)},
    }
