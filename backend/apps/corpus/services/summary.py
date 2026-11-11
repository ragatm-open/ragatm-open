"""Résumé d'un document (ou d'une page) de **corpus métier**.

Symétrique de ``apps.rag_core.services.summary_service``, pour l'autre famille de
documents. Ce qui est commun aux deux — découpage, prompts, boucle map-reduce,
budgets, décision sync/async — vit dans ``summary_engine`` et n'est pas recopié
ici. Ce module ne porte que ce qui diffère réellement :

1. **la source du contenu** : ``DomainCorpusChunk`` et non ``Chunk`` ;
2. **la source de la politique d'exécution** ;
3. **le cache** : table sœur ``DomainCorpusDocumentSummary``.

Politique LLM : l'espace CONSOMMATEUR
------------------------------------
Un ``DomainCorpusDocument`` n'a pas de ``RagSpace``. Le corpus fournit le
*contenu* ; l'espace qui déclenche l'action fournit la *politique d'exécution*
(provider, modèle, egress, budgets). C'est la seule lecture qui préserve les deux
invariants du projet : un workspace ne peut pas se voir imposer un provider qu'il
n'a pas choisi, et le contenu du corpus ne peut pas sortir vers un fournisseur que
la politique d'egress de cet espace interdit. Dériver la politique du corpus
lui-même (partagé, administré par le propriétaire global) reviendrait à laisser
une configuration hors du contrôle du workspace décider de ses egress.

Corollaire assumé : sans espace consommateur valide, **on refuse**. Il n'existe
aucun contexte global de repli — un tel repli serait, par construction, plus
permissif que la politique qu'il remplace.

Le contrôle d'accès (association workspace ↔ corpus, anti-énumération) est fait
en amont par ``apps.corpus.services.access`` ; ce service reçoit un document déjà
autorisé, et **revérifie** l'autorisation avant toute restitution issue du cache
ou d'une tâche asynchrone.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from apps.chat.services.chat_formatting import normalize_llm_overrides
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.router import LLMError, generate_text
from apps.corpus.models import (
    CorpusDocumentStatus,
    DomainCorpusChunk,
    DomainCorpusDocumentSummary,
)
from apps.corpus.services import summary_events as events
from apps.corpus.services.access import workspace_can_read_corpus
from apps.rag_core.services import summary_engine as engine
from apps.rag_core.services.pricing import estimate_cost_usd, split_usage_tokens
from apps.rag_core.services.summary_service import (
    DocumentHasNoContent,
    DocumentNotReady,
    SummaryError,
    SummaryProviderError,
    SummaryResult,
    build_variant_key,
    normalize_summary_request,
)

logger = logging.getLogger(__name__)

#: Opération d'egress. Distincte de ``document.summary`` pour que l'audit d'egress
#: sache dire QUELLE famille de contenu est sortie vers quel fournisseur.
EGRESS_OPERATION = "domain_corpus.document.summary"

SOURCE_TYPE = "domain_corpus"

#: Nombre de lignes remontées par aller-retour SQL. Le résumé doit matérialiser ses
#: chunks (l'empreinte les parcourt, puis la génération les reparcourt), mais rien
#: n'oblige à le faire en un seul fetch : trois colonnes, par lots.
_CHUNK_FETCH_BATCH = 500


class CorpusSummaryDisabled(SummaryError):
    """Fonctionnalité non activée sur cette instance (feature flag).

    ``code = "not_found"`` volontairement : exposer un code distinct révélerait
    l'état d'activation, donc l'existence de la surface, à un client non habilité.
    La cause réelle (``reason="feature_disabled"``) part dans l'événement
    structuré, où l'exploitant la lit.
    """

    code = "not_found"
    http_status = 404


class MissingConsumerSpace(SummaryError):
    """Aucun RagSpace consommateur : impossible de résoudre une politique LLM."""

    code = "consumer_space_required"
    http_status = 400


class CorpusAccessRevoked(SummaryError):
    """L'accès du workspace au corpus a été retiré depuis la demande."""

    code = "not_found"
    http_status = 404


def is_enabled() -> bool:
    """Le résumé de corpus métier est-il activé sur cette instance ?

    Source de vérité UNIQUE : le backend. Le frontend peut masquer l'action, mais
    c'est cette fonction qui refuse — sinon une UI en avance de phase ouvrirait un
    chemin non qualifié.
    """
    return bool(getattr(settings, "DOMAIN_CORPUS_SUMMARY_ENABLED", False))


# ---------------------------------------------------------------------------
# Récupération des chunks
# ---------------------------------------------------------------------------
def fetch_domain_corpus_summary_chunks(document, *, scope: str, page: int | None = None):
    """Chunks ORDONNÉS d'un document de corpus métier, pour un scope donné.

    - ``scope="document"`` : tous les chunks du document ;
    - ``scope="page"``     : **uniquement** ``page_number=page``. Jamais de repli
      silencieux vers une autre page ni vers le document entier : une page vide est
      une erreur métier explicite, pas une invitation à résumer autre chose.

    Ordre : ``chunk_index`` puis ``id`` — le second départage les index dupliqués
    (possibles sur d'anciennes ingestions) pour que deux exécutions produisent la
    même empreinte, donc le même cache.

    Les chunks vides sont écartés ici plutôt que dans le moteur : ils fausseraient
    l'empreinte de cache sans jamais contribuer au contenu.
    """
    qs = DomainCorpusChunk.objects.filter(document_id=document.id)
    if scope == "page":
        # ``page`` est déjà normalisé (int) par ``normalize_summary_request`` ; un
        # scope page sans page y est retombé sur ``document``.
        qs = qs.filter(page_number=page)

    rows = [
        ch
        for ch in qs.order_by("chunk_index", "id")
        .only("id", "text", "page_number", "chunk_index")
        .iterator(chunk_size=_CHUNK_FETCH_BATCH)
        if (ch.text or "").strip()
    ]
    if not rows:
        if scope == "page":
            raise DocumentHasNoContent(f"La page {page} ne contient aucun extrait indexé.")
        raise DocumentHasNoContent("Le document ne contient aucun extrait indexé.")
    return rows


# ---------------------------------------------------------------------------
# Génération
# ---------------------------------------------------------------------------
def _generate(prompt: str, provider, model, *, options=None, think=None, context=None) -> dict:
    """Appel LLM. ``context`` absent => repli fail-closed (aucun egress externe).

    Le repli n'est pas une commodité : sans contexte résolu, envoyer du contenu de
    corpus chez un fournisseur externe échapperait à toute politique d'espace.
    """
    try:
        return generate_text(
            prompt=prompt, provider=provider, model=model, options=options, think=think,
            context=context or LlmExecutionContext.system_local_only(
                operation=EGRESS_OPERATION
            ),
            operation=EGRESS_OPERATION,
        )
    except LLMError as e:
        raise SummaryProviderError(str(e)) from e


def _llm_context(rag_space, *, user=None) -> engine.SummaryLLMContext:
    """Politique d'exécution dérivée de l'espace CONSOMMATEUR (cf. docstring module)."""
    if rag_space is None:
        raise MissingConsumerSpace(
            "Aucun espace consommateur : la politique d'exécution LLM est indéterminée."
        )
    provider, model = normalize_llm_overrides(rag_space)
    provider, model = engine.resolve_report_model(provider, model)
    return engine.SummaryLLMContext(
        provider=provider,
        model=model,
        options=engine.report_generation_options(),
        think=engine.report_think(model),
        egress_context=LlmExecutionContext.for_rag_space(
            rag_space, operation=EGRESS_OPERATION, user_id=getattr(user, "id", None)
        ),
    )


def _document_title(document) -> str:
    """Titre affiché dans le prompt : ``title`` s'il existe, sinon le nom du fichier."""
    return document.display_title


def _perform_generation(document, rag_space, mode, scope, page, language, chunks, *, user=None):
    prompts = engine.default_prompt_builders(title=_document_title(document), date_iso=None)
    try:
        return engine.generate_summary_from_chunks(
            chunks=chunks, prompts=prompts,
            mode=mode, scope=scope, page=page, language=language,
            llm=_llm_context(rag_space, user=user),
            generate=_generate,
            split_tokens=lambda provider, usage, total: split_usage_tokens(
                provider, usage, total=total
            ),
        )
    except engine.EmptyContentError as e:
        raise DocumentHasNoContent(str(e)) from e
    except engine.EmptyAnswerError as e:
        raise SummaryProviderError(str(e)) from e


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_STATUS_STR = {"READY": "ready", "PENDING": "pending", "FAILED": "failed"}


def _result_from_row(row: DomainCorpusDocumentSummary, *, cached: bool) -> SummaryResult:
    """Projette une ligne de cache dans le MÊME ``SummaryResult`` que le privé.

    Un type de retour commun est ce qui permet à la vue et à la persistance du fil
    d'être partagées : deux dataclasses jumelles auraient fini par diverger d'un
    champ, et c'est le fil de discussion qui l'aurait affiché.
    """
    return SummaryResult(
        summary=row.summary or "",
        mode=row.mode,
        scope=row.scope,
        page=row.page if row.scope == "page" else None,
        language=row.language,
        truncated=row.truncated,
        provider=row.provider or None,
        model=row.model or None,
        tokens=int(row.total_tokens or 0),
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        cost_usd=row.cost_usd,
        cached=cached,
        generated_at=row.updated_at,
        log_id=None,  # pas de RagQueryLog : cf. `_audit_note`
        status=_STATUS_STR.get(row.status, "ready"),
        summary_id=str(row.id),
        error=row.error_code or row.error_message or "",
    )


def get_cached_corpus_summary(
    *, document, rag_space, mode, scope, page, language, user=None
) -> SummaryResult | None:
    """Lecture seule de l'état d'un résumé métier (polling). Aucune génération.

    ``rag_space`` participe à la clé : le cache est indexé par espace consommateur
    (cf. docstring du modèle). ``user`` est requis pour que ``auto`` se résolve
    exactement comme au POST, sinon le polling interrogerait une variante qui
    n'existe pas.
    """
    mode, scope, page, language = normalize_summary_request(
        mode, scope, page, language, user=user
    )
    row = DomainCorpusDocumentSummary.objects.filter(
        document=document,
        rag_space=rag_space,
        variant_key=build_variant_key(mode, scope, page, language),
    ).first()
    if row is None:
        return None
    return _result_from_row(row, cached=(row.status == "READY"))


def _settle_chat_messages(summary_id, *, ready: bool, content: str = "", error: str = "") -> None:
    """Propage l'état terminal aux messages de chat en attente (fan-out).

    Fail-open volontaire, comme le chemin privé : un incident de persistance du
    message ne doit pas annuler un résumé correctement généré.
    """
    try:
        from apps.chat.services.summary_messages import (
            complete_messages_for_summary,
            fail_messages_for_summary,
        )

        if ready:
            complete_messages_for_summary(summary_id=summary_id, content=content)
        else:
            fail_messages_for_summary(summary_id=summary_id, error=error)
    except Exception:  # noqa: BLE001
        logger.exception("settle chat messages failed (corpus summary_id=%s)", summary_id)


def _finalize(*, document, rag_space, workspace, user, mode, scope, page, language,
              fingerprint, variant_key, gen, refresh: bool) -> SummaryResult:
    cost = estimate_cost_usd(
        provider=gen.provider, model=gen.model,
        prompt_tokens=gen.prompt_tokens, completion_tokens=gen.completion_tokens,
    )
    existing = DomainCorpusDocumentSummary.objects.filter(
        document=document, rag_space=rag_space, variant_key=variant_key
    ).first()
    revision = (existing.refresh_revision if existing else 0) + (1 if refresh and existing else 0)

    row, _created = DomainCorpusDocumentSummary.objects.update_or_create(
        document=document,
        rag_space=rag_space,
        variant_key=variant_key,
        defaults=dict(
            corpus_id=document.corpus_id,
            workspace=workspace,
            created_by=(existing.created_by if existing and existing.created_by_id else user),
            status="READY",
            error_message="",
            error_code="",
            mode=mode,
            scope=scope,
            page=page if scope == "page" else None,
            language=language,
            summary=gen.answer,
            truncated=gen.truncated,
            source_fingerprint=fingerprint,
            refresh_revision=revision,
            provider=(gen.provider or ""),
            model=(gen.model or ""),
            prompt_tokens=gen.prompt_tokens,
            completion_tokens=gen.completion_tokens,
            total_tokens=gen.total_tokens,
            cost_usd=cost,
        ),
    )
    events.emit(
        events.EVENT_COMPLETED,
        source_type=SOURCE_TYPE,
        user_id=getattr(user, "id", None),
        workspace_id=getattr(workspace, "id", None),
        rag_space_id=getattr(rag_space, "id", None),
        corpus_id=document.corpus_id,
        document_id=document.id,
        summary_id=row.id,
        scope=scope, page=page, mode=mode, language=language,
        provider=gen.provider, model=gen.model,
        prompt_tokens=gen.prompt_tokens, completion_tokens=gen.completion_tokens,
        total_tokens=gen.total_tokens, cost_usd=cost,
    )
    _settle_chat_messages(row.id, ready=True, content=gen.answer)
    return _result_from_row(row, cached=False)


def _enqueue_async(*, document, rag_space, workspace, user, mode, scope, page, language,
                   fingerprint, variant_key) -> SummaryResult:
    row, _created = DomainCorpusDocumentSummary.objects.update_or_create(
        document=document,
        rag_space=rag_space,
        variant_key=variant_key,
        defaults=dict(
            corpus_id=document.corpus_id,
            workspace=workspace,
            created_by=user,
            status="PENDING",
            error_message="",
            error_code="",
            mode=mode,
            scope=scope,
            page=page if scope == "page" else None,
            language=language,
            summary="",
            truncated=False,
            source_fingerprint=fingerprint,
            provider="",
            model="",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            cost_usd=None,
        ),
    )
    try:
        from apps.corpus.tasks import generate_domain_corpus_summary_task

        generate_domain_corpus_summary_task.delay(
            summary_id=str(row.id), user_id=getattr(user, "id", None)
        )
    except Exception as e:  # broker indisponible : échec observable, pas de PENDING fantôme
        logger.exception("corpus summary enqueue failed (summary_id=%s): %s", row.id, e)
        row.status = "FAILED"
        row.error_code = "enqueue_failed"
        row.error_message = f"enqueue_failed: {e}"[:1000]
        row.save(update_fields=["status", "error_code", "error_message", "updated_at"])
        _settle_chat_messages(row.id, ready=False, error="enqueue_failed")
    return _result_from_row(row, cached=False)


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def summarize_corpus_document(
    *,
    user,
    workspace,
    rag_space,
    document,
    mode: str = engine.DEFAULT_MODE,
    scope: str = engine.DEFAULT_SCOPE,
    page=None,
    language: str = "auto",
    refresh: bool = False,
    allow_async: bool = True,
) -> SummaryResult:
    """Génère (ou réutilise) le résumé d'un document de corpus **déjà autorisé**.

    L'autorisation est REVÉRIFIÉE ici, avant toute lecture de cache : une
    habilitation retirée entre la résolution et la restitution doit bloquer, et un
    résumé déjà en base ne doit jamais devenir un contournement du contrôle
    d'accès.
    """
    if not is_enabled():
        events.emit(
            events.EVENT_ACCESS_DENIED,
            reason="feature_disabled",
            source_type=SOURCE_TYPE,
            user_id=getattr(user, "id", None),
            workspace_id=getattr(workspace, "id", None),
            document_id=getattr(document, "id", None),
        )
        raise CorpusSummaryDisabled("not_found")

    if not workspace_can_read_corpus(workspace, document.corpus_id):
        events.emit(
            events.EVENT_ACCESS_DENIED,
            reason="corpus_not_authorized",
            source_type=SOURCE_TYPE,
            user_id=getattr(user, "id", None),
            workspace_id=getattr(workspace, "id", None),
            corpus_id=document.corpus_id,
            document_id=document.id,
        )
        raise CorpusAccessRevoked("not_found")

    mode, scope, page, language = normalize_summary_request(
        mode, scope, page, language, user=user
    )
    # Résolue AVANT toute génération : un espace consommateur absent ou invalide
    # doit refuser tôt, pas après avoir chargé le document.
    llm_probe = _llm_context(rag_space, user=user)

    events.emit(
        events.EVENT_REQUESTED,
        source_type=SOURCE_TYPE,
        user_id=getattr(user, "id", None),
        workspace_id=getattr(workspace, "id", None),
        rag_space_id=getattr(rag_space, "id", None),
        corpus_id=document.corpus_id,
        document_id=document.id,
        scope=scope, page=page, mode=mode, language=language,
        provider=llm_probe.provider, model=llm_probe.model,
    )

    if document.status != CorpusDocumentStatus.READY:
        events.emit(
            events.EVENT_FAILED,
            reason="document_not_ready",
            source_type=SOURCE_TYPE,
            document_id=document.id, corpus_id=document.corpus_id,
            workspace_id=getattr(workspace, "id", None),
            status=document.status,
        )
        raise DocumentNotReady(
            "Le document n'est pas prêt (indexation en cours, en attente ou échouée)."
        )

    chunks = fetch_domain_corpus_summary_chunks(document, scope=scope, page=page)
    fingerprint = engine.fingerprint(chunks, prompt_version=engine.prompt_version())
    variant_key = build_variant_key(mode, scope, page, language)

    # --- Cache : réutiliser si l'empreinte des chunks est inchangée -----------
    if not refresh:
        existing = DomainCorpusDocumentSummary.objects.filter(
            document=document, rag_space=rag_space, variant_key=variant_key
        ).first()
        if existing is not None and existing.source_fingerprint == fingerprint:
            if existing.status in ("READY", "PENDING"):
                events.emit(
                    events.EVENT_CACHE_HIT,
                    source_type=SOURCE_TYPE,
                    user_id=getattr(user, "id", None),
                    workspace_id=getattr(workspace, "id", None),
                    rag_space_id=getattr(rag_space, "id", None),
                    corpus_id=document.corpus_id,
                    document_id=document.id,
                    summary_id=existing.id,
                    scope=scope, page=page, mode=mode, language=language,
                    status=existing.status,
                )
                return _result_from_row(existing, cached=(existing.status == "READY"))
            # FAILED → on régénère ci-dessous

    blocks, _capped = engine.split_into_blocks(chunks)
    long = len(blocks) > 1
    est_tokens = engine.estimate_output_tokens(blocks)
    heavy = est_tokens > engine.async_max_sync_output_tokens()

    if allow_async and _should_run_async(long, heavy=heavy):
        events.emit(
            events.EVENT_STARTED,
            source_type=SOURCE_TYPE, document_id=document.id, corpus_id=document.corpus_id,
            workspace_id=getattr(workspace, "id", None),
            rag_space_id=getattr(rag_space, "id", None),
            scope=scope, page=page, mode=mode, language=language,
            chunk_count=len(chunks), **{"async": True},
        )
        return _enqueue_async(
            document=document, rag_space=rag_space, workspace=workspace, user=user,
            mode=mode, scope=scope, page=page, language=language,
            fingerprint=fingerprint, variant_key=variant_key,
        )

    events.emit(
        events.EVENT_STARTED,
        source_type=SOURCE_TYPE, document_id=document.id, corpus_id=document.corpus_id,
        workspace_id=getattr(workspace, "id", None),
        rag_space_id=getattr(rag_space, "id", None),
        scope=scope, page=page, mode=mode, language=language,
        chunk_count=len(chunks), **{"async": False},
    )
    gen = _perform_generation(document, rag_space, mode, scope, page, language, chunks, user=user)
    return _finalize(
        document=document, rag_space=rag_space, workspace=workspace, user=user,
        mode=mode, scope=scope, page=page, language=language,
        fingerprint=fingerprint, variant_key=variant_key, gen=gen, refresh=refresh,
    )


def _should_run_async(long: bool, heavy: bool = False) -> bool:
    """Décide sync/async — MÊME critère que le résumé privé.

    Volontairement une fonction locale plutôt qu'un import du service privé : elle
    lit deux settings et n'a pas d'autre corps. Le critère (long OU lourd, async
    activé, hors mode eager) est celui du moteur, testé des deux côtés.
    """
    if not (long or heavy) or not bool(getattr(settings, "SUMMARY_ASYNC_ENABLED", True)):
        return False
    if bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)):
        return False  # pas de broker en eager → on génère inline
    return True


# ---------------------------------------------------------------------------
# Exécution asynchrone (Celery)
# ---------------------------------------------------------------------------
def run_corpus_summary_generation_for_row(
    row: DomainCorpusDocumentSummary, *, user_id=None
) -> SummaryResult:
    """Exécute la génération pour une ligne ``PENDING``. Appelée par la tâche Celery.

    **Revérifie l'autorisation au démarrage.** Une tâche enfilée porte une décision
    d'accès prise plus tôt ; entre-temps l'association corpus ↔ workspace a pu être
    désactivée. Sans ce contrôle, la file d'attente serait un contournement
    différé du modèle de permissions.
    """
    document = row.document
    workspace = row.workspace

    if not workspace_can_read_corpus(workspace, document.corpus_id):
        events.emit(
            events.EVENT_ACCESS_DENIED,
            reason="corpus_access_revoked_before_generation",
            source_type=SOURCE_TYPE,
            workspace_id=getattr(workspace, "id", None),
            corpus_id=document.corpus_id,
            document_id=document.id,
            summary_id=row.id,
        )
        raise CorpusAccessRevoked("not_found")

    if document.status != CorpusDocumentStatus.READY:
        raise DocumentNotReady("Le document n'est pas prêt.")

    chunks = fetch_domain_corpus_summary_chunks(document, scope=row.scope, page=row.page)
    fingerprint = engine.fingerprint(chunks, prompt_version=engine.prompt_version())

    user = None
    if user_id is not None:
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(id=user_id).first()

    gen = _perform_generation(
        document, row.rag_space, row.mode, row.scope, row.page, row.language, chunks, user=user
    )
    return _finalize(
        document=document, rag_space=row.rag_space, workspace=workspace, user=user,
        mode=row.mode, scope=row.scope, page=row.page, language=row.language,
        fingerprint=fingerprint, variant_key=row.variant_key, gen=gen, refresh=False,
    )


def mark_row_failed(row: DomainCorpusDocumentSummary, *, code: str, message: str = "") -> None:
    """Marque une ligne en échec ET termine les messages du fil qui l'attendaient."""
    row.status = "FAILED"
    row.error_code = (code or "summary_failed")[:64]
    row.error_message = (message or code or "")[:1000]
    row.save(update_fields=["status", "error_code", "error_message", "updated_at"])
    events.emit(
        events.EVENT_FAILED,
        source_type=SOURCE_TYPE,
        document_id=row.document_id, corpus_id=row.corpus_id,
        workspace_id=row.workspace_id, rag_space_id=row.rag_space_id,
        summary_id=row.id, error_code=row.error_code,
    )
    _settle_chat_messages(row.id, ready=False, error=row.error_code)


def _pending_timeout_s() -> int:
    """Délai (s) au-delà duquel un résumé PENDING est considéré bloqué.

    Même réglage que le côté privé, et même règle : le défaut appartient au
    registre. Deux replis locaux, c'est deux valeurs qui finissent par diverger.
    """
    return engine.setting_int("SUMMARY_PENDING_TIMEOUT_S")


def reap_stale_corpus_summaries(
    *, timeout_s: int | None = None, now: datetime | None = None
) -> int:
    """Marque ``FAILED`` les résumés métier coincés en ``PENDING`` au-delà du délai.

    Même filet de sécurité que ``reap_stale_summaries`` côté privé : une tâche
    perdue laisserait sinon un message de fil bloqué sur « en cours » pour toujours.
    """
    timeout = int(timeout_s if timeout_s is not None else _pending_timeout_s())
    cutoff = (now or timezone.now()) - timedelta(seconds=timeout)
    stale = DomainCorpusDocumentSummary.objects.filter(status="PENDING", updated_at__lt=cutoff)
    stale_ids = list(stale.values_list("id", flat=True))
    count = stale.update(
        status="FAILED",
        error_code="stale_timeout",
        error_message=(
            f"stale_timeout: résumé resté PENDING au-delà de {timeout}s "
            "(tâche perdue ?). Relancez avec refresh=true."
        ),
    )
    if count:
        logger.warning(
            "reap_stale_corpus_summaries: %d résumé(s) PENDING reapé(s) (>%ss)", count, timeout
        )
        for sid in stale_ids:
            _settle_chat_messages(sid, ready=False, error="stale_timeout")
    return count
