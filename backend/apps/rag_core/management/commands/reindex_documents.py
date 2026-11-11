"""Réindexation CIBLÉE de documents depuis leur fichier source — P1.9-e (§9).

Réextrait, rechunke et revectorise un ou plusieurs documents **nommément désignés**. Ne
touche à rien d'autre : réindexer un corpus entier pour corriger deux documents ferait
dériver tout le reste sans nécessité, et rendrait la cause d'un futur écart indémêlable.

Idempotente : relancer sur un document déjà correct produit le même résultat (mêmes textes
de chunks, donc même empreinte documentaire).

Sauvegarde l'état AVANT (chunk ids, checksum, statut, meta) dans un fichier local — sans
quoi un retour arrière serait impossible et la comparaison avant/après invérifiable.

Codes de sortie : 0 succès · 1 au moins un document en échec · 2 précondition non tenue.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.rag_core.benchmark import manifest as mf
from apps.rag_core.models import Chunk, Document
from apps.rag_core.services.ingest.extraction.text_quality import (
    classify_text,
    extraction_quality_score,
)
from apps.rag_core.services.ingest.indexing import extract_and_index_bytes


def _snapshot(doc: Document) -> dict:
    chunks = list(Chunk.objects.filter(document=doc).order_by("chunk_index")
                  .values("id", "chunk_index", "text"))
    texts = [c["text"] or "" for c in chunks]
    return {
        "document_id": str(doc.id),
        "filename": doc.filename,
        "status": doc.status,
        "chunk_count": len(chunks),
        "chunk_ids": [str(c["id"]) for c in chunks],
        "checksum": mf.document_checksum(doc.filename, texts),
        "embedding_provider": doc.embedding_provider,
        "embedding_model": doc.embedding_model,
        "quality_classes": sorted({classify_text(t) for t in texts}) if texts else [],
        "meta_extraction": (doc.meta or {}).get("extraction", {}),
    }


class Command(BaseCommand):
    help = "Réextrait et réindexe des documents ciblés (par --document-id)."

    def add_arguments(self, parser):
        parser.add_argument("--document-id", action="append", required=True,
                            dest="document_ids")
        parser.add_argument("--ocr", action="store_true",
                            help="Autorise l'OCR local pour CES documents uniquement "
                                 "(ne modifie pas le réglage global).")
        parser.add_argument("--backup", help="Fichier local d'état avant/après (JSON).")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        docs = []
        for ident in opts["document_ids"]:
            try:
                docs.append(Document.objects.get(id=ident))
            except (Document.DoesNotExist, ValueError) as exc:
                raise CommandError(f"document introuvable : {ident}") from exc

        before = [_snapshot(d) for d in docs]
        for snap in before:
            self.stdout.write(
                f"  avant — {snap['document_id'][:8]}… chunks={snap['chunk_count']} "
                f"statut={snap['status']} qualité={snap['quality_classes']}"
            )

        if opts["dry_run"]:
            self.stdout.write("(dry-run : aucune écriture)")
            sys.exit(0)

        after = []
        failures = 0
        for doc in docs:
            try:
                raw = doc.storage_file.read()
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"✗ {doc.id} : fichier source illisible ({exc})")
                failures += 1
                continue
            finally:
                try:
                    doc.storage_file.close()
                except Exception:  # noqa: BLE001, S110
                    pass

            if not raw:
                self.stderr.write(f"✗ {doc.id} : fichier source vide")
                failures += 1
                continue

            # Les anciens chunks sont supprimés par l'indexation (pas de doublon possible).
            res = extract_and_index_bytes(
                doc=doc, space=doc.rag_space, file_bytes=raw,
                filename=doc.filename, content_type=doc.content_type or "application/pdf",
                ocr_enabled=True if opts["ocr"] else None,
            )
            doc.refresh_from_db()
            snap = _snapshot(doc)
            snap["chunks_created"] = res.chunks_created
            snap["ingest_status"] = res.status
            after.append(snap)
            if doc.status == "FAILED":
                failures += 1
                self.stdout.write(
                    f"✗ {str(doc.id)[:8]}… FAILED — {doc.error_message or ''}"[:160]
                )
            else:
                texts = list(Chunk.objects.filter(document=doc).values_list("text", flat=True))
                score = extraction_quality_score("\n".join(t or "" for t in texts))
                self.stdout.write(
                    f"✓ {str(doc.id)[:8]}… {snap['status']} chunks={snap['chunk_count']} "
                    f"qualité={snap['quality_classes']} score={score}"
                )

        if opts["backup"]:
            path = Path(opts["backup"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"before": before, "after": after},
                                       ensure_ascii=False, indent=2), encoding="utf-8")
            self.stdout.write(f"✓ état avant/après → {path}")

        sys.exit(1 if failures else 0)
