"""Enrichissement visuel DIFFÉRÉ (VISUAL-ENRICHMENT-DEFERRED).

Exécuté par ``visual_enrichment_task`` sur `ingest_visual`, APRÈS que la passe primaire
a rendu le document READY. Ne fait QUE ce qui sert la vision :

    lecture de la source durable
      -> redécouverte des visuels (PDF : découverte SEULE, avec les régions tabulaires
         persistées par la passe primaire — ni texte, ni OCR, ni tableaux ne sont
         refaits ; DOCX/PPTX : relecture du conteneur, sans OCR, seule voie d'accès aux
         médias et à leur contexte — documenté) ;
      -> par visuel NON encore publié : appel vision (relancé SEUL sur défaillance
         relançable), normalisation 1A, fragments, embeddings, publication ;
      -> état d'enrichissement.

Garanties :
  * idempotence — un visuel dont l'empreinte a déjà des fragments publiés n'est ni
    ré-analysé ni republié ; la publication d'un visuel remplace ses propres fragments
    sous verrou (jamais deux fois le même visuel) ;
  * publication PROGRESSIVE — chaque visuel réussi est publié dans sa propre transaction
    courte : un échec ultérieur ne perd rien ;
  * aucun rejeu hors du visuel en défaillance — ni extraction, ni OCR, ni les autres
    visuels ;
  * génération — une tâche dont la génération n'est plus celle du document (réindexation,
    source modifiée, suppression) ne publie jamais : no-op sûr ;
  * aucun fragment sans provenance : ceux-ci portent la méta visuelle complète (empreinte,
    locator, page, modèle, pipeline) produite par le normaliseur existant.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from django.db import transaction
from django.db.models import Max

from . import deferred as D

logger = logging.getLogger(__name__)

OUTCOME_DONE = "done"
OUTCOME_SKIPPED = "skipped"
OUTCOME_CONTINUE = "continue"


@dataclass
class EnrichmentOutcome:
    action: str
    reason: str = ""
    published: int = 0
    failed: int = 0
    skipped_published: int = 0
    skipped_terminal: int = 0
    vision_calls: int = 0
    asset_retries: int = 0
    failure_codes: dict = field(default_factory=dict)


class _Abort(Exception):
    """La génération n'est plus publiable (remplacée, source changée, document parti)."""


def _read_source(doc) -> bytes:
    f = doc.storage_file.open("rb")
    try:
        return f.read()
    finally:
        try:
            f.close()
        except Exception:  # noqa: BLE001
            pass


def discover_assets(*, doc, data: bytes, state: dict) -> list:
    """Visuels de la source — la MÊME découverte que la passe primaire, sans le reste."""
    kind = state.get("source_kind") or ""
    if kind == "pdf":
        from apps.rag_core.services.ingest.extraction import config as xcfg
        from apps.rag_core.services.ingest.extraction.pdf_visual import (
            PdfVisualLimits,
            discover_pdf_visual_assets,
        )

        from .structured import pdf_visual_max_assets_for, pdf_visual_max_per_page_for

        regions = {int(p): [tuple(float(v) for v in box) for box in boxes]
                   for p, boxes in (state.get("suppressed_regions") or {}).items()}
        # MÊME borne que la passe primaire (sinon les empreintes attendues divergent).
        limits = PdfVisualLimits.for_cap(pdf_visual_max_assets_for(doc.rag_space_id),
                                         max_per_page=pdf_visual_max_per_page_for(doc.rag_space_id))
        # MÊME décision vectorielle que la passe primaire (même espace, mêmes réglages).
        return list(discover_pdf_visual_assets(data, suppressed_regions=regions or None, limits=limits,
                                               max_pages=xcfg.pdf_max_pages(),
                                               vector_regions=xcfg.pdf_vector_regions_enabled_for(
                                                   doc.rag_space_id)).assets)
    if kind in ("docx", "pptx"):
        from apps.rag_core.services.ingest.extraction import extract_document

        # Relecture du CONTENEUR : les médias et leur contexte (légende, texte voisin,
        # titre de diapositive) n'existent qu'à travers elle. Sans OCR ; aucun fragment
        # textuel n'est reconstruit ni republié.
        result = extract_document(data, filename=doc.filename or f"document.{kind}",
                                  content_type=doc.content_type or "", ocr_enabled=False,
                                  visual_sources=frozenset({kind}))
        return list(result.visual_assets or [])
    return []


def _known_texts(document_id) -> list:
    """Lignes de l'évidence TEXTUELLE publiée : le normaliseur n'y ajoute pas de doublon."""
    from apps.rag_core.models import Chunk

    lines = []
    for text in (Chunk.objects.filter(document_id=document_id).exclude(meta__has_key="visual")
                 .values_list("text", flat=True)):
        lines.extend(line for line in (text or "").splitlines() if line.strip())
    return lines


def _published_hashes(document_id) -> set:
    from .rail import published_visual_hashes

    return set(published_visual_hashes(document_id))


def _asset_candidate(*, doc, space, units, tables, filename: str):
    """Fragments + vecteurs d'UN visuel, par la préparation de candidat EXISTANTE."""
    from apps.rag_core.services.ingest.indexing import prepare_visual_asset_candidate

    return prepare_visual_asset_candidate(doc=doc, space=space, units=units, tables=tables,
                                          filename=filename)


def _publish_asset(*, doc, space, generation: str, content_sha256: str, asset_hash: str,
                   candidate, state_patch: Callable[[dict], dict]) -> int:
    """Transaction COURTE : ajoute les fragments d'UN visuel, sans toucher au reste."""
    from apps.rag_core.models import Chunk, Document
    from apps.rag_core.models.document import DocumentStatus
    from apps.rag_core.services.ingest.indexing import _chunk_rows
    from apps.rag_core.services.ragspace_mutation import lock_ragspace_for_mutation

    with transaction.atomic():
        identity = candidate.identity if candidate is not None else None
        locked_space = lock_ragspace_for_mutation(space.id, expected_identity=identity)
        if getattr(locked_space, "deleted_at", None) is not None:
            raise _Abort("rag_space_deleted")
        locked = Document.objects.select_for_update().filter(id=doc.id).first()
        if locked is None:
            raise _Abort("not_found")
        state = D.state_of(locked)
        if state.get("generation") != generation:
            raise _Abort("superseded")
        if locked.status != DocumentStatus.READY or (locked.sha256 or "") != content_sha256:
            raise _Abort("source_changed")
        rows = []
        if candidate is not None:
            existing = (Chunk.objects.filter(document_id=locked.id).exclude(pipeline_id=None)
                        .values_list("pipeline_id", flat=True).first())
            if existing and candidate.pipeline_id and existing != candidate.pipeline_id:
                # Jamais deux pipelines d'embeddings dans un même document.
                raise _Abort("embedding_pipeline_changed")
            # Les fragments d'extraction structurée du même visuel ne sont pas les siens.
            from .structured import STRUCTURED_KEY

            (Chunk.objects.filter(document_id=locked.id, meta__visual__asset_hash=asset_hash)
             .exclude(meta__visual__has_key=STRUCTURED_KEY).delete())
            start = (Chunk.objects.filter(document_id=locked.id).aggregate(m=Max("chunk_index"))["m"])
            offset = 0 if start is None else int(start) + 1
            rows = _chunk_rows(locked_space, locked, candidate, index_offset=offset)
            Chunk.objects.bulk_create(rows, batch_size=200)
        meta = dict(locked.meta or {})
        meta[D.META_KEY] = state_patch(dict(state))
        Document.objects.filter(id=locked.id).update(meta=meta)
    return len(rows)


def run_enrichment(*, document_id, generation: str, client=None,
                   sleep: Optional[Callable[[float], None]] = None,
                   budget_s: Optional[float] = None) -> EnrichmentOutcome:
    from apps.llm_router.gateway.exceptions import VisionGatewayError
    from apps.llm_router.gateway.vision import VisionGatewayClient
    from apps.rag_core.models import Document
    from apps.rag_core.models.document import DocumentStatus

    from .contracts import (
        VisualAssetError,
        analysis_key,
        build_artifact,
        build_vision_request,
        logical_model,
    )
    from apps.rag_core.services.ragspace_mutation import RagSpaceIdentityChanged

    from .normalizers import normalize_artifact
    from .rail import (
        DEFAULT_LOCALE,
        MAX_ASSETS_PER_DOCUMENT,
        _append_table_fragments,
        ASSET_MAX_RETRIES,
        _retry_budget_s,
        analyze_asset_with_retry,
    )

    ASSET_MAX_ATTEMPTS = 1 + ASSET_MAX_RETRIES

    doc = Document.objects.select_related("rag_space").filter(id=document_id).first()
    if doc is None:
        return EnrichmentOutcome(OUTCOME_SKIPPED, "not_found")
    state = D.state_of(doc)
    if state.get("generation") != generation:
        return EnrichmentOutcome(OUTCOME_SKIPPED, "superseded")
    if doc.status != DocumentStatus.READY or (doc.sha256 or "") != state.get("content_sha256"):
        return EnrichmentOutcome(OUTCOME_SKIPPED, "not_ready_or_source_changed")
    space = doc.rag_space
    if getattr(space, "deleted_at", None) is not None:
        return EnrichmentOutcome(OUTCOME_SKIPPED, "rag_space_deleted")

    D.update_state(doc.id, generation=generation,
                   patch={"status": D.STATUS_RUNNING, "started_at": state.get("started_at") or D._now()})

    outcome = EnrichmentOutcome(OUTCOME_DONE)
    try:
        data = _read_source(doc)
    except Exception:  # noqa: BLE001 - source illisible : le document reste READY
        D.update_state(doc.id, generation=generation,
                       patch={"status": D.STATUS_FAILED, "error_code": "source_unreadable"})
        return EnrichmentOutcome(OUTCOME_DONE, "source_unreadable")

    assets = discover_assets(doc=doc, data=data, state=state)
    expected = set(state.get("asset_hashes") or [])
    found = {a.content_hash for a in assets}
    if expected and found != expected:
        logger.warning("visual enrichment discovery mismatch document_id=%s expected=%d found=%d common=%d",
                       doc.id, len(expected), len(found), len(expected & found))

    from django.conf import settings

    try:
        profile = logical_model(getattr(settings, "AI_GATEWAY_VISION_MODEL", "") or None)
    except VisualAssetError:
        D.update_state(doc.id, generation=generation,
                       patch={"status": D.STATUS_FAILED, "error_code": "visual_model_misconfigured"})
        return EnrichmentOutcome(OUTCOME_DONE, "visual_model_misconfigured")
    client = client or VisionGatewayClient.from_settings()
    known_texts = _known_texts(doc.id)
    # Déjà traités : fragments publiés (report ou génération courante) ET visuels déjà
    # analysés par CETTE génération sans rien d'indexable (décoratif, inconnu).
    already = _published_hashes(doc.id) | set(state.get("analyzed_hashes") or [])
    # Budget PAR VISUEL ET PAR GÉNÉRATION (CV-3D1), persisté dans l'état : une
    # continuation reprend le travail RESTANT du document, elle ne rouvre JAMAIS le budget
    # d'un visuel. `asset_attempts` compte les appels déjà engagés (écrit AVANT chaque
    # appel : un arrêt brutal en cours d'appel est compté) ; `terminal_failures` liste les
    # visuels épuisés ou en échec non relançable — plus aucun appel pour cette génération.
    attempts = {str(k): int(v) for k, v in (state.get("asset_attempts") or {}).items()}
    terminal = dict(state.get("terminal_failures") or {})
    deadline = time.monotonic() + (budget_s if budget_s is not None else _retry_budget_s())
    call_budget = float(getattr(client, "timeout_s", 0) or 0)
    filename = doc.filename or ""
    seen: set = set()
    continue_later = False

    def _persist(**patch) -> None:
        if not D.update_state(doc.id, generation=generation, patch=patch):
            raise _Abort("superseded")

    def _make_terminal(asset_hash: str, code: str) -> None:
        terminal[asset_hash] = code
        _persist(terminal_failures=dict(terminal))
        outcome.failed += 1
        outcome.failure_codes[code] = outcome.failure_codes.get(code, 0) + 1
        logger.warning("visual asset terminal document_id=%s code=%s asset=%s attempts=%d",
                       doc.id, code, asset_hash[:12], attempts.get(asset_hash, 0))

    for asset in assets:
        key = analysis_key(content_hash=asset.content_hash, task=asset.task, model=profile,
                           locale=DEFAULT_LOCALE, context=asset.context)
        if key in seen:
            continue
        if len(seen) >= MAX_ASSETS_PER_DOCUMENT:
            break
        seen.add(key)
        h = asset.content_hash
        if h in already:
            outcome.skipped_published += 1
            continue
        if h in terminal:
            outcome.skipped_terminal += 1
            logger.info("visual continuation skips terminal asset document_id=%s code=%s asset=%s",
                        doc.id, terminal[h], h[:12])
            continue
        remaining = ASSET_MAX_ATTEMPTS - attempts.get(h, 0)
        if remaining <= 0:
            # Budget consommé par des passes antérieures interrompues : terminal.
            try:
                _make_terminal(h, "visual_retry_budget_exhausted")
            except _Abort:
                outcome.action, outcome.reason = OUTCOME_SKIPPED, "superseded"
                return outcome
            continue
        # Aucun appel n'est lancé s'il ne peut pas se terminer avant l'échéance.
        if time.monotonic() + call_budget > deadline:
            continue_later = True
            break

        def _count_attempt(_h=h) -> None:
            attempts[_h] = attempts.get(_h, 0) + 1
            outcome.vision_calls += 1
            _persist(asset_attempts=dict(attempts))

        try:
            request = build_vision_request(asset, document_opaque_id=str(doc.id),
                                           locale=DEFAULT_LOCALE, model=profile)
            payload, retried = analyze_asset_with_retry(
                client, request, max_retries=remaining - 1, deadline=deadline, sleep=sleep,
                call_budget_s=call_budget, on_attempt=_count_attempt)
            outcome.asset_retries += retried
        except _Abort:
            outcome.action, outcome.reason = OUTCOME_SKIPPED, "superseded"
            return outcome
        except (VisionGatewayError, VisualAssetError) as exc:
            retried = int(getattr(exc, "asset_retries", 0) or 0)
            outcome.asset_retries += retried
            code = getattr(exc, "code", None) or "visual_request_invalid"
            try:
                if (getattr(exc, "retryable", False) and attempts.get(h, 0) < ASSET_MAX_ATTEMPTS
                        and not getattr(exc, "retries_exhausted", False)):
                    # Interruption RÉELLE (échéance de la tâche) : reprenable avec le budget
                    # restant par la continuation — jamais un budget neuf.
                    continue_later = True
                    break
                _make_terminal(h, code)
            except _Abort:
                outcome.action, outcome.reason = OUTCOME_SKIPPED, "superseded"
                return outcome
            continue

        try:
            artifact = build_artifact(asset, payload)
            out = normalize_artifact(artifact, context=asset.context, known_texts=known_texts)
            holder = type("Holder", (), {})()
            holder.tables = []
            for table in out.tables:
                _append_table_fragments(holder, table)
            candidate = _asset_candidate(doc=doc, space=space, units=out.units, tables=holder.tables,
                                         filename=filename)
        except Exception as exc:  # noqa: BLE001 - un visuel ne fait jamais échouer les autres
            logger.warning("visual enrichment asset indexing failed document_id=%s cls=%s asset=%s",
                           doc.id, exc.__class__.__name__, asset.content_hash[:12])
            # Terminal aussi : l'appel vision est consommé, une continuation le rejouerait.
            try:
                _make_terminal(h, "visual_enrichment_indexing_failed")
            except _Abort:
                outcome.action, outcome.reason = OUTCOME_SKIPPED, "superseded"
                return outcome
            continue

        def _patch(st, _h=asset.content_hash):
            st["assets_published"] = int(st.get("assets_published") or 0) + 1
            analyzed = list(st.get("analyzed_hashes") or [])
            if _h not in analyzed:
                analyzed.append(_h)
            st["analyzed_hashes"] = analyzed
            return st

        try:
            outcome.published += 1
            _publish_asset(doc=doc, space=space, generation=generation,
                           content_sha256=state.get("content_sha256") or "",
                           asset_hash=asset.content_hash, candidate=candidate, state_patch=_patch)
        except (_Abort, RagSpaceIdentityChanged) as exc:
            reason = str(exc) if isinstance(exc, _Abort) else "embedding_identity_changed"
            logger.info("visual enrichment aborted document_id=%s reason=%s", doc.id, reason)
            outcome.action, outcome.reason = OUTCOME_SKIPPED, reason
            return outcome

    if continue_later:
        outcome.action = OUTCOME_CONTINUE
        D.update_state(doc.id, generation=generation, patch={
            "asset_retries": int(state.get("asset_retries") or 0) + outcome.asset_retries,
        })
        return outcome

    # Échecs de la GÉNÉRATION (persistés), pas seulement ceux de cette passe : une passe
    # de continuation ne voit plus les échecs terminaux des passes précédentes.
    failed_total = len(terminal)
    total_ok = outcome.published + outcome.skipped_published
    if failed_total and total_ok:
        status = D.STATUS_PARTIAL
    elif failed_total:
        status = D.STATUS_FAILED
    else:
        status = D.STATUS_SUCCEEDED
    fresh = D.state_of(Document.objects.filter(id=doc.id).first() or doc)
    codes: dict = {}
    for code in terminal.values():
        codes[code] = codes.get(code, 0) + 1
    D.update_state(doc.id, generation=generation, patch={
        "status": status,
        "assets_failed": failed_total,
        "asset_retries": int(fresh.get("asset_retries") or 0) + outcome.asset_retries,
        "failure_codes": codes,
        "completed_at": D._now(),
    })
    outcome.reason = status
    return outcome
