"""Audit LECTURE SEULE du profil d'embeddings effectif + cohérence de l'index.

Répond à la question que l'audit de configuration a rendue urgente : « la config
annonce un provider, mais l'index est-il réellement utilisable par la recherche
vectorielle ? ». Un espace peut afficher ``ollama/nomic-embed-text`` et n'avoir
**aucun vecteur** : la clause de cohérence de ``search_chunks_vector`` exclut alors
100 % des documents, et le retrieval dégrade en FTS **sans le moindre signal**.

    python manage.py audit_embeddings_config          # tableau + code de sortie
    python manage.py audit_embeddings_config --json   # sortie machine

Codes de sortie :
    0 = aligné (config conforme ET index utilisable)
    1 = configuration divergente
    2 = index incompatible ou incomplet

Aucune écriture, aucun secret affiché.
"""
from __future__ import annotations

import json
import sys

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count

from apps.rag_core.models import Chunk, Document, RagSpace

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_INDEX = 2


class Command(BaseCommand):
    help = (
        "Audite (lecture seule) le profil d'embeddings effectif et la cohérence de "
        "l'index. Exit 0 = aligné, 1 = config divergente, 2 = index incompatible."
    )

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Sortie JSON machine.")

    def handle(self, *args, **opts):
        from apps.llm_router.embeddings import default_embedding_model_for_provider

        exp_provider = getattr(settings, "EMBEDDINGS_EXPECTED_PROVIDER", "ai_gateway")
        exp_model = getattr(settings, "EMBEDDINGS_EXPECTED_MODEL", "legacy-768")

        provider = str(getattr(settings, "EMBEDDINGS_PROVIDER", "") or "").strip().lower()
        model = (default_embedding_model_for_provider(provider) or "").strip()
        dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 0) or 0)
        hybrid = bool(getattr(settings, "RAG_HYBRID_ENABLED", False))
        min_score = float(getattr(settings, "RAG_MIN_SCORE", 0.0))

        config_issues: list[str] = []
        if provider != exp_provider:
            config_issues.append(f"provider={provider!r} != {exp_provider!r}")
        if model and model != exp_model:
            config_issues.append(f"model={model!r} != {exp_model!r}")
        if dim != 768:
            config_issues.append(f"dimension={dim} != 768")
        if not hybrid:
            config_issues.append("hybrid désactivé (leg vectoriel inutilisé)")

        # --- Index (agrégats uniquement, aucune donnée documentaire) ---
        n_spaces = RagSpace.objects.count()
        n_docs = Document.objects.count()
        n_chunks = Chunk.objects.count()
        n_with = Chunk.objects.filter(embedding__isnull=False).count()
        n_without = Chunk.objects.filter(embedding__isnull=True).count()

        has_fp = hasattr(Document, "embedding_provider")
        by_fp: list[dict] = []
        n_no_fp = n_incompatible = 0
        if has_fp:
            for row in (
                Document.objects.values("embedding_provider", "embedding_model")
                .annotate(n=Count("id"))
                .order_by("-n")
            ):
                by_fp.append(
                    {
                        "provider": row["embedding_provider"] or "",
                        "model": row["embedding_model"] or "",
                        "n": row["n"],
                    }
                )
            n_no_fp = Document.objects.filter(embedding_provider="").count()
            n_incompatible = (
                Document.objects.exclude(embedding_provider=exp_provider)
                .exclude(embedding_provider="")
                .count()
            )
            # Documents réellement utilisables par le leg vectoriel.
            n_vector_ready = Document.objects.filter(
                embedding_provider=exp_provider, embedding_model=exp_model,
                reindex_required=False,
            ).count()
        else:
            n_vector_ready = 0

        index_issues: list[str] = []
        if n_docs > 0:
            if n_without > 0:
                index_issues.append(f"{n_without} chunk(s) sans embedding")
            if has_fp and n_no_fp > 0:
                index_issues.append(f"{n_no_fp} document(s) sans empreinte provider/model")
            if has_fp and n_incompatible > 0:
                index_issues.append(
                    f"{n_incompatible} document(s) indexé(s) avec un autre provider"
                )
            # Le cas exact constaté en staging : des documents READY mais AUCUN
            # document exploitable par le vectoriel -> sémantique inerte, silencieuse.
            n_ready = Document.objects.filter(status="READY").count()
            if n_ready > 0 and n_vector_ready == 0:
                index_issues.append(
                    f"ALERTE : {n_ready} document(s) READY mais 0 exploitable par le "
                    "leg vectoriel -> recherche sémantique INERTE (dégrade en FTS)"
                )
        if not has_fp:
            index_issues.append(
                "code trop ancien : Document n'a pas d'empreinte embedding_provider"
            )

        payload = {
            "config": {
                "provider": provider,
                "model": model,
                "dimension": dim,
                "hybrid_enabled": hybrid,
                "min_score": min_score,
                "expected": {"provider": exp_provider, "model": exp_model, "dimension": 768},
                "issues": config_issues,
            },
            "index": {
                "rag_spaces": n_spaces,
                "documents": n_docs,
                "chunks": n_chunks,
                "chunks_with_embedding": n_with,
                "chunks_without_embedding": n_without,
                "documents_without_fingerprint": n_no_fp,
                "documents_incompatible": n_incompatible,
                "documents_vector_ready": n_vector_ready,
                "by_fingerprint": by_fp,
                "issues": index_issues,
            },
        }

        code = EXIT_OK
        if config_issues:
            code = EXIT_CONFIG
        if index_issues:
            code = EXIT_INDEX  # l'index prime : un index cassé est plus grave

        if opts.get("json"):
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            self._render(payload, code)
        sys.exit(code)

    def _render(self, p: dict, code: int) -> None:
        c, i = p["config"], p["index"]
        w = self.stdout.write
        w("── Profil d'embeddings effectif ──────────────────────────────")
        w(f"  provider            : {c['provider']}   (attendu: {c['expected']['provider']})")
        w(f"  model               : {c['model']}   (attendu: {c['expected']['model']})")
        w(f"  dimension           : {c['dimension']}   (attendu: 768)")
        w(f"  hybrid_enabled      : {c['hybrid_enabled']}")
        w(f"  min_score           : {c['min_score']}")
        w("── Index ─────────────────────────────────────────────────────")
        w(f"  RagSpaces / Documents / Chunks : {i['rag_spaces']} / {i['documents']} / {i['chunks']}")
        w(f"  chunks avec embedding          : {i['chunks_with_embedding']}")
        w(f"  chunks SANS embedding          : {i['chunks_without_embedding']}")
        w(f"  documents sans empreinte       : {i['documents_without_fingerprint']}")
        w(f"  documents incompatibles        : {i['documents_incompatible']}")
        w(f"  documents exploitables vectoriel: {i['documents_vector_ready']}")
        for fp in i["by_fingerprint"]:
            w(f"    - provider={fp['provider']!r} model={fp['model']!r} : {fp['n']}")
        for msg in c["issues"]:
            w(self.style.WARNING(f"  ⚠ config : {msg}"))
        for msg in i["issues"]:
            w(self.style.ERROR(f"  ✖ index  : {msg}"))
        if code == EXIT_OK:
            w(self.style.SUCCESS("✓ ALIGNÉ (config conforme, index utilisable)"))
        elif code == EXIT_CONFIG:
            w(self.style.WARNING("⚠ CONFIGURATION DIVERGENTE (exit 1)"))
        else:
            w(self.style.ERROR("✖ INDEX INCOMPATIBLE OU INCOMPLET (exit 2)"))
