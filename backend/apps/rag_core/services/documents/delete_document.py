# backend/apps/rag_core/services/documents/delete_document.py
from __future__ import annotations

"""
Suppression définitive (hard delete) d'un document et de tout ce qui en dépend.

Supprime :
- les chunks/extraits (FK CASCADE depuis Document) ;
- les résumés DocumentSummary (FK CASCADE) ;
- le fichier source `storage_file` (FileField) s'il existe — UNIQUEMENT via l'objet
  Document (jamais un chemin fourni par le client) ;
- le document lui-même.

Sécurité : on ne supprime jamais par filename/sha256 ni un chemin arbitraire ;
seul le `storage_file` rattaché au Document est supprimé (chemin dédié par doc :
`rag_docs/<rag_space_id>/<document_id>/<filename>` — non partagé entre documents).
Le contrôle d'accès/permissions est de la responsabilité de l'appelant (la vue).

VÉRACITÉ DU COMPTE RENDU
------------------------
``FieldFile.delete()`` de Django absorbe silencieusement l'absence du fichier :
appelé sur une source déjà disparue, il ne lève pas. Le rapport annonçait donc
``file_deleted=True`` pour un fichier que personne n'avait supprimé — un
exploitant purgeant un espace partiellement dégradé lisait « fichiers supprimés »
là où il n'y avait rien à supprimer. Le contrat distingue désormais trois issues
mutuellement exclusives :

- ``file_deleted``          : le fichier était là et a été supprimé ;
- ``file_already_missing``  : le fichier était déjà absent — rien n'a été fait ;
- ``warnings: ["file_delete_failed"]`` : le fichier était là et la suppression a
  échoué (la ligne est tout de même supprimée, l'anomalie est tracée).

Même sémantique que ``rag_space_deletion._delete_files`` et son compteur
``files_missing`` : une seule façon de dire « ce fichier n'était pas là ».
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from apps.rag_core.models import Chunk
from apps.rag_core.services.documents.source_availability import source_file_exists

logger = logging.getLogger(__name__)


@dataclass
class DeleteDocumentResult:
    deleted: bool
    document_id: str
    chunks_deleted: int
    #: Le fichier était présent ET a été supprimé. Jamais vrai pour un fichier
    #: qui n'était plus là — voir `file_already_missing`.
    file_deleted: bool
    #: La source était déjà absente du stockage : aucune suppression n'a eu lieu.
    file_already_missing: bool = False
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "deleted": self.deleted,
            "document_id": self.document_id,
            "chunks_deleted": self.chunks_deleted,
            "file_deleted": self.file_deleted,
            "file_already_missing": self.file_already_missing,
            "warnings": list(self.warnings),
        }


def delete_document(document, *, actor=None) -> DeleteDocumentResult:
    """Supprime définitivement ``document`` (+ chunks, résumés, fichier source).

    Ne lève pas pour un échec de suppression de fichier : la DB est nettoyée et un
    warning est remonté (jamais de fichier orphelin laissé silencieusement sans
    trace). Retourne un résumé safe (aucun contenu documentaire), dont
    ``file_deleted`` et ``file_already_missing`` décrivent ce qui s'est
    RÉELLEMENT passé côté stockage."""
    doc_id = str(document.id)
    rag_space_id = str(getattr(document, "rag_space_id", "") or "")
    warnings: List[str] = []

    chunks_deleted = Chunk.objects.filter(document_id=document.id).count()

    # Fichier source : suppression via le FileField uniquement (best-effort).
    # L'existence est testée AVANT, via l'API de stockage : sans ce test,
    # `FieldFile.delete()` ne lève pas sur un fichier absent et le rapport
    # annoncerait une suppression qui n'a pas eu lieu.
    file_deleted = False
    file_already_missing = False
    storage_file = getattr(document, "storage_file", None)
    if storage_file is not None and getattr(storage_file, "name", ""):
        if not source_file_exists(document):
            file_already_missing = True
            logger.info("document_delete: fichier source déjà absent (doc=%s)", doc_id)
        else:
            try:
                storage_file.delete(save=False)
                file_deleted = True
            except Exception as e:  # ne jamais bloquer la suppression DB sur l'I/O fichier
                warnings.append("file_delete_failed")
                logger.warning(
                    "document_delete: file delete failed (doc=%s): %s",
                    doc_id, e.__class__.__name__,
                )

    # Artefacts de crop (CV-3C) : leurs lignes partent avec la cascade, leurs fichiers
    # sont supprimés ici (best-effort, comme la source ; un échec reste un orphelin
    # visible de `media_storage_audit`).
    from django.core.files.storage import default_storage

    from apps.rag_core.services.raster.store import vision_storage_keys

    for key in vision_storage_keys(id=document.id):
        try:
            if default_storage.exists(key):
                default_storage.delete(key)
        except Exception as e:  # ne jamais bloquer la suppression DB sur l'I/O fichier
            if "vision_file_delete_failed" not in warnings:
                warnings.append("vision_file_delete_failed")
            logger.warning("document_delete: vision file delete failed (doc=%s): %s",
                           doc_id, e.__class__.__name__)

    # Audio : jobs ASR EN VOL de CE document libérés côté Gateway (jamais
    # ceux d'un autre document ni d'une autre application). La transcription persistée part
    # avec la cascade ; le spool du worker audio est balayé par sa prochaine étape.
    try:
        from apps.rag_core.services.ingest.audio import gateway_asr
        from apps.rag_core.services.ingest.audio.orchestrator import active_gateway_jobs

        for job_id in active_gateway_jobs(document.id):
            if not gateway_asr.delete(job_id) and "asr_job_delete_failed" not in warnings:
                warnings.append("asr_job_delete_failed")
    except Exception as e:  # ne jamais bloquer la suppression DB sur le Gateway
        logger.warning("document_delete: asr job cleanup failed (doc=%s): %s", doc_id, e.__class__.__name__)

    # Hard delete : CASCADE supprime chunks + DocumentSummary (+ versions, pages,
    # rasters, crops, transcriptions audio).
    document.delete()

    # Trace d'audit (sans contenu documentaire).
    logger.info(
        "document_deleted doc=%s rag_space=%s chunks_deleted=%s file_deleted=%s "
        "file_already_missing=%s actor=%s",
        doc_id, rag_space_id, chunks_deleted, file_deleted, file_already_missing,
        _actor_id(actor),
    )

    return DeleteDocumentResult(
        deleted=True,
        document_id=doc_id,
        chunks_deleted=chunks_deleted,
        file_deleted=file_deleted,
        file_already_missing=file_already_missing,
        warnings=warnings,
    )


def _actor_id(actor) -> Optional[str]:
    try:
        return str(getattr(actor, "id", None)) if actor is not None else None
    except Exception:
        return None
