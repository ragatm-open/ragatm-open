"""Service tabulaire — orchestration, permissions, exécution de bout en bout.

Point d'entrée unique du moteur. Il ne calcule rien lui-même : il enchaîne les
composants dans l'ordre imposé par l'architecture, et c'est lui qui garantit que
**l'ordre est respecté** — notamment que les permissions sont vérifiées avant toute
lecture de fichier, et que rien n'est exécuté avant validation complète.

    question → document accessible → feuille/table → colonnes → plan
             → validation structurelle → validation sémantique
             → compilation → exécution → provenance → résultat

Aucune étape n'est court-circuitable : chaque fonction en aval prend le type produit
par l'étape précédente, pas un dict.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from uuid import UUID

from django.db import transaction

from apps.rag_core.tabular import capacity as CAP
from apps.rag_core.tabular import catalog as C
from apps.rag_core.tabular import config as CFG
from apps.rag_core.tabular import deadline as DL
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import executor as EX
from apps.rag_core.tabular import plan_compiler as PC
from apps.rag_core.tabular import plan_schema as PS
from apps.rag_core.tabular import plan_validator as PV
from apps.rag_core.tabular import planner as PL
from apps.rag_core.tabular import preflight as PF
from apps.rag_core.tabular import provenance as P
from apps.rag_core.tabular import result_formatter as RF
from apps.rag_core.tabular import table_selector as TS

logger = logging.getLogger(__name__)

CSV_SUFFIXES = (".csv", ".tsv")
XLSX_SUFFIXES = (".xlsx",)


def answer_question(*, user, rag_space_id: UUID, question: str,
                    document_ids: list[UUID] | None = None) -> RF.TabularResult:
    """Répond à une question tabulaire. Lève une `TabularError` typée sinon."""
    if not CFG.query_enabled():
        raise E.TabularUnsupported(
            E.ERR_FEATURE_DISABLED,
            "le moteur tabulaire n'est pas activé sur cet environnement")

    limits = CFG.current_limits()
    started = time.perf_counter()
    # UNE échéance pour toute la requête. Les phases consomment le restant ; aucune
    # n'en recrée. Auparavant, catalogue, lecture et typage étaient hors budget, et
    # `execute` puis `count_selected` en ouvraient chacun un complet — mesuré 2,0×
    # le budget annoncé sur une requête filtrée.
    deadline = DL.Deadline.from_limits(limits)

    # Ordre délibéré : contrôle d'ACCÈS d'abord, slot ensuite, travail lourd après.
    #
    # Vérifier l'accès avant le slot évite qu'une rafale non autorisée n'occupe la
    # capacité — un attaquant sans droit ne doit pas pouvoir priver les utilisateurs
    # légitimes. Ce contrôle est léger : une requête sur le RagSpace, pas de document
    # lu, pas de catalogue construit.
    #
    # Tout ce qui coûte — résolution des documents, lecture des octets, préflight,
    # catalogue, typage, DuckDB — se déroule APRÈS l'acquisition. C'est ce qui rend la
    # limite utile : sans cela, vingt requêtes construiraient vingt catalogues avant
    # de se mettre en file.
    from apps.rag_core.tabular.permissions import require_access

    require_access(user=user, rag_space_id=rag_space_id)
    deadline.mark("permissions")

    with CAP.slot(deadline):
        return _answer_with_slot(user=user, rag_space_id=rag_space_id,
                                 question=question, document_ids=document_ids,
                                 limits=limits, deadline=deadline, started=started)


def _answer_with_slot(*, user, rag_space_id, question, document_ids, limits,
                      deadline, started) -> RF.TabularResult:
    """Corps de la requête, exécuté SOUS un slot de capacité."""
    document = _resolve_document(user=user, rag_space_id=rag_space_id,
                                 document_ids=document_ids, question=question)
    deadline.mark("resolve_document")
    deadline.raise_if_expired(phase="resolve_document")
    data, is_csv = _read_document_bytes(document)
    deadline.mark("read_bytes")

    # Refus PRÉCOCE : décider avant de lire, normaliser et transférer. Un document
    # hors limite était auparavant chargé plusieurs minutes avant d'être refusé — le
    # refus était juste, son coût ne l'était pas.
    PF.check(data, is_csv=is_csv, delimiter=";" if is_csv else ",", limits=limits,
             deadline=deadline)
    deadline.mark("preflight")

    catalog = _build_catalog(data, document=document, is_csv=is_csv)
    deadline.mark("catalog")
    deadline.raise_if_expired(phase="catalog")

    table = TS.select_table(question, catalog,
                            max_candidates=limits.max_candidate_tables)
    deadline.mark("table_selection")

    planned = PL.build_plan(question, catalog, table, limits=limits)
    deadline.mark("planning")
    deadline.raise_if_expired(phase="planning")

    # Le plan repasse TOUJOURS par la validation structurelle, même construit par
    # notre propre planner : c'est ce qui rend la frontière vérifiable, et ce qui
    # fera qu'un plan LLM n'aura aucun privilège le jour où il sera activé.
    plan = PS.parse_plan(planned.plan_payload)
    validated = PV.validate(plan, catalog, limits=limits)

    deadline.mark("validation")
    return _execute(plan=plan, validated=validated, catalog=catalog, data=data,
                    is_csv=is_csv, limits=limits, started=started, deadline=deadline)


def execute_validated_plan(*, user, rag_space_id: UUID, plan_payload: dict) -> RF.TabularResult:
    """Exécute un plan fourni tel quel (usage benchmark / futur planner LLM).

    Le plan est validé exactement comme celui du planner interne : mêmes contrôles,
    même ordre, mêmes refus. Un plan externe n'ouvre aucun raccourci.
    """
    if not CFG.query_enabled():
        raise E.TabularUnsupported(E.ERR_FEATURE_DISABLED, "moteur tabulaire désactivé")

    limits = CFG.current_limits()
    started = time.perf_counter()
    deadline = DL.Deadline.from_limits(limits)

    plan = PS.parse_plan(plan_payload)
    document = _resolve_document(user=user, rag_space_id=rag_space_id,
                                 document_ids=[plan.source.document_id], question="")
    if document.id != plan.source.document_id:
        raise E.TabularPermissionDenied(E.ERR_PERMISSION_DENIED, "document hors périmètre")

    data, is_csv = _read_document_bytes(document)
    PF.check(data, is_csv=is_csv, delimiter=";" if is_csv else ",", limits=limits,
             deadline=deadline)
    catalog = _build_catalog(data, document=document, is_csv=is_csv)
    deadline.mark("catalog")
    validated = PV.validate(plan, catalog, limits=limits)

    return _execute(plan=plan, validated=validated, catalog=catalog, data=data,
                    is_csv=is_csv, limits=limits, started=started, deadline=deadline)


# --------------------------------------------------------------------------- #
# Étapes
# --------------------------------------------------------------------------- #
def _resolve_document(*, user, rag_space_id: UUID, document_ids, question: str):
    """Résout le document en respectant STRICTEMENT le périmètre de l'utilisateur.

    Le filtrage par RagSpace accessible est appliqué dans la requête elle-même : un
    document hors périmètre n'est pas « trouvé puis rejeté », il n'existe pas pour
    cette requête. La distinction compte — elle évite que le message d'erreur ne
    révèle l'existence d'un document auquel l'utilisateur n'a pas droit.
    """
    from apps.rag_core.tabular.permissions import accessible_documents

    qs = accessible_documents(user=user, rag_space_id=rag_space_id)
    if document_ids:
        qs = qs.filter(id__in=list(document_ids))

    # `original_filename` est une PROPRIÉTÉ Python, pas une colonne : le filtre doit
    # porter sur `filename`, le champ réellement stocké.
    tabular = qs.filter(filename__iregex=r"\.(xlsx|csv|tsv)$")
    found = list(tabular[:5])

    if not found:
        raise E.TabularError(
            E.ERR_DOCUMENT_NOT_FOUND,
            "aucun document tabulaire accessible ne correspond à cette demande")
    if len(found) > 1:
        named = [d for d in found
                 if d.original_filename and d.original_filename.lower() in question.lower()]
        if len(named) == 1:
            return named[0]
        raise E.TabularClarificationRequired(
            E.ERR_AMBIGUOUS_DOCUMENT,
            "plusieurs documents tabulaires peuvent répondre",
            question="Sur quel document souhaitez-vous que le calcul porte ?",
            candidates=[d.original_filename for d in found])
    return found[0]


def _read_document_bytes(document) -> tuple[bytes, bool]:
    """Lit les octets du document via le stockage Django. Aucun chemin utilisateur.

    Le chemin vient exclusivement du champ `FileField` du modèle, jamais d'une entrée.
    """
    name = (document.original_filename or "").lower()
    if name.endswith(CSV_SUFFIXES):
        is_csv = True
    elif name.endswith(XLSX_SUFFIXES):
        is_csv = False
    else:
        raise E.TabularUnsupported(
            E.ERR_UNSUPPORTED_FILE_FORMAT,
            "seuls les formats .xlsx et .csv sont interrogeables")

    if not document.storage_file:
        raise E.TabularError(E.ERR_DOCUMENT_NOT_FOUND, "fichier source indisponible")
    with document.storage_file.open("rb") as fh:
        return fh.read(), is_csv


def _build_catalog(data: bytes, *, document, is_csv: bool) -> C.DocumentCatalog:
    """Construit le catalogue, lié au hash du document.

    Le catalogue est reconstruit à chaque requête dans ce lot : c'est plus lent, mais
    il ne peut structurellement pas être périmé. La persistance (`TabularTable`) sert
    l'inventaire et l'invalidation, pas le chemin de calcul — un catalogue en base qui
    ne correspondrait plus au fichier serait précisément le défaut à éviter.
    """
    builder = C.build_csv_catalog if is_csv else C.build_xlsx_catalog
    return builder(data, document_id=document.id,
                   content_sha256=document.sha256 or "",
                   filename=document.original_filename or "")


def _execute(*, plan, validated, catalog, data, is_csv, limits, started,
             deadline=None) -> RF.TabularResult:
    deadline = DL.ensure(deadline, limits)
    table = validated.table
    rows = C.load_rows(data, table, is_csv=is_csv, max_rows=limits.max_rows_scanned,
                       deadline=deadline)
    deadline.mark("read_and_type")
    deadline.raise_if_expired(phase="read_and_type")

    columns = [(PC.column_alias(c.position), c.inferred_type) for c in table.columns]
    query = PC.compile_plan(
        plan, positions=validated.positions,
        max_result_rows=limits.max_result_rows,
        column_types={c.column_id: c.inferred_type for c in table.columns})
    outcome = EX.execute(query, columns=columns, rows=rows, limits=limits,
                         deadline=deadline)

    warnings: list[str] = []
    if table.has_formulas:
        # Distinction exigée : valeur EN CACHE lue, pas recalculée par RagATM.
        warnings.append("valeurs de formules lues depuis le cache du classeur ; "
                        "RagATM ne recalcule jamais un classeur")

    execution_id = ""
    selected = _selected_row_count(plan, query, columns, rows, limits, outcome,
                                   deadline=deadline)

    if query.returns_rows:
        if plan.operation in ("argmin", "argmax"):
            result = _argmin_argmax(plan, table, outcome, query)
            evidence = P.build_evidence(
                plan=plan, catalog=catalog, table=table, result=result,
                selected_row_count=selected)
            out = RF.rows_result(
                plan=plan, table=table, columns=query.output_columns,
                rows=(outcome.rows[0],) if outcome.rows else (),
                evidence=evidence, selected_row_count=selected, warnings=warnings)
        else:
            evidence = P.build_evidence(
                plan=plan, catalog=catalog, table=table, result=outcome.rows,
                selected_row_count=selected)
            out = RF.rows_result(
                plan=plan, table=table, columns=query.output_columns,
                rows=outcome.rows, evidence=evidence,
                selected_row_count=selected, warnings=warnings)
    else:
        value = _scalar_value(plan, query, outcome)
        evidence = P.build_evidence(
            plan=plan, catalog=catalog, table=table, result=value,
            selected_row_count=selected)
        out = RF.scalar_result(
            plan=plan, table=table, value=value, evidence=evidence,
            selected_row_count=selected, warnings=warnings)

    execution_id = _persist_execution(plan=plan, catalog=catalog, table=table,
                                      result=out, elapsed=time.perf_counter() - started)
    out.execution_id = execution_id
    out.evidence["execution_id"] = execution_id
    return out


def _scalar_value(plan, query, outcome):
    if not outcome.rows:
        raise E.TabularError(
            E.ERR_EMPTY_SELECTION, "aucune ligne ne correspond aux critères")
    row = outcome.rows[0]

    if query.post_process == PC.POST_DIVIDE:
        total, n = row[0], row[1]
        if not n:
            raise E.TabularError(
                E.ERR_EMPTY_SELECTION,
                "aucune valeur numérique à moyenner dans la sélection")
        return EX.to_decimal(total) / Decimal(int(n))

    value = row[0]
    if value is None and plan.operation in ("sum", "minimum", "maximum"):
        raise E.TabularError(
            E.ERR_EMPTY_SELECTION, "aucune valeur exploitable dans la sélection")
    if isinstance(value, (Decimal, float)) and plan.operation != "count_rows":
        return EX.to_decimal(value)
    return value


def _argmin_argmax(plan, table, outcome, query):
    """Refuse un ex æquo au lieu de le trancher.

    `LIMIT 2` a été demandé exprès : si les deux lignes portent la même mesure,
    « quelle ligne » n'a pas de réponse unique. Répondre la première serait un choix
    arbitraire présenté comme un fait.
    """
    if not outcome.rows:
        raise E.TabularError(E.ERR_EMPTY_SELECTION, "aucune ligne à classer")
    if len(outcome.rows) > 1:
        first, second = outcome.rows[0], outcome.rows[1]
        if first[-1] == second[-1]:
            raise E.TabularError(
                E.ERR_TIE_UNRESOLVED,
                "plusieurs lignes portent la valeur extrême : "
                "« quelle ligne » n'a pas de réponse unique")
    return outcome.rows[0]


def _selected_row_count(plan, query, columns, rows, limits, outcome,
                        *, deadline=None) -> int:
    """Nombre de lignes RETENUES par les filtres — recompté, jamais déduit."""
    if not plan.filters:
        return len(rows)
    where_sql, params = _where_only(plan, query)
    return EX.count_selected(params, columns=columns, rows=rows,
                             where_sql=where_sql, limits=limits, deadline=deadline)


def _where_only(plan, query) -> tuple[str, tuple]:
    """Extrait la clause WHERE compilée, pour recompter la sélection."""
    marker = " WHERE "
    idx = query.sql.find(marker)
    if idx < 0:
        return "", ()
    tail = query.sql[idx:]
    for stop in (" ORDER BY ", " LIMIT "):
        cut = tail.find(stop)
        if cut > 0:
            tail = tail[:cut]
    return tail, query.params


def _persist_execution(*, plan, catalog, table, result, elapsed: float) -> str:
    """Trace l'exécution. Un échec de traçage ne doit jamais invalider un calcul juste."""
    try:
        from apps.rag_core.models import TabularQueryExecution
        with transaction.atomic():
            row = TabularQueryExecution.objects.create(
                document_id=catalog.document_id,
                plan_hash=plan.plan_hash(),
                evidence_hash=result.evidence.get("evidence_hash", ""),
                operation=plan.operation,
                schema_version=plan.schema_version,
                selected_row_count=result.row_count,
                duration_ms=int(elapsed * 1000),
                plan=plan.canonical_dict(),
                evidence=result.evidence,
            )
        return str(row.id)
    except Exception:  # pragma: no cover - la traçabilité n'est pas le calcul
        logger.warning("tabular: échec de persistance de l'exécution", exc_info=True)
        return ""
