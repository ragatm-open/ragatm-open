"""Audit READ-ONLY de la couverture de pipeline d'embeddings (lot 3, §12).

Répond à « avec quel pipeline cet espace est-il RÉELLEMENT indexé ? » — question
distincte de « avec quel pipeline est-il CONFIGURÉ ? », et c'est tout l'intérêt :
c'est l'écart entre les deux qui rend une migration partielle invisible.

N'écrit rien.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.llm_router.embeddings import resolve_space_embedding, space_is_governed
from apps.rag_core.models import RagSpace
from apps.rag_core.services.embedding_pipeline_coverage import (
    compute_coverage,
    observed_pipeline_families,
)


class Command(BaseCommand):
    help = (
        "Audit READ-ONLY : familles de pipeline d'embeddings réellement présentes "
        "dans un RagSpace (ou tous), et couverture face au pipeline attendu."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--rag-space", dest="rag_space", default=None,
            help="Limiter à un RagSpace (UUID). Sans argument : inventaire de tous les espaces.",
        )
        parser.add_argument(
            "--expected-family", dest="expected_family", default=None,
            help=(
                "Famille attendue explicite (ex. `fr-en-retrieval@1a37926b`). À défaut, "
                "elle est DÉDUITE des chunks présents — jamais fabriquée depuis le modèle."
            ),
        )
        parser.add_argument(
            "--probe", action="store_true",
            help=(
                "Demande son identité à la Gateway par un appel d'embedding RÉEL sur une "
                "chaîne neutre (aucun contenu documentaire). Seule façon de connaître la "
                "famille attendue d'un espace encore VIDE."
            ),
        )
        parser.add_argument(
            "--exclude-document", dest="exclude_documents", action="append", default=[],
            metavar="UUID",
            help=(
                "Retire un document du PÉRIMÈTRE mesuré (répétable). Il reste compté "
                "à part (`excluded_documents`), jamais effacé ni réétiqueté."
            ),
        )
        parser.add_argument("--json", dest="as_json", action="store_true", help="Sortie JSON.")

    def handle(self, *args, **opts):
        qs = RagSpace.objects.all().order_by("created_at")
        if opts["rag_space"]:
            qs = qs.filter(id=opts["rag_space"])
            if not qs.exists():
                raise CommandError(f"RagSpace introuvable: {opts['rag_space']}")

        expected = opts.get("expected_family")
        if opts.get("probe"):
            if not opts["rag_space"]:
                raise CommandError("--probe exige --rag-space (un appel réseau par espace).")
            from apps.rag_core.services.embedding_pipeline_coverage import expected_pipeline_family

            expected = expected_pipeline_family(qs.first(), probe=True)

        results = []
        for space in qs:
            provider, model = resolve_space_embedding(space)
            cov = compute_coverage(
                space, expected_family=expected,
                excluded_document_ids=opts.get("exclude_documents") or None,
            )
            row = {
                **cov.to_dict(),
                "rag_space_name": getattr(space, "name", ""),
                "configured_provider": provider or "",
                "configured_model": model or "",
                "observed_families": observed_pipeline_families(space),
            }
            results.append(row)

        if opts["as_json"]:
            self.stdout.write(json.dumps(results, indent=2, ensure_ascii=False))
            return

        w = self.stdout.write
        for r in results:
            w("")
            w(f"RagSpace   : {r['rag_space_name']} ({r['rag_space_id']})")
            w(f"Configuré  : provider={r['configured_provider'] or '—'} "
              f"model={r['configured_model'] or '—'} gouverné={r['governed']}")
            w(f"Attendu    : {r['expected_pipeline_family'] or '(indéterminé)'}")
            w(f"Documents  : {r['total_documents']} | Chunks : {r['total_chunks']}"
              + (f" | EXCLUS : {r['excluded_documents']} doc / {r['excluded_chunks']} chunks"
                 if r['excluded_documents'] else ""))
            w(f"  conformes         : {r['expected_pipeline_chunks']}")
            w(f"  autre pipeline    : {r['other_pipeline_chunks']}")
            w(f"  sans identité     : {r['null_pipeline_chunks']}")
            w(f"  docs complets     : {r['fully_conformant_documents']}")
            w(f"  docs PARTIELS     : {r['partially_conformant_documents']}")
            w(f"  docs non conformes: {r['non_conformant_documents']}")
            if r["observed_families"]:
                w("  familles observées :")
                for fam, n in r["observed_families"].items():
                    w(f"    {fam or '(NULL — chunk historique)':50} {n}")
            style = self.style.SUCCESS if r["status"] == "consistent" else self.style.WARNING
            w(style(f"  statut : {r['status']} | pilote acceptable : {r['pilot_acceptable']}"))
