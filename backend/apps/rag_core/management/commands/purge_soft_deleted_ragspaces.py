"""Purge CONTRÔLÉE des RagSpaces historiques soft-deleted (dette du 2026-07-16).

Avant la suppression définitive, le bouton « Supprimer définitivement » ne posait que
``deleted_at`` : les espaces ont disparu de l'interface en conservant documents, chunks
et **fichiers**. Cette commande liquide ce résidu.

`deleted_at` est **univoque** : son seul écrivain a toujours été l'ancienne vue DELETE,
et aucun chemin de restauration/archivage n'a jamais existé. Un espace marqué signifie
donc « l'utilisateur a demandé la suppression » — jamais « archivé ».
Cf. docs/07-operations/purge-soft-deleted-ragspaces.md.

Cette commande est un **orchestrateur d'exploitation**, pas une seconde implémentation
de purge : elle délègue à ``delete_rag_space_permanently`` (verrou, fichiers, base,
audit, gardes). Elle ne supprime elle-même **rien**.

    python manage.py purge_soft_deleted_ragspaces                      # inventaire seul
    python manage.py purge_soft_deleted_ragspaces --format json --output /tmp/r.json
    python manage.py purge_soft_deleted_ragspaces --rag-space-id <uuid> \
        --execute --confirm PURGE-SOFT-DELETED-RAGSPACES
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.chat.models import ChatThread
from apps.rag_core.models import Chunk, CorpusAnalysisJob, Document, DocumentSummary, RagSpace
from apps.rag_core.models.document import DocumentStatus


def _count_of(model, fk_field: str, *, extra: Q | None = None):
    """Compteur par SOUS-REQUÊTE (évite le produit cartésien des multi-annotations)."""
    qs = model.objects.filter(**{fk_field: OuterRef("pk")})
    if extra is not None:
        qs = qs.filter(extra)
    qs = qs.order_by().values(fk_field).annotate(n=Count("*")).values("n")
    return Coalesce(Subquery(qs, output_field=IntegerField()), 0)
from apps.rag_core.services.rag_space_deletion import (
    RagSpaceDeletionError,
    RagSpaceNotFound,
    delete_rag_space_permanently,
)

CONFIRM_FILTERED = "PURGE-SOFT-DELETED-RAGSPACES"
CONFIRM_ALL = "PURGE-ALL-SOFT-DELETED-RAGSPACES"

OPERATION = "historical_soft_deleted_ragspace_purge"
SOURCE = "management_command"

READY, WARNING, BLOCKED = "ready", "warning", "blocked"

# Statuts de job qui interdisent la purge : une tâche vivante pourrait écrire.
ACTIVE_JOB_STATUSES = ("pending", "running")
# Statuts de document qui traduisent une ingestion en cours.
INGESTING_STATUSES = (DocumentStatus.PENDING, DocumentStatus.INGESTING)


class Command(BaseCommand):
    help = (
        "Inventorie (et, sur confirmation explicite, purge) les RagSpaces historiques "
        "soft-deleted. SANS OPTION : inventaire seul, aucune suppression."
    )

    # ------------------------------------------------------------------ args
    def add_arguments(self, parser):
        mode = parser.add_argument_group("mode")
        mode.add_argument("--dry-run", action="store_true",
                          help="Inventaire seul (comportement PAR DÉFAUT).")
        mode.add_argument("--execute", action="store_true",
                          help="Purge réelle. Exige --confirm et un filtre (ou --all).")
        mode.add_argument("--confirm", default="",
                          help=f"Token requis en --execute : {CONFIRM_FILTERED} "
                               f"(ou {CONFIRM_ALL} avec --all).")
        mode.add_argument("--all", action="store_true",
                          help="DANGEREUX : autorise une purge SANS filtre. Exige "
                               f"--confirm {CONFIRM_ALL}.")
        mode.add_argument("--stop-on-error", action="store_true",
                          help="S'arrêter au premier échec (défaut : continuer).")

        flt = parser.add_argument_group("filtres (combinables)")
        flt.add_argument("--rag-space-id", default=None, help="UUID d'un RagSpace.")
        flt.add_argument("--workspace-id", default=None, type=int, help="ID du workspace.")
        flt.add_argument("--deleted-before", default=None,
                         help="AAAA-MM-JJ — supprimés STRICTEMENT avant (<).")
        flt.add_argument("--deleted-after", default=None,
                         help="AAAA-MM-JJ — supprimés à partir de (>=, inclusif).")
        flt.add_argument("--deleted-by", default=None, help="username ou id de l'auteur.")
        flt.add_argument("--name-contains", default=None, help="Filtre sur le nom.")
        flt.add_argument("--limit", type=int, default=None, help="Nombre max de candidats.")

        out = parser.add_argument_group("sortie")
        out.add_argument("--format", choices=["text", "json"], default="text")
        out.add_argument("--output", default=None, help="Écrit le rapport dans un fichier.")

    # --------------------------------------------------------------- handle
    def handle(self, *args, **opts):
        execute = bool(opts["execute"])
        filters = self._collect_filters(opts)
        self._validate(opts, filters, execute)

        candidates = self._inventory(opts, filters)

        report: dict[str, Any] = {
            "mode": "execute" if execute else "dry-run",
            "filters": filters,
            "summary": self._summarize(candidates),
            "rag_spaces": candidates,
        }

        if not execute:
            self._render(report, opts)
            return 0

        results = self._purge(candidates, opts, filters)
        report["mode"] = "execute"
        report["results"] = results
        report["summary"]["purged"] = results["purged"]
        report["summary"]["failed"] = results["failed"]
        report["summary"]["blocked"] = results["blocked"]
        report["summary"]["skipped"] = results["skipped"]
        self._render(report, opts)

        # Code 1 si au moins un échec OU un candidat bloqué (non purgé).
        if results["failed"] or results["blocked"]:
            sys.exit(1)
        return 0

    # ------------------------------------------------------------ validation
    def _collect_filters(self, opts) -> dict[str, Any]:
        keys = ("rag_space_id", "workspace_id", "deleted_before", "deleted_after",
                "deleted_by", "name_contains", "limit")
        return {k: opts[k] for k in keys if opts.get(k) not in (None, "")}

    def _validate(self, opts, filters, execute) -> None:
        if opts["dry_run"] and execute:
            raise CommandError("--dry-run et --execute sont exclusifs.")
        if not execute:
            return  # le dry-run n'exige rien : c'est le défaut sûr.

        # `--limit` seul ne délimite RIEN de sûr : il ne compte pas comme filtre.
        real_filters = {k: v for k, v in filters.items() if k != "limit"}

        if opts["all"]:
            if real_filters:
                raise CommandError("--all est incompatible avec un filtre : choisissez l'un ou l'autre.")
            if opts["confirm"] != CONFIRM_ALL:
                raise CommandError(
                    f"--all exige --confirm {CONFIRM_ALL} (token distinct, volontairement "
                    "plus difficile à fournir par accident)."
                )
        else:
            if not real_filters:
                raise CommandError(
                    "REFUS : une purge sans filtre effacerait TOUT l'historique soft-deleted. "
                    "Ciblez (--rag-space-id / --workspace-id / --deleted-before …) ou assumez "
                    f"--all avec --confirm {CONFIRM_ALL}."
                )
            if opts["confirm"] != CONFIRM_FILTERED:
                raise CommandError(
                    f"--execute exige --confirm {CONFIRM_FILTERED} "
                    f"(reçu : {opts['confirm'] or '<vide>'})."
                )

    def _parse_date(self, value: str, label: str):
        try:
            d = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as e:
            raise CommandError(f"{label} : date invalide « {value} » (attendu AAAA-MM-JJ).") from e
        return timezone.make_aware(datetime.combine(d, dtime.min))

    # ------------------------------------------------------------ inventaire
    def _queryset(self, opts):
        # SEULS les soft-deleted sont candidats : un espace actif n'en est jamais un.
        qs = RagSpace.objects.filter(deleted_at__isnull=False)

        if opts.get("rag_space_id"):
            qs = qs.filter(id=opts["rag_space_id"])
        if opts.get("workspace_id"):
            qs = qs.filter(workspace_id=opts["workspace_id"])
        if opts.get("deleted_before"):
            qs = qs.filter(deleted_at__lt=self._parse_date(opts["deleted_before"], "--deleted-before"))
        if opts.get("deleted_after"):
            qs = qs.filter(deleted_at__gte=self._parse_date(opts["deleted_after"], "--deleted-after"))
        if opts.get("name_contains"):
            qs = qs.filter(name__icontains=opts["name_contains"])
        if opts.get("deleted_by"):
            v = str(opts["deleted_by"])
            qs = qs.filter(Q(deleted_by__username=v) | Q(deleted_by__id=v)) if v.isdigit() \
                else qs.filter(deleted_by__username=v)

        # Compteurs par SOUS-REQUÊTES, et non par annotations multi-jointures.
        # Plusieurs Count(distinct=True) sur des relations différentes dans la MÊME
        # requête produisent un produit cartésien (documents × chunks × résumés × …) :
        # mesuré à ~40 s sur un espace de 577 docs / 25 847 chunks. Les sous-requêtes
        # comptent chaque relation indépendamment : aucun N+1, aucune explosion.
        qs = qs.select_related("workspace", "deleted_by").annotate(
            n_documents=_count_of(Document, "rag_space_id"),
            n_chunks=_count_of(Chunk, "rag_space_id"),
            n_summaries=_count_of(DocumentSummary, "rag_space_id"),
            n_jobs=_count_of(CorpusAnalysisJob, "rag_space_id"),
            n_threads=_count_of(ChatThread, "rag_space_id"),
            n_active_jobs=_count_of(CorpusAnalysisJob, "rag_space_id",
                                    extra=Q(status__in=ACTIVE_JOB_STATUSES)),
            n_ingesting=_count_of(Document, "rag_space_id",
                                  extra=Q(status__in=INGESTING_STATUSES)),
        ).order_by("deleted_at")

        if opts.get("limit"):
            qs = qs[: opts["limit"]]
        return qs

    def _inventory(self, opts, filters) -> list[dict[str, Any]]:
        now = timezone.now()
        out: list[dict[str, Any]] = []
        for s in self._queryset(opts).iterator():
            files_ok, files_missing, size = self._probe_files(s)
            anomalies: list[str] = []
            classification = READY

            if s.deleted_by_id is None:
                anomalies.append("no_deleted_by")
                classification = WARNING
            if files_missing:
                anomalies.append(f"documents_without_file:{files_missing}")
                classification = WARNING
            if s.n_active_jobs:
                anomalies.append(f"active_jobs:{s.n_active_jobs}")
                classification = BLOCKED
            if s.n_ingesting:
                anomalies.append(f"ingestion_in_progress:{s.n_ingesting}")
                classification = BLOCKED
            if files_ok is None:
                anomalies.append("storage_unreachable")
                classification = BLOCKED

            out.append({
                "id": str(s.id),
                "name": s.name,
                "workspace_id": s.workspace_id,
                "workspace_name": getattr(s.workspace, "name", ""),
                "deleted_at": s.deleted_at.isoformat() if s.deleted_at else None,
                "deleted_by": getattr(s.deleted_by, "username", None),
                "age_days": (now - s.deleted_at).days if s.deleted_at else None,
                "documents": s.n_documents,
                "chunks": s.n_chunks,
                "summaries": s.n_summaries,
                "jobs": s.n_jobs,
                "threads": s.n_threads,
                "files": files_ok or 0,
                "files_missing": files_missing,
                "bytes_estimated": size,
                "anomalies": anomalies,
                "classification": classification,
            })
        return out

    def _probe_files(self, space) -> tuple[int | None, int, int]:
        """Compte les fichiers réellement présents et leur volume.

        Retourne (présents|None si stockage injoignable, absents, octets).
        """
        present = missing = 0
        size = 0
        docs = Document.objects.filter(rag_space_id=space.id).only("id", "storage_file")
        for d in docs.iterator():
            f = getattr(d, "storage_file", None)
            name = getattr(f, "name", "") or ""
            if not name:
                continue
            try:
                if f.storage.exists(name):
                    present += 1
                    try:
                        size += f.storage.size(name)
                    except OSError:
                        pass
                else:
                    missing += 1
            except Exception:  # noqa: BLE001 — stockage injoignable : on bloque, on ne devine pas.
                return None, missing, size
        return present, missing, size

    def _summarize(self, candidates) -> dict[str, Any]:
        return {
            "candidates": len(candidates),
            "documents": sum(c["documents"] for c in candidates),
            "chunks": sum(c["chunks"] for c in candidates),
            "files": sum(c["files"] for c in candidates),
            "bytes_estimated": sum(c["bytes_estimated"] for c in candidates),
            "ready": sum(1 for c in candidates if c["classification"] == READY),
            "warning": sum(1 for c in candidates if c["classification"] == WARNING),
            "blocked": sum(1 for c in candidates if c["classification"] == BLOCKED),
        }

    # ----------------------------------------------------------------- purge
    def _purge(self, candidates, opts, filters) -> dict[str, Any]:
        purged = failed = blocked = skipped = 0
        details: list[dict[str, Any]] = []

        for c in candidates:
            if c["classification"] == BLOCKED:
                blocked += 1
                details.append({"id": c["id"], "status": "blocked", "anomalies": c["anomalies"]})
                self.stderr.write(f"BLOQUÉ  {c['name']} ({c['id']}) : {', '.join(c['anomalies'])}")
                continue

            try:
                # Toute la suppression (verrou, fichiers, base, audit) appartient au
                # service. La commande n'orchestre que la sélection et le rapport.
                delete_rag_space_permanently(
                    rag_space_id=c["id"],
                    actor=None,  # aucun utilisateur métier fictif : l'origine est dans l'audit.
                    audit_metadata={
                        "operation": OPERATION,
                        "source": SOURCE,
                        "original_deleted_at": c["deleted_at"],
                        "original_deleted_by": c["deleted_by"],
                        "command_filters": filters,
                    },
                )
                purged += 1
                details.append({"id": c["id"], "status": "purged"})
                self.stdout.write(f"PURGÉ   {c['name']} ({c['id']})")
            except RagSpaceNotFound:
                skipped += 1
                details.append({"id": c["id"], "status": "not_found"})
                self.stderr.write(f"ABSENT  {c['id']} (déjà purgé)")
            except RagSpaceDeletionError as e:
                failed += 1
                details.append({"id": c["id"], "status": "failed", "error_code": e.code})
                self.stderr.write(f"ÉCHEC   {c['name']} ({c['id']}) : {e.code} — rien supprimé")
                if opts["stop_on_error"]:
                    break

        return {"purged": purged, "failed": failed, "blocked": blocked,
                "skipped": skipped, "details": details}

    # ---------------------------------------------------------------- rendu
    def _render(self, report, opts) -> None:
        if opts["format"] == "json":
            payload = json.dumps(report, indent=2, ensure_ascii=False)
            if opts["output"]:
                Path(opts["output"]).write_text(payload, encoding="utf-8")
                self.stderr.write(f"rapport écrit : {opts['output']}")
            else:
                # stdout SANS texte parasite : la sortie doit rester parsable.
                self.stdout.write(payload)
            return

        s = report["summary"]
        for c in report["rag_spaces"]:
            self.stdout.write(
                f"\nRagSpace: {c['name']}\n"
                f"  ID          : {c['id']}\n"
                f"  Workspace   : {c['workspace_name']} ({c['workspace_id']})\n"
                f"  Deleted at  : {c['deleted_at']} ({c['age_days']} j)\n"
                f"  Deleted by  : {c['deleted_by'] or '— (inconnu)'}\n"
                f"  Documents   : {c['documents']}   Chunks: {c['chunks']}   "
                f"Résumés: {c['summaries']}   Jobs: {c['jobs']}   Threads: {c['threads']}\n"
                f"  Fichiers    : {c['files']}" + (f" (absents: {c['files_missing']})" if c["files_missing"] else "") + "\n"
                f"  Volume      : {c['bytes_estimated'] / 1e6:.1f} Mo\n"
                f"  Statut      : {c['classification']}"
                + (f"  [{', '.join(c['anomalies'])}]" if c["anomalies"] else "")
            )

        self.stdout.write(
            f"\n{'=' * 58}\n"
            f"Candidats : {s['candidates']}  (ready {s['ready']} / warning {s['warning']} / blocked {s['blocked']})\n"
            f"Documents : {s['documents']}\n"
            f"Chunks    : {s['chunks']}\n"
            f"Fichiers  : {s['files']}\n"
            f"Volume    : {s['bytes_estimated'] / 1e6:.1f} Mo"
        )
        if report["mode"] == "dry-run":
            self.stdout.write(self.style.WARNING(
                "\nDRY-RUN — aucune donnée n'a été supprimée.\n"
                f"Pour purger : --execute --confirm {CONFIRM_FILTERED} (avec un filtre)."
            ))
        else:
            r = report["results"]
            self.stdout.write(
                f"\nPurged: {r['purged']}  Failed: {r['failed']}  "
                f"Blocked: {r['blocked']}  Skipped: {r['skipped']}"
            )
        if opts["output"]:
            Path(opts["output"]).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
