# backend/apps/rag_core/services/documents/withdraw.py
from __future__ import annotations

"""
Retrait documentaire TRANSACTIONNEL (incrément A).

Pourquoi un service distinct de `delete_document`
--------------------------------------------------
`delete_document` supprime d'abord les fichiers puis la ligne, hors transaction : c'est
le bon ordre pour une suppression demandée par un humain, pas pour un retrait décidé par
une POLITIQUE (exclusion d'un expéditeur, d'un libellé, d'une période). Un retrait doit :

1. rendre le contenu INVISIBLE au retrieval dans la transaction de l'appelant — la ligne
   `Document` est supprimée, la cascade emporte chunks et embeddings ; le retrieval
   joint `rag_document` (clause `SQL_DOCUMENT_STATUS_CLAUSE`), donc plus rien n'est
   retrouvable au COMMIT, et rien ne l'est si la transaction est annulée ;
2. ne toucher aux fichiers qu'APRÈS le commit (`on_commit`) : un rollback ne laisse
   jamais une ligne vivante sans son fichier ;
3. rester sûr face aux tâches en vol : `ingest_document_task` ne crée jamais de
   `Document` ; une tâche qui retrouve un document disparu s'arrête (cf. tests).

Le quota se recalcule de lui-même : l'usage est un agrégat sur les lignes `Document`.
Le contrôle d'accès est de la responsabilité de l'appelant.
"""

import logging
from dataclasses import dataclass, field
from typing import Iterable, List

from django.db import transaction

logger = logging.getLogger(__name__)


@dataclass
class WithdrawResult:
    documents: int = 0
    chunks: int = 0
    files_scheduled: int = 0
    document_ids: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"documents": self.documents, "chunks": self.chunks,
                "files_scheduled": self.files_scheduled}


def _delete_files_after_commit(names: list[str], vision_keys: list[str], *, reason: str) -> None:
    from django.core.files.storage import default_storage

    failed = 0
    for key in [*names, *vision_keys]:
        try:
            if default_storage.exists(key):
                default_storage.delete(key)
        except Exception as e:  # un échec reste un orphelin visible de media_storage_audit
            failed += 1
            logger.warning("document_withdraw: file delete failed (%s): %s", reason, e.__class__.__name__)
    logger.info("document_withdraw: files cleaned reason=%s files=%s failed=%s",
                reason, len(names) + len(vision_keys), failed)


def withdraw_documents(document_ids: Iterable, *, reason: str) -> WithdrawResult:
    """Retire les documents `document_ids` (doit être appelé DANS une transaction).

    Idempotent : un identifiant déjà absent est ignoré. Ne lève pas pour le stockage.
    """
    from apps.rag_core.models import Chunk, Document
    from apps.rag_core.services.raster.store import vision_storage_keys

    ids = [i for i in {str(x) for x in document_ids if x}]
    out = WithdrawResult()
    if not ids:
        return out
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("withdraw_documents doit être appelé dans transaction.atomic()")

    docs = list(Document.objects.select_for_update().filter(id__in=ids).only("id", "storage_file"))
    if not docs:
        return out
    live_ids = [d.id for d in docs]
    names = [d.storage_file.name for d in docs if getattr(d.storage_file, "name", "")]
    vision_keys = vision_storage_keys(id__in=live_ids)
    out.chunks = Chunk.objects.filter(document_id__in=live_ids).count()
    deleted, _ = Document.objects.filter(id__in=live_ids).delete()
    out.documents = len(live_ids)
    out.document_ids = [str(i) for i in live_ids]
    out.files_scheduled = len(names) + len(vision_keys)
    transaction.on_commit(lambda: _delete_files_after_commit(names, vision_keys, reason=reason))
    logger.info("document_withdraw: reason=%s documents=%s chunks=%s", reason, out.documents, out.chunks)
    return out
