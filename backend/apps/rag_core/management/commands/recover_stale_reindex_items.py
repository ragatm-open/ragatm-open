"""Récupère les items de réindexation « stale » et les documents INGESTING orphelins.

Sans argument : DRY-RUN (n'écrit rien). ``--apply`` exécute la remédiation :
- items RUNNING au lease expiré → INTERRUPTED ;
- documents restés INGESTING sans item actif → FAILED (aucun chunk supprimé).

Voir docs/03-backend/reindexation-jobs.md.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.rag_core.services.ingest.reindex_jobs import recover_stale_items


class Command(BaseCommand):
    help = "Détecte/récupère les items de réindexation bloqués (dry-run par défaut)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Applique la remédiation (sinon dry-run, rien n'est modifié).",
        )

    def handle(self, *args, **opts):
        apply = bool(opts.get("apply"))
        res = recover_stale_items(dry_run=not apply)
        items = res.get("stale_items", [])
        docs = res.get("stale_ingesting_documents", [])
        mode = "APPLIQUÉ" if apply else "DRY-RUN (aucune écriture)"
        self.stdout.write(self.style.WARNING(f"[{mode}]"))
        self.stdout.write(f"Items RUNNING stale : {len(items)}")
        for i in items:
            self.stdout.write(f"  - item {i}")
        self.stdout.write(f"Documents INGESTING orphelins : {len(docs)}")
        for d in docs:
            self.stdout.write(f"  - document {d}")
        if not apply and (items or docs):
            self.stdout.write(self.style.NOTICE("Relancer avec --apply pour remédier."))
        if apply:
            self.stdout.write(self.style.SUCCESS("Remédiation appliquée (aucun chunk supprimé)."))
