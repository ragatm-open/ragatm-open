"""Adaptateur : cas `tabular_aggregation` (oracle) → cas `tabular_query_*` (exécutable).

RagATM porte deux briques tabulaires dont les rôles doivent rester distincts :

- **`tabular_aggregation`** définit *ce qu'est une réponse correcte* — schéma, sémantique,
  vérité terrain calculée hors ligne depuis les octets du classeur ;
- **`tabular_query_*`** exécute le *moteur réel* et mesure ses réponses.

Sans passerelle, ils deviendraient deux benchmarks concurrents avec deux datasets
officiels divergents. Ce module est la passerelle, et il est **volontairement pauvre** :
il traduit un format en un autre, rien de plus.

Ce que l'adaptateur ne fait JAMAIS
----------------------------------

Il ne recalcule pas la vérité terrain, n'appelle pas le moteur, ne change pas
l'opération, ne choisit pas une autre colonne, ne tranche aucune ambiguïté, n'invente
aucun seuil et ne normalise pas autrement que l'oracle. La raison est unique : le
système évalué ne doit jamais être la source de ses propres résultats attendus. Un
adaptateur qui « corrigerait » un cas selon ce que le moteur sait faire transformerait
le benchmark en tautologie.

Quand la traduction perdrait du sens, elle **échoue** — jamais de repli silencieux.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from apps.rag_core.benchmark import tabular_aggregation_schema as sch
from apps.rag_core.benchmark.stable_ground_truth import sha256_hex

# --- Codes de refus (§17) --------------------------------------------------
ERR_UNSUPPORTED_QUERY_OPERATION = "unsupported_query_operation"
ERR_UNSUPPORTED_QUERY_FILTER = "unsupported_query_filter"
ERR_UNSUPPORTED_POPULATION_POLICY = "unsupported_population_policy"
ERR_UNSUPPORTED_RESULT_TYPE = "unsupported_result_type"
ERR_SEMANTIC_LOSS = "semantic_loss"
ERR_CAPABILITY_MISMATCH = "query_engine_capability_mismatch"
ERR_INVALID_SCHEMA_VERSION = "invalid_schema_version"

#: Opération oracle -> opération du plan moteur.
#:
#: `filtered_count` et `filtered_sum` n'existent pas côté moteur : le filtrage y est
#: porté par les prédicats du plan, pas par le nom de l'opération. La traduction est
#: donc `count_rows`/`sum` **plus des filtres** — sans perte, puisque le cas conserve
#: ses filtres. C'est le seul endroit où le nom change sans que le sens change.
OPERATION_MAP: dict[str, str] = {
    sch.OP_COUNT_ROWS: "count_rows",
    sch.OP_FILTERED_COUNT: "count_rows",
    sch.OP_SUM: "sum",
    sch.OP_FILTERED_SUM: "sum",
    sch.OP_AVERAGE: "average",
    sch.OP_MINIMUM: "minimum",
    sch.OP_MAXIMUM: "maximum",
    sch.OP_ARGMIN: "argmin",
    sch.OP_ARGMAX: "argmax",
}

#: Opérateur de filtre oracle -> opérateur du plan moteur.
#:
#: `before`/`after` n'existent pas côté moteur : une date y est comparée comme toute
#: valeur ordonnée. La traduction vers `less_than`/`greater_than` conserve exactement
#: la sémantique — l'oracle compare lui aussi des dates ISO par ordre lexical.
FILTER_MAP: dict[str, str] = {
    sch.F_EQ: "equals",
    sch.F_NEQ: "not_equals",
    sch.F_GT: "greater_than",
    sch.F_GTE: "greater_or_equal",
    sch.F_LT: "less_than",
    sch.F_LTE: "less_or_equal",
    sch.F_BETWEEN: "between",
    sch.F_IN: "in",
    sch.F_CONTAINS: "contains",
    sch.F_IS_EMPTY: "is_null",
    sch.F_IS_NOT_EMPTY: "is_not_null",
    sch.F_BEFORE: "less_than",
    sch.F_AFTER: "greater_than",
}

#: Opérateurs de l'oracle sans équivalent moteur. Nommés pour refuser utilement.
UNMAPPABLE_FILTERS: frozenset[str] = frozenset({sch.F_NOT_IN, sch.F_NOT_CONTAINS})

#: Types de résultat traduisibles.
SUPPORTED_RESULT_TYPES: frozenset[str] = frozenset({
    sch.RESULT_INTEGER, sch.RESULT_DECIMAL, sch.RESULT_DATE,
    sch.RESULT_BOOLEAN, sch.RESULT_STRING, sch.RESULT_ROW_IDENTIFIER,
})


class AdaptationError(ValueError):
    """Refus d'adaptation. Porte un ``code`` stable — jamais de repli silencieux."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class AdaptedCase:
    """Cas exécutable + empreinte sémantique + traçabilité vers l'oracle."""

    case: dict[str, Any]
    semantic_fingerprint: str
    oracle_case_id: str
    content_sha256: str


# --------------------------------------------------------------------------- #
# Empreinte sémantique (§16)
# --------------------------------------------------------------------------- #
def semantic_fingerprint(*, content_sha256: str, sheet_name: str, table_range: str,
                         data_row_start: int, data_row_end: int, operation: str,
                         target_column: str, identity_columns: tuple[str, ...],
                         filters: list[dict[str, Any]], result_type: str,
                         result_value: Any, population_policy: dict[str, Any]) -> str:
    """Empreinte du **sens** d'un cas, pas de sa forme JSON.

    Deux représentations différentes du même cas — clés réordonnées, filtres saisis dans
    un autre ordre — doivent produire la même empreinte ; deux cas qui diffèrent par un
    seuil, un opérateur ou une colonne doivent en produire de différentes. C'est ce qui
    permet d'affirmer que la traduction n'a rien perdu.
    """
    payload = json.dumps({
        "document": (content_sha256 or "").lower(),
        "table": [sch.normalize_sheet_name(sheet_name),
                  sch.normalize_cell_range(table_range), data_row_start, data_row_end],
        "operation": operation,
        "target": sch.normalize_header(target_column or ""),
        "identity": sorted(sch.normalize_header(c) for c in identity_columns),
        "filters": sorted(json.dumps(f, sort_keys=True, ensure_ascii=False)
                          for f in filters),
        "result": [result_type, _canonical_value(result_value)],
        "population": population_policy,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256_hex(payload)


def _canonical_value(value: Any) -> Any:
    """Forme canonique d'une valeur attendue — jamais de `float`, comme partout ailleurs."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return {k: _canonical_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(v) for v in value]
    return None if value is None else str(value)


# --------------------------------------------------------------------------- #
# Adaptation
# --------------------------------------------------------------------------- #
def adapt_aggregation_case(case: dict[str, Any], *, question: str = "",
                           document_filename: str = "",
                           category: str = "aggregation") -> AdaptedCase:
    """Traduit un cas oracle validé en cas exécutable par le harnais moteur.

    ``question`` permet de fournir l'énoncé en langue naturelle du niveau end-to-end ;
    à défaut, celui du cas oracle est repris tel quel. Aucune reformulation n'est faite
    ici : réécrire une question changerait ce que le benchmark mesure.
    """
    if case.get("schema_version") != sch.TABULAR_AGGREGATION_SCHEMA_VERSION:
        raise AdaptationError(
            ERR_INVALID_SCHEMA_VERSION,
            f"version {case.get('schema_version')!r} (attendu "
            f"{sch.TABULAR_AGGREGATION_SCHEMA_VERSION})")

    gt = case.get("ground_truth") or {}
    operation = case.get("operation") or (gt.get("aggregation") or {}).get("function")
    if operation not in OPERATION_MAP:
        raise AdaptationError(
            ERR_UNSUPPORTED_QUERY_OPERATION,
            f"opération {operation!r} sans équivalent dans le plan moteur")

    doc_ref = gt.get("document_ref") or {}
    sha = (doc_ref.get("content_sha256") or "").lower()
    if not sha:
        raise AdaptationError(ERR_SEMANTIC_LOSS,
                              "content_sha256 absent : l'identité canonique serait perdue")

    locator = gt.get("table_locator") or {}
    for field_name in ("sheet_name", "table_range", "data_row_start", "data_row_end"):
        if locator.get(field_name) in (None, ""):
            raise AdaptationError(
                ERR_SEMANTIC_LOSS, f"table_locator.{field_name} absent : la table ciblée "
                                   "ne serait plus vérifiable")

    population = gt.get("row_population_policy") or {}
    if population.get("include_rows") not in (None, sch.INCLUDE_DATA_ROWS):
        raise AdaptationError(
            ERR_UNSUPPORTED_POPULATION_POLICY,
            f"include_rows={population.get('include_rows')!r} non représentable côté moteur")

    expected = gt.get("expected_result") or {}
    result_type = expected.get("type")
    if result_type not in SUPPORTED_RESULT_TYPES:
        raise AdaptationError(ERR_UNSUPPORTED_RESULT_TYPE, f"type {result_type!r}")

    aggregation = gt.get("aggregation") or {}
    target = (aggregation.get("target_column") or {}) or {}
    target_header = target.get("source_header") or ""
    identity = tuple((c or {}).get("source_header", "")
                     for c in (aggregation.get("identity_columns") or ()))

    filters = _adapt_filters((gt.get("selection") or {}).get("filters") or [])

    # `argmin`/`argmax` répondent par une identité de ligne : sans colonne d'identité,
    # la réponse attendue ne serait pas comparable à celle du moteur.
    if operation in (sch.OP_ARGMIN, sch.OP_ARGMAX) and not identity:
        raise AdaptationError(ERR_SEMANTIC_LOSS,
                              f"{operation} sans colonne d'identité")

    expected_columns = tuple(h for h in (target_header, *identity) if h)

    query_case = {
        "id": case.get("case_id") or "",
        "question": question or case.get("question") or "",
        "document": document_filename or doc_ref.get("filename") or "",
        "expected_sheet": locator["sheet_name"],
        "expected_table": locator["table_range"],
        "expected_columns": list(expected_columns),
        "expected_operation": OPERATION_MAP[operation],
        "expected_filters": filters,
        "expected_value": _canonical_value(expected.get("value")),
        "tolerance": expected.get("tolerance") or "0",
        "category": category,
        "expected_status": "ok",
        "expected_code": "",
        "note": f"oracle={case.get('case_id', '')} operation={operation}",
        # Identité canonique CONSERVÉE : le nom de fichier et l'id runtime du moteur
        # sont des commodités de résolution, jamais la vérité (§18).
        "content_sha256": sha,
        "oracle_operation": operation,
        "oracle_case_id": case.get("case_id") or "",
    }

    fingerprint = semantic_fingerprint(
        content_sha256=sha, sheet_name=locator["sheet_name"],
        table_range=locator["table_range"], data_row_start=locator["data_row_start"],
        data_row_end=locator["data_row_end"], operation=operation,
        target_column=target_header, identity_columns=identity, filters=filters,
        result_type=result_type, result_value=expected.get("value"),
        population_policy={"include_rows": population.get("include_rows",
                                                          sch.INCLUDE_DATA_ROWS),
                           "ignore_empty_rows": population.get("ignore_empty_rows", True)})
    query_case["semantic_fingerprint"] = fingerprint

    return AdaptedCase(case=query_case, semantic_fingerprint=fingerprint,
                       oracle_case_id=query_case["oracle_case_id"], content_sha256=sha)


def _adapt_filters(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Traduit les prédicats, en refusant ceux que le moteur ne sait pas exprimer."""
    out: list[dict[str, Any]] = []
    for f in filters:
        operator = f.get("operator")
        if operator in UNMAPPABLE_FILTERS:
            raise AdaptationError(
                ERR_UNSUPPORTED_QUERY_FILTER,
                f"opérateur {operator!r} sans équivalent dans le plan moteur")
        if operator not in FILTER_MAP:
            raise AdaptationError(ERR_UNSUPPORTED_QUERY_FILTER,
                                  f"opérateur {operator!r} inconnu")
        column = (f.get("column") or {}).get("source_header", "")
        if not column:
            raise AdaptationError(ERR_SEMANTIC_LOSS, "filtre sans colonne nommée")
        adapted: dict[str, Any] = {"column": column, "operator": FILTER_MAP[operator]}
        if f.get("values"):
            adapted["values"] = [_canonical_value(v) for v in f["values"]]
        elif f.get("value") is not None:
            adapted["value"] = _canonical_value(f["value"])
        out.append(adapted)
    return out


def adapt_many(cases: list[dict[str, Any]]) -> tuple[list[AdaptedCase], list[dict[str, Any]]]:
    """Adapte un lot, en séparant les refus — un cas non adaptable n'est pas perdu de vue."""
    adapted: list[AdaptedCase] = []
    refused: list[dict[str, Any]] = []
    for case in cases:
        try:
            adapted.append(adapt_aggregation_case(case))
        except AdaptationError as e:
            refused.append({"case_id": case.get("case_id", ""), "code": e.code,
                            "detail": str(e)[:160]})
    return adapted, refused
