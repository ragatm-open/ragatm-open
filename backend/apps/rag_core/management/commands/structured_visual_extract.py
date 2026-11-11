"""Extraction structurée expérimentale d'UN visuel.

    python manage.py structured_visual_extract --document <uuid> --list
    python manage.py structured_visual_extract --document <uuid> --asset <sha256|préfixe ≥ 12> \\
        --task chart|table|auto [--dry-run] [--include-response] [--emit-image]
    python manage.py structured_visual_extract --document <uuid> --auto-document

Outil de DIAGNOSTIC et de RATTRAPAGE : le fonctionnement nominal est automatique
(``structured_auto``, après le describe). ``--task chart|table`` force la tâche ;
``--task auto`` applique la classification du chemin automatique ; ``--auto-document``
rejoue la passe automatique de la génération courante (visuels non réglés seulement).
``--list`` est en lecture seule
(découverte existante, aucun appel). L'extraction exige STRUCTURED_VISUAL_ENABLED, un
espace de STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST et un environnement hors production.
``--dry-run`` appelle le Gateway et normalise sans rien écrire. Sortie : UNE ligne JSON
sur stdout (métadonnées ; réponse brute et image seulement sur demande).
"""
from __future__ import annotations

import base64
import json

from django.core.management.base import BaseCommand, CommandError

from apps.rag_core.models import Document
from apps.rag_core.services.ingest.visual import structured as S


class Command(BaseCommand):
    help = "Extraction structurée (chart|table) d'un visuel via AI Gateway, publiée dans l'index."

    def add_arguments(self, parser):
        parser.add_argument("--document", required=True)
        parser.add_argument("--list", action="store_true")
        parser.add_argument("--asset", default="")
        parser.add_argument("--task", choices=(*S.TASKS, "auto"))
        parser.add_argument("--auto-document", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--include-response", action="store_true")
        parser.add_argument("--emit-image", action="store_true")

    def handle(self, *args, **opts):
        doc = Document.objects.filter(id=opts["document"]).first()
        if doc is None:
            raise CommandError("document introuvable")
        if opts["list"]:
            assets = S.discover(doc)
            self.stdout.write(json.dumps({
                "document_id": str(doc.id), "filename": doc.filename,
                "rag_space_id": str(doc.rag_space_id), "status": doc.status,
                "assets": [{"asset_hash": a.content_hash, "source_page": a.source_page,
                            "ordinal": a.ordinal, "width": a.width, "height": a.height,
                            "bytes": len(a.content), "mime_type": a.mime_type, "origin": a.origin,
                            "source_kind": a.source_kind} for a in assets],
            }, ensure_ascii=False))
            return
        if opts["auto_document"]:
            from apps.rag_core.services.ingest.visual import deferred as D
            from apps.rag_core.services.ingest.visual import structured_auto as A

            try:
                S.check_scope(doc)
            except S.StructuredVisualRefused as exc:
                raise CommandError(f"refusé : {exc.code}") from None
            generation = D.state_of(doc).get("generation")
            if not generation:
                raise CommandError("refusé : aucune génération d'enrichissement")
            summary = A.run_auto(document_id=doc.id, generation=generation, reschedule=False)
            self.stdout.write(json.dumps({"document_id": str(doc.id), "generation": generation, **summary},
                                         ensure_ascii=False, default=str))
            return
        if opts["task"] == "auto":
            from apps.rag_core.services.ingest.visual import structured_auto as A

            evidence = A.describe_evidence(doc.id)
            asset = S.find_asset(S.discover(doc), opts["asset"])
            cls, proof = A.classify_from_describe(*evidence.get(asset.content_hash, (None, None)))
            task = A.TASK_FOR_CLASS.get(cls)
            if task is None:
                self.stdout.write(json.dumps({"asset_hash": asset.content_hash, "class": cls,
                                              "evidence": proof, "status": "skipped"}, ensure_ascii=False))
                return
            opts["task"], opts["asset"] = task, asset.content_hash
            self.stderr.write(f"auto: class={cls} evidence={proof} task={task}")
        if not opts["asset"] or not opts["task"]:
            raise CommandError("--asset et --task sont obligatoires hors --list")
        try:
            out = S.run_structured_extraction(document_id=doc.id, asset_hash=opts["asset"],
                                              task=opts["task"], do_publish=not opts["dry_run"])
        except S.StructuredVisualRefused as exc:
            raise CommandError(f"refusé : {exc.code}") from None
        payload = {
            "document_id": out.document_id, "filename": doc.filename, "asset_hash": out.asset_hash,
            "task": out.task, "status": out.status, "error_code": out.error_code,
            "gateway_calls": out.gateway_calls, "retries": out.retries, "elapsed_ms": out.elapsed_ms,
            "units": out.units, "table_fragments": out.table_fragments,
            "chunks_published": out.chunks_published, "chunks_replaced": out.chunks_replaced,
            "dry_run": bool(opts["dry_run"]), "units_text": out.units_text, "table_texts": out.table_texts,
        }
        if opts["include_response"]:
            payload["response"] = out.response
        if opts["emit_image"]:
            asset = S.find_asset(S.discover(doc), out.asset_hash)
            payload["image"] = {"mime_type": asset.mime_type, "source_page": asset.source_page,
                                "data": base64.b64encode(bytes(asset.content)).decode("ascii")}
        self.stdout.write(json.dumps(payload, ensure_ascii=False))
