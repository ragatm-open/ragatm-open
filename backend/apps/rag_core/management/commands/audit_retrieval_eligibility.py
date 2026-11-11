"""Audit NON DESTRUCTIF de l'invariant d'éligibilité au retrieval (Lot 2 — P0-A).

Compte les objets qui violent — ou approchent — l'invariant :

    un chunk n'est utilisable que si son document est READY
    et si son RagSpace n'est pas supprimé.

N'écrit rien, ne supprime rien. Conçu pour être rejoué en CI, en préflight de
migration, et après incident.

    python manage.py audit_retrieval_eligibility
    python manage.py audit_retrieval_eligibility --json
    python manage.py audit_retrieval_eligibility --fail-on-violation

TROIS NIVEAUX, à ne pas confondre
---------------------------------
* **bloquant** — l'invariant est contredit par les DONNÉES d'une façon que le code
  ne peut pas rattraper. Aujourd'hui : uniquement la fuite d'isolation (un chunk
  dont le ``rag_space`` diffère de celui de son document), car aucune clause de
  requête ne la corrige.
* **résiduel** — des chunks inéligibles EXISTENT en base mais sont désormais
  **inertes** : les requêtes de retrieval les excluent. C'est attendu sous la
  stratégie Option A (cf. ``domain/eligibility.py``) — un document ``FAILED``
  conserve ses anciens chunks, ils ne sont simplement plus servis. À traiter comme
  de l'hygiène de données, pas comme un incident.
* **informatif** — signaux de qualité (chunks sans embedding, extraction partielle).

``--fail-on-violation`` ne réagit qu'au niveau **bloquant**. ``--strict-data`` y
ajoute le niveau résiduel, pour les déploiements qui exigent zéro résidu.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import connection

#: Requêtes d'audit. `blocking=True` => compte non nul == violation d'invariant.
CHECKS: list[dict] = [
    {
        "key": "chunks_of_non_ready_documents",
        "blocking": False,
        "residual": True,
        "label": "Chunks rattachés à un document non-READY",
        "sql": """
            SELECT count(*) FROM rag_chunk c
            JOIN rag_document d ON d.id = c.document_id
            WHERE d.status <> 'READY'
        """,
    },
    {
        "key": "chunks_in_soft_deleted_spaces",
        "blocking": False,
        "residual": True,
        "label": "Chunks appartenant à un RagSpace soft-deleted",
        "sql": """
            SELECT count(*) FROM rag_chunk c
            JOIN rag_document d ON d.id = c.document_id
            JOIN rag_space s ON s.id = d.rag_space_id
            WHERE s.deleted_at IS NOT NULL
        """,
    },
    {
        "key": "chunks_space_mismatch",
        "blocking": True,
        "label": "Chunks dont le rag_space diffère de celui de leur document (fuite d'isolation)",
        "sql": """
            SELECT count(*) FROM rag_chunk c
            JOIN rag_document d ON d.id = c.document_id
            WHERE c.rag_space_id <> d.rag_space_id
        """,
    },
    {
        "key": "corpus_chunks_of_non_ready_documents",
        "blocking": False,
        "residual": True,
        "label": "Chunks de corpus métier rattachés à un document non-READY",
        "sql": """
            SELECT count(*) FROM domain_corpus_chunk c
            JOIN domain_corpus_document d ON d.id = c.document_id
            WHERE d.status <> 'READY'
        """,
    },
    {
        "key": "ready_documents_without_chunks",
        "blocking": False,
        "label": "Documents READY sans aucun chunk (indexation vide)",
        "sql": """
            SELECT count(*) FROM rag_document d
            WHERE d.status = 'READY'
              AND NOT EXISTS (SELECT 1 FROM rag_chunk c WHERE c.document_id = d.id)
        """,
    },
    {
        "key": "non_ready_documents_with_chunks",
        "blocking": False,
        "label": "Documents non-READY conservant des chunks (attendu : réindexation interrompue)",
        "sql": """
            SELECT count(*) FROM rag_document d
            WHERE d.status <> 'READY'
              AND EXISTS (SELECT 1 FROM rag_chunk c WHERE c.document_id = d.id)
        """,
    },
    {
        "key": "chunks_without_embedding",
        "blocking": False,
        "label": "Chunks sans embedding (invisibles au leg vectoriel)",
        "sql": "SELECT count(*) FROM rag_chunk WHERE embedding IS NULL",
    },
    {
        "key": "soft_deleted_spaces",
        "blocking": False,
        "label": "RagSpaces soft-deleted (à purger ou restaurer)",
        "sql": "SELECT count(*) FROM rag_space WHERE deleted_at IS NOT NULL",
    },
    {
        "key": "documents_partial_extraction",
        "blocking": False,
        "label": "Documents READY dont l'extraction est PARTIELLE (éligibles, cf. doctrine)",
        "sql": """
            SELECT count(*) FROM rag_document d
            WHERE d.status = 'READY'
              AND d.meta -> 'extraction' ->> 'extraction_status' = 'partial'
        """,
    },
]


class Command(BaseCommand):
    help = "Audite l'invariant d'éligibilité au retrieval (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Sortie JSON.")
        parser.add_argument(
            "--fail-on-violation",
            action="store_true",
            help="Code de sortie 1 si une catégorie BLOQUANTE est non vide.",
        )
        parser.add_argument(
            "--strict-data",
            action="store_true",
            help="Traite aussi les catégories RÉSIDUELLES comme bloquantes.",
        )

    def handle(self, *args, **opts):
        results: dict[str, dict] = {}
        with connection.cursor() as cur:
            for check in CHECKS:
                cur.execute(check["sql"])
                count = int(cur.fetchone()[0] or 0)
                results[check["key"]] = {
                    "count": count,
                    "blocking": check["blocking"],
                    "residual": bool(check.get("residual")),
                    "label": check["label"],
                }

        def _is_fatal(row: dict) -> bool:
            return row["blocking"] or bool(opts["strict_data"] and row["residual"])

        violations = {k: v for k, v in results.items() if _is_fatal(v) and v["count"] > 0}
        residuals = {k: v for k, v in results.items() if v["residual"] and v["count"] > 0}

        if opts["json"]:
            self.stdout.write(json.dumps(
                {
                    "checks": results,
                    "violations": sorted(violations),
                    "violation_total": sum(v["count"] for v in violations.values()),
                    "residuals": sorted(residuals),
                    "residual_total": sum(v["count"] for v in residuals.values()),
                    "ok": not violations,
                },
                ensure_ascii=False,
                indent=2,
            ))
        else:
            self.stdout.write("Audit d'éligibilité au retrieval\n")
            for row in results.values():
                if row["blocking"]:
                    mark = "✗ " if row["count"] else "✓ "
                elif row["residual"]:
                    mark = "~ " if row["count"] else "✓ "
                else:
                    mark = "  "
                self.stdout.write(f"{mark}{row['count']:>8}  {row['label']}")
            self.stdout.write("")
            self.stdout.write("  ✓ conforme   ✗ BLOQUANT   ~ résiduel (inerte : exclu du retrieval)")
            self.stdout.write("")
            if violations:
                self.stdout.write(self.style.ERROR(
                    f"{len(violations)} catégorie(s) BLOQUANTE(S) non vide(s)."
                ))
            else:
                self.stdout.write(self.style.SUCCESS(
                    "Invariant respecté : aucun chunk inéligible ne peut être servi."
                ))
            if residuals:
                total = sum(v["count"] for v in residuals.values())
                self.stdout.write(
                    f"Résidu en base : {total} chunk(s) inéligible(s) mais INERTES "
                    "(exclus par les requêtes). Hygiène de données — voir "
                    "purge_soft_deleted_ragspaces."
                )

        if violations and opts["fail_on_violation"]:
            raise SystemExit(1)
