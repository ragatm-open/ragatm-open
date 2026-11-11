"""Passe B — sélection du lot pilote `tabular_aggregation` sous contraintes.

La passe A (:mod:`tabular_inventory`) a mesuré le corpus : 32 029 tables, 18 282
exploitables, de l'ordre de 6·10⁵ cas viables. Elle ne produit rien. Cette passe-ci
**choisit** environ 80 cas de revue dans cette masse — et rien d'autre : ni split, ni
holdout, ni métrique moteur, ni dataset gelé.

Le problème n'est pas de trouver 80 cas : n'importe quel parcours en produirait. Le
problème est qu'un parcours qui accepte les premiers cas disponibles produit un
échantillon **conforme au corpus**, donc dominé par ce que le corpus a de plus banal :
217 217 `filtered_count` contre 8 204 `argmin`, 275 266 `easy` contre 20 954 `hard`,
2 733 `before` noyés dans 603 563 candidats. Un tel lot mesurerait la fréquence des
motifs, pas la couverture des comportements.

La sélection est donc **pilotée par les quotas**, jamais par l'ordre du pool : à chaque
tour on identifie le quota le plus mal servi, puis le meilleur candidat *encore
éligible* pour ce quota. Déterministe de bout en bout — aucun aléa, aucun parcours de
dict, départage final par `case_id`.

Deux familles de contraintes, qui ne se traitent pas pareil.

**Les plafonds de concentration et de fuite ne sont jamais relâchés.** Un document ne
donne pas plus de 2 cas, une feuille ou une table pas plus de 1, une valeur attendue pas
plus de 2. Ils sont vérifiés à *chaque* prise, y compris lorsqu'un quota secondaire
resterait inatteint : un quota manquant se documente, il ne se rachète pas en
concentrant l'échantillon sur une feuille généreuse.

**Les quotas de couverture, eux, sont des cibles.** Ils sont poursuivis dans l'ordre de
priorité fixé par la mission — opération, filtre, difficulté, taille, valeur attendue —
et ce qui n'est pas atteint est rapporté comme tel.

Cette passe ne matérialise pas les 6·10⁵ cas : elle n'en retient que des **facettes**
(:class:`CandidateFacets`), dédupliquées par table. Comme le plafond `per_table` vaut 1,
deux cas d'une même table qui partagent la même signature sont interchangeables — n'en
garder qu'un ne retire aucune option à la sélection tout en divisant le pool par un
ordre de grandeur. Les cas retenus sont re-matérialisés à la fin, à l'identique, par le
générateur d'origine.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from apps.rag_core.benchmark import tabular_aggregation_schema as sch
from apps.rag_core.benchmark import tabular_case_generator as gen
from apps.rag_core.benchmark import tabular_reference_executor as ex
from apps.rag_core.benchmark import tabular_types as tt

# --------------------------------------------------------------------------- #
# Bornes de taille de table
# --------------------------------------------------------------------------- #
#: Seuil global : sous 5 lignes de données, une agrégation n'a pas de sens statistique.
#: Inchangé par rapport à la passe A.
MIN_TABLE_ROWS = 5

#: Tranches **affinées** par rapport à :data:`tabular_inventory.SIZE_BUCKETS`. La passe A
#: mesurait `10-24` d'un bloc ; les quotas de la passe B distinguent `≥ 15`, donc la
#: tranche doit être coupée à 15. Sans cette coupe, un lot entièrement composé de tables
#: de 10 à 14 lignes satisferait formellement un quota conçu pour garantir des tables
#: réellement volumineuses.
SIZE_BUCKETS = ((5, 9), (10, 14), (15, 24), (25, 99), (100, 10**9))

#: Tranches comptant comme « grande table » pour les quotas de taille.
SIZE_AT_LEAST_15 = ("15-24", "25-99", "100+")
SIZE_AT_LEAST_25 = ("25-99", "100+")


def size_bucket(rows: int) -> str:
    """Tranche de taille d'une table, tranches affinées de la passe B."""
    for low, high in SIZE_BUCKETS:
        if low <= rows <= high:
            return f"{low}-{high}" if high < 10**9 else f"{low}+"
    return f"<{SIZE_BUCKETS[0][0]}"


# --------------------------------------------------------------------------- #
# Quotas — figés par la mission, dérivés de l'inventaire de la passe A
# --------------------------------------------------------------------------- #
TARGET_CASES = 80
ACCEPTABLE_RANGE = (70, 90)

#: Neuf opérations MVP. `argmin` n'est pas traité à part : ses 8 204 candidats suffisent
#: largement à la cible de 8, sa rareté relative (< 2 % du corpus) ne se traduit pas en
#: rareté absolue.
OPERATION_QUOTAS: dict[str, tuple[int, int, int]] = {          # min, cible, max
    op: (6, 8, 10) for op in sorted(sch.SUPPORTED_OPERATIONS)
}

FILTER_QUOTAS: dict[str, tuple[int, int, int]] = {             # min, cible, max
    sch.F_EQ: (8, 10, 14),
    sch.F_GT: (8, 12, 14),
    sch.F_LT: (8, 12, 14),
    sch.F_BETWEEN: (8, 12, 14),
    sch.F_BEFORE: (4, 6, 8),
    sch.F_AFTER: (4, 6, 8),
}

#: ⚠️ Les minima de filtre ci-dessus totalisent 40 cas filtrés, alors que seules deux
#: opérations en portent un — `filtered_count` et `filtered_sum` — plafonnées à 10
#: chacune, soit **20 cas filtrés au maximum**. Les deux familles de quotas ne peuvent
#: donc pas être satisfaites ensemble : la contrainte est structurelle, pas
#: conjoncturelle, et aucun corpus ne la lèverait.
#:
#: L'ordre de priorité tranche — *opération* passe avant *filtre* — donc les maxima
#: d'opération tiennent et les minima de filtre cèdent. Ce qui est garanti à leur place
#: est le **plancher de recevabilité** : les six opérateurs présents, `before` et
#: `after` à 4 chacun, soit 12 cas filtrés — largement tenable sous le plafond de 20.
#: Le solde est ensuite distribué vers les minima, et ce qui reste manquant est rapporté
#: dans ``unmet`` plutôt que compensé.
FILTER_FLOORS: dict[str, int] = {
    sch.F_EQ: 1, sch.F_GT: 1, sch.F_LT: 1, sch.F_BETWEEN: 1,
    sch.F_BEFORE: 4, sch.F_AFTER: 4,
}

DIFFICULTY_QUOTAS: dict[str, tuple[int, int, int]] = {         # min, cible, max
    gen.DIFFICULTY_EASY: (28, 30, 30),
    gen.DIFFICULTY_MEDIUM: (36, 38, 40),
    #: Sans minimum explicite, `hard` disparaîtrait : il pèse 3,5 % du corpus, donc
    #: moins de 3 cas sur 80 par tirage proportionnel.
    gen.DIFFICULTY_HARD: (10, 12, 14),
}

#: Plafonds de concentration et de fuite. **Jamais relâchés.**
CONCENTRATION_CAPS = {
    "per_document": 2,
    "per_sheet": 1,
    "per_table": 1,
    "per_expected_value": 2,
}

#: Un résultat numérique nul est légitime mais peu discriminant : un moteur qui répond
#: systématiquement 0 ne doit pas engranger de points.
MAX_ZERO_RESULTS = 4

#: Un petit entier de comptage revient massivement dans le corpus (`integer:7` : 26 219
#: occurrences). Le plafond porte sur la **valeur normalisée**, pas sur la chaîne typée,
#: pour que `integer:7` et `decimal:7` ne se partagent pas deux quotas distincts.
MAX_SAME_SMALL_COUNT = 2
SMALL_COUNT_MAX_VALUE = 20

#: Plafond de la tranche 5-9 lignes : ces tables sont admissibles mais ne doivent pas
#: constituer le lot.
MAX_SMALL_TABLES = 12
#: Minima de volumétrie, pour que le lot exerce réellement l'agrégation.
MIN_TABLES_AT_LEAST_15 = 40
MIN_TABLES_AT_LEAST_25 = 16

#: Ordre de priorité en cas de conflit, imposé par la mission. Sert à départager deux
#: candidats également éligibles : celui qui fait avancer la dimension la plus
#: prioritaire gagne.
PRIORITY_ORDER = ("operation", "filter", "difficulty", "size", "expected_value")


# --------------------------------------------------------------------------- #
# Facettes d'un candidat
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CandidateFacets:
    """Ce qu'il faut savoir d'un cas pour le sélectionner — pas le cas lui-même.

    Un cas complet pèse quelques kilo-octets (question, vérité terrain, évidence) ; le
    corpus en produit de l'ordre de 6·10⁵. Les facettes en retiennent la centaine
    d'octets qui pilote la sélection, et le cas est re-matérialisé après coup.
    """

    case_id: str
    document_sha256: str
    filename: str
    sheet_name: str
    table_range: str
    operation: str
    #: ``None`` pour un cas sans filtre (`sum`, `count_rows`, `argmax`…).
    filter_operator: str | None
    #: `numeric`, `date` ou `category` — un `between` sur date compte dans `between`
    #: *et* dans le type `date`, mais jamais dans `before` ni `after`.
    filter_value_type: str | None
    difficulty: str
    table_rows: int
    #: Valeur attendue canonique **complète**. Pour un `row_identifier`, l'identifiant
    #: entier (``row_identifier:{"code":"ABC"}``) et non le type générique : deux
    #: `argmax` désignant deux lignes différentes sont deux cas distincts.
    expected_value_key: str
    #: Résultat numérique nul — plafonné à part.
    is_zero_result: bool
    #: Clé de comptage normalisée, ou ``None`` si le résultat n'est pas un petit entier
    #: issu d'une opération de comptage.
    small_count_key: str | None

    @property
    def sheet_key(self) -> str:
        return f"{self.document_sha256}:{self.sheet_name}"

    @property
    def table_key(self) -> str:
        return f"{self.sheet_key}:{self.table_range}"

    @property
    def size_bucket(self) -> str:
        return size_bucket(self.table_rows)

    @property
    def signature(self) -> tuple:
        """Signature d'interchangeabilité au sein d'une même table.

        Le plafond `per_table` vaut 1 : deux cas d'une même table de même signature
        s'excluent mutuellement et satisfont exactement les mêmes quotas. En garder un
        seul ne retire donc aucune option à la sélection.
        """
        return (self.operation, self.filter_operator, self.filter_value_type,
                self.difficulty, self.expected_value_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "document_sha256": self.document_sha256,
            "filename": self.filename,
            "sheet_name": self.sheet_name,
            "table_range": self.table_range,
            "operation": self.operation,
            "filter_operator": self.filter_operator,
            "filter_value_type": self.filter_value_type,
            "difficulty": self.difficulty,
            "table_rows": self.table_rows,
            "size_bucket": self.size_bucket,
            "expected_value_key": self.expected_value_key,
            "is_zero_result": self.is_zero_result,
        }


def _canonical_value_key(case: dict) -> str:
    """Valeur attendue sous forme canonique et **complète**.

    Diffère volontairement de :func:`tabular_inventory._value_key`, qui réduit tout
    `row_identifier` au marqueur générique ``<row_identifier>``. Cette réduction convient
    à un inventaire — elle mesure la part des cas de type identifiant — mais appliquée à
    un plafond de sélection elle limiterait le lot à 2 `argmin`/`argmax` en tout, alors
    que deux identifiants différents sont deux réponses différentes.
    """
    result = case["ground_truth"]["expected_result"]
    value = result["value"]
    if isinstance(value, dict):
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return f"{result['type']}:{payload}"
    if isinstance(value, Decimal):
        value = format(value.normalize(), "f")
    return f"{result['type']}:{value}"


#: Opérations dont le résultat *est* un comptage.
COUNTING_OPERATIONS = (sch.OP_COUNT_ROWS, sch.OP_FILTERED_COUNT)


def _small_count_key(case: dict) -> str | None:
    """Clé de comptage normalisée, ou ``None``.

    Normalisée signifie : sur la valeur entière, pas sur la chaîne typée. Sans cela
    `integer:7` et `decimal:7` compteraient pour deux valeurs distinctes et le lot
    pourrait accumuler des « combien de lignes ? → 7 » sous deux étiquettes.
    """
    if case["operation"] not in COUNTING_OPERATIONS:
        return None
    value = case["ground_truth"]["expected_result"]["value"]
    if isinstance(value, dict):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if number != number.to_integral_value():
        return None
    integral = int(number)
    if not 0 <= integral <= SMALL_COUNT_MAX_VALUE:
        return None
    return f"count:{integral}"


def _is_zero_result(case: dict) -> bool:
    value = case["ground_truth"]["expected_result"]["value"]
    if isinstance(value, dict):
        return False
    try:
        return Decimal(str(value)) == 0
    except Exception:
        return False


#: Rôle de la colonne filtrée, déduit du type déclaré — sert à distinguer un `between`
#: sur date d'un `between` sur nombre.
_DATE_TYPES = (tt.TYPE_DATE, tt.TYPE_DATETIME)


def _filter_facets(case: dict, column_types: dict[str, str]) -> tuple[str | None, str | None]:
    """Opérateur de filtre et type de la valeur filtrée.

    Les cas du générateur portent zéro ou un filtre ; un jour où ils en porteraient
    plusieurs, c'est le premier dans l'ordre canonique qui qualifie le cas — le rapport
    de sélection resterait exact, seule la granularité du quota s'émousserait.
    """
    filters = case["ground_truth"]["selection"]["filters"]
    if not filters:
        return None, None
    first = filters[0]
    ctype = column_types.get(first["column"]["normalized_header"], "")
    if ctype in _DATE_TYPES:
        value_type = "date"
    elif ctype in (tt.TYPE_STRING, tt.TYPE_BOOLEAN):
        value_type = "category"
    else:
        value_type = "numeric"
    return first["operator"], value_type


def facets_from_case(case: dict, *, source: gen.TableSource, table_rows: int,
                     column_types: dict[str, str]) -> CandidateFacets:
    """Réduit un cas complet à ses facettes de sélection."""
    operator, value_type = _filter_facets(case, column_types)
    return CandidateFacets(
        case_id=case["case_id"],
        document_sha256=source.content_sha256,
        filename=source.filename,
        sheet_name=source.locator.sheet_name,
        table_range=source.locator.table_range,
        operation=case["operation"],
        filter_operator=operator,
        filter_value_type=value_type,
        difficulty=case.get("difficulty") or gen.DIFFICULTY_EASY,
        table_rows=table_rows,
        expected_value_key=_canonical_value_key(case),
        is_zero_result=_is_zero_result(case),
        small_count_key=_small_count_key(case),
    )


# --------------------------------------------------------------------------- #
# Collecte du pool
# --------------------------------------------------------------------------- #
#: Plafonds neutralisés : la collecte doit voir *tout* ce que la table offre. Les
#: plafonds du générateur borneraient l'offre avant que la sélection ait choisi, ce qui
#: reviendrait à sélectionner dans un échantillon déjà biaisé.
UNCAPPED = {scope: 10**9 for scope in gen.DEFAULT_CAPS}


def collect_table_facets(source: gen.TableSource, table: ex.LoadedTable,
                         *, rag_space: str) -> list[CandidateFacets]:
    """Facettes des cas viables d'une table, dédupliquées par signature.

    Les tables sous :data:`MIN_TABLE_ROWS` lignes ne renvoient rien : le seuil global
    est une condition d'admissibilité, pas un quota négociable.
    """
    population = ex.build_population(table, source.locator, sch.RowPopulationPolicy())
    if len(population) < MIN_TABLE_ROWS:
        return []

    try:
        profile = gen.profile_table(table, population)
    except (ex.TabularExecutionError, tt.TabularTypeError):
        return []
    if not profile.numeric and not profile.categorical:
        return []

    stats = gen.GenerationStats()
    cases, _ = gen.generate_for_table(source, table, rag_space=rag_space,
                                      stats=stats, caps=UNCAPPED, counters={})

    by_signature: dict[tuple, CandidateFacets] = {}
    for case in cases:
        facets = facets_from_case(case, source=source, table_rows=len(population),
                                  column_types=profile.types)
        previous = by_signature.get(facets.signature)
        # Départage par `case_id` : reproductible entre exécutions et entre machines.
        if previous is None or facets.case_id < previous.case_id:
            by_signature[facets.signature] = facets
    return sorted(by_signature.values(), key=lambda f: f.case_id)


def ambiguous_question_case_ids(cases: Iterable[dict]) -> set[str]:
    """Cas dont l'énoncé ne suffit pas à désigner *un* classeur.

    Les questions produites par le générateur nomment la feuille et la table, jamais le
    document. Le corpus contenant des classeurs quasi jumeaux (mêmes feuilles, périodes
    ou versions différentes — `… 122025` et `… 092024`, `… 12 2025` et `… 12 2025_w`),
    deux cas peuvent porter **le même énoncé et deux réponses différentes**.

    Un tel cas est structurellement insoluble : aucun moteur, et aucun humain, ne peut
    trancher entre les deux classeurs à partir de l'énoncé seul. Le compter comme un
    échec mesurerait l'ambiguïté du corpus, pas la capacité de calcul.

    La détection est signalée au relecteur plutôt qu'appliquée d'office : retirer ces cas
    relève de la validation humaine, et l'énoncé lui-même vient du générateur, hors du
    périmètre de la passe B.
    """
    seen: dict[str, list[str]] = {}
    for case in cases:
        seen.setdefault(" ".join(case["question"].split()).casefold(), []).append(
            case["case_id"])
    return {case_id for ids in seen.values() if len(ids) > 1 for case_id in ids}


def prune_pool(pool: Iterable[CandidateFacets]) -> list[CandidateFacets]:
    """Réduit le pool sans changer la sélection, en dédupliquant **par feuille**.

    Le corpus produit de l'ordre de 4·10⁵ facettes, alors que le plafond `per_sheet`
    vaut 1 : au plus 858 cas sont sélectionnables en tout, et au plus 2 par document.
    Balayer 4·10⁵ candidats à chaque prise coûterait des centaines de millions de tests
    d'éligibilité pour choisir 80 cas.

    La réduction est **exacte, pas heuristique**. Une seule prise a lieu par feuille ;
    deux candidats d'une même feuille qui partagent la même signature de quota sont donc
    interchangeables : mêmes quotas servis, mêmes plafonds consommés (même document,
    même feuille, même valeur attendue), et le plafond `per_table` ne les départage pas
    puisqu'un seul des deux sera pris de toute façon. On conserve le plus petit
    `case_id` — exactement celui que le départage de :func:`_progress_score` aurait
    choisi entre deux candidats à égalité.
    """
    kept: dict[tuple, CandidateFacets] = {}
    for c in pool:
        key = (c.sheet_key, c.operation, c.filter_operator, c.filter_value_type,
               c.difficulty, c.size_bucket, c.expected_value_key, c.is_zero_result,
               c.small_count_key)
        previous = kept.get(key)
        if previous is None or c.case_id < previous.case_id:
            kept[key] = c
    return sorted(kept.values(), key=lambda c: c.case_id)


# --------------------------------------------------------------------------- #
# État de la sélection
# --------------------------------------------------------------------------- #
@dataclass
class SelectionState:
    """Compteurs de la sélection en cours, et test d'éligibilité."""

    selected: list[CandidateFacets] = field(default_factory=list)
    per_document: Counter = field(default_factory=Counter)
    per_sheet: Counter = field(default_factory=Counter)
    per_table: Counter = field(default_factory=Counter)
    per_expected_value: Counter = field(default_factory=Counter)
    per_small_count: Counter = field(default_factory=Counter)
    zero_results: int = 0
    by_operation: Counter = field(default_factory=Counter)
    by_filter: Counter = field(default_factory=Counter)
    by_filter_value_type: Counter = field(default_factory=Counter)
    by_difficulty: Counter = field(default_factory=Counter)
    by_size_bucket: Counter = field(default_factory=Counter)

    # ------------------------------------------------------- plafonds durs
    def blocking_cap(self, c: CandidateFacets) -> str | None:
        """Nom du plafond qui interdit ce candidat, ou ``None`` s'il est éligible.

        Aucune de ces vérifications n'est contournable : elles délimitent ce qu'un
        échantillon a le droit d'être, alors que les quotas décrivent ce qu'on aimerait
        qu'il contienne.
        """
        if c.table_rows < MIN_TABLE_ROWS:
            return "min_table_rows"
        if self.per_table[c.table_key] >= CONCENTRATION_CAPS["per_table"]:
            return "per_table"
        if self.per_sheet[c.sheet_key] >= CONCENTRATION_CAPS["per_sheet"]:
            return "per_sheet"
        if self.per_document[c.document_sha256] >= CONCENTRATION_CAPS["per_document"]:
            return "per_document"
        if self.per_expected_value[c.expected_value_key] >= CONCENTRATION_CAPS["per_expected_value"]:
            return "per_expected_value"
        if c.is_zero_result and self.zero_results >= MAX_ZERO_RESULTS:
            return "zero_results"
        if c.small_count_key and self.per_small_count[c.small_count_key] >= MAX_SAME_SMALL_COUNT:
            return "small_count"
        if c.size_bucket == "5-9" and self.by_size_bucket["5-9"] >= MAX_SMALL_TABLES:
            return "small_tables"
        if self.by_operation[c.operation] >= OPERATION_QUOTAS[c.operation][2]:
            return "operation_max"
        if c.filter_operator and self.by_filter[c.filter_operator] >= FILTER_QUOTAS[c.filter_operator][2]:
            return "filter_max"
        if self.by_difficulty[c.difficulty] >= DIFFICULTY_QUOTAS[c.difficulty][2]:
            return "difficulty_max"
        return None

    def is_eligible(self, c: CandidateFacets) -> bool:
        return self.blocking_cap(c) is None

    # ------------------------------------------------------------- prise
    def take(self, c: CandidateFacets) -> None:
        self.selected.append(c)
        self.per_document[c.document_sha256] += 1
        self.per_sheet[c.sheet_key] += 1
        self.per_table[c.table_key] += 1
        self.per_expected_value[c.expected_value_key] += 1
        if c.small_count_key:
            self.per_small_count[c.small_count_key] += 1
        if c.is_zero_result:
            self.zero_results += 1
        self.by_operation[c.operation] += 1
        self.by_difficulty[c.difficulty] += 1
        self.by_size_bucket[c.size_bucket] += 1
        if c.filter_operator:
            self.by_filter[c.filter_operator] += 1
        if c.filter_value_type:
            self.by_filter_value_type[c.filter_value_type] += 1

    # ------------------------------------------------------------ déficits
    def deficits(self, *, filter_requirements: dict[str, int] | None = None,
                 ) -> dict[str, int]:
        """Ce qui manque encore, quota par quota, pour atteindre les **minima**.

        ``filter_requirements`` permet de viser le plancher de recevabilité plutôt que
        les minima de filtre — voir :data:`FILTER_FLOORS` pour pourquoi les deux ne
        peuvent pas coïncider.
        """
        required = (filter_requirements if filter_requirements is not None
                    else {f: q[0] for f, q in FILTER_QUOTAS.items()})
        out: dict[str, int] = {}
        for op, (low, _, _) in OPERATION_QUOTAS.items():
            if self.by_operation[op] < low:
                out[f"operation:{op}"] = low - self.by_operation[op]
        for f, low in required.items():
            if self.by_filter[f] < low:
                out[f"filter:{f}"] = low - self.by_filter[f]
        for d, (low, _, _) in DIFFICULTY_QUOTAS.items():
            if self.by_difficulty[d] < low:
                out[f"difficulty:{d}"] = low - self.by_difficulty[d]
        big = sum(self.by_size_bucket[b] for b in SIZE_AT_LEAST_15)
        if big < MIN_TABLES_AT_LEAST_15:
            out["size:>=15"] = MIN_TABLES_AT_LEAST_15 - big
        huge = sum(self.by_size_bucket[b] for b in SIZE_AT_LEAST_25)
        if huge < MIN_TABLES_AT_LEAST_25:
            out["size:>=25"] = MIN_TABLES_AT_LEAST_25 - huge
        return out


# --------------------------------------------------------------------------- #
# Prédicats de quota
# --------------------------------------------------------------------------- #
def _matches(quota: str, c: CandidateFacets) -> bool:
    """Ce candidat fait-il avancer ce quota ?"""
    kind, _, key = quota.partition(":")
    if kind == "operation":
        return c.operation == key
    if kind == "filter":
        return c.filter_operator == key
    if kind == "difficulty":
        return c.difficulty == key
    if kind == "size":
        buckets = SIZE_AT_LEAST_25 if key == ">=25" else SIZE_AT_LEAST_15
        return c.size_bucket in buckets
    return False


def _quota_rank(quota: str) -> int:
    """Rang de priorité d'un quota, selon l'ordre imposé par la mission."""
    kind = quota.partition(":")[0]
    return PRIORITY_ORDER.index(kind) if kind in PRIORITY_ORDER else len(PRIORITY_ORDER)


def _progress_score(c: CandidateFacets, state: SelectionState,
                    deficits: dict[str, int]) -> tuple:
    """À quel point ce candidat sert les quotas **encore** insatisfaits.

    Un candidat qui comble trois déficits à la fois vaut mieux qu'un candidat qui n'en
    comble qu'un : à quota égal, on prend celui qui rend la suite plus facile. Le score
    est pondéré par la priorité (opération avant filtre avant difficulté avant taille),
    puis départagé par `case_id` pour rester reproductible.

    ``deficits`` est calculé **une fois par prise** et passé ici : le recalculer par
    candidat coûterait un balayage complet des quotas pour chacun des ~10⁵ éléments du
    pool, à chacune des 80 prises.
    """
    score = 0
    for quota in deficits:
        if _matches(quota, c):
            score += len(PRIORITY_ORDER) - _quota_rank(quota)
    # Une valeur attendue encore inédite est préférable à une deuxième occurrence.
    unseen_value = 1 if state.per_expected_value[c.expected_value_key] == 0 else 0
    return (-score, -unseen_value, c.case_id)


# --------------------------------------------------------------------------- #
# Sélection
# --------------------------------------------------------------------------- #
@dataclass
class SelectionOutcome:
    selected: list[CandidateFacets]
    state: SelectionState
    unmet: dict[str, int]
    pool_size: int
    #: Taille du pool après déduplication par feuille (voir :func:`prune_pool`).
    pruned_pool_size: int = 0
    stages: list[dict[str, Any]] = field(default_factory=list)


def select_pilot(pool: Iterable[CandidateFacets], *, target: int = TARGET_CASES,
                 ) -> SelectionOutcome:
    """Choisit ~``target`` cas sous contraintes, de façon déterministe.

    L'ordre des étapes suit la mission : minima d'opération, minima de filtre (dont
    `before`/`after`), difficulté `hard` puis `medium`, tailles de table, puis
    complétion. À chaque étape le quota le plus **rare dans le pool restant** est servi
    en premier : servir d'abord `before` (2 733 candidats corpus, très concentrés) plutôt
    que `gt` (84 865) évite de consommer les quelques documents qui portent des dates.
    """
    raw = list(pool)
    candidates = prune_pool(raw)
    state = SelectionState()
    outcome = SelectionOutcome(selected=state.selected, state=state, unmet={},
                               pool_size=len(raw), pruned_pool_size=len(candidates))

    def _serve(quotas: list[str], label: str,
               filter_requirements: dict[str, int] | None = None) -> None:
        """Sert une famille de quotas jusqu'à épuisement des minima ou du pool."""
        taken = 0
        while True:
            deficits = {q: n for q, n
                        in state.deficits(filter_requirements=filter_requirements).items()
                        if q in quotas}
            if not deficits or len(state.selected) >= target:
                break
            # Rareté d'abord : le quota qui a le moins d'options éligibles passe devant.
            options = {q: [c for c in candidates
                           if _matches(q, c) and state.is_eligible(c)]
                       for q in deficits}
            servable = {q: opts for q, opts in options.items() if opts}
            if not servable:
                break
            quota = min(servable, key=lambda q: (len(servable[q]), _quota_rank(q), q))
            full = state.deficits()
            best = min(servable[quota], key=lambda c: _progress_score(c, state, full))
            state.take(best)
            taken += 1
        outcome.stages.append({"stage": label, "taken": taken,
                               "selected_total": len(state.selected)})

    _serve([f"operation:{op}" for op in OPERATION_QUOTAS], "minima_operations")
    # Plancher de recevabilité d'abord : les six opérateurs présents et `before`/`after`
    # à 4. C'est le seul volet de filtre qui soit garantissable sous les maxima
    # d'opération (voir FILTER_FLOORS) ; le viser en premier évite qu'un opérateur
    # abondant absorbe les 20 places filtrées avant que `before` ait été servi.
    _serve([f"filter:{f}" for f in FILTER_FLOORS], "plancher_filtres",
           filter_requirements=FILTER_FLOORS)
    _serve([f"filter:{f}" for f in FILTER_QUOTAS], "minima_filtres")
    _serve([f"difficulty:{gen.DIFFICULTY_HARD}"], "minimum_hard")
    _serve([f"difficulty:{gen.DIFFICULTY_MEDIUM}"], "minimum_medium")
    _serve(["size:>=25", "size:>=15"], "minima_tailles")
    _serve([f"difficulty:{gen.DIFFICULTY_EASY}"], "minimum_easy")

    _fill_to_target(candidates, state, target)
    outcome.stages.append({"stage": "completion", "taken": 0,
                           "selected_total": len(state.selected)})
    outcome.unmet = state.deficits()
    return outcome


def _fill_to_target(candidates: list[CandidateFacets], state: SelectionState,
                    target: int) -> None:
    """Complète jusqu'à la cible en poursuivant les **cibles** (et non les minima).

    Une fois tous les minima tenus, il reste des places. Les remplir au fil du pool
    ramènerait la distribution du corpus par la fenêtre ; on poursuit donc les cibles
    intermédiaires (8 par opération, 12 par filtre à seuil) tant qu'elles ne sont pas
    atteintes, puis on prend ce qui reste d'éligible.
    """
    while len(state.selected) < target:
        wanted: list[CandidateFacets] = []
        for c in candidates:
            if not state.is_eligible(c):
                continue
            below_op = state.by_operation[c.operation] < OPERATION_QUOTAS[c.operation][1]
            below_filter = (c.filter_operator is not None
                            and state.by_filter[c.filter_operator] < FILTER_QUOTAS[c.filter_operator][1])
            if below_op or below_filter:
                wanted.append(c)
        pool = wanted or [c for c in candidates if state.is_eligible(c)]
        if not pool:
            break
        full = state.deficits()
        state.take(min(pool, key=lambda c: _progress_score(c, state, full)))


# --------------------------------------------------------------------------- #
# Rapport de quotas
# --------------------------------------------------------------------------- #
def quota_report(outcome: SelectionOutcome) -> dict[str, Any]:
    """Rapport des quotas **atteints et non atteints**, sans arrondi favorable."""
    state = outcome.state
    n = len(state.selected)

    def _line(observed: int, quotas: tuple[int, int, int]) -> dict[str, Any]:
        low, target, high = quotas
        return {"observed": observed, "min": low, "target": target, "max": high,
                "met": low <= observed <= high}

    big = sum(state.by_size_bucket[b] for b in SIZE_AT_LEAST_15)
    huge = sum(state.by_size_bucket[b] for b in SIZE_AT_LEAST_25)
    small = state.by_size_bucket["5-9"]

    admissible = (
        ACCEPTABLE_RANGE[0] <= n <= ACCEPTABLE_RANGE[1]
        and len([o for o in OPERATION_QUOTAS if state.by_operation[o]]) == len(OPERATION_QUOTAS)
        and len([f for f in FILTER_QUOTAS if state.by_filter[f]]) == len(FILTER_QUOTAS)
        and state.by_difficulty[gen.DIFFICULTY_HARD] >= 10
        and state.by_filter[sch.F_BEFORE] >= 4
        and state.by_filter[sch.F_AFTER] >= 4
        and big >= MIN_TABLES_AT_LEAST_15
        and (max(state.per_document.values()) if state.per_document else 0) <= 2
        and (max(state.per_sheet.values()) if state.per_sheet else 0) <= 1
        and (max(state.per_table.values()) if state.per_table else 0) <= 1
        and (max(state.per_expected_value.values()) if state.per_expected_value else 0) <= 2
        and state.zero_results <= MAX_ZERO_RESULTS
    )

    return {
        "n_cases": n,
        "pool_size": outcome.pool_size,
        "pruned_pool_size": outcome.pruned_pool_size,
        "acceptable_range": list(ACCEPTABLE_RANGE),
        "admissible": admissible,
        "operations": {op: _line(state.by_operation[op], q)
                       for op, q in sorted(OPERATION_QUOTAS.items())},
        "filters": {f: _line(state.by_filter[f], q)
                    for f, q in sorted(FILTER_QUOTAS.items())},
        "filter_value_types": dict(sorted(state.by_filter_value_type.items())),
        "difficulties": {d: _line(state.by_difficulty[d], q)
                         for d, q in sorted(DIFFICULTY_QUOTAS.items())},
        "table_sizes": {
            "buckets": dict(sorted(state.by_size_bucket.items())),
            "at_least_15": {"observed": big, "min": MIN_TABLES_AT_LEAST_15,
                            "met": big >= MIN_TABLES_AT_LEAST_15},
            "at_least_25": {"observed": huge, "min": MIN_TABLES_AT_LEAST_25,
                            "met": huge >= MIN_TABLES_AT_LEAST_25},
            "small_5_9": {"observed": small, "max": MAX_SMALL_TABLES,
                          "met": small <= MAX_SMALL_TABLES},
        },
        "concentration": {
            "documents": len(state.per_document),
            "max_per_document": max(state.per_document.values()) if state.per_document else 0,
            "sheets": len(state.per_sheet),
            "max_per_sheet": max(state.per_sheet.values()) if state.per_sheet else 0,
            "tables": len(state.per_table),
            "max_per_table": max(state.per_table.values()) if state.per_table else 0,
        },
        "expected_values": {
            "distinct": len(state.per_expected_value),
            "max_occurrences": (max(state.per_expected_value.values())
                                if state.per_expected_value else 0),
            "zero_results": {"observed": state.zero_results, "max": MAX_ZERO_RESULTS,
                             "met": state.zero_results <= MAX_ZERO_RESULTS},
            "max_same_small_count": (max(state.per_small_count.values())
                                     if state.per_small_count else 0),
        },
        "unmet": dict(sorted(outcome.unmet.items())),
        #: Les minima de filtre ne sont pas atteignables sous les maxima d'opération —
        #: contrainte structurelle documentée ici pour qu'un `unmet` sur `filter:*` ne
        #: soit pas lu comme une défaillance du corpus ou du sélecteur.
        "filter_ceiling": {
            "filtered_operations": [sch.OP_FILTERED_COUNT, sch.OP_FILTERED_SUM],
            "max_filtered_cases": (OPERATION_QUOTAS[sch.OP_FILTERED_COUNT][2]
                                   + OPERATION_QUOTAS[sch.OP_FILTERED_SUM][2]),
            "sum_of_filter_minima": sum(low for low, _, _ in FILTER_QUOTAS.values()),
            "guaranteed_floor": dict(sorted(FILTER_FLOORS.items())),
            "note": ("Seules filtered_count et filtered_sum portent un filtre : les "
                     "minima de filtre excèdent le plafond d'opération. L'ordre de "
                     "priorité (opération > filtre) fait céder les minima de filtre ; "
                     "le plancher de recevabilité, lui, est garanti."),
        },
        "stages": outcome.stages,
    }
