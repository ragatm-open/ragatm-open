# backend/apps/rag_core/services/ingest/reindex.py
from __future__ import annotations

import logging

"""
Réindexation d'un document existant (chemin utilisé par l'upload synchrone HTTP
et par le worker Celery).

Délègue désormais à la couche d'extraction multi-format commune
(`indexing.extract_and_index_bytes`) : TXT/PDF + Markdown/DOCX/CSV/HTML/OCR.
"""

from apps.rag_core.models import Document

from apps.rag_core.services.documents.source_availability import (
    NO_STORAGE_FILE,
    SOURCE_FILE_MISSING,
    source_file_exists,
)

from django.db import IntegrityError, transaction

from apps.rag_core.repositories.document_repository import (
    find_active_duplicate,
    is_duplicate_content_collision,
)

from .attempts import ingesting_update_fields
from .indexing import extract_and_index_bytes, settle_terminal_failure
from .types import DUPLICATE_CONTENT, DuplicateContentError, IngestError, IngestResult
from .utils import sha256_bytes

logger = logging.getLogger(__name__)


def reindex_existing_document(
    *, document_id: str, allow_retry: bool = False, ocr_enabled: bool | None = None,
    extraction_timeout_seconds: int | None = None,
    last_attempt: bool = False,
    drop_visual_evidence: bool = False,
    visual_primary: bool = False,
    access_context: dict | None = None,
) -> IngestResult:
    """Ré-indexe un document existant :
    - relit les octets depuis storage_file ;
    - ré-extrait via la couche d'extraction (détection MIME + validation + format) ;
    - recrée chunks + embeddings.

    ``allow_retry`` (worker Celery) : laisse remonter ``RetryableIngestError`` sur
    erreur transitoire d'embeddings/indexing au lieu de la dégrader en FAILED, afin
    que la tâche puisse retry. Défaut ``False`` (chemins sync HTTP/management).

    ``ocr_enabled`` (P1 bis — OCR à la demande) : ``True`` force l'OCR local pour
    CETTE réindexation uniquement (sans activer l'OCR globalement). ``None`` =
    lire la configuration globale (comportement historique). L'OCR reste
    local-only et sélectif (pages pauvres en texte natif).

    ``last_attempt`` : la tâche Celery n'a plus de relance. Seul
    un ENRICHISSEMENT visuel relançable peut alors se dégrader en avertissement ; un
    échec PRIMARY reste un échec. ``drop_visual_evidence`` : opt-in EXPLICITE pour
    retirer l'évidence visuelle publiée quand l'analyse visuelle est éteinte (distinct
    de ``force``). Transmis seulement quand ils diffèrent de leur défaut.

    ``extraction_timeout_seconds`` (lot 3 — stratégie « délai étendu ») : budget
    d'extraction de CET appel. ``None`` = budget standard configuré. Ces deux
    paramètres sont les SEULS leviers d'une stratégie de reprise ; aucun ne relâche
    une limite de sécurité (taille, pages, MIME, formats refusés).
    """
    doc = Document.objects.select_related("rag_space").filter(id=document_id).first()
    if not doc:
        raise IngestError("not_found")

    if not getattr(doc, "storage_file", None) or not getattr(doc.storage_file, "name", ""):
        raise IngestError(NO_STORAGE_FILE)

    # GARDE : la source est DÉCLARÉE mais absente du stockage.
    # Sans ce contrôle, `storage_file.open("rb")` plus bas lève un
    # `FileNotFoundError` que personne ne classe : il se dégrade en
    # `internal_error`, rangé parmi les causes ENVIRONNEMENTALES — l'opérateur se
    # voit alors proposer « réessayer », geste qui ne peut jamais aboutir puisque
    # le fichier ne reviendra pas tout seul. Le code dédié bascule le conseil vers
    # « fournir la source à nouveau ». Contrôle ponctuel, jamais dans le chemin de
    # lecture RAG (cf. `documents/source_availability`).
    if not source_file_exists(doc):
        raise IngestError(SOURCE_FILE_MISSING)

    space = doc.rag_space

    # GARDE : ne jamais (re)créer de chunks dans un espace supprimé.
    # Une purge (DELETE /rag-spaces/<id>/) fait disparaître la ligne : le document
    # aussi, donc ce chemin s'arrête plus haut sur "not_found". La garde couvre le
    # résiduel : les espaces marqués `deleted_at` par l'ANCIEN soft delete, sur
    # lesquels une réindexation recréait silencieusement des chunks (défaut constaté
    # à l'évaluation). Cf. docs/03-backend/suppression-ragspace.md.
    if getattr(space, "deleted_at", None) is not None:
        raise IngestError("rag_space_deleted")
    filename = doc.filename or (doc.storage_file.name if doc.storage_file else "document.bin")
    content_type = doc.content_type or ""

    # AUDIO : la source d'extraction est la TRANSCRIPTION persistée,
    # jamais les octets sonores (jusqu'à 600 Mo, inutiles à l'extraction). L'empreinte et
    # la taille du document restent celles de l'AUDIO (dédoublonnage, quotas, provenance).
    from .audio import orchestrator as audio_orch

    if audio_orch.is_audio_document(doc):
        transcript = audio_orch.ready_transcript_for(doc)
        if transcript is None:
            from .extraction import config as _acfg

            if not _acfg.audio_ingest_enabled():
                raise IngestError("audio_ingestion_disabled")
            raise audio_orch.AudioTranscriptionDeferred(
                str(audio_orch.schedule_transcription(doc, access_context=access_context).id))
        from .audio.transcript import canonical_bytes

        Document.objects.filter(id=doc.id).update(
            **ingesting_update_fields(new_attempt=False), error_message="",
        )
        audio_orch._patch_audio_meta(doc.id, stage="indexing", transcript_id=str(transcript.id))
        visual_kwargs = {"access_context": access_context} if access_context else {}
        return extract_and_index_bytes(
            doc=doc, space=space,
            file_bytes=canonical_bytes(transcript=transcript, audio_filename=filename),
            filename=filename, content_type=content_type, ocr_enabled=False,
            allow_retry=allow_retry, extraction_timeout_seconds=extraction_timeout_seconds,
            **visual_kwargs,
        )

    # Lecture des octets (best-effort, fermeture garantie).
    f = None
    try:
        f = doc.storage_file.open("rb")
        file_bytes = f.read()
    finally:
        try:
            if f is not None:
                f.close()
        except Exception:
            pass

    # Met à jour les métadonnées de base avant extraction. L'instance `doc` n'est
    # volontairement PAS rafraîchie ensuite : elle porte le statut lu AVANT ce passage
    # en INGESTING, que `extract_and_index_bytes` relève (avec l'existence des
    # fragments) pour décider du statut final si la tentative échoue (INGEST-1C).
    # Horodatage de tentative conservé si le worker l'a déjà ouverte ; un item de job
    # (depuis READY/FAILED) commence ici.
    try:
        # Point de sauvegarde : une violation d'intégrité (ci-dessous) doit laisser la
        # connexion utilisable pour régler l'échec dans la foulée.
        with transaction.atomic():
            Document.objects.filter(id=doc.id).update(
                **ingesting_update_fields(new_attempt=False),
                error_message="",
                size_bytes=len(file_bytes),
                sha256=sha256_bytes(file_bytes),
                content_type=content_type,
                filename=filename,
            )
    except IntegrityError as exc:
        # COLLISION DE CONTENU : quitter FAILED ferait entrer cette ligne dans l'index
        # unique partiel alors qu'un document ACTIF de même sha256 l'occupe déjà
        # (plusieurs FAILED identiques pouvaient coexister ; dès que l'un redevient
        # actif, les autres ne peuvent plus être relancés). Ce n'est ni une panne ni
        # un cas transitoire : échec MÉTIER typé, définitif pour cette ligne, réglé
        # ici sous verrou (statut FAILED inchangé, 0 fragment, `duplicate_of`).
        # Toute AUTRE violation d'intégrité est propagée telle quelle.
        if not is_duplicate_content_collision(exc):
            raise
        winner = find_active_duplicate(space_id=doc.rag_space_id, sha256=doc.sha256, exclude_id=doc.id)
        if winner is None:
            raise
        err = DuplicateContentError(str(winner.id), document_id=str(doc.id))
        settle_terminal_failure(
            doc.id, message=str(err),
            failure_meta_patch={
                "extraction": {"error_code": DUPLICATE_CONTENT, "extraction_status": "failed"},
                "duplicate_of": str(winner.id),
            },
        )
        logger.warning(
            "ingest_duplicate_content document_id=%s duplicate_of=%s rag_space_id=%s",
            doc.id, winner.id, doc.rag_space_id,
        )
        raise err

    visual_kwargs = {}
    if last_attempt:
        visual_kwargs["last_attempt"] = True
    if drop_visual_evidence:
        visual_kwargs["drop_visual_evidence"] = True
    if visual_primary:
        visual_kwargs["visual_primary"] = True
    if access_context:
        visual_kwargs["access_context"] = access_context
    return extract_and_index_bytes(
        doc=doc,
        space=space,
        file_bytes=file_bytes,
        filename=filename,
        content_type=content_type,
        ocr_enabled=ocr_enabled,
        allow_retry=allow_retry,
        extraction_timeout_seconds=extraction_timeout_seconds,
        **visual_kwargs,
    )
