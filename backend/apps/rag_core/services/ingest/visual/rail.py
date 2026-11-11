"""Rail visuel du pipeline d'ingestion.

Place dans le pipeline :

    extraction (découverte des visuels, aucun appel)
        -> rail visuel : analyse Gateway + normalisation 1A   [PRÉPARATION, hors TX2]
        -> décision d'évidence (PRIMARY / ENRICHMENT, relance, échec terminal)
        -> candidat 1F (texte + tableaux + unités visuelles)
        -> TX2 : revalidation de l'évidence visuelle publiée, sous verrou, DB seule.

La règle n'est pas « l'appel a réussi » mais « le candidat porte-t-il une évidence
utile ». Le rôle se déduit de l'évidence déjà produite par les rails non visuels, jamais
de l'extension du fichier.

Aucune écriture : le rail enrichit l'``ExtractionResult`` et rend une décision ; le
règlement d'état reste celui d'INGEST-1F.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from apps.llm_router.gateway.exceptions import VisionContractDrift, VisionGatewayError
from apps.rag_core.services.ingest.extraction.base import table_is_evidence

from .contracts import (
    VisualAssetError,
    analysis_key,
    build_artifact,
    build_vision_request,
    logical_model,
)

logger = logging.getLogger(__name__)

ROLE_PRIMARY = "primary"
ROLE_ENRICHMENT = "enrichment"

#: Candidat sans évidence utile après une analyse RÉUSSIE (inconnu, décoratif, vide).
CODE_NO_USABLE_EVIDENCE = "visual_no_usable_evidence"
#: Analyse PRIMARY en échec (erreur Gateway, transport, dérive de contrat).
CODE_ANALYSIS_FAILED = "visual_analysis_failed"
#: Réindexation qui perdrait l'évidence publiée d'un visuel toujours présent, parce que
#: son analyse a ÉCHOUÉ (ou qu'une publication concurrente plus riche est intervenue).
CODE_REINDEX_DEGRADED = "visual_reindex_degraded"
#: Réindexation, fonctionnalité éteinte, qui effacerait une évidence visuelle publiée.
CODE_DISABLED_WOULD_DEGRADE = "visual_analysis_disabled_would_degrade"

WARN_PARTIAL = "visual_analysis_partial"
WARN_SKIPPED_LIMIT = "visual_analysis_skipped_limit"
WARN_CONTRACT_DRIFT = "visual_contract_drift"
WARN_MULTIFRAME = "visual_multiframe_first_frame_only"

#: Borne DOCUMENT de travail visuel (anti-emballement). Une image autonome n'en a qu'un.
MAX_ASSETS_PER_DOCUMENT = 32
#: Borne des listes de diagnostic persistées.
MAX_LISTED = 10
#: Une image autonome n'a pas de langue documentaire propre : défaut du contrat V1.
DEFAULT_LOCALE = "fr"

ACTION_PROCEED = "proceed"
#: Historique : relance du candidat ENTIER. N'est plus jamais rendu depuis
#: VISUAL-ENRICHMENT-DEFERRED — une défaillance relançable est rejouée AU NIVEAU DU
#: VISUEL (`analyze_asset_with_retry`), jamais par une relance du document.
ACTION_RETRY = "retry"
ACTION_FAIL = "fail"

#: Attente entre deux relances d'un visuel — lue À L'APPEL (remplaçable par les tests).
_sleep = time.sleep

#: Relances d'UN visuel : le budget du contrat de relance existant (tâche d'ingestion,
#: `max_retries=3`, attente 2/4/8 s). La granularité change, pas la tolérance.
ASSET_MAX_RETRIES = 3


def _retry_budget_s() -> float:
    """Temps au-delà duquel on ne RELANCE plus un visuel dans la tâche en cours : la
    moitié du plafond souple Celery, pour laisser l'indexation finir dans la tâche."""
    from django.conf import settings

    return float(getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 540) or 540) * 0.5


def analyze_asset_with_retry(client, request, *, max_retries: int = ASSET_MAX_RETRIES,
                             deadline: Optional[float] = None,
                             sleep: Optional[Callable[[float], None]] = None,
                             call_budget_s: Optional[float] = None,
                             on_attempt: Optional[Callable[[], None]] = None):
    """Un appel vision pour UN visuel, rejoué SEUL sur défaillance relançable.

    Rien d'autre n'est rejoué : ni l'extraction, ni l'OCR, ni les autres visuels. Une
    erreur non relançable (413, 502 de schéma, dérive de contrat) est levée tout de
    suite ; une relançable l'est quand le budget de relances ou de temps est épuisé.
    Rend ``(data, retries)``.

    Budget mural (CV-3D1) : une relance n'est tentée que si l'attente (max(backoff,
    ``Retry-After``)) ET la durée possible de l'appel suivant (``call_budget_s``, défaut
    = timeout du client) tiennent avant ``deadline`` — l'ancien calcul ne comptait que
    l'attente. ``on_attempt`` est appelé AVANT chaque appel HTTP (comptage persistant
    des tentatives par l'appelant) ; ``max_retries`` peut donc être un budget RESTANT.
    """
    from apps.rag_core.tasks import _backoff_s

    budget = float(call_budget_s if call_budget_s is not None else getattr(client, "timeout_s", 0) or 0)
    retries = 0
    while True:
        if on_attempt is not None:
            on_attempt()
        try:
            return client.analyze(request).data, retries
        except VisionGatewayError as exc:
            # Les relances déjà faites voyagent avec l'échec : sans cela, un visuel qui
            # finit en échec après 3 relances serait compté comme jamais relancé.
            exc.asset_retries = retries
            exc.retries_exhausted = False
            if not exc.retryable:
                raise
            if retries >= max_retries:
                exc.retries_exhausted = True
                logger.warning("visual asset retry exhausted code=%s retries=%d", exc.code, retries)
                raise
            wait = float(_backoff_s(retries + 1))
            retry_after = getattr(exc, "retry_after_s", None)
            if retry_after is not None:
                wait = max(wait, float(retry_after))
            if deadline is not None and time.monotonic() + wait + budget > deadline:
                raise
            retries += 1
            logger.warning("visual asset retry scheduled code=%s retry=%d/%d in %ss",
                           exc.code, retries, max_retries, wait)
            (sleep or _sleep)(wait)


@dataclass(frozen=True)
class VisualDecision:
    action: str
    code: Optional[str] = None
    role: Optional[str] = None


@dataclass
class _Failure:
    asset_hash: str
    ordinal: int
    error: VisionGatewayError


def _has_non_visual_evidence(result) -> bool:
    if (result.text or "").strip():
        return True
    if any((getattr(u, "text", None) or "").strip() for u in (result.units or [])):
        return True
    return any(table_is_evidence(t) for t in (result.tables or []))


def candidate_visual_hashes(result) -> frozenset:
    """Visuels dont le candidat porte une évidence (unités et fragments tabulaires)."""
    hashes = set()
    for unit in result.visual_units or []:
        visual = (getattr(unit, "meta", None) or {}).get("visual") or {}
        if visual.get("asset_hash"):
            hashes.add(visual["asset_hash"])
    for table in result.tables or []:
        visual = ((table.get("meta") or {}) if isinstance(table, dict) else {}).get("visual") or {}
        if visual.get("asset_hash"):
            hashes.add(visual["asset_hash"])
    return frozenset(hashes)


def published_visual_hashes(document_id) -> frozenset:
    """Visuels dont l'index PUBLIÉ porte une évidence. Lecture seule.

    Les extractions structurées expérimentales (``meta.visual.structured``) n'en sont
    pas : elles ne doivent ni dispenser un visuel de sa description, ni peser sur la
    garde de réindexation."""
    from apps.rag_core.models import Chunk

    from .structured import STRUCTURED_KEY

    rows = (Chunk.objects.filter(document_id=document_id, meta__has_key="visual")
            .exclude(meta__visual__has_key=STRUCTURED_KEY)
            .values_list("meta__visual__asset_hash", flat=True))
    return frozenset(h for h in rows if isinstance(h, str) and h)


@dataclass(frozen=True)
class VisualPublicationGuard:
    """Revalidation SOUS VERROU (TX2) des hypothèses de la préparation — DB seule.

    ``baseline`` : évidence visuelle publiée lue en préparation ;
    ``candidate`` : évidence visuelle portée par le candidat.
    Les pertes admises sont celles que la préparation a évaluées (``baseline - candidate``).
    Toute autre perte vient d'une publication concurrente jamais évaluée : refus.
    """

    baseline: frozenset
    candidate: frozenset

    def check_locked(self, document_id) -> None:
        from apps.rag_core.services.ingest.types import IngestError

        current = published_visual_hashes(document_id)
        unexpected = (current - self.candidate) - (self.baseline - self.candidate)
        if unexpected:
            logger.warning("visual publication aborted: concurrent visual evidence would be lost "
                           "(doc=%s, lost=%d)", document_id, len(unexpected))
            raise IngestError(CODE_REINDEX_DEGRADED)


def _summary(role: str) -> dict:
    return {
        "role": role,
        "assets_total": 0,
        "assets_unique": 0,
        "analyzed": 0,
        "failed": 0,
        "unknown": 0,
        "decorative": 0,
        "requires_check": 0,
        "skipped_limit": 0,
        "visible_text_deduplicated": 0,
        "regions_omitted": 0,
        "units": 0,
        "tables": 0,
        "failure_codes": {},
        "failed_locators": [],
        "pipelines": [],
    }


def _append_table_fragments(result, visual_table) -> int:
    """Fragments du tableau visuel -> ``result.tables``, via la sérialisation EXISTANTE."""
    from apps.rag_core.services.ingest.extraction.pdf_tables import (
        fragment_to_meta,
        fragment_to_text,
    )

    added = 0
    visual_meta = visual_table.visual_meta or {}
    for row in visual_table.table.rows:
        for fragment in row.fragments:
            text = fragment_to_text(visual_table.table, row, fragment)
            if not text:
                continue
            meta = fragment_to_meta(visual_table.table, row, fragment)
            meta["page_number"] = fragment.page_number
            if visual_meta.get("visual"):
                meta["visual"] = dict(visual_meta["visual"])
            if visual_meta.get("locator"):
                meta["locator"] = dict(visual_meta["locator"])
            if visual_meta.get("slide_number") is not None:
                meta["slide_number"] = visual_meta["slide_number"]
            result.tables.append({"markdown": text, "meta": meta})
            added += 1
    return added


def _record_failure(summary: dict, failures: list, asset, error: VisionGatewayError) -> None:
    failures.append(_Failure(asset.content_hash, asset.ordinal, error))
    summary["failed"] += 1
    codes = summary["failure_codes"]
    if error.code in codes or len(codes) < MAX_LISTED:
        codes[error.code] = codes.get(error.code, 0) + 1
    if len(summary["failed_locators"]) < MAX_LISTED:
        summary["failed_locators"].append({"asset_hash": asset.content_hash, "ordinal": asset.ordinal,
                                           "code": error.code})
    logger.warning("visual analysis failed code=%s retryable=%s http_status=%s asset=%s",
                   error.code, error.retryable, error.http_status, asset.content_hash[:12])


def _record_pipeline(summary: dict, artifact) -> None:
    entry = {"logical": artifact.model["logical"], "digest": artifact.model["digest"],
             "pipeline_id": artifact.pipeline["pipeline_id"],
             "prompt_profile_version": artifact.pipeline["prompt_profile_version"]}
    if entry not in summary["pipelines"] and len(summary["pipelines"]) < MAX_LISTED:
        summary["pipelines"].append(entry)


def run_visual_rail(result, *, document_opaque_id: str, retries_remain: bool = False,
                    published_hashes: frozenset, client=None, model: Optional[str] = None,
                    sleep: Optional[Callable[[float], None]] = None) -> VisualDecision:
    """Analyse les visuels découverts, enrichit ``result`` et décide.

    Chemin EN LIGNE (vision PRIMAIRE : image autonome, PDF/DOCX/PPTX sans autre
    évidence). Chaque visuel en défaillance relançable est rejoué SEUL, dans la tâche
    (``analyze_asset_with_retry``) ; ``retries_remain`` n'a plus d'effet (conservé pour
    compatibilité d'appel) : aucune défaillance visuelle ne relance le document.

    Ordre de décision :
      1. un visuel dont l'évidence est PUBLIÉE a échoué -> ``visual_reindex_degraded`` ;
      3. aucune évidence utile -> ``visual_analysis_failed`` (échec) ou
         ``visual_no_usable_evidence`` (analyse réussie mais vide) ;
      4. sinon publication, avec avertissements bornés.
    """
    from apps.llm_router.gateway.vision import VisionGatewayClient
    from apps.rag_core.services.ingest.visual.normalizers import normalize_artifact

    assets = list(result.visual_assets or [])
    base_useful = _has_non_visual_evidence(result)
    role = ROLE_ENRICHMENT if base_useful else ROLE_PRIMARY
    summary = _summary(role)
    failures: list = []
    skipped_hashes: set = set()
    known_texts = [line for line in (result.text or "").splitlines() if line.strip()]
    client = client or VisionGatewayClient.from_settings()

    try:
        profile = logical_model(model)
    except VisualAssetError:
        profile, model_error = None, VisionGatewayError("visual_model_misconfigured", retryable=False)
    else:
        model_error = None

    seen_keys: set = set()
    seen_hashes: set = set()
    deadline = time.monotonic() + _retry_budget_s()
    for asset in assets:
        summary["assets_total"] += 1
        seen_hashes.add(asset.content_hash)
        key = analysis_key(content_hash=asset.content_hash, task=asset.task, model=profile,
                           locale=DEFAULT_LOCALE, context=asset.context)
        if key in seen_keys:
            continue
        if len(seen_keys) >= MAX_ASSETS_PER_DOCUMENT:
            summary["skipped_limit"] += 1
            skipped_hashes.add(asset.content_hash)
            continue
        seen_keys.add(key)
        if model_error is not None:
            _record_failure(summary, failures, asset, model_error)
            continue
        try:
            request = build_vision_request(asset, document_opaque_id=document_opaque_id,
                                           locale=DEFAULT_LOCALE, model=profile)
            data, retried = analyze_asset_with_retry(client, request, deadline=deadline, sleep=sleep)
            summary["asset_retries"] = summary.get("asset_retries", 0) + retried
        except VisionGatewayError as exc:
            summary["asset_retries"] = summary.get("asset_retries", 0) + int(getattr(exc, "asset_retries", 0))
            _record_failure(summary, failures, asset, exc)
            continue
        except VisualAssetError:
            _record_failure(summary, failures, asset,
                            VisionGatewayError("visual_request_invalid", retryable=False))
            continue

        summary["analyzed"] += 1
        artifact = build_artifact(asset, data)
        _record_pipeline(summary, artifact)
        if artifact.visual_kind in ("unknown", "decorative"):
            summary[artifact.visual_kind] += 1
        if artifact.requires_visual_check:
            summary["requires_check"] += 1
        out = normalize_artifact(artifact, context=asset.context, known_texts=known_texts)
        summary["regions_omitted"] += out.omitted_regions
        for unit in out.units:
            summary["visible_text_deduplicated"] += int(
                unit.meta["visual"].get("visible_text_deduplicated", 0)
                + unit.meta["visual"].get("lines_deduplicated", 0))
            result.visual_units.append(unit)
            summary["units"] += 1
        for table in out.tables:
            summary["tables"] += _append_table_fragments(result, table)
    summary["assets_unique"] = len(seen_hashes)

    if summary["skipped_limit"]:
        result.add_warning(WARN_SKIPPED_LIMIT)
    if any(isinstance(f.error, VisionContractDrift) for f in failures):
        result.add_warning(WARN_CONTRACT_DRIFT)
    if failures:
        result.add_warning(WARN_PARTIAL)
    result.metadata["visual"] = summary
    _refinalize(result)

    lost = ({f.asset_hash for f in failures} | skipped_hashes) & set(published_hashes)
    if lost:
        return VisualDecision(ACTION_FAIL, CODE_REINDEX_DEGRADED, role)

    visual_useful = bool(summary["units"]) or any(
        table_is_evidence(t) for t in result.tables if isinstance(t, dict)
        and ((t.get("meta") or {}).get("visual")))
    if not (base_useful or visual_useful):
        return VisualDecision(ACTION_FAIL, CODE_ANALYSIS_FAILED if failures else CODE_NO_USABLE_EVIDENCE,
                              role)
    return VisualDecision(ACTION_PROCEED, None, role)


def _refinalize(result) -> None:
    """Statut et métadonnées canoniques recalculés après ajout de l'évidence visuelle."""
    md = result.metadata
    result.finalize(fmt=md.get("format") or "", mime=result.detected_mime, method=result.method)
