"""Identité d'enfilement d'une ingestion — garde contre les messages Celery périmés.

Défaut mesuré en staging le 21/09/2026 : 257 messages `ingest_visual` pour 247 documents ;
8 documents en file 2 ou 3 fois, chacun RETRAITÉ autant de fois (extraction, OCR et
appels vision refaits pour rien). Le garde d'upload (`ingestion_already_in_progress`)
empêche les NOUVEAUX doublons ; ce module règle ceux qui restent, côté worker.

Deux régimes :

* message NOUVEAU (porteur d'un ``enqueue_token``) — chaque enfilement grave un jeton
  dans ``Document.meta["ingest_enqueue"]`` AVANT d'envoyer la tâche. Seule la tâche qui
  porte le jeton COURANT s'exécute : un message remplacé par un enfilement plus récent
  est ignoré, quel que soit le statut. Une relance Celery renvoie les mêmes kwargs, donc
  le même jeton : elle passe.
* message ANCIEN (sans jeton, émis avant ce lot) — règle d'état : première exécution
  (``retries == 0``), non redélivrée, sur un document déjà terminé (READY/FAILED) ou
  en cours de traitement (INGESTING) = doublon. Une relance (``retries > 0``, le
  document a été remis en PENDING par ``_handle_ingest_failure``) et une redélivrance
  après crash (``redelivered``) passent toujours.

Aucun contenu documentaire n'est journalisé : identifiants et raison seulement.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from django.db.models import F, Value
from django.db.models.expressions import CombinedExpression
from django.db.models.fields.json import JSONField
from django.db.models.functions import Coalesce

logger = logging.getLogger(__name__)

META_KEY = "ingest_enqueue"
SKIP_EVENT = "stale_duplicate_task_skipped"


def new_token() -> str:
    return uuid.uuid4().hex


def stamp_enqueue_token(document_id) -> str:
    """Grave un jeton d'enfilement neuf sur le document ; rend le jeton.

    Fusion JSONB atomique (``meta || {...}``) : aucune autre clé de ``meta`` n'est
    touchée, aucune lecture-modification-écriture concurrente ne peut en perdre.
    """
    from apps.rag_core.models.document import Document

    token = new_token()
    patch = Value({META_KEY: {"token": token}}, output_field=JSONField())
    Document.objects.filter(id=document_id).update(
        meta=CombinedExpression(Coalesce(F("meta"), Value({}, output_field=JSONField())), "||", patch,
                                output_field=JSONField()),
    )
    return token


@dataclass(frozen=True)
class Admission:
    proceed: bool
    reason: str = ""


def admit(*, doc, token: str | None, retries: int, redelivered: bool,
          from_broker: bool = True) -> Admission:
    """La tâche doit-elle traiter ce document ? Décision PURE sur l'état relu.

    ``from_broker`` : le message a été LIVRÉ par le broker (file réelle). La règle d'état
    des messages sans jeton ne vise que les anciens messages en file : un appel direct
    ou en mode eager (outillage, tests, exécution synchrone) n'est jamais un doublon.
    """
    from apps.rag_core.models.document import DocumentStatus

    current = ((doc.meta or {}).get(META_KEY) or {}).get("token")
    if token:
        if current and current != token:
            return Admission(False, "superseded_enqueue")
        return Admission(True)
    if not from_broker:
        return Admission(True)
    if current:
        # Message SANS jeton, document QUI EN PORTE UN : le message est antérieur au
        # dernier enfilement (tout enfilement récent grave un jeton, et une relance
        # récente le transporte). Relance ou redélivrance comprise, il est périmé.
        # Constaté en staging le 21/09/2026 : d'anciens messages de RELANCE (retries
        # 1-3) de deux PDF en boucle de timeout ont retraité, après leur réindexation,
        # des documents déjà READY — quatre retraitements inutiles.
        return Admission(False, "superseded_by_newer_enqueue")
    if retries or redelivered:
        return Admission(True)
    if doc.status in (DocumentStatus.READY, DocumentStatus.FAILED):
        return Admission(False, "already_settled")
    if doc.status == DocumentStatus.INGESTING:
        return Admission(False, "already_in_progress")
    return Admission(True)


def log_skip(*, document_id, task_id: str, reason: str, log_prefix: str) -> None:
    logger.info("%s document_id=%s task_id=%s reason=%s source=%s",
                SKIP_EVENT, document_id, task_id, reason, log_prefix)
