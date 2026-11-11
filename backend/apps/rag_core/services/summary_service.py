# backend/apps/rag_core/services/summary_service.py
"""Résumé d'un **seul** document à partir de ses chunks déjà indexés.

Garanties (Étape 1 — MVP) :
- on ne résume qu'un document à la fois, jamais un mélange de l'espace ;
- on part uniquement des chunks du document ciblé (``document_id`` filtré),
  pas du retrieval RAG global (qui mélangerait les documents de l'espace) ;
- on respecte STRICTEMENT le provider LLM configuré sur le RagSpace
  (OpenAI vs Ollama) via ``normalize_llm_overrides`` ;
- documents longs : troncature bornée + drapeau ``truncated`` (le map-reduce
  et l'async sont reportés à une étape ultérieure).

Étape 4a — cache & coûts :
- le résumé est persisté (``DocumentSummary``) et réutilisé tant que les chunks
  du document n'ont pas changé (empreinte ``source_fingerprint``) ; ``refresh``
  force la régénération ;
- tokens (entrée/sortie/total) et coût estimé sont calculés et journalisés.

Le contrôle d'accès (anti-énumération, isolation workspace) est fait par la
vue via ``load_accessible_document`` ; ce service reçoit un ``Document`` déjà
autorisé.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

from apps.chat.services.chat_formatting import normalize_llm_overrides
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.router import LLMError, generate_text
from apps.rag_core.models.chunk import Chunk
from apps.rag_core.models.document import Document, DocumentStatus
from apps.rag_core.models.summary import DocumentSummary
from apps.rag_core.services import summary_engine as engine
from apps.rag_core.services.pricing import estimate_cost_usd, split_usage_tokens


# --- Budgets contexte (lus à l'appel pour rester surchargeables par settings/tests) ---
# Délégués au moteur générique : le privé et le corpus métier doivent partager les
# MÊMES budgets, sinon deux documents identiques se résument différemment selon leur
# provenance. Les noms privés sont conservés — ils sont utilisés par les tests.
_setting_int = engine.setting_int
_max_total = engine.max_total_chars
_max_per_chunk = engine.max_chars_per_chunk


def _async_enabled() -> bool:
    return bool(getattr(settings, "SUMMARY_ASYNC_ENABLED", True))


ALLOWED_MODES = engine.ALLOWED_MODES
ALLOWED_SCOPES = engine.ALLOWED_SCOPES
DEFAULT_MODE = engine.DEFAULT_MODE
DEFAULT_SCOPE = engine.DEFAULT_SCOPE
DEFAULT_LANGUAGE = engine.DEFAULT_LANGUAGE


def _coerce_int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class SummaryError(Exception):
    """Erreur métier mappée en réponse HTTP par la vue."""

    code = "summary_error"
    http_status = 400


class DocumentNotReady(SummaryError):
    code = "document_not_ready"
    http_status = 409


class DocumentHasNoContent(SummaryError):
    code = "document_no_content"
    http_status = 409


class SummaryProviderError(SummaryError):
    code = "summary_provider_unavailable"
    http_status = 502


@dataclass
class SummaryResult:
    summary: str
    mode: str
    scope: str
    page: int | None
    language: str
    truncated: bool
    provider: str | None
    model: str | None
    tokens: int
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    cached: bool
    generated_at: datetime | None
    log_id: str | None
    status: str = "ready"  # ready | pending | failed
    summary_id: str | None = None
    error: str = ""


# --- Prompts & empreinte : délégués au moteur générique --------------------
#
# Les textes vivent désormais dans ``summary_engine`` afin que le résumé privé et
# le résumé de corpus métier produisent le MÊME prompt à l'octet près. Les alias
# ci-dessous préservent les noms historiques de ce module (utilisés par les tests
# et par les appelants internes) : aucune chaîne n'a été modifiée au passage.
_SYSTEM_PROMPT = engine.SYSTEM_PROMPT
_MODE_INSTRUCTIONS = engine.MODE_INSTRUCTIONS
_SYSTEM_PROMPT_EN = engine.SYSTEM_PROMPT_EN
_MODE_INSTRUCTIONS_EN = engine.MODE_INSTRUCTIONS_EN
SUMMARY_PROMPT_VERSION = engine.SUMMARY_PROMPT_VERSION
_prompt_version = engine.prompt_version
_split_into_blocks = engine.split_into_blocks
_block_context = engine.block_context
_hash = engine.hash_text


def _fingerprint(chunks) -> str:
    """Empreinte des chunks + version de prompt (cf. ``summary_engine.fingerprint``)."""
    return engine.fingerprint(chunks, prompt_version=engine.prompt_version())


def _build_prompt(*, document: Document, mode: str, scope: str, page: int | None, language: str, context: str) -> str:
    return engine.build_prompt(
        title=document.filename, date_iso=_document_date_iso(document), mode=mode,
        scope=scope, page=page, language=language, context=context,
    )


def _build_map_prompt(*, document: Document, language: str, index: int, total: int, context: str) -> str:
    return engine.build_map_prompt(
        title=document.filename, language=language, index=index, total=total, context=context,
    )


def _build_reduce_prompt(*, document: Document, mode: str, scope: str, page: int | None, language: str, partials: str) -> str:
    return engine.build_reduce_prompt(
        title=document.filename, date_iso=_document_date_iso(document), mode=mode,
        scope=scope, page=page, language=language, partials=partials,
    )


def _document_date_iso(document: Document) -> str | None:
    d = getattr(document, "document_date", None)
    return d.isoformat() if d else None


def _split_tokens(provider: str | None, usage, total: int | None) -> tuple[int | None, int | None, int | None]:
    """Décompose l'usage provider en (prompt, completion, total).

    Délègue au splitter centralisé (apps.rag_core.services.pricing) pour garantir
    un comptage identique entre chat, résumé et benchmarks.
    """
    return split_usage_tokens(provider, usage, total=total)


def _create_log(
    *, user, document: Document, mode: str, scope: str, page: int | None, prompt_hash: str,
    provider: str | None, model: str | None, answer: str,
    prompt_tokens: int | None, completion_tokens: int | None, total_tokens: int | None,
    cost_estimated: float | None = None,
):
    """Journalise le résumé dans RagQueryLog (policy=summary). Best-effort.

    ``user`` peut être ``None`` (génération async sans utilisateur résolu) : on
    n'écrit alors pas de log (RagQueryLog.user est obligatoire).
    """
    if user is None:
        return None
    try:
        from apps.audit.models import RagQueryLog

        scope_label = f"{scope}:{page}" if scope == "page" and page is not None else scope
        return RagQueryLog.objects.create(
            user=user,
            workspace=document.rag_space.workspace,
            rag_space=document.rag_space,
            question=f"[summary:{mode}:{scope_label}] {document.filename}",
            answer=answer or "",
            status=RagQueryLog.STATUS_OK,
            sources=[{"document_id": str(document.id), "filename": document.filename}],
            sources_schema_version=1,
            prompt_hash=prompt_hash or "",
            llm_provider=(provider or ""),
            llm_model=(model or ""),
            llm_prompt_tokens=prompt_tokens,
            llm_completion_tokens=completion_tokens,
            llm_total_tokens=total_tokens,
            llm_cost_estimated=cost_estimated,
            policy="summary",
        )
    except Exception:
        # L'audit ne doit jamais casser la réponse fonctionnelle.
        return None


# --- Normalisation & clé de variante (partagées POST/GET/async) -------------
def _resolve_language(language, user) -> tuple[str, str | None, str | None, str]:
    """Résout la langue d'un résumé. Renvoie ``(pref, ui_locale, resolved, effective)``.

    ``language`` est une PRÉFÉRENCE (auto|fr|en) ; un choix forcé fr/en est respecté.

    ``auto`` sur un résumé est un cas particulier : il n'y a NI question NI historique
    de fil, donc les deux premiers échelons du résolveur sont muets. On enchaîne alors
    sur les préférences du COMPTE — d'abord ``preferred_answer_language``, puis
    ``preferred_locale`` en repli ultime (le seul signal disponible pour un résumé
    déclenché hors conversation, cf. la docstring de ``resolve_answer_language``). Sans
    cela, ``auto`` retomberait invariablement sur ``fr`` et un utilisateur anglophone ne
    pourrait JAMAIS obtenir de résumé anglais.

    Flag plateforme OFF → ``resolve_target`` renvoie ``None`` → ``fr`` : comportement ET
    clés de cache historiques inchangés, à l'octet près.
    """
    from apps.rag_core.services.answer_language import coerce_pref, resolve_target

    pref = coerce_pref((language or "auto").strip().lower() or "auto")
    ui_locale = None
    if pref == "auto" and user is not None:
        from apps.accounts.models import get_preferred_answer_language, get_preferred_locale

        pref = coerce_pref(get_preferred_answer_language(user))
        ui_locale = get_preferred_locale(user)
    resolved = resolve_target(pref=pref, question="", ui_locale=ui_locale)
    return pref, ui_locale, resolved, (resolved or DEFAULT_LANGUAGE)


def _normalize(mode, scope, page, language, *, user=None) -> tuple[str, str, int | None, str]:
    from apps.rag_core.services.answer_language import log_language_decision

    mode = (mode or DEFAULT_MODE).strip().lower()
    if mode not in ALLOWED_MODES:
        mode = DEFAULT_MODE
    # Le concret fr/en entre dans `variant_key` (cache) et pilote la structure EN/FR.
    pref, ui_locale, resolved, language = _resolve_language(language, user)
    scope = (scope or DEFAULT_SCOPE).strip().lower()
    if scope not in ALLOWED_SCOPES:
        scope = DEFAULT_SCOPE
    page = _coerce_int(page)
    if scope == "page" and page is None:
        scope = DEFAULT_SCOPE
    log_language_decision(
        surface="summary", pref=pref, ui_locale=ui_locale, resolved=resolved,
        effective_language=language, mode=mode, scope=scope,
        variant_key=build_variant_key(mode, scope, page, language),
    )
    return mode, scope, page, language


#: Normalisation PUBLIQUE d'une demande de résumé (mode/scope/page/langue).
#: Exposée pour que le résumé de corpus métier applique EXACTEMENT les mêmes règles
#: de validation et la même résolution de langue : deux normalisations distinctes
#: produiraient deux clés de variante pour une même demande.
normalize_summary_request = _normalize


def resolve_summary_language(language, *, user=None) -> str:
    """Langue CONCRÈTE (``fr``|``en``) d'un résumé, sans rien générer ni journaliser.

    Même résolution que le POST et le polling. Exposée pour que les appelants qui
    doivent afficher un texte AVANT la génération (placeholder « en cours », message
    d'échec) parlent la même langue que le résumé lui-même — sinon le fil mélange
    français et anglais.

    Ne passe PAS par ``_normalize`` : celui-ci journalise une décision complète avec
    `mode`/`scope`/`variant_key`, qu'il faudrait ici inventer. Un seul événement par
    résumé réel, émis au bon endroit.
    """
    return _resolve_language(language, user)[3]


def build_variant_key(mode: str, scope: str, page: int | None, language: str) -> str:
    return f"{mode}:{scope}:{page if scope == 'page' else ''}:{language}"


def _fetch_chunks(document: Document, scope: str, page: int | None):
    qs = Chunk.objects.filter(document_id=document.id)
    if scope == "page":
        qs = qs.filter(page_number=page)
    chunks = list(qs.order_by("chunk_index").only("id", "text", "page_number", "chunk_index"))
    if not chunks:
        if scope == "page":
            raise DocumentHasNoContent(f"La page {page} ne contient aucun extrait indexé.")
        raise DocumentHasNoContent("Le document ne contient aucun extrait indexé.")
    return chunks


_STATUS_STR = {"READY": "ready", "PENDING": "pending", "FAILED": "failed"}


def _result_from_row(row: DocumentSummary, *, cached: bool, log_id: str | None = None) -> SummaryResult:
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
        log_id=log_id,
        status=_STATUS_STR.get(row.status, "ready"),
        summary_id=str(row.id),
        error=row.error_message or "",
    )


# --- Génération LLM : simple (court) ou map-reduce (long) -------------------
@dataclass
class _Gen:
    answer: str
    pt: int | None
    ct: int | None
    tt: int | None
    provider: str | None
    model: str | None
    truncated: bool
    prompt_hash: str


_report_options = engine.report_generation_options


def _generate(prompt: str, provider, model, *, options=None, think=None, context=None) -> dict:
    try:
        return generate_text(
            prompt=prompt, provider=provider, model=model, options=options, think=think,
            # Fail-closed : sans contexte résolu, aucun egress externe possible.
            # Ce call-site envoyait `document.filename` + le contenu indexé chez
            # OpenAI sans qu'aucun réglage d'espace ne puisse l'empêcher (P0-C #8).
            context=context or LlmExecutionContext.system_local_only(
                operation="document.summary"
            ),
            operation="document.summary",
        )
    except LLMError as e:
        raise SummaryProviderError(str(e)) from e


def _perform_generation(document: Document, mode: str, scope: str, page: int | None, language: str, chunks) -> _Gen:
    """Résume les chunks d'un ``Document`` privé.

    Ne contient plus que ce qui est PROPRE au privé : le provider et la politique
    d'egress de l'espace propriétaire. Le découpage, les prompts et la boucle
    map-reduce vivent dans ``summary_engine`` et sont partagés avec le corpus
    métier — ``_generate`` reste local pour que la substitution de ``generate_text``
    en test continue de porter sur CE module.
    """
    provider, model = normalize_llm_overrides(document.rag_space)
    # Politique d'egress de l'espace propriétaire du document.
    egress_ctx = LlmExecutionContext.for_rag_space(
        document.rag_space, operation="document.summary"
    )
    provider, model = engine.resolve_report_model(provider, model)

    llm = engine.SummaryLLMContext(
        provider=provider,
        model=model,
        options=engine.report_generation_options(),
        think=engine.report_think(model),
        egress_context=egress_ctx,
    )
    # Les constructeurs passent par les fonctions de CE module (et non par celles du
    # moteur) : elles restent ainsi le point de substitution historique du service
    # privé, tout en produisant exactement les mêmes chaînes.
    prompts = engine.SummaryPromptBuilders(
        build=lambda **kw: _build_prompt(document=document, **kw),
        build_map=lambda **kw: _build_map_prompt(document=document, **kw),
        build_reduce=lambda **kw: _build_reduce_prompt(document=document, **kw),
    )
    try:
        gen = engine.generate_summary_from_chunks(
            chunks=chunks, prompts=prompts,
            mode=mode, scope=scope, page=page, language=language,
            llm=llm, generate=_generate, split_tokens=_split_tokens,
        )
    except engine.EmptyContentError as e:
        raise DocumentHasNoContent(str(e)) from e
    except engine.EmptyAnswerError as e:
        raise SummaryProviderError(str(e)) from e

    return _Gen(
        gen.answer, gen.prompt_tokens, gen.completion_tokens, gen.total_tokens,
        gen.provider, gen.model, gen.truncated, gen.prompt_hash,
    )


def _finalize(*, document, user, mode, scope, page, language, fingerprint, variant_key, gen: _Gen) -> SummaryResult:
    cost = estimate_cost_usd(provider=gen.provider, model=gen.model, prompt_tokens=gen.pt, completion_tokens=gen.ct)
    obj, _created = DocumentSummary.objects.update_or_create(
        document=document,
        variant_key=variant_key,
        defaults=dict(
            rag_space=document.rag_space,
            status="READY",
            error_message="",
            mode=mode,
            scope=scope,
            page=page if scope == "page" else None,
            language=language,
            summary=gen.answer,
            truncated=gen.truncated,
            source_fingerprint=fingerprint,
            provider=(gen.provider or ""),
            model=(gen.model or ""),
            prompt_tokens=gen.pt,
            completion_tokens=gen.ct,
            total_tokens=gen.tt,
            cost_usd=cost,
        ),
    )
    log = _create_log(
        user=user, document=document, mode=mode, scope=scope, page=page, prompt_hash=gen.prompt_hash,
        provider=gen.provider, model=gen.model, answer=gen.answer,
        prompt_tokens=gen.pt, completion_tokens=gen.ct, total_tokens=gen.tt,
        cost_estimated=cost,
    )
    # Fan-out : termine les messages de chat qui attendaient CE résumé. Un
    # DocumentSummary étant un cache partagé, il peut y en avoir plusieurs, dans
    # des fils et chez des utilisateurs différents. C'est ce qui fait aboutir le
    # chemin asynchrone sans dépendre d'un quelconque callback du frontend.
    _settle_chat_messages(obj.id, ready=True, content=gen.answer)
    return _result_from_row(obj, cached=False, log_id=str(log.id) if log else None)


def _settle_chat_messages(summary_id, *, ready: bool, content: str = "", error: str = "") -> None:
    """Propage l'état terminal d'un résumé aux messages de chat en attente.

    Fail-open volontaire : un incident de persistance du message ne doit pas
    annuler un résumé correctement généré. L'échec est journalisé (jamais
    silencieux), le résumé reste servi, et le reaper rattrapera le message.
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
        logger.exception("settle chat messages failed (summary_id=%s)", summary_id)


# Estimations de coût/latence : partagées avec le résumé de corpus métier, pour
# que les deux familles basculent en asynchrone sur le MÊME critère.
_report_num_predict = engine.report_num_predict
_async_max_sync_output_tokens = engine.async_max_sync_output_tokens
_estimate_generation_calls = engine.estimate_generation_calls
_estimate_output_tokens = engine.estimate_output_tokens


def _should_run_async(long: bool, heavy: bool = False) -> bool:
    """Décide sync/async. ``long`` = map-reduce ; ``heavy`` = estimation > seuil.

    Async seulement si (long OU heavy), l'async est activé, et on n'est pas en mode
    eager (pas de broker → génération inline). ``heavy`` défaut False => parité
    stricte avec l'ancienne signature à un argument.
    """
    if not (long or heavy) or not _async_enabled():
        return False
    if bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)):
        return False  # pas de broker en eager → on génère inline
    return True


def _enqueue_async(*, user, document, mode, scope, page, language, fingerprint, variant_key) -> SummaryResult:
    row, _created = DocumentSummary.objects.update_or_create(
        document=document,
        variant_key=variant_key,
        defaults=dict(
            rag_space=document.rag_space,
            status="PENDING",
            error_message="",
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
        from apps.rag_core.tasks import generate_document_summary_task

        generate_document_summary_task.delay(summary_id=str(row.id), user_id=getattr(user, "id", None))
    except Exception as e:  # broker indisponible / rejet : échec observable, pas de PENDING fantôme.
        logger.exception("summary enqueue failed (summary_id=%s): %s", row.id, e)
        row.status = "FAILED"
        row.error_message = f"enqueue_failed: {e}"[:1000]
        row.save(update_fields=["status", "error_message", "updated_at"])
        # Sans ceci, un broker indisponible laisserait le message du fil bloqué
        # sur « génération en cours » pour toujours.
        _settle_chat_messages(row.id, ready=False, error="enqueue_failed")
    return _result_from_row(row, cached=False)


def get_cached_summary(*, document: Document, mode, scope, page, language, user=None) -> SummaryResult | None:
    """Lecture seule de l'état d'un résumé (cache/polling). Aucune génération.

    ``user`` est REQUIS pour que le polling résolve `auto` exactement comme le POST
    qui a créé la variante : sans lui, `auto` retomberait sur `fr` ici et sur la
    préférence du compte là-bas, et le polling interrogerait une variante de cache
    qui n'existe pas (résumé « introuvable » en boucle).
    """
    mode, scope, page, language = _normalize(mode, scope, page, language, user=user)
    variant_key = build_variant_key(mode, scope, page, language)
    row = DocumentSummary.objects.filter(document=document, variant_key=variant_key).first()
    if row is None:
        return None
    return _result_from_row(row, cached=(row.status == "READY"))


def _pending_timeout_s() -> int:
    """Délai (s) au-delà duquel un résumé PENDING est considéré bloqué.

    Le défaut, le type et les bornes vivent au REGISTRE — pas ici. Un repli local
    serait au mieux redondant, au pire faux : il masquerait une valeur surchargée
    par l'exploitant. C'est le contrat de ``setting_int``, qui refuse
    délibérément un défaut d'appel.
    """
    return _setting_int("SUMMARY_PENDING_TIMEOUT_S")


def reap_stale_summaries(*, timeout_s: int | None = None, now: datetime | None = None) -> int:
    """Marque ``FAILED`` les résumés coincés en ``PENDING`` au-delà du délai.

    Résilience (Priorité C) : si une tâche Celery est perdue (broker/worker down,
    enqueue jamais consommé), la ligne resterait ``PENDING`` indéfiniment et le
    polling GET ne se terminerait jamais. Ce reaper (planifié + commande manuelle)
    transforme ces blocages en échec explicite ``stale_timeout``, que le client
    peut relancer via ``refresh=true``. Renvoie le nombre de lignes reapées.
    """
    timeout = int(timeout_s if timeout_s is not None else _pending_timeout_s())
    ref = now or timezone.now()
    cutoff = ref - timedelta(seconds=timeout)
    stale = DocumentSummary.objects.filter(status="PENDING", updated_at__lt=cutoff)
    # Identifiants relevés AVANT la mise à jour : après, la queryset ne matche plus.
    stale_ids = list(stale.values_list("id", flat=True))
    count = stale.update(
        status="FAILED",
        error_message=(
            f"stale_timeout: résumé resté PENDING au-delà de {timeout}s "
            "(tâche perdue ?). Relancez avec refresh=true."
        ),
    )
    if count:
        logger.warning("reap_stale_summaries: %d résumé(s) PENDING reapé(s) (>%ss)", count, timeout)
        # Le reaper est le filet de sécurité du fil : un résumé abandonné doit
        # devenir un message en échec explicite, jamais un « en cours » éternel.
        for sid in stale_ids:
            _settle_chat_messages(sid, ready=False, error="stale_timeout")
    return count


def run_summary_generation_for_row(row: DocumentSummary, *, user_id=None) -> SummaryResult:
    """Exécute la génération (map-reduce inclus) pour une ligne PENDING. Appelée
    par la tâche Celery. Lève ``SummaryError`` en cas de problème métier."""
    document = row.document
    if document.status != DocumentStatus.READY:
        raise DocumentNotReady("Le document n'est pas prêt.")
    chunks = _fetch_chunks(document, row.scope, row.page)
    fingerprint = _fingerprint(chunks)

    user = None
    if user_id is not None:
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(id=user_id).first()

    gen = _perform_generation(document, row.mode, row.scope, row.page, row.language, chunks)
    return _finalize(
        document=document, user=user, mode=row.mode, scope=row.scope, page=row.page,
        language=row.language, fingerprint=fingerprint, variant_key=row.variant_key, gen=gen,
    )


def summarize_document(
    *,
    user,
    document: Document,
    mode: str = DEFAULT_MODE,
    scope: str = DEFAULT_SCOPE,
    page=None,
    # `auto` (et non `fr`) : un appelant qui n'exprime PAS de langue laisse le
    # résolveur trancher d'après les préférences du compte. Flag plateforme OFF =>
    # `fr`, donc aucun changement pour les appelants existants (routeur d'actions).
    language: str = "auto",
    refresh: bool = False,
    allow_async: bool = True,
) -> SummaryResult:
    """Génère (ou réutilise) le résumé d'un document déjà autorisé.

    ``scope="page"`` (avec ``page``) restreint aux chunks de cette page ; sinon le
    document complet. Jamais de mélange avec d'autres documents. ``refresh=True``
    ignore le cache. Les documents **longs** sont résumés en **map-reduce** ; si un
    broker Celery est disponible (hors mode eager), la génération est **asynchrone**
    (réponse ``status="pending"``, polling via ``get_cached_summary``).
    """
    mode, scope, page, language = _normalize(mode, scope, page, language, user=user)

    if document.status != DocumentStatus.READY:
        raise DocumentNotReady("Le document n'est pas prêt (indexation en cours, en attente ou échouée).")

    chunks = _fetch_chunks(document, scope, page)
    fingerprint = _fingerprint(chunks)
    variant_key = build_variant_key(mode, scope, page, language)

    # --- Cache : réutiliser si l'empreinte des chunks est inchangée ---
    if not refresh:
        existing = DocumentSummary.objects.filter(document=document, variant_key=variant_key).first()
        if existing is not None and existing.source_fingerprint == fingerprint:
            if existing.status == "READY":
                return _result_from_row(existing, cached=True)
            if existing.status == "PENDING":
                return _result_from_row(existing, cached=False)
            # FAILED → on régénère ci-dessous

    blocks, _capped = _split_into_blocks(chunks)
    long = len(blocks) > 1
    # Estimation de coût : bascule async aussi les rapports mono-bloc lourds
    # (au-delà du seuil), pas seulement le map-reduce.
    est_tokens = _estimate_output_tokens(blocks)
    heavy = est_tokens > _async_max_sync_output_tokens()

    if allow_async and _should_run_async(long, heavy=heavy):
        logger.info(
            "summary_async decision=async reason=%s blocks=%d est_output_tokens=%d threshold=%d",
            "map_reduce" if long else "heavy_single",
            len(blocks), est_tokens, _async_max_sync_output_tokens(),
        )
        return _enqueue_async(
            user=user, document=document, mode=mode, scope=scope, page=page,
            language=language, fingerprint=fingerprint, variant_key=variant_key,
        )

    gen = _perform_generation(document, mode, scope, page, language, chunks)
    return _finalize(
        document=document, user=user, mode=mode, scope=scope, page=page,
        language=language, fingerprint=fingerprint, variant_key=variant_key, gen=gen,
    )
