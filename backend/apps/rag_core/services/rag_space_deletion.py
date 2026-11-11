"""Suppression DÉFINITIVE (purge) d'un RagSpace : base + fichiers physiques + audit.

Ce service est le SEUL point d'entrée de la purge. La vue API reste mince
(permissions -> appel -> mapping HTTP) ; aucune logique implicite par signal.

Pourquoi un service explicite plutôt qu'un `space.delete()` :

- Django **ne supprime pas** les fichiers d'un ``FileField`` quand la ligne disparaît.
  Une cascade SQL ne prouve donc RIEN sur le stockage : sans ce service, la purge
  laisserait tous les originaux sur le disque, sans plus aucune référence pour les
  retrouver (orphelins invisibles).
- L'audit doit survivre à l'objet supprimé, donc être écrit hors de la transaction
  qui le supprime.

ORDRE (fichiers d'abord, base ensuite) — décision assumée :

    verrou -> inventaire -> suppression des fichiers -> suppression DB -> audit

Le pire scénario possible est « lignes DB supprimées + fichiers encore présents » :
les fichiers deviennent alors des orphelins **définitivement introuvables** (plus
aucune référence). En supprimant les fichiers d'abord, l'échec bascule vers le cas
inverse — « fichiers supprimés + DB intacte » — qui est **détectable** (
``manage.py audit_media_storage`` le signale en `missing_file`) et **réparable**
(restauration). On échange un risque silencieux contre un risque visible.

Le verrou (``select_for_update``) est conservé pendant toute l'opération : deux
purges concurrentes sont sérialisées, la seconde constate l'absence et renvoie 404.

COMPTEURS — l'audit d'échec porte l'état RÉELLEMENT atteint
(``documents_total``, ``chunks_total``, ``files_total``, ``files_deleted``,
``files_missing``, ``files_failed``). C'est la seule information qui permet une
reprise : « la purge a échoué » ne dit pas si zéro ou dix-sept fichiers ont déjà
été détruits. La suppression des fichiers s'arrête donc au PREMIER échec — chaque
fichier détruit au-delà est une restauration de plus, pour rien.

Cf. docs/03-backend/suppression-ragspace.md
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from apps.audit.action_audit import emit_action_event
from apps.rag_core.models import Chunk, Document, RagSpace

logger = logging.getLogger(__name__)

ACTION_NAME = "ragspace.delete"
EVENT_NAME = "action.ragspace.delete"

ERROR_STORAGE_DELETE_FAILED = "storage_delete_failed"
ERROR_DB_DELETE_FAILED = "db_delete_failed"


class RagSpaceNotFound(Exception):
    """Le RagSpace demandé n'existe pas (ou plus)."""


class RagSpaceDeletionError(Exception):
    """La purge a échoué. Rien n'a été supprimé en base."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


@dataclass
class RagSpaceDeletionCounters:
    """Compteurs de la purge, mis à jour AU FUR ET À MESURE.

    Ils existent pour l'audit d'ÉCHEC : quand la purge s'arrête en cours de
    route, la question de l'opérateur est « combien de fichiers ont déjà été
    détruits ? ». Sans compteur, la reprise se fait à l'aveugle. Un résultat de
    succès n'aurait pas besoin de cette structure — un échec, si.

    Strictement numérique : jamais de nom de fichier, de clé de stockage ni de
    chemin (ces compteurs partent dans l'audit et dans les journaux).
    """

    documents_total: int = 0
    chunks_total: int = 0
    files_total: int = 0
    files_deleted: int = 0
    files_missing: int = 0
    files_failed: int = 0
    #: Artefacts de crop (CV-3C), comptés À PART : `files_*` reste « un fichier source
    #: par document ».
    vision_files_total: int = 0
    vision_files_deleted: int = 0
    vision_files_missing: int = 0
    vision_files_failed: int = 0

    def as_metadata(self) -> dict[str, int]:
        return {
            "documents_total": self.documents_total,
            "chunks_total": self.chunks_total,
            "files_total": self.files_total,
            "files_deleted": self.files_deleted,
            "files_missing": self.files_missing,
            "files_failed": self.files_failed,
            "vision_files_total": self.vision_files_total,
            "vision_files_deleted": self.vision_files_deleted,
            "vision_files_missing": self.vision_files_missing,
            "vision_files_failed": self.vision_files_failed,
        }


@dataclass
class RagSpaceDeletionResult:
    rag_space_id: str
    rag_space_name: str
    workspace_id: int
    documents_deleted: int = 0
    chunks_deleted: int = 0
    files_deleted: int = 0
    files_missing: int = 0
    duration_ms: int = 0
    status: str = "completed"
    warnings: list[str] = field(default_factory=list)
    counters: RagSpaceDeletionCounters = field(default_factory=RagSpaceDeletionCounters)


def _audit(*, status: str, actor, space_id, workspace_id, metadata: dict[str, Any],
           error_code: str = "", duration_ms: int | None = None) -> None:
    """Écrit l'audit HORS de la transaction de purge (sinon un rollback l'effacerait).

    ``ActionAuditEvent.rag_space_id`` est un UUID nu (pas une FK) : l'événement
    survit donc à la disparition du RagSpace. C'est voulu.
    """
    emit_action_event(
        event_name=EVENT_NAME,
        action_name=ACTION_NAME,
        actor=actor,
        status=status,
        workspace_id=str(workspace_id or ""),
        rag_space_id=space_id,
        resource_type="rag_space",
        resource_id=space_id,
        error_code=error_code,
        duration_ms=duration_ms,
        metadata=metadata,
    )


def _delete_files(documents: list[Document], counters: RagSpaceDeletionCounters) -> None:
    """Supprime les fichiers via l'API de stockage Django (jamais ``os.remove``).

    Passer par ``field.storage`` garde le service compatible avec tout backend
    configuré (FileSystemStorage aujourd'hui — cf. ADR-001).

    Met à jour ``counters`` à chaque fichier, et **s'arrête au premier échec** :
    poursuivre détruirait des fichiers supplémentaires alors que la purge est
    déjà condamnée à échouer. Chaque fichier détruit en trop est une restauration
    de plus. Lève :class:`RagSpaceDeletionError` — l'appelant annule la
    transaction et audite les compteurs atteints.
    """
    for doc in documents:
        f = getattr(doc, "storage_file", None)
        name = getattr(f, "name", "") or ""
        if not name:
            # Document sans fichier : ne compte NI dans files_total, NI ailleurs.
            continue
        storage = f.storage
        try:
            if not storage.exists(name):
                # Un fichier déjà absent ne doit jamais faire échouer la purge.
                counters.files_missing += 1
                logger.info("ragspace_delete: fichier déjà absent (doc=%s)", doc.id)
                continue
            storage.delete(name)
            counters.files_deleted += 1
        except Exception as e:  # noqa: BLE001
            counters.files_failed += 1
            # On ne journalise ni le chemin complet ni le contenu.
            logger.error(
                "ragspace_delete: échec suppression fichier (doc=%s error_type=%s) "
                "— arrêt ; fichiers déjà supprimés=%d",
                doc.id, e.__class__.__name__, counters.files_deleted,
            )
            raise RagSpaceDeletionError(
                ERROR_STORAGE_DELETE_FAILED,
                f"{counters.files_failed} fichier(s) non supprimé(s)",
            ) from e


def _delete_vision_files(keys: list[str], counters: RagSpaceDeletionCounters) -> None:
    """Fichiers de crop (CV-3C) : même règle que les sources — stockage Django, arrêt
    au premier échec, aucun chemin journalisé."""
    from django.core.files.storage import default_storage

    for key in keys:
        try:
            if not default_storage.exists(key):
                counters.vision_files_missing += 1
                continue
            default_storage.delete(key)
            counters.vision_files_deleted += 1
        except Exception as e:  # noqa: BLE001
            counters.vision_files_failed += 1
            logger.error(
                "ragspace_delete: échec suppression artefact vision (error_type=%s) — arrêt",
                e.__class__.__name__,
            )
            raise RagSpaceDeletionError(
                ERROR_STORAGE_DELETE_FAILED,
                f"{counters.vision_files_failed} artefact(s) vision non supprimé(s)",
            ) from e


def delete_rag_space_permanently(
    *, rag_space_id, actor=None, audit_metadata: dict[str, Any] | None = None
) -> RagSpaceDeletionResult:
    """Purge irréversible d'un RagSpace : fichiers puis données puis audit.

    Le contrôle d'accès est de la responsabilité de l'appelant (la vue).

    ``audit_metadata`` : contexte d'origine à joindre à l'audit (ex. purge de résidus
    historiques lancée par une commande d'exploitation — cf.
    ``purge_soft_deleted_ragspaces``). Permet de distinguer les origines SANS dupliquer
    la logique de suppression. Ne doit contenir aucun contenu documentaire ni secret.

    Raises:
        RagSpaceNotFound: identifiant inexistant.
        RagSpaceDeletionError: échec du stockage ou de la base — RIEN n'est supprimé
            en base (la transaction est annulée) ; l'échec est audité.
    """
    started = time.monotonic()

    # Pré-vol hors verrou : distinguer « n'existe pas » (404) du reste.
    head = RagSpace.objects.filter(id=rag_space_id).values("id", "name", "workspace_id").first()
    if head is None:
        raise RagSpaceNotFound(str(rag_space_id))

    space_name = head["name"]
    workspace_id = head["workspace_id"]
    base_meta = {"rag_space_name": space_name, **(audit_metadata or {})}

    # Compteurs vivants : partagés entre l'inventaire, la suppression et TOUS les
    # chemins d'audit. C'est ce partage qui garantit qu'un échec est audité avec
    # l'état réellement atteint, et non avec des zéros.
    counters = RagSpaceDeletionCounters()

    _audit(status="started", actor=actor, space_id=rag_space_id,
           workspace_id=workspace_id, metadata=dict(base_meta))

    try:
        with transaction.atomic():
            # Verrou : sérialise deux purges concurrentes et fige l'inventaire.
            space = RagSpace.objects.select_for_update().filter(id=rag_space_id).first()
            if space is None:
                raise RagSpaceNotFound(str(rag_space_id))

            # --- Inventaire figé sous verrou ---
            documents = list(
                Document.objects.filter(rag_space_id=space.id).only("id", "storage_file")
            )
            counters.documents_total = len(documents)
            counters.chunks_total = Chunk.objects.filter(rag_space_id=space.id).count()
            counters.files_total = sum(
                1 for d in documents if getattr(getattr(d, "storage_file", None), "name", "")
            )

            from apps.rag_core.services.raster.store import vision_storage_keys

            vision_keys = vision_storage_keys(rag_space_id=space.id)
            counters.vision_files_total = len(vision_keys)

            # --- 1. Fichiers physiques (avant la base : cf. en-tête) ---
            _delete_files(documents, counters)
            _delete_vision_files(vision_keys, counters)

            # --- 2. Base : suppression en lot puis cascade ---
            # Chunk est la table volumineuse : suppression en lot pour éviter que
            # le collector Django ne charge des dizaines de milliers d'objets.
            Chunk.objects.filter(rag_space_id=space.id).delete()
            # La cascade emporte le reste (documents, résumés, jobs, threads,
            # RagQueryLog, LlmSettingsChangeLog). L'audit ActionAuditEvent, lui,
            # n'a pas de FK vers RagSpace : il survit.
            space.delete()

    except RagSpaceNotFound:
        raise
    except RagSpaceDeletionError as e:
        _audit(status="failed", actor=actor, space_id=rag_space_id,
               workspace_id=workspace_id,
               metadata={**base_meta, **counters.as_metadata()}, error_code=e.code,
               duration_ms=int((time.monotonic() - started) * 1000))
        raise
    except Exception as e:  # noqa: BLE001
        _audit(status="failed", actor=actor, space_id=rag_space_id,
               workspace_id=workspace_id,
               metadata={**base_meta, **counters.as_metadata()},
               error_code=ERROR_DB_DELETE_FAILED,
               duration_ms=int((time.monotonic() - started) * 1000))
        logger.exception("ragspace_delete: échec inattendu (space=%s)", rag_space_id)
        raise RagSpaceDeletionError(ERROR_DB_DELETE_FAILED, e.__class__.__name__) from e

    duration_ms = int((time.monotonic() - started) * 1000)
    result = RagSpaceDeletionResult(
        rag_space_id=str(rag_space_id),
        rag_space_name=space_name,
        workspace_id=workspace_id,
        documents_deleted=counters.documents_total,
        chunks_deleted=counters.chunks_total,
        files_deleted=counters.files_deleted,
        files_missing=counters.files_missing,
        duration_ms=duration_ms,
        counters=counters,
    )

    _audit(
        status="completed", actor=actor, space_id=rag_space_id, workspace_id=workspace_id,
        duration_ms=duration_ms,
        metadata={
            **base_meta,
            # Nomenclature unifiée (succès ET échec) …
            **counters.as_metadata(),
            # … et clés historiques conservées : des consommateurs (tests,
            # exploitation) les lisent déjà. On ajoute, on ne renomme pas.
            "documents_deleted": result.documents_deleted,
            "chunks_deleted": result.chunks_deleted,
        },
    )
    logger.info(
        "ragspace_delete: purge terminée (space=%s docs=%s chunks=%s fichiers=%s absents=%s %sms)",
        rag_space_id, result.documents_deleted, result.chunks_deleted,
        result.files_deleted, result.files_missing, duration_ms,
    )
    return result
