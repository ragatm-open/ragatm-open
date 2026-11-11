"""Chiffrer un ré-embedding AVANT de le lancer — LECTURE SEULE, stricte.

Pourquoi une commande séparée du ré-embedding lui-même
------------------------------------------------------
Une migration d'embeddings se décide sur des volumes : combien de documents sont
éligibles, combien de fragments dépassent le plafond du modèle visé, combien d'appels
cela représente. Ces chiffres doivent pouvoir être établis en production **sans**
qu'aucune écriture ne soit possible — un `--dry-run` sur une commande qui sait écrire
laisse toujours la possibilité d'un drapeau oublié.

Cette commande n'appelle donc jamais `reembed_document`. Elle ne peut pas écrire :
elle ne connaît que `preflight_space` et `preflight_document`.

Elle n'appelle pas non plus la Gateway : l'éligibilité se décide sur l'état de la base
et sur la table de capacités déclarée. Une Gateway indisponible ne doit pas empêcher
de préparer une migration.

    python manage.py reembed_preflight --rag-space <uuid> --provider ai_gateway --model legacy-768
    python manage.py reembed_preflight --rag-space <uuid> --json rapport.json --details
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.rag_core.models import Document, RagSpace
from apps.rag_core.services.ingest.reembed import preflight_document, preflight_space


class Command(BaseCommand):
    help = "Chiffre un ré-embedding sans rien écrire (documents éligibles, volumes, coût)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--rag-space", required=True, help="UUID de l'espace.")
        parser.add_argument("--provider", default="", help="Provider cible (défaut : celui de l'espace).")
        parser.add_argument("--model", default="", help="Modèle cible (défaut : celui de l'espace).")
        parser.add_argument("--details", action="store_true",
                            help="Détaille les documents INÉLIGIBLES (au plus 20).")
        parser.add_argument("--json", dest="json_out", default="")

    def handle(self, *args, **options) -> None:
        try:
            space = RagSpace.objects.get(id=options["rag_space"])
        except (RagSpace.DoesNotExist, ValueError, TypeError) as exc:
            raise CommandError(f"espace introuvable : {options['rag_space']}") from exc

        agg = preflight_space(
            space,
            provider=options["provider"] or None,
            model=options["model"] or None,
        )
        rapport = agg.as_dict()
        rapport["rag_space_name"] = space.name
        rapport["space_identity"] = f"{space.embedding_provider}/{space.embedding_model}"

        if options["details"]:
            inaptes = []
            for doc in Document.objects.filter(rag_space=space).iterator():
                pf = preflight_document(doc, provider=agg.target_provider, model=agg.target_model)
                if not pf.eligible:
                    inaptes.append(pf.as_dict())
                if len(inaptes) >= 20:
                    break
            rapport["ineligible_documents"] = inaptes

        if options["json_out"]:
            Path(options["json_out"]).write_text(
                json.dumps(rapport, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )

        self._render(rapport, agg)

    def _render(self, r: dict, agg) -> None:
        w = self.stdout.write
        w(f"préflight de ré-embedding — « {r['rag_space_name']} » ({agg.rag_space_id[:8]})")
        w(f"  identité de l'espace : {r['space_identity']}")
        w(f"  identité visée       : {r['target_identity']}")
        w("")
        w(f"  documents            : {agg.documents_total}")
        w(f"    éligibles          : {agg.documents_eligible}")
        w(f"    déjà à l'identité  : {agg.documents_already_target}")
        for motif, n in sorted(agg.ineligible_by_reason.items()):
            w(f"    inéligibles ({motif}) : {n}")
        w("")
        w(f"  fragments à recalculer : {agg.chunks_eligible}")
        w(f"  plus long fragment     : {agg.max_chunk_chars} car.")
        if agg.chunks_over_limit:
            w(f"  fragments HORS LIMITE  : {agg.chunks_over_limit} — refusés, jamais tronqués")
        w(f"  appels d'embedding     : ~{agg.estimated_embedding_calls} (lots)")
        w("")
        w("  répartition des identités actuelles :")
        for cle, n in sorted(agg.identity_distribution.items(), key=lambda kv: -kv[1]):
            w(f"    {cle:<40} {n}")
        w("")
        if not agg.space_identity_matches_target:
            # Ce n'est pas un défaut de la primitive, c'est une propriété de la
            # migration : tant que l'espace déclare une autre identité, la garde de
            # cohérence P1-D exclut les documents migrés de la recherche vectorielle.
            w("ATTENTION — l'espace ne déclare PAS l'identité visée.")
            w("  Les documents ré-embarqués porteraient une empreinte différente de celle")
            w("  de leur espace : la garde de cohérence les rendrait INVISIBLES à la")
            w("  recherche vectorielle jusqu'à la bascule de l'espace. L'ordre des deux")
            w("  opérations, et la fenêtre entre elles, relèvent du lot de migration.")
        else:
            w("l'espace déclare déjà l'identité visée : aucune fenêtre d'invisibilité.")
