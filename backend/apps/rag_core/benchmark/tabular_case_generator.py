"""Générateur déterministe de cas `tabular_aggregation`.

Deux principes gouvernent ce module.

**Aucun LLM, nulle part.** Les questions sortent de gabarits explicites et la vérité
terrain est calculée par l'exécuteur de référence depuis les octets du classeur. Un
modèle qui rédigerait la question ou produirait la réponse rendrait le benchmark
circulaire : on mesurerait le générateur, pas le système évalué.

**Un cas douteux est un cas perdu.** Le générateur rejette abondamment — agrégat déjà
matérialisé dans une ligne TOTAL, colonne constante, sélection vide, filtre qui retient
tout, population d'une seule ligne. Chaque refus porte un code stable. Il vaut mieux un
dataset pilote honnête qu'un dataset volumineux dont les cas ne mesurent rien.

La **découverte** des tables s'appuie sur les métadonnées d'ingestion (bornes de table
relevées dans le classeur), mais **toutes les valeurs** sont relues depuis le fichier
source : la vérité terrain reste indépendante du découpage en chunks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from apps.rag_core.benchmark import tabular_aggregation_schema as sch
from apps.rag_core.benchmark import tabular_reference_executor as ex
from apps.rag_core.benchmark import tabular_types as tt
from apps.rag_core.benchmark.stable_ground_truth import StableDocumentRef

#: Bornes de « colonne catégorielle » : assez de valeurs pour que le filtre discrimine,
#: assez peu pour qu'il désigne un sous-ensemble et non une ligne unique.
MIN_CATEGORY_VALUES = 2
MAX_CATEGORY_VALUES = 12
#: Une population trop courte rend toute agrégation triviale ou instable.
MIN_POPULATION_ROWS = 5
#: En dessous, une somme ou une moyenne se lit directement dans la cellule.
MIN_AGGREGATED_ROWS = 2

# --- Plafonds de diversité (§35) -------------------------------------------
DEFAULT_CAPS = {
    "per_table": 3,
    "per_sheet": 6,
    "per_document": 8,
    "per_operation": 40,
    "per_template": 30,
    "per_expected_value": 4,
}

# --- Génération de filtres à seuil (§6-11) ---------------------------------
#: Codes de rejet propres aux filtres à seuil. Nommés séparément : masquer un seuil
#: dégénéré sous un code générique rendrait le rapport d'inventaire illisible.
ERR_EMPTY_FILTER_SELECTION = "empty_filter_selection"
ERR_FULL_FILTER_SELECTION = "full_filter_selection"
ERR_INSUFFICIENT_DISTINCT_VALUES = "insufficient_distinct_values"
ERR_AMBIGUOUS_FILTER_THRESHOLD = "ambiguous_filter_threshold"

#: Un seuil n'a de sens que s'il peut découper : deux valeurs distinctes suffisent pour
#: `gt`/`lt`, trois pour un intervalle qui exclut quelque chose des deux côtés.
MIN_DISTINCT_FOR_THRESHOLD = 2
MIN_DISTINCT_FOR_BETWEEN = 3

DIFFICULTY_EASY = "easy"
DIFFICULTY_MEDIUM = "medium"
DIFFICULTY_HARD = "hard"


@dataclass(frozen=True)
class TableSource:
    """Où lire la table : identité logique + chemin du classeur sur disque."""

    content_sha256: str
    filename: str
    path: str
    locator: sch.TableLocator


@dataclass
class GenerationStats:
    tables_inspected: int = 0
    tables_usable: int = 0
    candidates: int = 0
    accepted: int = 0
    rejected: int = 0
    operations: dict[str, int] = field(default_factory=dict)
    rejection_codes: dict[str, int] = field(default_factory=dict)

    def reject(self, code: str) -> None:
        self.rejected += 1
        self.rejection_codes[code] = self.rejection_codes.get(code, 0) + 1

    def accept(self, operation: str) -> None:
        self.accepted += 1
        self.operations[operation] = self.operations.get(operation, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables_inspected": self.tables_inspected,
            "tables_usable": self.tables_usable,
            "candidates": self.candidates,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "operations": dict(sorted(self.operations.items())),
            "rejection_codes": dict(sorted(self.rejection_codes.items(),
                                           key=lambda kv: -kv[1])),
        }


@dataclass(frozen=True)
class ColumnProfile:
    """Ce qu'une table offre comme matière à agrégation."""

    numeric: tuple[str, ...]
    categorical: tuple[str, ...]
    dates: tuple[str, ...]
    identity: tuple[str, ...]
    headers: dict[str, str]          # normalisé -> en-tête source
    types: dict[str, str]


# --------------------------------------------------------------------------- #
# Profilage d'une table
# --------------------------------------------------------------------------- #
def profile_table(table: ex.LoadedTable, population: list[ex.TableRow]) -> ColumnProfile:
    """Classe les colonnes selon ce qu'on peut légitimement en faire."""
    types = ex.column_types(table)
    headers = {sch.normalize_header(h): h for h in table.headers if h}
    numeric, categorical, dates, identity = [], [], [], []

    for key, source in headers.items():
        if sch.is_generic_header(source):
            continue
        ctype = types.get(key, tt.TYPE_EMPTY)
        values = [r.get(key) for r in population]
        non_empty = [v for v in values if not tt.is_empty(v)]
        if not non_empty:
            continue
        distinct = {tt.fold_for_match(v) for v in non_empty}

        if ctype in tt.NUMERIC_TYPES:
            # Une colonne constante ne mesure rien : somme = n x valeur, max = valeur.
            if len(distinct) > 1:
                numeric.append(key)
        elif ctype == tt.TYPE_STRING:
            all_distinct = len(distinct) == len(non_empty)
            # Catégorielle : il faut de la RÉPÉTITION. Une colonne à valeurs toutes
            # uniques donnerait un filtre ne retenant qu'une ligne — agrégation triviale.
            if (MIN_CATEGORY_VALUES <= len(distinct) <= MAX_CATEGORY_VALUES
                    and not all_distinct):
                categorical.append(key)
            if all_distinct and len(non_empty) == len(population):
                identity.append(key)   # valeurs toutes distinctes -> identifiant de ligne
        elif ctype in (tt.TYPE_DATE, tt.TYPE_DATETIME):
            dates.append(key)

    return ColumnProfile(tuple(numeric), tuple(categorical), tuple(dates),
                         tuple(identity), headers, types)


# --------------------------------------------------------------------------- #
# Gabarits de question (§25-26) — déterministes, autonomes
# --------------------------------------------------------------------------- #
TEMPLATES = {
    sch.OP_COUNT_ROWS: "Dans la feuille « {sheet} », combien de lignes compte le tableau « {table} » ?",
    sch.OP_FILTERED_COUNT: "Dans la feuille « {sheet} », combien de lignes ont « {filter_column} » égal à « {value} » ?",
    sch.OP_SUM: "Dans la feuille « {sheet} », quelle est la somme de « {target} » ?",
    sch.OP_FILTERED_SUM: "Dans la feuille « {sheet} », quelle est la somme de « {target} » pour les lignes où « {filter_column} » vaut « {value} » ?",
    sch.OP_AVERAGE: "Dans la feuille « {sheet} », quelle est la moyenne de « {target} » ?",
    sch.OP_MINIMUM: "Dans la feuille « {sheet} », quelle est la valeur minimale de « {target} » ?",
    sch.OP_MAXIMUM: "Dans la feuille « {sheet} », quelle est la valeur maximale de « {target} » ?",
    sch.OP_ARGMIN: "Dans la feuille « {sheet} », quel « {identity} » a la plus petite valeur de « {target} » ?",
    sch.OP_ARGMAX: "Dans la feuille « {sheet} », quel « {identity} » a la plus grande valeur de « {target} » ?",
}

#: Gabarits des filtres à seuil : la question doit nommer la colonne filtrée,
#: l'opérateur ET la ou les bornes — sinon elle n'est pas autonome (§26).
THRESHOLD_TEMPLATES = {
    (sch.OP_FILTERED_COUNT, sch.F_GT): "Dans la feuille « {sheet} », combien de lignes ont « {filter_column} » supérieur à {value} ?",
    (sch.OP_FILTERED_COUNT, sch.F_LT): "Dans la feuille « {sheet} », combien de lignes ont « {filter_column} » inférieur à {value} ?",
    (sch.OP_FILTERED_COUNT, sch.F_BETWEEN): "Dans la feuille « {sheet} », combien de lignes ont « {filter_column} » compris entre {low} et {high} ?",
    (sch.OP_FILTERED_COUNT, sch.F_BEFORE): "Dans la feuille « {sheet} », combien de lignes ont « {filter_column} » antérieur au {value} ?",
    (sch.OP_FILTERED_COUNT, sch.F_AFTER): "Dans la feuille « {sheet} », combien de lignes ont « {filter_column} » postérieur au {value} ?",
    (sch.OP_FILTERED_SUM, sch.F_GT): "Dans la feuille « {sheet} », quelle est la somme de « {target} » pour les lignes où « {filter_column} » est supérieur à {value} ?",
    (sch.OP_FILTERED_SUM, sch.F_LT): "Dans la feuille « {sheet} », quelle est la somme de « {target} » pour les lignes où « {filter_column} » est inférieur à {value} ?",
    (sch.OP_FILTERED_SUM, sch.F_BETWEEN): "Dans la feuille « {sheet} », quelle est la somme de « {target} » pour les lignes où « {filter_column} » est compris entre {low} et {high} ?",
    (sch.OP_FILTERED_SUM, sch.F_BEFORE): "Dans la feuille « {sheet} », quelle est la somme de « {target} » pour les lignes dont « {filter_column} » est antérieur au {value} ?",
    (sch.OP_FILTERED_SUM, sch.F_AFTER): "Dans la feuille « {sheet} », quelle est la somme de « {target} » pour les lignes dont « {filter_column} » est postérieur au {value} ?",
}


def render_question(operation: str, **kw) -> str:
    return TEMPLATES[operation].format(**kw)


def question_contains_answer(question: str, result: Any) -> bool:
    """Une question qui contient sa réponse ne mesure rien."""
    if isinstance(result, dict):
        return any(tt.fold_for_match(v) in tt.fold_for_match(question)
                   for v in result.values() if str(v).strip())
    text = str(result).strip()
    if not text or len(text) < 2:
        return False
    return tt.fold_for_match(text) in tt.fold_for_match(question)


def classify_difficulty(operation: str, *, filters: int, column_type: str) -> str:
    """Difficulté d'après la **structure du cas**, jamais la taille du fichier (§36)."""
    if operation in (sch.OP_ARGMIN, sch.OP_ARGMAX) or filters >= 2:
        return DIFFICULTY_HARD
    if filters == 1 or column_type in (tt.TYPE_DATE, tt.TYPE_DATETIME, tt.TYPE_PERCENTAGE):
        return DIFFICULTY_MEDIUM
    return DIFFICULTY_EASY


# --------------------------------------------------------------------------- #
# Détection d'un agrégat déjà matérialisé (§27)
# --------------------------------------------------------------------------- #
def aggregate_is_materialized(table: ex.LoadedTable, locator: sch.TableLocator,
                              key: str, result: Decimal) -> bool:
    """Vrai si une ligne écartée (TOTAL…) porte déjà la valeur calculée.

    Ce cas relève du `retrieval_lookup` : la réponse se lit dans une cellule. Le
    classer en agrégation surestimerait la capacité de calcul du système évalué.
    """
    policy = sch.RowPopulationPolicy()
    for row in table.rows:
        if not locator.data_row_start <= row.row_number <= locator.data_row_end:
            continue
        labels = [v for v in row.values.values() if not tt.is_empty(v)]
        if not any(policy.is_excluded_label(v) for v in labels):
            continue
        raw = row.get(key)
        if tt.is_empty(raw):
            continue
        try:
            if tt.normalize_decimal(raw) == result:
                return True
        except tt.TabularTypeError:
            continue
    return False


# --------------------------------------------------------------------------- #
# Génération pour une table
# --------------------------------------------------------------------------- #
def generate_for_table(source: TableSource, table: ex.LoadedTable, *, rag_space: str,
                       stats: GenerationStats, caps: dict[str, int] | None = None,
                       counters: dict[str, dict[str, int]] | None = None,
                       ) -> tuple[list[dict], list[dict]]:
    """Produit les cas d'une table, en ordre **déterministe**, plus leurs rejets."""
    caps = caps or DEFAULT_CAPS
    counters = counters if counters is not None else {}
    cases: list[dict] = []
    rejects: list[dict] = []

    def _reject(code: str, detail: str = "") -> None:
        stats.reject(code)
        rejects.append({"sheet_name": source.locator.sheet_name,
                        "table_range": source.locator.table_range,
                        "code": code, "detail": detail})

    population = ex.build_population(table, source.locator, sch.RowPopulationPolicy())
    if len(population) < MIN_POPULATION_ROWS:
        _reject(sch.ERR_TRIVIAL_SINGLE_ROW, f"{len(population)} lignes de données")
        return cases, rejects

    try:
        profile = profile_table(table, population)
    except (ex.TabularExecutionError, tt.TabularTypeError) as e:
        # Erreur Excel (#REF!, #DIV/0!), formule sans cache, type inexploitable :
        # la table entière est écartée plutôt que profilée à moitié.
        _reject(getattr(e, "code", sch.ERR_UNSUPPORTED_CELL_TYPE), str(e)[:160])
        return cases, rejects

    if not profile.numeric and not profile.categorical:
        _reject(sch.ERR_CONSTANT_TARGET_COLUMN, "aucune colonne numérique ni catégorielle")
        return cases, rejects
    stats.tables_usable += 1

    doc = StableDocumentRef(content_sha256=source.content_sha256, filename=source.filename)
    table_key = f"{source.content_sha256}:{source.locator.sheet_name}:{source.locator.table_range}"

    def _bump(scope: str, key: str) -> bool:
        """Incrémente un compteur de diversité ; faux si le plafond est atteint."""
        slot = counters.setdefault(scope, {})
        if slot.get(key, 0) >= caps[scope]:
            return False
        slot[key] = slot.get(key, 0) + 1
        return True

    # Ordre canonique : trié, jamais dépendant d'un parcours de dict ni d'un aléa.
    for spec in _iter_specs(profile, source.locator):
        stats.candidates += 1
        op = spec["operation"]
        try:
            case = _build_case(spec, source=source, table=table, doc=doc,
                               rag_space=rag_space, profile=profile)
        except (ex.TabularExecutionError, sch.TabularAggregationError,
                tt.TabularTypeError) as e:
            _reject(getattr(e, "code", sch.ERR_UNSUPPORTED_CELL_TYPE), str(e)[:160])
            continue
        if case is None:
            continue

        for scope, key in (("per_table", table_key),
                           ("per_sheet", f"{source.content_sha256}:{source.locator.sheet_name}"),
                           ("per_document", source.content_sha256),
                           ("per_operation", op),
                           ("per_template", op),
                           ("per_expected_value", str(case["ground_truth"]["expected_result"]["value"]))):
            if not _bump(scope, key):
                _reject(sch.ERR_DUPLICATE_CASE, f"plafond {scope} atteint")
                break
        else:
            cases.append(case)
            stats.accept(op)

    return cases, rejects


def _iter_specs(profile: ColumnProfile, locator: sch.TableLocator):
    """Énumère les cas envisageables, dans un ordre stable et **entrelacé**.

    Énumérer colonne par colonne épuiserait le plafond `per_table` sur les premières
    opérations : un lot entier de sommes et de moyennes, aucun extremum. L'ordre
    alterne donc les opérations, pour que chacune ait sa chance sous plafond.
    """
    numeric = sorted(profile.numeric)
    categorical = sorted(profile.categorical)
    identity = sorted(profile.identity)

    by_operation: dict[str, list[dict]] = {sch.OP_COUNT_ROWS: [{"operation": sch.OP_COUNT_ROWS}]}
    for op in (sch.OP_SUM, sch.OP_AVERAGE, sch.OP_MINIMUM, sch.OP_MAXIMUM):
        by_operation[op] = [{"operation": op, "target": col} for col in numeric]
    chosen_identity = select_identity_column(identity)
    for op in (sch.OP_ARGMIN, sch.OP_ARGMAX):
        by_operation[op] = ([{"operation": op, "target": col, "identity": chosen_identity}
                             for col in numeric] if chosen_identity else [])
    by_operation[sch.OP_FILTERED_COUNT] = [
        {"operation": sch.OP_FILTERED_COUNT, "filter_column": cat} for cat in categorical]
    by_operation[sch.OP_FILTERED_SUM] = [
        {"operation": sch.OP_FILTERED_SUM, "filter_column": cat, "target": col}
        for cat in categorical for col in numeric[:1]]

    # Filtres à SEUIL : sur colonnes numériques (gt/lt/between) et dates
    # (before/after/between). Une colonne numérique peut être à la fois filtrée et
    # agrégée ; on évite seulement de filtrer et sommer la MÊME colonne, ce qui
    # rendrait le cas circulaire.
    dates = sorted(profile.dates)
    for operator in (sch.F_GT, sch.F_LT, sch.F_BETWEEN):
        by_operation[sch.OP_FILTERED_COUNT] += [
            {"operation": sch.OP_FILTERED_COUNT, "filter_column": col,
             "filter_operator": operator} for col in numeric]
        by_operation[sch.OP_FILTERED_SUM] += [
            {"operation": sch.OP_FILTERED_SUM, "filter_column": f, "target": t_,
             "filter_operator": operator}
            for f in numeric for t_ in numeric if f != t_][:2]
    for operator in (sch.F_BEFORE, sch.F_AFTER, sch.F_BETWEEN):
        by_operation[sch.OP_FILTERED_COUNT] += [
            {"operation": sch.OP_FILTERED_COUNT, "filter_column": col,
             "filter_operator": operator} for col in dates]
        by_operation[sch.OP_FILTERED_SUM] += [
            {"operation": sch.OP_FILTERED_SUM, "filter_column": d, "target": t_,
             "filter_operator": operator}
            for d in dates for t_ in numeric[:1]]

    # Tour de rôle : une opération, puis la suivante, avant de revenir à la première.
    # L'ordre est ROTATIONNÉ par table : sous un plafond de 3 cas par table, un ordre
    # fixe ferait toujours gagner les mêmes opérations et le dataset entier n'aurait
    # que des comptages et des sommes. La rotation dérive de l'identité logique de la
    # table — donc reproductible, jamais aléatoire.
    order = (sch.OP_COUNT_ROWS, sch.OP_FILTERED_COUNT, sch.OP_SUM, sch.OP_MAXIMUM,
             sch.OP_AVERAGE, sch.OP_ARGMAX, sch.OP_FILTERED_SUM, sch.OP_MINIMUM,
             sch.OP_ARGMIN)
    offset = _rotation_offset(locator, len(order))
    rotated = order[offset:] + order[:offset]

    depth = max((len(v) for v in by_operation.values()), default=0)
    for i in range(depth):
        for op in rotated:
            bucket = by_operation.get(op) or []
            if i < len(bucket):
                yield bucket[i]


def select_identity_column(identity_columns: tuple[str, ...] | list[str]) -> str | None:
    """Colonne d'identité retenue pour un `argmin`/`argmax`, ou ``None`` s'il n'y en a pas.

    Une table peut porter plusieurs colonnes à valeurs toutes distinctes (référence
    interne, code métier, libellé unique). Aucune n'est « la bonne » dans l'absolu : le
    cas généré **nomme explicitement** celle qu'il utilise, donc aucune ambiguïté ne
    subsiste dans la vérité terrain.

    Ce n'est donc pas une résolution d'ambiguïté — c'est le choix du cas à produire. La
    règle est l'**ordre lexicographique de l'en-tête normalisé** : arbitraire dans son
    fond, mais stable, reproductible entre exécutions et machines, et indépendante de
    l'ordre des colonnes dans le classeur. Deux réingestions produisent le même cas.
    """
    if not identity_columns:
        return None
    return sorted(identity_columns)[0]


def _rotation_offset(locator: sch.TableLocator, modulo: int) -> int:
    """Décalage stable dérivé de l'identité de la table (jamais `hash()` Python)."""
    from apps.rag_core.benchmark.stable_ground_truth import sha256_hex

    digest = sha256_hex(f"{locator.sheet_name}|{locator.table_range}")
    return int(digest[:8], 16) % modulo


def _build_case(spec: dict, *, source: TableSource, table: ex.LoadedTable,
                doc: StableDocumentRef, rag_space: str,
                profile: ColumnProfile) -> dict | None:
    """Construit et VALIDE un cas ; renvoie ``None`` si le cas est sans intérêt."""
    op = spec["operation"]
    target_key = spec.get("target")
    filter_key = spec.get("filter_column")
    identity_key = spec.get("identity")

    filters: tuple[sch.Filter, ...] = ()
    filter_value = ""
    threshold_labels: dict[str, str] = {}
    filter_operator = spec.get("filter_operator")

    if filter_key and filter_operator:
        # Filtre à SEUIL : la borne vient des valeurs réellement observées dans la
        # colonne, jamais d'une valeur fabriquée. Une date de fin d'année qui ne figure
        # pas dans le tableau produirait un cas que rien ne justifie.
        population = ex.build_population(table, source.locator, sch.RowPopulationPolicy())
        column_type = profile.types.get(filter_key, "")
        observed = []
        for row in population:
            raw = row.get(filter_key)
            if tt.is_empty(raw):
                continue
            observed.append(tt.coerce_for_aggregation(raw, column_type=column_type))
        if filter_operator == sch.F_BETWEEN:
            low, high = select_between_bounds(observed)
            filters = (sch.Filter(column=sch.ColumnRef(source_header=profile.headers[filter_key]),
                                  operator=sch.F_BETWEEN, values=(low, high)),)
            threshold_labels = {"low": _render_bound(low), "high": _render_bound(high)}
        else:
            bound = select_threshold(observed, operator=filter_operator)
            filters = (sch.Filter(column=sch.ColumnRef(source_header=profile.headers[filter_key]),
                                  operator=filter_operator, value=bound),)
            threshold_labels = {"value": _render_bound(bound)}
    elif filter_key:
        # Valeur de filtre = modalité la plus fréquente, choisie de façon déterministe
        # (fréquence puis ordre alphabétique) — jamais un `first()` sur un parcours.
        population = ex.build_population(table, source.locator, sch.RowPopulationPolicy())
        counts: dict[str, int] = {}
        for row in population:
            raw = row.get(filter_key)
            if not tt.is_empty(raw):
                counts[tt.normalize_text(raw)] = counts.get(tt.normalize_text(raw), 0) + 1
        if not counts:
            return None
        filter_value = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        if counts[filter_value] == len(population):
            return None          # un filtre qui retient tout n'est pas un filtre
        filters = (sch.Filter(column=sch.ColumnRef(source_header=profile.headers[filter_key]),
                              operator=sch.F_EQ, value=filter_value),)

    aggregation = sch.Aggregation(
        function=op,
        target_column=(sch.ColumnRef(source_header=profile.headers[target_key])
                       if target_key else None),
        identity_columns=((sch.ColumnRef(source_header=profile.headers[identity_key]),)
                          if identity_key else ()),
    )
    provisional = sch.TabularAggregationGroundTruth(
        document_ref=doc, table_locator=source.locator, aggregation=aggregation,
        expected_result=sch.ExpectedResult(type=sch.RESULT_INTEGER, value=0),
        filters=filters)

    result = ex.execute(table, provisional)

    if op not in (sch.OP_COUNT_ROWS, sch.OP_FILTERED_COUNT):
        if result.selected_row_count < MIN_AGGREGATED_ROWS:
            raise ex.TabularExecutionError(
                sch.ERR_TRIVIAL_SINGLE_ROW,
                f"{result.selected_row_count} ligne agrégée")
        if op in (sch.OP_SUM, sch.OP_FILTERED_SUM) and isinstance(result.value, Decimal):
            if aggregate_is_materialized(table, source.locator, target_key, result.value):
                raise ex.TabularExecutionError(
                    sch.ERR_MATERIALIZED_AGGREGATE,
                    "une ligne de synthèse porte déjà ce total")

    if result.result_type == sch.RESULT_INTEGER:
        expected = sch.ExpectedResult(type=sch.RESULT_INTEGER, value=int(result.value))
    elif result.result_type == sch.RESULT_ROW_IDENTIFIER:
        expected = sch.ExpectedResult(type=sch.RESULT_ROW_IDENTIFIER, value=result.value)
    else:
        expected = sch.ExpectedResult(type=sch.RESULT_DECIMAL, value=result.value,
                                      comparison=sch.CMP_EXACT_DECIMAL)

    if filter_operator:
        question = THRESHOLD_TEMPLATES[(op, filter_operator)].format(
            sheet=source.locator.sheet_name,
            filter_column=profile.headers.get(filter_key, ""),
            target=profile.headers.get(target_key, ""), **threshold_labels)
    else:
        question = render_question(
            op, sheet=source.locator.sheet_name,
            table=source.locator.table_name or source.locator.table_range,
            target=profile.headers.get(target_key, ""),
            filter_column=profile.headers.get(filter_key, ""),
            identity=profile.headers.get(identity_key, ""),
            value=filter_value)

    if question_contains_answer(question, result.value):
        raise ex.TabularExecutionError(
            sch.ERR_QUESTION_CONTAINS_ANSWER, "la réponse figure dans l'énoncé")

    ground_truth = sch.TabularAggregationGroundTruth(
        document_ref=doc, table_locator=source.locator, aggregation=aggregation,
        expected_result=expected, filters=filters, evidence=result.evidence)
    ground_truth.validate(column_types=profile.types)

    case = sch.to_case_dict(
        case_id=sch.build_aggregation_case_id(
            document_sha256=source.content_sha256, sheet_name=source.locator.sheet_name,
            table_range=source.locator.table_range, operation=op, filters=filters,
            target_column=profile.headers.get(target_key, "")),
        question=question, rag_space=rag_space, ground_truth=ground_truth,
        operation=op,
        difficulty=classify_difficulty(op, filters=len(filters),
                                       column_type=profile.types.get(target_key or "", "")),
    )
    sch.validate_case(case)
    return case


# --------------------------------------------------------------------------- #
# Seuils de filtre — observés, déterministes, non triviaux (§6-11)
# --------------------------------------------------------------------------- #
def distinct_sorted(values: list) -> list:
    """Valeurs distinctes triées canoniquement — la base de toute sélection de seuil.

    Normaliser, dédupliquer puis trier AVANT de choisir est ce qui rend le seuil
    indépendant de l'ordre des lignes dans le classeur. Choisir « la première valeur
    rencontrée » ferait dépendre le cas de l'ordre de saisie du tableau.
    """
    return sorted({v for v in values if v is not None})


def select_threshold(values: list, *, operator: str) -> object:
    """Seuil **observé** découpant la population en deux parts non vides.

    On part de la médiane des valeurs distinctes — le point qui équilibre le mieux le
    découpage — puis on s'écarte symétriquement jusqu'à trouver un seuil qui laisse des
    lignes des deux côtés. Le premier candidat retenu l'est dans une liste **triée**,
    jamais dans l'ordre du classeur.
    """
    distinct = distinct_sorted(values)
    if len(distinct) < MIN_DISTINCT_FOR_THRESHOLD:
        raise _FilterThresholdError(
            ERR_INSUFFICIENT_DISTINCT_VALUES,
            f"{len(distinct)} valeur(s) distincte(s) : aucun seuil ne peut découper")
    total = len([v for v in values if v is not None])
    middle = len(distinct) // 2
    for offset in range(len(distinct)):
        for index in {middle - offset, middle + offset}:
            if not 0 <= index < len(distinct):
                continue
            candidate = distinct[index]
            kept = (len([v for v in values if v is not None and v > candidate])
                    if operator in (sch.F_GT, sch.F_AFTER)
                    else len([v for v in values if v is not None and v < candidate]))
            if 0 < kept < total:
                return candidate
    raise _FilterThresholdError(
        ERR_AMBIGUOUS_FILTER_THRESHOLD,
        "aucun seuil observé ne produit un sous-ensemble strict")


def select_between_bounds(values: list) -> tuple:
    """Bornes d'intervalle prises aux tiers, ajustées jusqu'à un découpage strict."""
    distinct = distinct_sorted(values)
    if len(distinct) < MIN_DISTINCT_FOR_BETWEEN:
        raise _FilterThresholdError(
            ERR_INSUFFICIENT_DISTINCT_VALUES,
            f"{len(distinct)} valeur(s) distincte(s) : intervalle sans intérêt")
    total = len([v for v in values if v is not None])
    n = len(distinct)
    for spread in range(n):
        low_index = max(0, n // 3 - spread)
        high_index = min(n - 1, 2 * n // 3 + spread)
        low, high = distinct[low_index], distinct[high_index]
        if low >= high:
            continue
        kept = len([v for v in values if v is not None and low <= v <= high])
        if 0 < kept < total:
            return low, high
    raise _FilterThresholdError(
        ERR_AMBIGUOUS_FILTER_THRESHOLD,
        "aucun intervalle observé ne produit un sous-ensemble strict")


class _FilterThresholdError(ex.TabularExecutionError):
    """Échec de sélection de seuil — porte un code de rejet dédié."""


def _render_bound(value) -> str:
    """Borne telle qu'elle apparaît dans la question.

    Rendue **exactement** comme le schéma la sérialise (`str`), et non sous une forme
    normalisée plus jolie. Afficher « entre 3 et 7.75 » quand la vérité terrain porte
    « 3.0 » ferait valider par un humain une borne différente de celle du cas — un écart
    invisible et coûteux au moment de la relecture.
    """
    return str(value)
