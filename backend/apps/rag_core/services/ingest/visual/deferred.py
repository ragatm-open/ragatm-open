"""Vision DIFFÉRÉE (VISUAL-ENRICHMENT-DEFERRED) — décision de la passe primaire.

Mesure qui fonde ce module (RCA staging du 21/09/2026) : 97 % des PDF passés par le rail
visuel portaient déjà un texte natif exploitable, et y attendaient pourtant une heure
derrière une file de vision à un seul slot ; la vision n'y apportait qu'un enrichissement
(aucun cas « essentiel » observé). READY ne dépend donc plus de la vision quand le
document a déjà une évidence non visuelle.

Décision DÉTERMINISTE, prise sur le résultat RÉEL de l'extraction — jamais par un modèle :

    image autonome, ou reprise `visual_primary`   -> ``inline``   (vision AVANT READY)
    PDF/DOCX/PPTX, rail actif, évidence non visuelle
        + visuels découverts                      -> ``deferred`` (READY, puis enrichissement)
        + aucun visuel                            -> ``deferred`` sans tâche (NOT_APPLICABLE)
    PDF/DOCX/PPTX, rail actif, AUCUNE évidence non visuelle
        + visuels découverts                      -> ``handoff``  (reprise primaire sur ingest_visual)
    tout le reste                                  -> ``inline`` (chemin historique inchangé)

L'état d'enrichissement vit dans ``Document.meta["visual_enrichment"]`` (JSON existant :
aucune migration). ``READY`` dit « exploitable par la recherche », jamais « vision
terminée » : c'est cet état qui le dit.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from django.utils import timezone

logger = logging.getLogger(__name__)

META_KEY = "visual_enrichment"
DEFERRED_SOURCES = frozenset({"pdf", "docx", "pptx"})

STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_PARTIAL = "partial"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({STATUS_NOT_APPLICABLE, STATUS_PARTIAL, STATUS_SUCCEEDED, STATUS_FAILED})

MODE_INLINE = "inline"
MODE_DEFERRED = "deferred"
MODE_HANDOFF = "handoff"


@dataclass
class DeferredPlan:
    """Plan d'une passe primaire qui publie SANS vision."""

    state: dict
    #: Fragments visuels PUBLIÉS reportés tels quels (texte + méta) : visuels toujours
    #: présents, même modèle logique. Ré-embarqués avec le candidat, jamais ré-analysés.
    carried: list = field(default_factory=list)

    @property
    def enrich(self) -> bool:
        return self.state.get("status") == STATUS_PENDING

    @property
    def generation(self) -> str:
        return self.state["generation"]


def _now() -> str:
    return timezone.now().isoformat()


def _regions_json(regions) -> dict:
    out = {}
    for page, boxes in (regions or {}).items():
        out[str(int(page))] = [[float(v) for v in box] for box in (boxes or [])]
    return out


def decide_mode(*, result, visual_sources, visual_primary: bool) -> str:
    from .rail import _has_non_visual_evidence

    kind = getattr(result, "visual_source_kind", "") or ""
    if visual_primary or kind not in DEFERRED_SOURCES or kind not in (visual_sources or ()):
        return MODE_INLINE
    if _has_non_visual_evidence(result):
        return MODE_DEFERRED
    if getattr(result, "visual_assets", None):
        return MODE_HANDOFF
    return MODE_INLINE


def _current_logical_model() -> Optional[str]:
    from .contracts import vision_model_from_settings

    try:
        return vision_model_from_settings()
    except Exception:  # noqa: BLE001 - profil invalide : rien n'est reporté
        return "\x00invalid"


def published_visual_rows(document_id) -> list:
    """Fragments visuels publiés du document (texte + méta), en ordre d'index.

    Hors extractions structurées expérimentales : jamais reportées à la réindexation."""
    from apps.rag_core.models import Chunk

    from .structured import STRUCTURED_KEY

    return list(Chunk.objects.filter(document_id=document_id, meta__has_key="visual")
                .exclude(meta__visual__has_key=STRUCTURED_KEY)
                .order_by("chunk_index")
                .values("text", "meta", "page_number"))


def plan_deferred(*, doc, result, content_sha256: str, drop_visual_evidence: bool) -> DeferredPlan:
    """État d'enrichissement + report des fragments visuels encore valides."""
    kind = getattr(result, "visual_source_kind", "") or ""
    hashes = []
    for asset in getattr(result, "visual_assets", None) or []:
        if asset.content_hash not in hashes:
            hashes.append(asset.content_hash)
    state = {
        "status": STATUS_PENDING if hashes else STATUS_NOT_APPLICABLE,
        "generation": uuid.uuid4().hex,
        "content_sha256": content_sha256,
        "source_kind": kind,
        "assets_discovered": len(hashes),
        "asset_hashes": hashes,
        "assets_published": 0,
        "assets_failed": 0,
        "asset_retries": 0,
        "failure_codes": {},
        "carried": 0,
        "queued_at": _now() if hashes else None,
        "updated_at": _now(),
    }
    regions = getattr(result, "visual_suppressed_regions", None)
    if kind == "pdf" and regions:
        state["suppressed_regions"] = _regions_json(regions)

    carried = []
    if hashes and not drop_visual_evidence:
        model = _current_logical_model()
        keep = set(hashes)
        for row in published_visual_rows(doc.id):
            visual = (row["meta"] or {}).get("visual") or {}
            logical = (visual.get("model") or {}).get("logical")
            if visual.get("asset_hash") in keep and (model is None or logical == model):
                carried.append(row)
    state["carried"] = len({(r["meta"].get("visual") or {}).get("asset_hash") for r in carried})
    return DeferredPlan(state=state, carried=carried)


def supersede_for_v2(plan: DeferredPlan) -> DeferredPlan:
    """Document EFFECTIVEMENT dans le périmètre Vision V2 : le rail V1
    différé ne le traite pas aussi. L'état le dit (``superseded_by``) ; les fragments V1
    déjà publiés et reportés restent intacts. Hors périmètre V2, jamais appelé."""
    if plan.state.get("status") == STATUS_PENDING:
        plan.state.update({"status": STATUS_NOT_APPLICABLE, "superseded_by": "visual_v2",
                           "queued_at": None, "updated_at": _now()})
    return plan


def carried_specs(plan: Optional[DeferredPlan], *, start_chunk_index: int) -> list:
    """ChunkSpec des fragments visuels reportés, numérotés APRÈS le candidat."""
    from apps.rag_core.services.chunking import ChunkSpec

    if plan is None:
        return []
    out = []
    idx = int(start_chunk_index)
    for row in plan.carried:
        meta = dict(row["meta"] or {})
        for key in ("chunk_index", "char_start", "char_end", "page_number", "source"):
            meta.pop(key, None)
        text = row["text"] or ""
        out.append(ChunkSpec(chunk_index=idx, text=text, page_number=row.get("page_number"),
                             char_start=0, char_end=len(text),
                             source=(row["meta"] or {}).get("source") or "", meta=meta))
        idx += 1
    return out


# --------------------------------------------------------------------------- #
# Enfilements — RÉFÉRENCES uniquement (jamais d'octets de document ni d'image)
# --------------------------------------------------------------------------- #
def enqueue_visual_enrichment(*, document_id, generation: str, request_id: Optional[str] = None,
                              access_context: Optional[dict] = None) -> bool:
    from apps.rag_core.services.ingest.queues import INGEST_VISUAL_QUEUE
    from apps.rag_core.tasks import visual_enrichment_task

    try:
        visual_enrichment_task.apply_async(
            kwargs={"document_id": str(document_id), "generation": generation, "request_id": request_id,
                    "access_context": _ids_only(access_context, request_id)},
            queue=INGEST_VISUAL_QUEUE,
        )
        return True
    except Exception:  # noqa: BLE001 - le document reste READY ; l'état le dit
        logger.exception("visual enrichment enqueue failed document_id=%s", document_id)
        update_state(document_id, generation=generation, patch={"status": STATUS_FAILED,
                                                                 "error_code": "enqueue_failed"})
        return False


def _ids_only(access_context: Optional[dict], request_id: Optional[str]) -> dict:
    """Contexte d'accès transmis tel que l'ingestion l'a reçu : IDENTIFIANTS seulement.
    La tâche suivante revalide les droits depuis la base (même contrat que l'upload)."""
    ctx = access_context or {}
    return {"user_id": ctx.get("user_id"), "request_id": ctx.get("request_id") or request_id}


def enqueue_visual_primary(*, doc, request_id: Optional[str] = None,
                           access_context: Optional[dict] = None) -> bool:
    """Reprise PRIMAIRE sur `ingest_visual` d'un document dont la seule évidence est
    visuelle. Le document est remis en PENDING AVANT l'envoi (le worker visuel, s'il
    prend la tâche aussitôt, ne voit jamais un statut écrasé après coup)."""
    from apps.rag_core.models.document import Document, DocumentStatus
    from apps.rag_core.services.ingest.enqueue_token import stamp_enqueue_token
    from apps.rag_core.services.ingest.queues import ingest_queue_for
    from apps.rag_core.tasks import ingest_document_task

    Document.objects.filter(id=doc.id).update(status=DocumentStatus.PENDING)
    kwargs = {"document_id": str(doc.id), "request_id": request_id, "visual_primary": True,
              "access_context": _ids_only(access_context, request_id),
              "enqueue_token": stamp_enqueue_token(doc.id)}
    queue = ingest_queue_for(filename=doc.filename, visual_primary=True)
    ingest_document_task.apply_async(kwargs=kwargs, queue=queue) if queue else ingest_document_task.delay(**kwargs)
    logger.info("visual primary handoff document_id=%s queue=%s", doc.id, queue or "default")
    return True


# --------------------------------------------------------------------------- #
# État
# --------------------------------------------------------------------------- #
def update_state(document_id, *, generation: str, patch: dict) -> bool:
    """Fusionne ``patch`` dans l'état d'enrichissement SI la génération est courante.

    Sous verrou de ligne : une génération remplacée (réindexation, changement de source)
    n'écrit jamais rien. Rend False si l'état n'appartient plus à cette génération.
    """
    from django.db import transaction

    from apps.rag_core.models.document import Document

    with transaction.atomic():
        doc = Document.objects.select_for_update().filter(id=document_id).first()
        if doc is None:
            return False
        meta = dict(doc.meta or {})
        state = dict(meta.get(META_KEY) or {})
        if state.get("generation") != generation:
            return False
        state.update(patch)
        state["updated_at"] = _now()
        meta[META_KEY] = state
        Document.objects.filter(id=doc.id).update(meta=meta)
    return True


def state_of(doc) -> dict:
    return dict((getattr(doc, "meta", None) or {}).get(META_KEY) or {})


def public_status(doc) -> Optional[str]:
    """Projection API : statut d'enrichissement, ou None si non applicable/inconnu."""
    return state_of(doc).get("status") or None
