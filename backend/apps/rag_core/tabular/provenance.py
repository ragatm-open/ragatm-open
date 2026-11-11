"""Provenance — d'où vient exactement ce chiffre.

Contrainte fondatrice : la provenance est **dérivée de l'exécution réelle**, jamais
reconstruite après coup et jamais rédigée par un LLM. Elle décrit le plan qui a tourné,
les lignes qu'il a retenues, la colonne qu'il a mesurée.

Une réponse numériquement correcte accompagnée d'une provenance fausse est comptée
comme un échec : elle est pire qu'une erreur visible, parce qu'elle est vérifiable en
apparence.

`plan_hash` et `evidence_hash` permettent de rejouer et de comparer sans stocker les
valeurs sensibles du tableau.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from apps.rag_core.tabular import catalog as C
from apps.rag_core.tabular import plan_schema as PS


def _canonical(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        return json.dumps({k: _canonical(v) for k, v in sorted(value.items())},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "" if value is None else str(value)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Evidence:
    """Preuve d'exécution, sérialisable telle quelle dans la réponse API."""

    document_id: str
    filename: str
    content_sha256: str
    sheet_name: str
    table_id: str
    data_range: str
    header_range: str
    operation: str
    target_column: str = ""
    target_column_id: str = ""
    result_unit: str = ""
    filters: list[dict] = field(default_factory=list)
    selected_row_count: int = 0
    table_row_count: int = 0
    plan_hash: str = ""
    evidence_hash: str = ""
    #: Renseigné pour un résultat portant sur des lignes précises.
    row_references: list[str] = field(default_factory=list)
    #: État des formules de la table — jamais « recalculé ».
    formulas: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "filename": self.filename,
            "content_sha256": self.content_sha256,
            "sheet_name": self.sheet_name,
            "table_id": self.table_id,
            "data_range": self.data_range,
            "header_range": self.header_range,
            "operation": self.operation,
            "target_column": self.target_column,
            "target_column_id": self.target_column_id,
            "result_unit": self.result_unit,
            "filters": self.filters,
            "selected_row_count": self.selected_row_count,
            "table_row_count": self.table_row_count,
            "plan_hash": self.plan_hash,
            "evidence_hash": self.evidence_hash,
            "row_references": self.row_references,
            "formulas": self.formulas,
        }


def build_evidence(*, plan: PS.TabularPlan, catalog: C.DocumentCatalog,
                   table: C.CatalogTable, result: Any, selected_row_count: int,
                   row_references: list[str] | None = None) -> Evidence:
    """Construit la preuve à partir du plan EXÉCUTÉ et de son résultat."""
    target_col = None
    if plan.target is not None:
        target_col = table.column_by_id(plan.target.column_id)

    filters_repr = []
    for f in plan.filters:
        col = table.column_by_id(f.column_id)
        entry = {
            "column": col.original_name if col else str(f.column_id),
            "operator": f.operator,
        }
        if f.value is not None:
            entry["value"] = _canonical(f.value)
        if f.values:
            entry["values"] = [_canonical(v) for v in f.values]
        filters_repr.append(entry)

    ev = Evidence(
        document_id=str(catalog.document_id),
        filename=catalog.filename,
        content_sha256=catalog.content_sha256,
        sheet_name=table.sheet_name,
        table_id=str(table.table_id),
        data_range=table.data_range,
        header_range=table.header_range,
        operation=plan.operation,
        target_column=target_col.original_name if target_col else "",
        target_column_id=str(target_col.column_id) if target_col else "",
        result_unit=target_col.unit if target_col else "",
        filters=filters_repr,
        selected_row_count=selected_row_count,
        table_row_count=table.row_count,
        plan_hash=plan.plan_hash(),
        row_references=list(row_references or []),
        formulas={
            # Distinction explicite exigée par la spécification : lire une valeur en
            # cache n'est pas recalculer un classeur, et le système ne doit jamais
            # laisser croire le contraire.
            "table_has_formulas": table.has_formulas,
            "uncached_formula_present": table.has_uncached_formula,
            "recalculated_by_ragatm": False,
        },
    )

    ev.evidence_hash = sha256_hex("|".join([
        ev.plan_hash,
        ev.content_sha256,
        ev.table_id,
        ev.data_range,
        str(ev.selected_row_count),
        _canonical(result),
    ]))
    return ev
