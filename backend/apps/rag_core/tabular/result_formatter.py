"""Mise en forme du résultat — structuré d'abord, narration ensuite (et jamais avant).

Le contrat de sortie : **le résultat structuré est la vérité**. La narration LLM, si
elle est activée, décrit ce résultat ; elle ne le produit pas, ne le corrige pas, ne
l'arrondit pas. C'est pourquoi ce module ne connaît aucun LLM.

Les nombres sortent en **chaîne** (`"1284350.00"`), pas en `float` JSON. Un flottant
JSON traverse les clients en perdant des décimales sur les grands montants ; une chaîne
décimale traverse intacte et se compare exactement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from apps.rag_core.tabular import catalog as C
from apps.rag_core.tabular import plan_schema as PS
from apps.rag_core.tabular import provenance as P

ANSWER_KIND = "tabular"


@dataclass
class TabularResult:
    status: str
    answer_kind: str
    operation: str
    value: Any = None
    unit: str = ""
    row_count: int = 0
    rows: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    execution_id: str = ""

    def to_dict(self) -> dict:
        out = {
            "status": self.status,
            "answer_kind": self.answer_kind,
            "operation": self.operation,
            "value": self.value,
            "unit": self.unit,
            "row_count": self.row_count,
            "evidence": self.evidence,
            "warnings": self.warnings,
        }
        if self.rows:
            out["rows"] = self.rows
        if self.execution_id:
            out["execution_id"] = self.execution_id
        return out


def format_decimal(value: Decimal, *, scale: int | None = None) -> str:
    """Rend un `Decimal` en chaîne stable. `ROUND_HALF_UP` — convention comptable.

    Python arrondit `HALF_EVEN` par défaut : `2.5` y devient `2`, ce qui surprend sur
    une facture. L'arrondi n'intervient que si une échelle est demandée.
    """
    if scale is None:
        return format(value.normalize(), "f")
    quant = Decimal(1).scaleb(-scale)
    return format(value.quantize(quant, rounding=ROUND_HALF_UP), "f")


def scalar_result(*, plan: PS.TabularPlan, table: C.CatalogTable, value: Any,
                  evidence: P.Evidence, selected_row_count: int,
                  warnings: list[str], execution_id: str = "") -> TabularResult:
    unit = ""
    if plan.target is not None:
        col = table.column_by_id(plan.target.column_id)
        if col is not None:
            unit = col.unit or ("%" if col.inferred_type == "percentage" else "")

    if isinstance(value, Decimal):
        rendered: Any = format_decimal(value)
    elif isinstance(value, int):
        rendered = value
    elif value is None:
        rendered = None
    else:
        rendered = str(value)

    return TabularResult(
        status="ok", answer_kind=ANSWER_KIND, operation=plan.operation,
        value=rendered, unit=unit, row_count=selected_row_count,
        evidence=evidence.to_dict(), warnings=list(warnings),
        execution_id=execution_id,
    )


def rows_result(*, plan: PS.TabularPlan, table: C.CatalogTable,
                columns: tuple[tuple[str, Any], ...], rows: tuple[tuple, ...],
                evidence: P.Evidence, selected_row_count: int,
                warnings: list[str], execution_id: str = "") -> TabularResult:
    """Résultat en lignes : chaque cellule est nommée par son en-tête d'ORIGINE.

    Les alias internes (`c0`, `c1`) ne sortent jamais de l'API : ils sont un détail de
    compilation. Le client voit les noms du classeur.
    """
    names = []
    for _alias, column_id in columns:
        col = table.column_by_id(column_id)
        names.append(col.original_name if col else str(column_id))

    payload = []
    for row in rows:
        entry = {}
        for name, cell in zip(names, row):
            entry[name] = format_decimal(cell) if isinstance(cell, Decimal) else cell
        payload.append(entry)

    return TabularResult(
        status="ok", answer_kind=ANSWER_KIND, operation=plan.operation,
        value=None, unit="", row_count=selected_row_count, rows=payload,
        evidence=evidence.to_dict(), warnings=list(warnings),
        execution_id=execution_id,
    )
