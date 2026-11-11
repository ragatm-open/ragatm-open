"""Inventaire exhaustif du potentiel `tabular_aggregation` d'un corpus (passe A).

Cette passe **mesure**, elle ne produit pas. Elle répond à une seule question : *que
contient réellement ce corpus ?* — pour que les quotas de la passe B soient dérivés des
faits plutôt que devinés.

Deux règles la distinguent du générateur.

**Aucun plafond.** Les plafonds de diversité servent à borner un échantillon ; appliqués
ici, ils feraient mesurer les plafonds au lieu du corpus. Un inventaire sous quota est
un inventaire faux.

**Aucune matérialisation.** Un corpus de 11 429 tables produit de l'ordre de 10^5
candidats : les garder en mémoire ou sur disque n'aurait aucun intérêt et beaucoup
d'inconvénients. On calcule chaque résultat — c'est le seul moyen de connaître les
valeurs attendues dominantes et les rejets réels — puis on n'en conserve que des
**compteurs**.

Aucun JSONL officiel, aucun cas gelé, aucun split, aucun `holdout-state` ne sort d'ici.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from apps.rag_core.benchmark import tabular_aggregation_schema as sch
from apps.rag_core.benchmark import tabular_case_generator as gen
from apps.rag_core.benchmark import tabular_reference_executor as ex
from apps.rag_core.benchmark import tabular_types as tt

#: Bornes de taille de table, pour situer le corpus sans stocker chaque table.
SIZE_BUCKETS = ((5, 9), (10, 24), (25, 99), (100, 499), (500, 1999), (2000, 10**9))


def size_bucket(rows: int) -> str:
    for low, high in SIZE_BUCKETS:
        if low <= rows <= high:
            return f"{low}-{high}" if high < 10**9 else f"{low}+"
    return f"<{SIZE_BUCKETS[0][0]}"


@dataclass
class Inventory:
    """Compteurs d'inventaire. Aucun cas conservé, seulement des agrégats."""

    tables_inspected: int = 0
    tables_usable: int = 0
    candidates: int = 0
    viable: int = 0
    rejected: int = 0

    documents: set[str] = field(default_factory=set)
    sheets: set[tuple[str, str]] = field(default_factory=set)
    usable_documents: set[str] = field(default_factory=set)
    usable_sheets: set[tuple[str, str]] = field(default_factory=set)

    by_operation: Counter = field(default_factory=Counter)
    rejection_codes: Counter = field(default_factory=Counter)
    table_sizes: Counter = field(default_factory=Counter)
    column_types: Counter = field(default_factory=Counter)
    column_roles: Counter = field(default_factory=Counter)
    difficulties: Counter = field(default_factory=Counter)
    filter_operators: Counter = field(default_factory=Counter)

    #: Densité : combien de cas viables par document / feuille / table.
    per_document: Counter = field(default_factory=Counter)
    per_sheet: Counter = field(default_factory=Counter)
    per_table: Counter = field(default_factory=Counter)
    #: Redondance : combien de fois une même valeur attendue revient.
    expected_values: Counter = field(default_factory=Counter)

    def note_rejection(self, code: str) -> None:
        self.rejected += 1
        self.rejection_codes[code] += 1

    # ------------------------------------------------------------------ rapport
    def to_dict(self, *, top: int = 15) -> dict[str, Any]:
        viable = max(self.viable, 1)
        dominant_values = self.expected_values.most_common(top)
        rare = sorted(op for op in sch.SUPPORTED_OPERATIONS
                      if self.by_operation.get(op, 0) < 0.02 * viable)
        return {
            "tables_inspected": self.tables_inspected,
            "tables_usable": self.tables_usable,
            "candidates": self.candidates,
            "viable": self.viable,
            "rejected": self.rejected,
            "documents_seen": len(self.documents),
            "documents_usable": len(self.usable_documents),
            "sheets_seen": len(self.sheets),
            "sheets_usable": len(self.usable_sheets),
            "by_operation": dict(sorted(self.by_operation.items())),
            "operations_absent": sorted(sch.SUPPORTED_OPERATIONS - set(self.by_operation)),
            "operations_rare_under_2pct": rare,
            "rejection_codes": dict(self.rejection_codes.most_common()),
            "table_sizes": dict(sorted(self.table_sizes.items())),
            "column_types": dict(self.column_types.most_common()),
            "column_roles": dict(self.column_roles.most_common()),
            "difficulties": dict(sorted(self.difficulties.items())),
            "filter_operators": dict(self.filter_operators.most_common()),
            "density": {
                "per_document": _density(self.per_document),
                "per_sheet": _density(self.per_sheet),
                "per_table": _density(self.per_table),
            },
            "expected_value_redundancy": {
                "distinct_values": len(self.expected_values),
                "top": [{"value": v[:40], "count": n} for v, n in dominant_values],
                "share_of_top_10_pct": _share(self.expected_values, 10),
            },
            "template_redundancy": {
                # Un gabarit == une opération : la redondance de gabarit EST la
                # concentration par opération.
                "share_of_dominant_operation_pct": _share(self.by_operation, 1),
            },
        }


def _density(counter: Counter) -> dict[str, Any]:
    if not counter:
        return {"units": 0, "min": 0, "median": 0, "max": 0, "mean": 0.0}
    values = sorted(counter.values())
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return {"units": len(values), "min": values[0], "median": median,
            "max": values[-1], "mean": round(sum(values) / len(values), 2)}


def _share(counter: Counter, top_n: int) -> float:
    total = sum(counter.values())
    if not total:
        return 0.0
    return round(100 * sum(n for _, n in counter.most_common(top_n)) / total, 1)


# --------------------------------------------------------------------------- #
# Inventaire d'une table
# --------------------------------------------------------------------------- #
def inventory_table(source: gen.TableSource, table: ex.LoadedTable, inv: Inventory) -> None:
    """Compte le potentiel d'une table, **sans plafond et sans produire de cas**."""
    inv.tables_inspected += 1
    inv.documents.add(source.content_sha256)
    inv.sheets.add((source.content_sha256, source.locator.sheet_name))

    population = ex.build_population(table, source.locator, sch.RowPopulationPolicy())
    inv.table_sizes[size_bucket(len(population))] += 1
    if len(population) < gen.MIN_POPULATION_ROWS:
        inv.note_rejection(sch.ERR_TRIVIAL_SINGLE_ROW)
        return

    try:
        profile = gen.profile_table(table, population)
        types = profile.types
    except (ex.TabularExecutionError, tt.TabularTypeError) as e:
        inv.note_rejection(getattr(e, "code", sch.ERR_UNSUPPORTED_CELL_TYPE))
        return

    for key, ctype in types.items():
        if key:
            inv.column_types[ctype] += 1
    inv.column_roles["numeric"] += len(profile.numeric)
    inv.column_roles["categorical"] += len(profile.categorical)
    inv.column_roles["date"] += len(profile.dates)
    inv.column_roles["identity"] += len(profile.identity)

    if not profile.numeric and not profile.categorical:
        inv.note_rejection(sch.ERR_CONSTANT_TARGET_COLUMN)
        return

    inv.tables_usable += 1
    inv.usable_documents.add(source.content_sha256)
    inv.usable_sheets.add((source.content_sha256, source.locator.sheet_name))

    doc_key = source.content_sha256
    sheet_key = f"{doc_key}:{source.locator.sheet_name}"
    table_key = f"{sheet_key}:{source.locator.table_range}"

    for spec in gen._iter_specs(profile, source.locator):
        inv.candidates += 1
        try:
            case = gen._build_case(spec, source=source, table=table,
                                   doc=_doc_ref(source), rag_space="_inventory",
                                   profile=profile)
        except (ex.TabularExecutionError, sch.TabularAggregationError,
                tt.TabularTypeError) as e:
            inv.note_rejection(getattr(e, "code", sch.ERR_UNSUPPORTED_CELL_TYPE))
            continue
        if case is None:
            inv.note_rejection(sch.ERR_EMPTY_SELECTION)
            continue

        # Viable : on n'en garde que des compteurs, jamais le cas lui-même.
        inv.viable += 1
        inv.by_operation[case["operation"]] += 1
        inv.difficulties[case.get("difficulty") or "?"] += 1
        inv.per_document[doc_key] += 1
        inv.per_sheet[sheet_key] += 1
        inv.per_table[table_key] += 1
        inv.expected_values[_value_key(case)] += 1
        for f in case["ground_truth"]["selection"]["filters"]:
            inv.filter_operators[f["operator"]] += 1


def _doc_ref(source: gen.TableSource):
    from apps.rag_core.benchmark.stable_ground_truth import StableDocumentRef
    return StableDocumentRef(content_sha256=source.content_sha256, filename=source.filename)


def _value_key(case: dict) -> str:
    """Clé de redondance : le résultat attendu, sous forme canonique et non sensible."""
    result = case["ground_truth"]["expected_result"]
    value = result["value"]
    if isinstance(value, dict):
        return f"{result['type']}:<row_identifier>"
    if isinstance(value, Decimal):
        value = format(value.normalize(), "f")
    return f"{result['type']}:{value}"
