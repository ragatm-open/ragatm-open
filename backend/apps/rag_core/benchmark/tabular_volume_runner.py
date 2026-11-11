"""Qualification VOLUMÉTRIQUE du moteur tabulaire de production.

Le runner pilote la chaîne réelle, dans l'ordre réel —

    catalogue → sélection de table → planner → `parse_plan` → validation sémantique
              → chargement des lignes → compilation → exécution DuckDB
              → décompte des lignes retenues → provenance → mise en forme

— et chronomètre **chaque étage séparément**. Un temps total agrégé ne dit pas où passe
le temps ; or c'est précisément la question qu'une qualification volumétrique doit
trancher.

Deux étages absents de la spécification initiale sont mesurés quand même, parce que les
omettre donnerait une image fausse :

- **`load_rows`** — la matérialisation des lignes typées en mémoire Python. Ce n'est ni
  le catalogue ni l'exécution, et c'est loin d'être gratuit.
- **`count_selected`** — le recomptage des lignes retenues. Il ouvre une SECONDE
  connexion et réinsère l'INTÉGRALITÉ des lignes. Toute requête filtrée paie donc deux
  fois l'ingestion. Le fondre dans « exécution » masquerait un doublement de coût.

**Mesure de la mémoire.** Trois instruments, qui ne mesurent pas la même chose :

- `resource.getrusage(RUSAGE_SELF).ru_maxrss` — maximum de RSS atteint depuis le début
  du PROCESSUS, en Kio sous Linux. C'est une ligne de crête : **elle ne redescend
  jamais**. Comparer deux tailles dans le même processus est donc trompeur — la seconde
  hérite du pic de la première. D'où l'exécution de chaque taille dans un
  **sous-processus dédié** (cf. `__main__`), seule façon de rendre ce chiffre comparable.
- Échantillonnage de `/proc/self/statm` par un thread à 50 ms — donne un RSS avant,
  un RSS max et un delta réellement attribuables à la fenêtre mesurée.
- `tracemalloc` — pic d'allocation **Python uniquement**. Il ne voit pas le tas C++ de
  DuckDB ni les tampons d'openpyxl. Un écart important entre `tracemalloc` et le RSS
  échantillonné est donc attendu, et informatif. **Il est désactivé par défaut** : il
  instrumente chaque allocation Python et fausse les latences mesurées d'un facteur
  important (mesuré sur cette base de code : cf. `trace_python`). On l'active pour une
  passe dédiée, jamais pendant la campagne de latence.

**Aucune base de données.** Comme `tabular_query_runner`, le catalogue est construit
depuis les octets du fichier. La seule étape de production substituée est la résolution
du document par périmètre utilisateur, qui n'a aucune incidence volumétrique.
"""
from __future__ import annotations

import json
import os
import resource
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid5

from apps.rag_core.benchmark import tabular_volume_corpus as VC
from apps.rag_core.tabular import catalog as C
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import executor as EX
from apps.rag_core.tabular import plan_compiler as PC
from apps.rag_core.tabular import plan_schema as PS
from apps.rag_core.tabular import plan_validator as PV
from apps.rag_core.tabular import planner as PL
from apps.rag_core.tabular import provenance as P
from apps.rag_core.tabular import result_formatter as RF
from apps.rag_core.tabular import table_selector as TS

#: Espace de noms des identifiants de documents. `uuid5` et non `uuid4` : un identifiant
#: tiré au hasard changerait le `plan_hash` à chaque exécution et rendrait le contrôle
#: de déterminisme faux — il mesurerait le tirage, pas le moteur.
_DOC_NAMESPACE = UUID("6f1f1b1e-0000-4000-8000-000000000002")


def document_id_for(filename: str) -> UUID:
    return uuid5(_DOC_NAMESPACE, filename)


# --------------------------------------------------------------------------- #
# Limites
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Limits:
    """Limites du moteur, fournies en dur.

    Même raisonnement que `tabular_query_runner._Limits` : passer par
    `config.current_limits()` ferait dépendre la mesure des settings de la machine, donc
    produirait des résultats non comparables d'un environnement à l'autre.

    Deux valeurs s'écartent du patron, et c'est délibéré :

    - `max_rows_scanned` monte à 2 000 000. Le plafond de production (100 000) ferait
      REFUSER tout fichier au-delà de 100 k lignes avec `too_many_rows_scanned` — il n'y
      aurait alors rien à mesurer à 500 k et 1 M. La qualification vise le comportement
      du moteur au-delà de son plafond nominal, pas le plafond lui-même.
    - `max_query_seconds` monte à 900 s, pour que le délai ne vienne pas tronquer une
      mesure. Les sondes de délai le rabaissent explicitement.
    """

    max_rows_scanned: int = 2_000_000
    max_columns: int = 256
    max_filters: int = 16
    max_result_rows: int = 1_000
    max_groups: int = 1_000
    max_query_seconds: float = 900.0
    max_memory_mb: int = 2_048
    max_candidate_tables: int = 50
    max_candidate_columns: int = 512
    column_match_min_score: float = 0.60
    column_match_min_margin: float = 0.15


LIMITS = _Limits()


# --------------------------------------------------------------------------- #
# Instrumentation mémoire
# --------------------------------------------------------------------------- #
def _rss_bytes() -> int:
    """RSS courant du processus, lu dans `/proc/self/statm` (pages -> octets)."""
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):  # pragma: no cover - hors Linux
        return 0


def _ru_maxrss_bytes() -> int:
    """Ligne de crête de RSS depuis le début du processus. NE REDESCEND JAMAIS."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


class _RssSampler:
    """Échantillonne le RSS pendant la fenêtre mesurée.

    `ru_maxrss` seul ne permet pas d'attribuer un pic à une étape précise : c'est un
    maximum cumulé sur toute la vie du processus. L'échantillonnage donne, lui, un
    maximum RÉELLEMENT observé pendant la fenêtre — au prix d'un risque de manquer un
    pic plus court que la période d'échantillonnage, ce qui est dit dans le rapport.
    """

    def __init__(self, period: float = 0.05):
        self._period = period
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.before = 0
        self.peak = 0

    def __enter__(self) -> "_RssSampler":
        self.before = self.peak = _rss_bytes()

        def _loop():
            while not self._stop.wait(self._period):
                self.peak = max(self.peak, _rss_bytes())

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.peak = max(self.peak, _rss_bytes())
        return False

    @property
    def delta(self) -> int:
        return max(0, self.peak - self.before)


@dataclass
class MemorySample:
    rss_before_mb: float = 0.0
    rss_peak_mb: float = 0.0
    rss_delta_mb: float = 0.0
    ru_maxrss_mb: float = 0.0
    tracemalloc_peak_mb: float = 0.0

    def to_dict(self) -> dict:
        return {k: round(v, 2) for k, v in asdict(self).items()}


# --------------------------------------------------------------------------- #
# Chronométrage par étage
# --------------------------------------------------------------------------- #
#: Étages chronométrés, dans l'ordre d'exécution. L'ordre est celui du rapport.
STAGES = (
    "catalog", "table_select", "planning", "parse_plan", "validation",
    "load_rows", "compilation", "execution", "count_selected",
    "provenance", "formatting",
)


class _Stopwatch:
    """Accumule le temps par étage. Un étage non atteint reste absent, pas à zéro.

    La distinction compte : « 0 ms » se lit comme « instantané », alors que l'étage n'a
    parfois jamais tourné (requête sans filtre -> pas de `count_selected`).
    """

    def __init__(self):
        self.timings: dict[str, float] = {}

    def measure(self, stage: str, fn: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        try:
            return fn()
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            self.timings[stage] = self.timings.get(stage, 0.0) + elapsed

    @property
    def total_ms(self) -> float:
        return sum(self.timings.values())


# --------------------------------------------------------------------------- #
# Cas de requête
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QueryCase:
    """Une requête à mesurer.

    `payload` construit le plan directement depuis le catalogue. `question`, quand elle
    est fournie, fait passer la mesure par le PLANNER DÉTERMINISTE de production : c'est
    lui qui est alors chronométré à l'étage `planning`, et son plan est comparé au plan
    de référence. Toutes les formes ne sont pas exprimables par le planner (il ne couvre
    ni `contains`, ni `between` de date, ni `in`) — le cas retombe alors sur `payload`,
    ce qui est signalé dans le rapport plutôt que masqué.
    """

    case_id: str
    label: str
    operation: str
    payload: Callable[[C.DocumentCatalog, C.CatalogTable], dict]
    question: str = ""
    #: Sélectivité attendue du filtre, pour lire le résultat.
    expected_selectivity: str = ""


def _col(table: C.CatalogTable, name: str) -> C.CatalogColumn:
    col = next((c for c in table.columns if c.original_name == name), None)
    if col is None:  # pragma: no cover - corpus maîtrisé
        raise LookupError(f"colonne {name!r} absente ({[c.original_name for c in table.columns]})")
    return col


def _source(catalog: C.DocumentCatalog, table: C.CatalogTable) -> dict:
    return {"document_id": str(catalog.document_id), "sheet_id": str(table.sheet_id),
            "table_id": str(table.table_id)}


def _base(catalog, table, operation: str, **extra) -> dict:
    return {"schema_version": PS.PLAN_SCHEMA_VERSION, "operation": operation,
            "source": _source(catalog, table), **extra}


#: Matrice de requêtes. Elle couvre les treize formes exigées par la qualification, et
#: distingue explicitement un filtre SÉLECTIF (« Corse », une ligne sur 997) d'un filtre
#: NON SÉLECTIF (« Gagné », une ligne sur cinq) : sans cette opposition, on ne peut pas
#: dire si le coût suit le nombre de lignes RETENUES ou le nombre de lignes PARCOURUES.
CASES: tuple[QueryCase, ...] = (
    QueryCase(
        "count_rows", "Décompte des lignes, sans filtre", "count_rows",
        lambda cat, t: _base(cat, t, "count_rows"),
        question="combien de lignes dans le fichier ?"),
    QueryCase(
        "sum_selective", "Somme, filtre SÉLECTIF (Région = Corse, ~0,1 %)", "sum",
        lambda cat, t: _base(cat, t, "sum",
                             target={"column_id": str(_col(t, "Montant HT").column_id)},
                             filters=[{"column_id": str(_col(t, "Région").column_id),
                                       "operator": "equals", "value": VC.REGION_RARE}]),
        expected_selectivity="~0,1 %"),
    QueryCase(
        "sum_non_selective", "Somme, filtre NON sélectif (Statut = Gagné, ~20 %)", "sum",
        lambda cat, t: _base(cat, t, "sum",
                             target={"column_id": str(_col(t, "Montant HT").column_id)},
                             filters=[{"column_id": str(_col(t, "Statut").column_id),
                                       "operator": "equals", "value": "Gagné"}]),
        expected_selectivity="~20 %"),
    QueryCase(
        "average", "Moyenne (SUM/COUNT puis division Decimal)", "average",
        lambda cat, t: _base(cat, t, "average",
                             target={"column_id": str(_col(t, "Montant HT").column_id)}),
        question="quelle est la moyenne du montant ht ?"),
    QueryCase(
        "minimum", "Minimum", "minimum",
        lambda cat, t: _base(cat, t, "minimum",
                             target={"column_id": str(_col(t, "Montant HT").column_id)}),
        # Sans « quel » : `planner._ARG_PATTERN` fait basculer « QUEL est le minimum »
        # vers `argmin` (la question porte alors sur l'identité d'une ligne, pas sur une
        # valeur). C'est le comportement documenté du planner — mais il mesurerait une
        # autre opération que celle que ce cas prétend mesurer.
        question="minimum du montant ht"),
    QueryCase(
        "maximum", "Maximum", "maximum",
        lambda cat, t: _base(cat, t, "maximum",
                             target={"column_id": str(_col(t, "Montant HT").column_id)}),
        question="maximum du montant ht"),
    QueryCase(
        "top_n", "Top 10 par Montant HT décroissant", "top_n",
        lambda cat, t: _base(cat, t, "top_n", limit=10,
                             order_by=[{"column_id": str(_col(t, "Montant HT").column_id),
                                        "direction": "desc"}],
                             select_columns=[str(_col(t, "Identifiant").column_id)])),
    QueryCase(
        "bottom_n", "Bottom 10 par Montant HT croissant", "bottom_n",
        lambda cat, t: _base(cat, t, "bottom_n", limit=10,
                             order_by=[{"column_id": str(_col(t, "Montant HT").column_id),
                                        "direction": "asc"}],
                             select_columns=[str(_col(t, "Identifiant").column_id)])),
    QueryCase(
        "argmin", "Argmin : quelle ligne porte le Montant HT le plus faible", "argmin",
        lambda cat, t: _base(cat, t, "argmin",
                             target={"column_id": str(_col(t, "Montant HT").column_id)},
                             select_columns=[str(_col(t, "Identifiant").column_id)])),
    QueryCase(
        "argmax", "Argmax : quelle ligne porte le Montant HT le plus élevé", "argmax",
        lambda cat, t: _base(cat, t, "argmax",
                             target={"column_id": str(_col(t, "Montant HT").column_id)},
                             select_columns=[str(_col(t, "Identifiant").column_id)])),
    QueryCase(
        "contains_text", "`contains` sur colonne texte (strpos + lower)", "count_rows",
        lambda cat, t: _base(cat, t, "count_rows",
                             filters=[{"column_id": str(_col(t, "Texte court").column_id),
                                       "operator": "contains", "value": "lot n°7"}]),
        expected_selectivity="~7,7 %"),
    QueryCase(
        "between_date", "`between` sur colonne date (1er semestre 2024)", "count_rows",
        lambda cat, t: _base(cat, t, "count_rows",
                             filters=[{"column_id": str(_col(t, "Date").column_id),
                                       "operator": "between",
                                       "values": ["2024-01-01", "2024-06-30"]}]),
        expected_selectivity="~16,6 %"),
    QueryCase(
        "in_values", "`in` sur trois régions", "count_rows",
        lambda cat, t: _base(cat, t, "count_rows",
                             filters=[{"column_id": str(_col(t, "Région").column_id),
                                       "operator": "in",
                                       "values": ["Ouest", "Est", "Nord"]}]),
        expected_selectivity="~42,8 %"),
)

#: Sous-ensemble mesuré aux très grandes tailles. Justification dans le rapport :
#: l'ingestion DuckDB coûte ~2 ms par ligne (cf. `probe_ingestion_split`), donc la
#: matrice complète à 1 M de lignes demanderait plus de dix heures. On conserve les
#: trois formes qui suffisent à établir la COURBE : un scalaire sans filtre, un scalaire
#: filtré (qui paie l'ingestion deux fois) et un tri complet.
SCALING_CASE_IDS = ("count_rows", "sum_selective", "top_n")


def case_by_id(case_id: str) -> QueryCase:
    return next(c for c in CASES if c.case_id == case_id)


# --------------------------------------------------------------------------- #
# Exécution d'un cas
# --------------------------------------------------------------------------- #
@dataclass
class QueryObservation:
    """Ce que le moteur a produit, et ce qu'il a coûté, pour un cas."""

    case_id: str
    label: str = ""
    status: str = "ok"
    code: str = ""
    message: str = ""
    operation: str = ""
    value: Any = None
    row_count: int = 0
    selected_row_count: int | None = None
    scanned_row_count: int = 0
    plan_hash: str = ""
    evidence_hash: str = ""
    sql: str = ""
    timings_ms: dict[str, float] = field(default_factory=dict)
    total_ms: float = 0.0
    memory: MemorySample = field(default_factory=MemorySample)
    planner_used: bool = False
    planner_note: str = ""

    def signature(self) -> tuple:
        """Empreinte comparée d'une exécution à l'autre (déterminisme, concurrence).

        Le résultat SEUL ne suffit pas : un moteur qui rendrait le même nombre par un
        plan différent n'est pas déterministe au sens utile, puisque la justification
        affichée changerait sans que le chiffre bouge.
        """
        return (self.status, self.code, str(self.value), self.plan_hash,
                self.evidence_hash, self.sql, self.selected_row_count)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id, "label": self.label, "status": self.status,
            "code": self.code, "message": self.message, "operation": self.operation,
            "value": str(self.value) if self.value is not None else None,
            "row_count": self.row_count,
            "selected_row_count": self.selected_row_count,
            "scanned_row_count": self.scanned_row_count,
            "plan_hash": self.plan_hash[:16], "evidence_hash": self.evidence_hash[:16],
            "timings_ms": {k: round(v, 3) for k, v in self.timings_ms.items()},
            "total_ms": round(self.total_ms, 3),
            "memory": self.memory.to_dict(),
            "planner_used": self.planner_used, "planner_note": self.planner_note,
        }


@dataclass
class LoadedDocument:
    """Catalogue + lignes typées, réutilisables entre les cas d'une même taille.

    Le partage est un CHOIX de mesure, pas une simplification du moteur : le catalogue
    et le chargement sont chronométrés une fois, à part (`prepare_document`), puis
    chaque cas est mesuré sur son propre coût. Refaire le chargement à chaque cas
    mesurerait treize fois la même chose et rendrait la campagne inexécutable.
    Les cas qui veulent la chaîne intégrale passent par `run_case(..., reload=True)`.
    """

    file: VC.VolumeFile
    data: bytes
    catalog: C.DocumentCatalog
    table: C.CatalogTable
    rows: list[tuple]
    columns: list[tuple[str, str]]
    timings_ms: dict[str, float]
    memory: MemorySample


def prepare_document(vfile: VC.VolumeFile, *, limits=LIMITS,
                     trace_python: bool = False) -> LoadedDocument:
    """Lecture, catalogue, sélection de table et chargement des lignes — chronométrés.

    `trace_python` active `tracemalloc`. **Il fausse les latences** (il intercepte
    chaque allocation Python) : on ne l'active que pour une passe dédiée à la mesure du
    tas Python, jamais pour la campagne de latence.
    """
    watch = _Stopwatch()
    if trace_python:
        tracemalloc.start()
    with _RssSampler() as rss:
        data = watch.measure("read_file", lambda: vfile.path.read_bytes())
        builder = C.build_csv_catalog if vfile.kind == "csv" else C.build_xlsx_catalog
        catalog = watch.measure("catalog", lambda: builder(
            data, document_id=document_id_for(vfile.name),
            content_sha256=P.sha256_hex(vfile.sha256), filename=vfile.name))
        table = watch.measure("table_select", lambda: TS.select_table(
            vfile.name, catalog, max_candidates=limits.max_candidate_tables))
        rows = watch.measure("load_rows", lambda: C.load_rows(
            data, table, is_csv=vfile.kind == "csv",
            max_rows=limits.max_rows_scanned))
    peak = 0
    if trace_python:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

    return LoadedDocument(
        file=vfile, data=data, catalog=catalog, table=table, rows=rows,
        columns=[(PC.column_alias(c.position), c.inferred_type) for c in table.columns],
        timings_ms=watch.timings,
        memory=MemorySample(
            rss_before_mb=rss.before / 1e6, rss_peak_mb=rss.peak / 1e6,
            rss_delta_mb=rss.delta / 1e6, ru_maxrss_mb=_ru_maxrss_bytes() / 1e6,
            tracemalloc_peak_mb=peak / 1e6),
    )


def run_case(case: QueryCase, doc: LoadedDocument, *, limits=LIMITS,
             sample_memory: bool = True) -> QueryObservation:
    """Exécute un cas sur un document déjà chargé. Ne lève jamais : tout devient statut."""
    obs = QueryObservation(case_id=case.case_id, label=case.label)
    watch = _Stopwatch()
    catalog, table = doc.catalog, doc.table

    # Échantillonnage RSS seulement : léger (un thread qui lit `/proc` toutes les
    # 50 ms). `tracemalloc` est délibérément absent de ce chemin — il fausserait la
    # latence que ce cas existe précisément pour mesurer.
    sampler = _RssSampler() if sample_memory else None
    if sampler is not None:
        sampler.__enter__()

    try:
        # -- Planification ---------------------------------------------------
        reference = case.payload(catalog, table)
        if case.question:
            def _plan_via_planner():
                return PL.build_plan(case.question, catalog, table, limits=limits)
            try:
                planned = watch.measure("planning", _plan_via_planner)
                payload = planned.plan_payload
                obs.planner_used = True
                if payload != reference:
                    obs.planner_note = "plan du planner ≠ plan de référence"
            except E.TabularError as exc:
                obs.planner_note = f"planner en échec ({exc.code}) — repli sur le plan direct"
                payload = reference
        else:
            payload = watch.measure("planning", lambda: reference)
            obs.planner_note = "forme non exprimable par le planner déterministe"

        # -- Frontière de confiance -----------------------------------------
        plan = watch.measure("parse_plan", lambda: PS.parse_plan(payload))
        obs.operation = plan.operation
        obs.plan_hash = plan.plan_hash()
        validated = watch.measure(
            "validation", lambda: PV.validate(plan, catalog, limits=limits))

        # -- Compilation -----------------------------------------------------
        query = watch.measure("compilation", lambda: PC.compile_plan(
            plan, positions=validated.positions,
            max_result_rows=limits.max_result_rows,
            column_types={c.column_id: c.inferred_type for c in table.columns}))
        obs.sql = query.sql

        # -- Exécution -------------------------------------------------------
        outcome = watch.measure("execution", lambda: EX.execute(
            query, columns=doc.columns, rows=doc.rows, limits=limits))
        obs.scanned_row_count = outcome.scanned_row_count

        selected = watch.measure("count_selected", lambda: _selected_row_count(
            plan, query, doc.columns, doc.rows, limits))
        obs.selected_row_count = selected

        # -- Valeur ----------------------------------------------------------
        if query.returns_rows:
            value = _rows_value(plan, outcome)
            obs.row_count = len(outcome.rows)
        else:
            value = _scalar_value(plan, query, outcome)
            obs.row_count = 1
        obs.value = value

        # -- Provenance et mise en forme -------------------------------------
        evidence = watch.measure("provenance", lambda: P.build_evidence(
            plan=plan, catalog=catalog, table=table, result=value,
            selected_row_count=selected))
        obs.evidence_hash = evidence.evidence_hash

        def _format():
            if query.returns_rows:
                return RF.rows_result(
                    plan=plan, table=table, columns=query.output_columns,
                    rows=outcome.rows, evidence=evidence,
                    selected_row_count=selected, warnings=list(catalog.warnings))
            return RF.scalar_result(
                plan=plan, table=table, value=value, evidence=evidence,
                selected_row_count=selected, warnings=list(catalog.warnings))

        watch.measure("formatting", _format)
        obs.status = "ok"
    except E.TabularError as exc:
        obs.status, obs.code, obs.message = exc.status, exc.code, exc.message
    except Exception as exc:                      # noqa: BLE001 - on veut TOUT voir
        # Une exception NON typée qui remonte jusqu'ici est un défaut du moteur : le
        # contrat est que tout échec porte un code stable. On la conserve telle quelle
        # plutôt que de la traduire, sinon le défaut disparaîtrait du rapport.
        obs.status = "exception_non_typee"
        obs.code = type(exc).__name__
        obs.message = str(exc)[:400]

    if sampler is not None:
        sampler.__exit__()
        obs.memory.rss_before_mb = sampler.before / 1e6
        obs.memory.rss_peak_mb = sampler.peak / 1e6
        obs.memory.rss_delta_mb = sampler.delta / 1e6
        obs.memory.ru_maxrss_mb = _ru_maxrss_bytes() / 1e6

    obs.timings_ms = watch.timings
    obs.total_ms = watch.total_ms
    return obs


# --------------------------------------------------------------------------- #
# Reprises du service (identiques à `tabular_query_runner`)
# --------------------------------------------------------------------------- #
def _selected_row_count(plan, query, columns, rows, limits) -> int:
    if not plan.filters:
        return len(rows)
    marker = " WHERE "
    idx = query.sql.find(marker)
    where_sql = ""
    if idx >= 0:
        tail = query.sql[idx:]
        for stop in (" ORDER BY ", " LIMIT "):
            cut = tail.find(stop)
            if cut > 0:
                tail = tail[:cut]
        where_sql = tail
    return EX.count_selected(query.params, columns=columns, rows=rows,
                             where_sql=where_sql, limits=limits)


def _scalar_value(plan, query, outcome):
    if not outcome.rows:
        raise E.TabularError(E.ERR_EMPTY_SELECTION, "aucune ligne ne correspond")
    row = outcome.rows[0]
    if query.post_process == PC.POST_DIVIDE:
        total, n = row[0], row[1]
        if not n:
            raise E.TabularError(E.ERR_EMPTY_SELECTION, "aucune valeur numérique à moyenner")
        return EX.to_decimal(total) / Decimal(int(n))
    value = row[0]
    if value is None and plan.operation in ("sum", "minimum", "maximum"):
        raise E.TabularError(E.ERR_EMPTY_SELECTION, "aucune valeur exploitable")
    if isinstance(value, (Decimal, float)) and plan.operation != "count_rows":
        return EX.to_decimal(value)
    return value


def _rows_value(plan, outcome) -> list[Any]:
    if not outcome.rows:
        raise E.TabularError(E.ERR_EMPTY_SELECTION, "aucune ligne à classer")
    if plan.operation in ("argmin", "argmax"):
        if len(outcome.rows) > 1 and outcome.rows[0][-1] == outcome.rows[1][-1]:
            raise E.TabularError(E.ERR_TIE_UNRESOLVED,
                                 "plusieurs lignes portent la valeur extrême")
        return [outcome.rows[0][0]]
    return [r[0] for r in outcome.rows]


# --------------------------------------------------------------------------- #
# Décomposition ingestion / requête
# --------------------------------------------------------------------------- #
def probe_ingestion_split(doc: LoadedDocument, *, limits=LIMITS,
                          sql: str = "SELECT COUNT(*) FROM t") -> dict:
    """Sépare, DANS `executor.execute`, l'ingestion des lignes de la requête elle-même.

    `execute()` fait deux choses très inégales dans un seul appel : il INSÈRE toutes les
    lignes par `executemany`, puis exécute une requête. Un chiffre global laisserait
    croire que « DuckDB est lent », alors que le calcul lui-même se compte en
    millisecondes. La sonde rejoue le corps de `execute()` en utilisant SES PROPRES
    fonctions (`_connect`, `_ddl_create_table`) — elle observe, elle ne réimplémente pas.
    """
    conn = EX._connect(memory_limit_mb=limits.max_memory_mb)
    try:
        t0 = time.perf_counter()
        conn.execute(EX._ddl_create_table(doc.columns))
        t1 = time.perf_counter()
        marks = ", ".join("?" for _ in doc.columns)
        conn.executemany(f"INSERT INTO {PC.TABLE_NAME} VALUES ({marks})", doc.rows)
        t2 = time.perf_counter()
        conn.execute(sql).fetchall()
        t3 = time.perf_counter()
    finally:
        conn.close()

    insert_ms = (t2 - t1) * 1000.0
    return {
        "rows": len(doc.rows),
        "ddl_ms": round((t1 - t0) * 1000.0, 3),
        "insert_ms": round(insert_ms, 1),
        "select_ms": round((t3 - t2) * 1000.0, 3),
        "insert_rows_per_s": round(len(doc.rows) / max(1e-9, t2 - t1), 1),
        "us_per_row": round(insert_ms * 1000.0 / max(1, len(doc.rows)), 1),
    }


# --------------------------------------------------------------------------- #
# Sonde : plafond mémoire
# --------------------------------------------------------------------------- #
def probe_memory_limit(doc: LoadedDocument, *, tiny_mb: int = 1,
                       limits=LIMITS) -> dict:
    """Vérifie que `TABULAR_MAX_MEMORY_MB` est RÉELLEMENT appliqué par DuckDB.

    Trois questions distinctes, souvent confondues :

    1. DuckDB honore-t-il `SET memory_limit` ? (sonde bas niveau, sur `_connect`)
    2. Le moteur traduit-il le dépassement en `memory_exceeded` ? (sonde `execute`)
    3. Un résultat PARTIEL peut-il sortir avec le statut `ok` ? (le mode d'échec NO-GO)

    Et une quatrième, décisive pour ce moteur : le dépassement survenu pendant
    l'INSERTION est-il traduit lui aussi ? L'insertion se fait hors du `try` qui mappe
    les erreurs mémoire — un dépassement à l'ingestion ne peut donc pas devenir
    `memory_exceeded`.
    """
    out: dict[str, Any] = {"tiny_mb": tiny_mb}

    # -- 1. DuckDB honore-t-il le réglage ? --------------------------------
    # Requête volontairement gourmande, construite sans lire aucun fichier : `range`
    # est une fonction table interne, elle ne franchit pas `enable_external_access`.
    try:
        conn = EX._connect(memory_limit_mb=tiny_mb)
        try:
            applied = conn.execute(
                "SELECT current_setting('memory_limit')").fetchone()[0]
            out["memory_limit_applique"] = str(applied)
            started = time.perf_counter()
            try:
                conn.execute(
                    "SELECT count(*) FROM ("
                    "  SELECT i, repeat('x', 200) AS pad FROM range(3000000) tbl(i)"
                    "  ORDER BY pad, i DESC"
                    ")").fetchall()
                out["duckdb_applique_le_plafond"] = False
                out["duckdb_erreur"] = ""
            except Exception as exc:  # noqa: BLE001
                out["duckdb_applique_le_plafond"] = EX._is_memory_error(exc)
                out["duckdb_erreur"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            out["duckdb_probe_s"] = round(time.perf_counter() - started, 2)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        out["duckdb_connexion_erreur"] = f"{type(exc).__name__}: {str(exc)[:160]}"

    # -- 2 & 3. Le moteur traduit-il, et que rend-il ? ---------------------
    tight = replace(limits, max_memory_mb=tiny_mb)
    case = case_by_id("top_n")                     # ORDER BY : le plus gourmand
    obs = run_case(case, doc, limits=tight, sample_memory=False)
    out["moteur_statut"] = obs.status
    out["moteur_code"] = obs.code
    out["moteur_message"] = obs.message[:200]
    out["moteur_valeur"] = str(obs.value) if obs.value is not None else None
    out["resultat_partiel_avec_ok"] = bool(obs.status == "ok" and obs.value is not None)
    out["code_attendu"] = E.ERR_MEMORY_EXCEEDED

    # -- 4. La requête SUIVANTE fonctionne-t-elle ? ------------------------
    suivant = run_case(case_by_id("count_rows"), doc, limits=limits, sample_memory=False)
    out["requete_suivante_statut"] = suivant.status
    out["requete_suivante_valeur"] = str(suivant.value)
    return out


# --------------------------------------------------------------------------- #
# Sonde : délai maximal
# --------------------------------------------------------------------------- #
def probe_timeout(doc: LoadedDocument, *, timeout_s: float = 0.1,
                  limits=LIMITS) -> dict:
    """Vérifie le délai maximal — et surtout : `conn.interrupt()` INTERROMPT-IL vraiment ?

    C'est la question que ce module refuse d'esquiver. `_Timeout` arme un
    `threading.Timer` qui appelle `conn.interrupt()`, pendant que le thread principal
    reste bloqué dans `conn.execute`. Deux comportements sont possibles :

    - **l'interruption fonctionne** : DuckDB abandonne le calcul et `execute` rend la
      main au bout de ~`timeout_s` ;
    - **elle ne fonctionne pas** : `execute` ne rend la main qu'à la fin NATURELLE de la
      requête. Le moteur lèverait quand même `query_timeout` (`guard.fired` est vrai),
      mais après avoir consommé toutes les ressources — un délai de façade.

    On tranche au CHRONOMÈTRE : une requête dont on connaît la durée naturelle est
    relancée sous un délai bien plus court, et l'on compare les temps de retour.
    """
    out: dict[str, Any] = {"timeout_s": timeout_s}

    # -- Durée NATURELLE d'une requête volontairement coûteuse -------------
    # Construite avec `range` : la table est produite en interne, ce qui permet une
    # charge de plusieurs secondes sans payer les heures d'ingestion `executemany`.
    lourd = ("SELECT count(*) FROM ("
             "  SELECT i, repeat('y', 120) AS pad FROM range(4000000) tbl(i)"
             "  ORDER BY pad DESC, i"
             ")")
    conn = EX._connect(memory_limit_mb=limits.max_memory_mb)
    try:
        started = time.perf_counter()
        try:
            conn.execute(lourd).fetchall()
            out["duree_naturelle_s"] = round(time.perf_counter() - started, 3)
            out["duree_naturelle_erreur"] = ""
        except Exception as exc:  # noqa: BLE001
            out["duree_naturelle_s"] = round(time.perf_counter() - started, 3)
            out["duree_naturelle_erreur"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        conn.close()

    # -- La MÊME requête sous `_Timeout` de production ---------------------
    conn = EX._connect(memory_limit_mb=limits.max_memory_mb)
    try:
        started = time.perf_counter()
        with EX._Timeout(conn, timeout_s) as guard:
            try:
                conn.execute(lourd).fetchall()
                interrompu = False
                erreur = ""
            except Exception as exc:  # noqa: BLE001
                interrompu = True
                erreur = f"{type(exc).__name__}: {str(exc)[:120]}"
        out["duree_sous_timeout_s"] = round(time.perf_counter() - started, 3)
        out["timer_declenche"] = guard.fired
        out["requete_interrompue"] = interrompu
        out["erreur_interruption"] = erreur
    finally:
        conn.close()

    naturelle = out.get("duree_naturelle_s", 0.0)
    sous_timeout = out["duree_sous_timeout_s"]
    # Verdict : l'interruption est RÉELLE si le retour se fait nettement avant la fin
    # naturelle. Le seuil (moitié de la durée naturelle) évite de conclure sur du bruit.
    out["interruption_reelle"] = bool(
        naturelle > 4 * timeout_s and sous_timeout < naturelle / 2)
    out["verdict_interruption"] = (
        "interruption effective : DuckDB abandonne le calcul"
        if out["interruption_reelle"] else
        "PAS d'interruption démontrée : le retour se fait à la fin naturelle")

    # -- Bout en bout : que garantit RÉELLEMENT `max_query_seconds` ? ------
    # `_Timeout` n'enveloppe que le `conn.execute(query.sql)` — l'ingestion
    # (`conn.executemany`) est AVANT le bloc gardé. Or l'ingestion représente la quasi-
    # totalité du temps de mur (cf. `probe_ingestion_split`). Une requête peut donc
    # dépasser d'un facteur 40 son délai annoncé et sortir avec le statut `ok`.
    tight = replace(limits, max_query_seconds=timeout_s)
    started = time.perf_counter()
    obs = run_case(case_by_id("top_n"), doc, limits=tight, sample_memory=False)
    out["moteur_duree_totale_s"] = round(time.perf_counter() - started, 3)
    out["moteur_statut"] = obs.status
    out["moteur_code"] = obs.code
    out["code_attendu"] = E.ERR_QUERY_TIMEOUT
    out["moteur_valeur"] = str(obs.value) if obs.value is not None else None
    out["moteur_execution_ms"] = round(obs.timings_ms.get("execution", 0.0), 1)

    depassement = out["moteur_duree_totale_s"] / max(1e-9, timeout_s)
    out["depassement_du_delai_annonce"] = round(depassement, 1)
    out["delai_couvre_l_ingestion"] = not (obs.status == "ok" and depassement > 2)
    out["verdict_couverture"] = (
        "le délai borne bien la durée totale de la requête"
        if out["delai_couvre_l_ingestion"] else
        f"le délai NE BORNE QUE le SELECT : la requête a duré "
        f"{out['moteur_duree_totale_s']} s sous un délai de {timeout_s} s "
        f"(×{depassement:.0f}) et sort en statut `ok`")

    # -- La connexion est-elle nettoyée, la requête suivante passe-t-elle ? -
    suivant = run_case(case_by_id("count_rows"), doc, limits=limits, sample_memory=False)
    out["requete_suivante_statut"] = suivant.status
    out["requete_suivante_valeur"] = str(suivant.value)
    return out


# --------------------------------------------------------------------------- #
# Sonde : concurrence
# --------------------------------------------------------------------------- #
#: Scénarios de concurrence. Ils ne testent pas la même chose :
#: - `meme_doc_meme_question` : détecte un cache partagé ou un état de connexion résiduel ;
#: - `meme_doc_questions_differentes` : détecte une FUITE de résultat entre requêtes ;
#: - `docs_differents` : détecte un état global indexé par document.
CONCURRENCY_SCENARIOS = ("meme_doc_meme_question",
                         "meme_doc_questions_differentes",
                         "docs_differents")


#: Cas par défaut des scénarios à question unique. `sum_selective` est FILTRÉ : il paie
#: donc l'ingestion deux fois (exécution + recomptage), ce qui est exactement ce qu'on
#: veut de la campagne — mais fait de lui le cas le plus cher de la matrice.
DEFAULT_CONCURRENCY_CASE = "sum_selective"

#: Questions du scénario « même document, questions différentes ».
DEFAULT_CONCURRENCY_POOL = ("count_rows", "sum_selective", "sum_non_selective", "average",
                            "minimum", "maximum", "top_n", "bottom_n", "argmin", "argmax")


def probe_concurrency(docs: list[LoadedDocument], *, scenario: str, workers: int,
                      limits=LIMITS, repetitions: int = 1,
                      case_id: str = DEFAULT_CONCURRENCY_CASE,
                      case_pool: tuple[str, ...] = DEFAULT_CONCURRENCY_POOL) -> dict:
    """Lance `workers` requêtes simultanées et vérifie qu'aucune n'en contamine une autre.

    Le contrôle décisif n'est pas « ça n'a pas planté » mais **l'identité des signatures**
    : pour un couple (document, cas) donné, le résultat, le `plan_hash` et
    l'`evidence_hash` doivent être rigoureusement les mêmes qu'en exécution solitaire.
    Une fuite d'état entre threads se manifesterait par une signature divergente bien
    avant de produire une exception.

    `case_id` et `case_pool` existent pour une raison précise : ce que la sonde cherche —
    une contamination entre threads — ne dépend pas du COÛT des requêtes lancées. Une
    suite de tests peut donc choisir des cas non filtrés (une ingestion au lieu de deux)
    sur un petit corpus et obtenir la même propriété en une fraction du temps, tandis que
    la campagne garde les cas chers, plus représentatifs de la charge réelle.
    """
    if scenario == "meme_doc_meme_question":
        jobs = [(docs[0], case_by_id(case_id))] * workers
    elif scenario == "meme_doc_questions_differentes":
        pool = [case_by_id(cid) for cid in case_pool]
        jobs = [(docs[0], pool[i % len(pool)]) for i in range(workers)]
    elif scenario == "docs_differents":
        jobs = [(docs[i % len(docs)], case_by_id(case_id)) for i in range(workers)]
    else:  # pragma: no cover - liste fermée
        raise ValueError(scenario)
    jobs = jobs * repetitions

    # Référence SÉQUENTIELLE : la vérité à laquelle la concurrence doit se conformer.
    reference: dict[tuple[str, str], tuple] = {}
    for doc, case in jobs:
        key = (doc.file.name, case.case_id)
        if key not in reference:
            reference[key] = run_case(case, doc, limits=limits,
                                      sample_memory=False).signature()

    latencies: list[float] = []
    observations: list[QueryObservation] = []
    #: (document, cas, plan_hash) — le document fait partie de la clé, cf. plus bas.
    empreintes: list[tuple[str, str, str]] = []
    errors: list[str] = []

    def _job(item):
        doc, case = item
        started = time.perf_counter()
        obs = run_case(case, doc, limits=limits, sample_memory=False)
        return obs, (time.perf_counter() - started) * 1000.0, doc.file.name

    with _RssSampler() as rss:
        wall_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool_exec:
            for obs, latency, docname in pool_exec.map(_job, jobs):
                observations.append(obs)
                empreintes.append((docname, obs.case_id, obs.plan_hash))
                latencies.append(latency)
                if obs.status != "ok":
                    errors.append(f"{docname}/{obs.case_id}: {obs.status}/{obs.code}")
                if obs.signature() != reference[(docname, obs.case_id)]:
                    errors.append(f"FUITE {docname}/{obs.case_id}: signature divergente")
        wall_s = time.perf_counter() - wall_start

    ordered = sorted(latencies)
    def _pct(p: float) -> float:
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, int(p * len(ordered)))]

    # Indépendance des preuves. Le `plan_hash` est l'empreinte du PLAN, et le plan porte
    # le `document_id` de sa source : l'unité de comparaison est donc le COUPLE
    # (document, cas), jamais le cas seul. Trois propriétés, qui ne disent pas la même
    # chose et qu'il serait faux de confondre :
    #
    # - `stable_par_couple` : deux exécutions du même couple partagent leur `plan_hash` —
    #   c'est le déterminisme ;
    # - `distinct_entre_cas` : au sein d'UN document, deux questions différentes ne
    #   partagent jamais un `plan_hash` — c'est l'absence de collision ;
    # - `distinct_entre_documents` : un même cas posé sur deux documents produit deux
    #   `plan_hash` — c'est la preuve que la provenance est par document et non
    #   mutualisée. `None` quand un seul document est sollicité : rien à conclure.
    par_couple: dict[tuple[str, str], set[str]] = {}
    par_document: dict[str, dict[str, str]] = {}
    par_cas: dict[str, dict[str, str]] = {}
    for docname, case_id, plan_hash in empreintes:
        par_couple.setdefault((docname, case_id), set()).add(plan_hash)
        par_document.setdefault(docname, {})[case_id] = plan_hash
        par_cas.setdefault(case_id, {})[docname] = plan_hash

    hash_stable_par_couple = all(len(v) == 1 for v in par_couple.values())
    hash_distinct_entre_cas = all(
        len(set(cas.values())) == len(cas) for cas in par_document.values())
    partages = [docs for docs in par_cas.values() if len(docs) > 1]
    hash_distinct_entre_documents = (
        all(len(set(docs.values())) == len(docs) for docs in partages)
        if partages else None)

    return {
        "scenario": scenario,
        "workers": workers,
        "requetes": len(jobs),
        "wall_s": round(wall_s, 3),
        "debit_req_par_s": round(len(jobs) / max(1e-9, wall_s), 2),
        "p50_ms": round(_pct(0.50), 1),
        "p95_ms": round(_pct(0.95), 1),
        "max_ms": round(max(latencies) if latencies else 0.0, 1),
        "erreurs": len([e for e in errors if not e.startswith("FUITE")]),
        "timeouts": sum(1 for o in observations if o.code == E.ERR_QUERY_TIMEOUT),
        "fuites": len([e for e in errors if e.startswith("FUITE")]),
        "details_erreurs": errors[:10],
        "resultats_stables": not errors,
        "plan_hash_stable_par_couple": hash_stable_par_couple,
        "plan_hash_distinct_entre_cas": hash_distinct_entre_cas,
        "plan_hash_distinct_entre_documents": hash_distinct_entre_documents,
        "rss_peak_mb": round(rss.peak / 1e6, 1),
        "rss_delta_mb": round(rss.delta / 1e6, 1),
        "ru_maxrss_mb": round(_ru_maxrss_bytes() / 1e6, 1),
    }


# --------------------------------------------------------------------------- #
# Campagne pour UNE taille
# --------------------------------------------------------------------------- #
def run_size(row_count: int, *, case_ids: tuple[str, ...] | None = None,
             limits=LIMITS, dirty: bool = False, directory: Path | None = None,
             with_split_probe: bool = True) -> dict:
    """Prépare le document d'une taille, puis mesure les cas demandés.

    **À exécuter dans un processus dédié** : `ru_maxrss` est une ligne de crête qui ne
    redescend jamais, donc une taille mesurée après une plus grande hériterait de son
    pic et le chiffre serait faux.
    """
    vfile = VC.generate_csv(row_count, dirty=dirty, directory=directory)
    doc = prepare_document(vfile, limits=limits)

    cases = CASES if case_ids is None else tuple(case_by_id(c) for c in case_ids)
    observations = [run_case(c, doc, limits=limits) for c in cases]

    split = probe_ingestion_split(doc, limits=limits) if with_split_probe else None

    return {
        "row_count": row_count,
        "file": vfile.to_dict(),
        "table": {
            "row_count": doc.table.row_count,
            "column_count": doc.table.column_count,
            "data_range": doc.table.data_range,
            "columns": [{"nom": c.original_name, "type": c.inferred_type,
                         "null_ratio": c.null_ratio} for c in doc.table.columns],
        },
        "rows_loaded": len(doc.rows),
        "preparation_ms": {k: round(v, 1) for k, v in doc.timings_ms.items()},
        "preparation_memory": doc.memory.to_dict(),
        "ingestion_split": split,
        "cases": [o.to_dict() for o in observations],
        "environment": _environment(),
    }


def _environment() -> dict:
    import duckdb
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:  # pragma: no cover
        load1 = load5 = load15 = -1.0
    return {
        "duckdb": duckdb.__version__,
        "cpu_count": os.cpu_count(),
        "loadavg": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "ru_maxrss_mb_fin": round(_ru_maxrss_bytes() / 1e6, 1),
    }


# --------------------------------------------------------------------------- #
# Rapport
# --------------------------------------------------------------------------- #
def stage_table(report: dict) -> str:
    """Tableau markdown : un étage par colonne, un cas par ligne."""
    lines = ["| cas | " + " | ".join(STAGES) + " | total |",
             "| --- |" + " --- |" * (len(STAGES) + 1)]
    for case in report["cases"]:
        cells = []
        for stage in STAGES:
            v = case["timings_ms"].get(stage)
            cells.append("—" if v is None else f"{v:.1f}")
        lines.append(f"| {case['case_id']} | " + " | ".join(cells) +
                     f" | {case['total_ms']:.1f} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI — un sous-processus par taille (cf. docstring de `run_size`)
# --------------------------------------------------------------------------- #
def _main() -> None:  # pragma: no cover - point d'entrée manuel
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=VC.FAST_SIZE)
    parser.add_argument("--cases", default="", help="ids séparés par des virgules")
    parser.add_argument("--dirty", action="store_true")
    parser.add_argument("--probe", default="",
                        choices=["", "memory", "timeout", "concurrency", "split"])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    case_ids = tuple(c for c in args.cases.split(",") if c) or None

    if args.probe:
        vfile = VC.generate_csv(args.rows, dirty=args.dirty)
        doc = prepare_document(vfile)
        if args.probe == "memory":
            payload = probe_memory_limit(doc)
        elif args.probe == "timeout":
            payload = probe_timeout(doc)
        elif args.probe == "split":
            payload = probe_ingestion_split(doc)
        else:
            second = prepare_document(VC.generate_csv(args.rows, dirty=True))
            payload = {"scenarios": [
                probe_concurrency([doc, second], scenario=s, workers=w)
                for s in CONCURRENCY_SCENARIOS for w in (1, 2, 5, 10)
            ]}
        payload["environment"] = _environment()
    else:
        payload = run_size(args.rows, case_ids=case_ids, dirty=args.dirty)

    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":  # pragma: no cover
    _main()
