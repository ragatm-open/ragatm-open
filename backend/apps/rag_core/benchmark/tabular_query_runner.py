"""Exécution du dataset `tabular_query` contre le MOTEUR DE PRODUCTION.

Le runner ne recalcule rien : il pilote la chaîne réelle, dans l'ordre réel —

    catalogue → sélection de table → planner → `parse_plan` → validation sémantique
              → compilation → exécution DuckDB → provenance

— et se contente d'observer ce qui en sort. Toute simplification du chemin invaliderait
la mesure : un benchmark qui court-circuite le composant qu'il évalue ne mesure que
lui-même.

**Indépendance de la vérité terrain.** Ce module n'importe pas — et ne doit jamais
importer — `apps.rag_core.benchmark.tabular_reference_executor`. Les valeurs attendues
viennent exclusivement de `tabular_query_dataset`, qui les calcule en Python nu à partir
des lignes sources. Si le moteur et sa référence partageaient un normaliseur, un défaut
de conversion serait reproduit des deux côtés et resterait invisible. Un test
(`test_tabular_query_benchmark`) fige cette règle en inspectant le source du module.

**Aucune base de données.** Le catalogue est construit directement depuis les octets du
fichier, comme dans les tests unitaires du moteur. Une seule étape de production exige
la base — la résolution du document par périmètre utilisateur — et elle est remplacée
ici par un équivalent explicitement signalé (`_resolve_document`), dont la règle est
copiée du service : citation explicite du fichier, sinon clarification.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from apps.rag_core.benchmark import tabular_query_dataset as ds
from apps.rag_core.benchmark import tabular_query_metrics as met
from apps.rag_core.tabular import catalog as C
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import executor as EX
from apps.rag_core.tabular import plan_compiler as PC
from apps.rag_core.tabular import plan_schema as PS
from apps.rag_core.tabular import plan_validator as PV
from apps.rag_core.tabular import planner as PL
from apps.rag_core.tabular import provenance as P
from apps.rag_core.tabular import table_selector as TS

#: Nombre d'exécutions par cas. Cinq, parce que le déterminisme se constate sur des
#: répétitions et qu'une seconde exécution ne distingue pas « stable » de « chanceux ».
RUNS_PER_CASE = 5

#: Espace de noms des identifiants de documents. `uuid5` plutôt qu'`uuid4` : un
#: identifiant tiré au hasard changerait le `plan_hash` à chaque exécution et rendrait
#: le test de déterminisme faux — il mesurerait le tirage, pas le moteur.
_DOC_NAMESPACE = UUID("6f1f1b1e-0000-4000-8000-000000000001")


@dataclass(frozen=True)
class _Limits:
    """Limites du moteur, fournies en dur.

    Copiées du patron des tests unitaires : passer par `config.current_limits()`
    imposerait les settings Django et ferait dépendre le benchmark de la configuration
    de la machine, donc de résultats non comparables d'un environnement à l'autre.
    """

    max_rows_scanned: int = 100_000
    max_columns: int = 256
    max_filters: int = 16
    max_result_rows: int = 1_000
    max_groups: int = 1_000
    max_query_seconds: float = 10.0
    max_memory_mb: int = 256
    max_candidate_tables: int = 50
    max_candidate_columns: int = 512
    column_match_min_score: float = 0.60
    column_match_min_margin: float = 0.15


LIMITS = _Limits()


# --------------------------------------------------------------------------- #
# Observation
# --------------------------------------------------------------------------- #
@dataclass
class Observation:
    """Ce que le moteur a réellement produit pour un cas, sur une exécution."""

    status: str = ds.STATUS_OK
    code: str = ""
    message: str = ""
    document: str = ""
    sheet: str = ""
    table_range: str = ""
    columns: list[str] = field(default_factory=list)
    operation: str = ""
    filters: list[dict[str, Any]] = field(default_factory=list)
    value: Any = None
    plan_hash: str = ""
    evidence_hash: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    sql: str = ""
    params: tuple[Any, ...] = ()
    selected_row_count: int | None = None
    #: Trace de résolution documentaire : document attendu, document résolu, méthode.
    #: Journalisée systématiquement — un dataset exécuté contre le mauvais classeur doit
    #: être visible dans les observations, pas seulement dans un verdict agrégé.
    document_resolution: dict[str, Any] = field(default_factory=dict)

    def signature(self) -> tuple:
        """Empreinte comparée d'une exécution à l'autre.

        Y figurent le résultat, le plan et la preuve : un moteur qui rendrait le même
        nombre par un plan différent n'est pas déterministe au sens utile du terme,
        parce que la justification affichée changerait sans que le chiffre bouge.
        """
        value = met.canonical(self.value)
        if isinstance(value, list):
            value = tuple(value)
        return (self.status, self.code, value,
                self.plan_hash, self.evidence_hash, self.sql, self.params)


#: Codes de refus de la résolution canonique. Aucun n'a de repli silencieux.
ERR_MISSING_CONTENT_SHA256 = "missing_content_sha256"
ERR_DOCUMENT_NOT_FOUND = "document_not_found"
ERR_DUPLICATE_CONTENT_SHA256 = "duplicate_content_sha256"


class DocumentResolutionError(ValueError):
    """Échec de résolution canonique. Porte un ``code`` stable."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def document_id_for(content_sha256: str) -> UUID:
    """Identifiant runtime dérivé de l'**identité canonique**, jamais du nom de fichier.

    Deux documents homonymes doivent recevoir deux identifiants ; un document renommé
    doit conserver le sien. Dériver du nom inversait exactement ces deux propriétés.
    """
    return uuid5(_DOC_NAMESPACE, (content_sha256 or "").lower())


def resolve_document_canonically(case: dict[str, Any],
                                 corpus: ds.Corpus) -> tuple[ds.CorpusDocument, dict[str, Any]]:
    """Résout le document d'un cas par son **hachage de contenu**.

    Le nom de fichier ne sert plus qu'au diagnostic : un dataset figé doit pouvoir être
    rejoué après un renommage, et ne jamais s'exécuter contre un homonyme. Zéro
    correspondance et plusieurs correspondances sont **toutes deux** des refus — choisir
    dans une pluralité reviendrait à deviner quel classeur le dataset visait.
    """
    sha = (case.get("content_sha256") or "").strip().lower()
    if not sha:
        raise DocumentResolutionError(
            ERR_MISSING_CONTENT_SHA256,
            f"cas {case.get('id', '?')} sans content_sha256 : identité non vérifiable")
    matches = corpus.by_content_sha256(sha)
    if not matches:
        raise DocumentResolutionError(
            ERR_DOCUMENT_NOT_FOUND, f"aucun document de hachage {sha[:12]}…")
    if len(matches) > 1:
        raise DocumentResolutionError(
            ERR_DUPLICATE_CONTENT_SHA256,
            f"{len(matches)} documents partagent le hachage {sha[:12]}… : "
            f"{sorted(d.filename for d in matches)}")
    doc = matches[0]
    trace = {
        "expected_document": case.get("document") or "",
        "expected_content_sha256": sha,
        "resolved_document": doc.filename,
        "resolved_content_sha256": doc.content_sha256,
        "resolution": "content_sha256",
        # Un renommage est légitime et doit être VISIBLE, pas silencieux.
        "renamed": bool(case.get("document")) and case["document"] != doc.filename,
    }
    return doc, trace


# --------------------------------------------------------------------------- #
# Chemin de production
# --------------------------------------------------------------------------- #
def _resolve_document(question: str, corpus: ds.Corpus) -> ds.CorpusDocument:
    """Élit le document cité dans la question.

    **Seule étape substituée.** En production, `service._resolve_document` filtre par
    RagSpace accessible puis applique exactement cette règle : si plusieurs documents
    tabulaires sont candidats, celui dont le nom de fichier apparaît dans la question
    l'emporte ; sinon `ambiguous_document`. La substitution supprime la base, pas la
    règle — et surtout pas le refus, qui est ce que le cas mesure.
    """
    named = [d for d in corpus.documents.values()
             if d.filename.lower() in question.lower()]
    if len(named) == 1:
        return named[0]
    raise E.TabularClarificationRequired(
        E.ERR_AMBIGUOUS_DOCUMENT,
        "plusieurs documents tabulaires peuvent répondre",
        question="Sur quel document souhaitez-vous que le calcul porte ?",
        candidates=sorted(corpus.documents))


def _build_catalog(doc: ds.CorpusDocument) -> C.DocumentCatalog:
    builder = C.build_csv_catalog if doc.is_csv else C.build_xlsx_catalog
    return builder(
        doc.data,
        document_id=document_id_for(doc.content_sha256),
        # Le hash du contenu est celui du fichier : la preuve doit pouvoir être
        # rattachée aux octets exacts qui ont produit le chiffre.
        content_sha256=P.sha256_hex(doc.data.decode("latin-1")),
        filename=doc.filename)


def run_once(case: dict[str, Any], corpus: ds.Corpus, *, limits=LIMITS) -> Observation:
    """Une exécution complète d'un cas. Ne lève jamais : tout échec devient un statut."""
    obs = Observation()
    try:
        # Le type de cas détermine la voie AVANT l'exécution : un cas qui nomme son
        # document se résout par son hachage ; un cas qui n'en nomme aucun mesure
        # justement la sélection par le moteur. L'invariant est vérifié d'abord, pour
        # qu'aucun état incohérent n'arrive ici où le choix deviendrait ambigu.
        ds.validate_document_identity([case])
        if (case.get("content_sha256") or "").strip():
            doc, resolution_trace = resolve_document_canonically(case, corpus)
        else:
            doc, resolution_trace = _resolve_document(case["question"], corpus), {
                "resolution": "question", "expected_document": case.get("document") or ""}
        obs.document = doc.filename
        obs.document_resolution = resolution_trace

        catalog = _build_catalog(doc)
        table = TS.select_table(case["question"], catalog,
                                max_candidates=limits.max_candidate_tables)
        obs.sheet = table.sheet_name
        obs.table_range = table.data_range

        planned = PL.build_plan(case["question"], catalog, table, limits=limits)

        # Le plan repasse par la validation structurelle même s'il vient du planner
        # interne : c'est ce qui rend la frontière de confiance vérifiable.
        plan = PS.parse_plan(planned.plan_payload)
        obs.operation = plan.operation
        validated = PV.validate(plan, catalog, limits=limits)

        # Colonnes et filtres sont relevés dès que le plan est validé, AVANT exécution :
        # relevés depuis la preuve, ils seraient absents de tout cas qui échoue plus
        # tard — et une sélection vide serait comptée comme « aucun filtre demandé »
        # au lieu de « ce filtre-ci n'a rien retenu ».
        obs.columns = _plan_columns(plan, table)
        obs.filters = _plan_filters(plan, table)
        obs.plan_hash = plan.plan_hash()

        rows = C.load_rows(doc.data, table, is_csv=doc.is_csv,
                           max_rows=limits.max_rows_scanned)
        columns = [(PC.column_alias(c.position), c.inferred_type) for c in table.columns]
        query = PC.compile_plan(
            plan, positions=validated.positions,
            max_result_rows=limits.max_result_rows,
            # Sans les types, la comparaison texte redevient sensible à la casse :
            # le banc mesurerait un moteur différent de celui que sert le service.
            column_types={c.column_id: c.inferred_type for c in table.columns})
        obs.sql, obs.params = query.sql, query.params

        outcome = EX.execute(query, columns=columns, rows=rows, limits=limits)
        selected = _selected_row_count(plan, query, columns, rows, limits, outcome)
        obs.selected_row_count = selected

        if query.returns_rows:
            value = _rows_value(plan, outcome)
        else:
            value = _scalar_value(plan, query, outcome)
        obs.value = value

        evidence = P.build_evidence(plan=plan, catalog=catalog, table=table,
                                    result=value, selected_row_count=selected)
        obs.evidence = evidence.to_dict()
        obs.evidence_hash = evidence.evidence_hash
        obs.status = ds.STATUS_OK
    except ds.DocumentIdentityError as exc:
        # Incohérence du CAS lui-même, pas du corpus : le dataset est mal formé.
        obs.status = ds.STATUS_ERROR
        obs.code = exc.code
        obs.message = str(exc)
        obs.document_resolution = {"resolution": "invalid_case", "error": exc.code}
    except DocumentResolutionError as exc:
        # Échec d'identité documentaire : le cas ne s'exécute PAS contre un document
        # approchant. C'est un échec de harnais, distinct d'une erreur moteur — d'où
        # un statut `error` portant le code de résolution et la trace attendue/résolue.
        obs.status = ds.STATUS_ERROR
        obs.code = exc.code
        obs.message = str(exc)
        obs.document_resolution = {
            "resolution": "content_sha256",
            "expected_document": case.get("document") or "",
            "expected_content_sha256": (case.get("content_sha256") or "").lower(),
            "resolved_document": "", "resolved_content_sha256": "", "error": exc.code,
        }
    except E.TabularError as exc:
        obs.status = exc.status
        obs.code = exc.code
        obs.message = exc.message
    return obs


def _plan_columns(plan: PS.TabularPlan, table: C.CatalogTable) -> list[str]:
    """Colonnes réellement engagées par le plan : mesure, filtres, tri.

    Prises sur le PLAN et non sur la preuve : la preuve n'expose pas de colonne cible
    pour un classement, alors que le tri en désigne bien une.
    """
    ids = []
    if plan.target is not None:
        ids.append(plan.target.column_id)
    ids.extend(f.column_id for f in plan.filters)
    ids.extend(s.column_id for s in plan.order_by)
    names = []
    for cid in ids:
        col = table.column_by_id(cid)
        if col is not None and col.original_name not in names:
            names.append(col.original_name)
    return sorted(names)


def _plan_filters(plan: PS.TabularPlan, table: C.CatalogTable) -> list[dict[str, Any]]:
    """Filtres du plan, exprimés avec les noms d'en-tête d'origine (comme la preuve)."""
    out: list[dict[str, Any]] = []
    for f in plan.filters:
        col = table.column_by_id(f.column_id)
        entry: dict[str, Any] = {
            "column": col.original_name if col else str(f.column_id),
            "operator": f.operator,
        }
        if f.value is not None:
            entry["value"] = _canonical_scalar(f.value)
        if f.values:
            entry["values"] = [_canonical_scalar(v) for v in f.values]
        out.append(entry)
    return out


def _scalar_value(plan, query, outcome):
    """Reproduit la mise en forme scalaire du service — y compris ses refus.

    Notamment : une sélection vide n'est pas « zéro ». Zéro est une valeur ; l'absence
    de ligne est une information différente, et la confondre serait un faux résultat.
    """
    if not outcome.rows:
        raise E.TabularError(E.ERR_EMPTY_SELECTION, "aucune ligne ne correspond")
    row = outcome.rows[0]

    if query.post_process == PC.POST_DIVIDE:
        total, n = row[0], row[1]
        if not n:
            raise E.TabularError(E.ERR_EMPTY_SELECTION,
                                 "aucune valeur numérique à moyenner")
        return EX.to_decimal(total) / Decimal(int(n))

    value = row[0]
    if value is None and plan.operation in ("sum", "minimum", "maximum"):
        raise E.TabularError(E.ERR_EMPTY_SELECTION, "aucune valeur exploitable")
    if isinstance(value, (Decimal, float)) and plan.operation != "count_rows":
        return EX.to_decimal(value)
    return value


def _rows_value(plan, outcome) -> list[Any]:
    """Valeur observable d'un résultat en lignes : la première colonne projetée.

    C'est la colonne d'identité pour `argmin`/`argmax` (le plan la projette seule) et
    la première colonne de la table pour un classement — dans les deux cas, ce que la
    question demande de nommer.
    """
    if not outcome.rows:
        raise E.TabularError(E.ERR_EMPTY_SELECTION, "aucune ligne à classer")

    if plan.operation in ("argmin", "argmax"):
        # `LIMIT 2` est délibéré côté compilateur : deux lignes portant la même mesure
        # signifient que « quelle ligne » n'a pas de réponse unique.
        if len(outcome.rows) > 1 and outcome.rows[0][-1] == outcome.rows[1][-1]:
            raise E.TabularError(
                E.ERR_TIE_UNRESOLVED,
                "plusieurs lignes portent la valeur extrême")
        return [outcome.rows[0][0]]
    return [r[0] for r in outcome.rows]


def _selected_row_count(plan, query, columns, rows, limits, outcome) -> int:
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


# --------------------------------------------------------------------------- #
# Boucle de benchmark
# --------------------------------------------------------------------------- #
@dataclass
class CaseResult:
    """Bilan d'un cas : la première exécution fait foi, les cinq jugent la stabilité."""

    case: dict[str, Any]
    observation: Observation
    deterministic: bool
    signatures: int
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def latency_ms(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0.0


def run_case(case: dict[str, Any], corpus: ds.Corpus, *,
             runs: int = RUNS_PER_CASE, limits=LIMITS) -> CaseResult:
    observations: list[Observation] = []
    latencies: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        observations.append(run_once(case, corpus, limits=limits))
        latencies.append((time.perf_counter() - started) * 1000.0)

    signatures = {o.signature() for o in observations}
    return CaseResult(case=case, observation=observations[0],
                      deterministic=len(signatures) == 1,
                      signatures=len(signatures), latencies_ms=latencies)


def run_benchmark(directory: str | Path, *, runs: int = RUNS_PER_CASE,
                  limits=LIMITS) -> dict[str, Any]:
    """Génère le corpus, exécute tous les cas, renvoie le rapport complet."""
    corpus, cases = ds.build_dataset(directory)
    results = [run_case(c, corpus, runs=runs, limits=limits) for c in cases]

    evaluations = [met.evaluate_case(r.case, _as_dict(r.observation),
                                     deterministic=r.deterministic,
                                     latency_ms=r.latency_ms)
                   for r in results]
    summary = met.aggregate(evaluations)
    return {
        "meta": {
            "dataset": "tabular_query",
            "seed": ds.SEED,
            "corpus_directory": str(directory),
            "documents": sorted(corpus.documents),
            "cases": len(cases),
            "runs_per_case": runs,
        },
        "summary": summary,
        "cases": [e.to_dict() for e in evaluations],
    }


def _as_dict(obs: Observation) -> dict[str, Any]:
    return {
        "status": obs.status,
        "code": obs.code,
        "message": obs.message,
        "document": obs.document,
        "sheet": obs.sheet,
        "table": obs.table_range,
        "columns": list(obs.columns),
        "operation": obs.operation,
        "filters": list(obs.filters),
        "value": obs.value,
        "plan_hash": obs.plan_hash,
        "evidence_hash": obs.evidence_hash,
        "evidence": dict(obs.evidence),
        "sql": obs.sql,
        "params": [str(p) for p in obs.params],
        "selected_row_count": obs.selected_row_count,
    }


# --------------------------------------------------------------------------- #
# Rapport
# --------------------------------------------------------------------------- #
def to_markdown(report: dict[str, Any]) -> str:
    """Rapport de référence lisible. Aucune valeur n'y est arrondie à la hausse."""
    s = report["summary"]
    meta = report["meta"]
    lines: list[str] = []

    lines.append("# Benchmark — moteur tabulaire de production")
    lines.append("")
    lines.append("## Contexte")
    lines.append(f"- **Dataset** : {meta['dataset']} (graine {meta['seed']})")
    lines.append(f"- **Documents** : {', '.join(meta['documents'])}")
    lines.append(f"- **Cas** : {meta['cases']} · {meta['runs_per_case']} exécutions par cas")
    lines.append("")

    lines.append("## Métriques")
    lines.append("")
    lines.append("| Métrique | Valeur | Base |")
    lines.append("| --- | --- | --- |")
    for key, label in met.METRIC_LABELS.items():
        entry = s["metrics"].get(key)
        if entry is None:
            continue
        lines.append(f"| {label} | {_pct(entry['value'])} | {entry['support']} |")
    lat = s["latency_ms"]
    lines.append(f"| latence p50 / p95 (ms) | {lat['p50']:.1f} / {lat['p95']:.1f} | "
                 f"{meta['cases']} |")
    lines.append("")

    lines.append("## Par catégorie")
    lines.append("")
    lines.append("| Catégorie | Cas | Statut correct | Réponse correcte |")
    lines.append("| --- | --- | --- | --- |")
    for name, block in sorted(s["by_category"].items()):
        lines.append(f"| {name} | {block['cases']} | "
                     f"{_pct(block['status_accuracy'])} | "
                     f"{_pct(block['answer_accuracy'])} |")
    lines.append("")

    failing = [c for c in report["cases"] if not c["correct"]]
    lines.append("## Cas en échec")
    lines.append("")
    if not failing:
        lines.append("Aucun.")
    else:
        lines.append("| id | catégorie | attendu | obtenu | écart |")
        lines.append("| --- | --- | --- | --- | --- |")
        for c in failing:
            attendu = c["expected_status"]
            if c["expected_code"]:
                attendu += f" / {c['expected_code']}"
            obtenu = c["observed_status"]
            if c["observed_code"]:
                obtenu += f" / {c['observed_code']}"
            lines.append(f"| {c['id']} | {c['category']} | {attendu} | {obtenu} | "
                         f"{(c['reason'] or '').replace('|', '/')[:70]} |")
    lines.append("")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def _canonical_scalar(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return "" if value is None else str(value)
