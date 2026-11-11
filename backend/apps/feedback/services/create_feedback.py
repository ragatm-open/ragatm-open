"""Création d'un feedback — chemin SYNCHRONE du clic sur le pouce.

Contrainte dominante : ce code s'exécute pendant que l'utilisateur attend. Il ne
fait donc, volontairement, **que** des lectures indexées et une écriture. Aucun
appel LLM, aucune recherche vectorielle, aucun balayage de table, aucun
sous-processus. Tout ce qui coûte est repoussé dans la tâche Celery, enfilée
APRÈS le commit.

La copie immédiate des identifiants historiques n'est pas une optimisation :
c'est la condition de survie de l'incident. Différer cette copie à la tâche
asynchrone rouvrirait exactement la fenêtre que ce lot ferme — entre le clic et
la capture, l'espace peut être supprimé.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from apps.feedback.models import COMMENT_MAX_CHARS, AnswerFeedback
from apps.feedback.services import identity, priority
from apps.feedback.services.context import current_git_sha

logger = logging.getLogger(__name__)


class FeedbackAlreadyExists(Exception):
    """Un feedback existe déjà pour ce (log, utilisateur)."""

    def __init__(self, existing: AnswerFeedback):
        self.existing = existing
        super().__init__("feedback already exists")


#: Correspondance des motifs HISTORIQUES vers les catégories actuelles.
#: `missing_sources` était ambigu — « pas assez de sources » ou « pas les
#: bonnes » ? — et c'est justement l'ambiguïté qui le rendait inexploitable. Il
#: est traduit vers `wrong_sources`, l'interprétation la plus fréquente.
LEGACY_REASON_MAP = {
    "missing_sources": AnswerFeedback.KIND_WRONG_SOURCES,
    "irrelevant": AnswerFeedback.KIND_WRONG_SOURCES,
    "incorrect": AnswerFeedback.KIND_WRONG_ANSWER,
    "incomplete": AnswerFeedback.KIND_WRONG_ANSWER,
    "other": AnswerFeedback.KIND_OTHER,
}


def normalize_kind(*, kind: str | None, legacy_reason: str | None) -> str:
    """Catégorie canonique à partir du payload actuel OU de l'ancien.

    Une valeur inconnue retombe sur ``other`` plutôt que d'être rejetée : perdre
    un signalement parce que le client envoie une catégorie non reconnue serait
    un mauvais échange.
    """
    k = (kind or "").strip()
    if k in AnswerFeedback.KIND_VALUES:
        return k
    if k:
        # Catégorie FOURNIE mais inconnue : on ne perd pas le signalement, on le
        # range dans `other`. Le rejeter punirait l'utilisateur d'un client
        # obsolète ou d'une faute de frappe côté appelant.
        return AnswerFeedback.KIND_OTHER
    legacy = (legacy_reason or "").strip()
    if legacy:
        return LEGACY_REASON_MAP.get(legacy, AnswerFeedback.KIND_OTHER)
    # Rien de fourni (cas normal du pouce haut) : pas de catégorie inventée.
    return ""


def build_historic_fields(log) -> dict:
    """Fige tout ce qui rend l'incident interprétable après disparition du log."""
    sources = list(getattr(log, "sources", None) or [])
    src_fp = identity.sources_fingerprint(sources)
    return {
        "query_log_id_hist": log.id,
        "workspace_id_hist": str(getattr(log, "workspace_id", "") or "")[:64],
        "rag_space_id_hist": getattr(log, "rag_space_id", None),
        "question_hash": identity.sha256_text(getattr(log, "question", "")),
        "answer_hash": identity.sha256_text(getattr(log, "answer", "")),
        "prompt_hash": (getattr(log, "prompt_hash", "") or "")[:64],
        "sources_fingerprint": src_fp,
        "policy_hist": (getattr(log, "policy", "") or "")[:64],
        "llm_provider_hist": (getattr(log, "llm_provider", "") or "")[:64],
        "llm_model_hist": (getattr(log, "llm_model", "") or "")[:128],
        "log_created_at": getattr(log, "created_at", None),
    }


def create_feedback(
    *,
    user,
    log,
    rating: int,
    kind: str = "",
    comment: str = "",
    expected_document: str = "",
    expected_term: str = "",
    message_id=None,
    thread_id=None,
) -> AnswerFeedback:
    """Crée un ``AnswerFeedback`` durable. Lève :class:`FeedbackAlreadyExists`.

    L'enfilement de la capture diagnostique se fait via ``transaction.on_commit``
    et jamais avant : une tâche démarrée dans la transaction pourrait lire une
    ligne qui n'existe pas encore, ou qui n'existera jamais si le commit échoue.
    """
    hist = build_historic_fields(log)
    pseudonym = identity.pseudonymize_user(getattr(user, "pk", None))
    fingerprint = identity.dedup_fingerprint(
        question=getattr(log, "question", ""),
        rag_space_id_hist=hist["rag_space_id_hist"],
        llm_model_hist=hist["llm_model_hist"],
        sources_fingerprint_value=hist["sources_fingerprint"],
    )

    score = priority.compute_base_priority(
        kind=kind,
        policy_hist=hist["policy_hist"],
        rating=rating,
        dedup_fingerprint=fingerprint,
        expected_document=expected_document,
        expected_term=expected_term,
        distinct_reporters=priority.count_distinct_reporters(
            fingerprint, including_pseudonym=pseudonym
        ),
    )

    try:
        with transaction.atomic():
            feedback = AnswerFeedback.objects.create(
                query_log=log,
                created_by=user,
                message_id=message_id,
                thread_id=thread_id,
                user_pseudonym=pseudonym,
                rating=int(rating),
                kind=kind,
                comment=(comment or "")[:COMMENT_MAX_CHARS],
                expected_document=(expected_document or "")[:512],
                expected_term=(expected_term or "")[:512],
                status=AnswerFeedback.STATUS_NEW,
                priority_score=score,
                dedup_fingerprint=fingerprint,
                app_git_sha=current_git_sha(),
                **hist,
            )
    except IntegrityError:
        existing = AnswerFeedback.objects.filter(query_log=log, created_by=user).first()
        if existing is None:
            raise
        raise FeedbackAlreadyExists(existing) from None

    logger.info(
        "feedback_created feedback_id=%s rating=%s kind=%s policy=%s priority=%s",
        feedback.id,
        feedback.rating,
        feedback.kind or "-",
        feedback.policy_hist or "-",
        feedback.priority_score,
    )

    transaction.on_commit(lambda: _enqueue_snapshot(feedback.id))
    return feedback


def _enqueue_snapshot(feedback_id) -> None:
    """Enfile la capture diagnostique si le lot 2 est présent ET activé.

    Import tardif et fail-open : le lot 1 doit fonctionner seul, et une panne du
    broker ne doit jamais transformer un clic en erreur pour l'utilisateur —
    l'écriture du signal est déjà commitée à ce stade.

    Première des DEUX gardes : elle évite une tâche inutile. La seconde, dans le
    worker, garantit l'invariant même si la configuration change entre
    l'enfilement et l'exécution.
    """
    try:
        from apps.feedback.diagnostics.policy import diagnostics_enabled

        if not diagnostics_enabled():
            # Aucune capacité de capture activée : le feedback reste seul. On
            # n'écrit pas une seconde copie de la question, de la réponse et des
            # extraits pour 30 jours au nom d'un diagnostic qui est désactivé.
            logger.info(
                "feedback_snapshot_skipped feedback_id=%s reason=diagnostics_disabled",
                feedback_id,
            )
            return

        from apps.feedback.tasks import capture_feedback_snapshot_task

        capture_feedback_snapshot_task.delay(str(feedback_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "feedback.snapshot enqueue skipped feedback_id=%s error_code=%s",
            feedback_id,
            type(exc).__name__,
        )


def update_feedback(
    feedback: AnswerFeedback,
    *,
    kind: str | None = None,
    comment: str | None = None,
    expected_document: str | None = None,
    expected_term: str | None = None,
) -> AnswerFeedback:
    """Modification EXPLICITE d'un feedback existant (PATCH).

    Seuls les champs que l'utilisateur peut légitimement corriger sont
    modifiables : la note (`rating`) et tout l'historique restent figés. Un
    utilisateur qui change d'avis sur la note crée un cas nouveau, il ne réécrit
    pas le passé.
    """
    fields: list[str] = []
    if kind is not None and kind in AnswerFeedback.KIND_VALUES:
        feedback.kind = kind
        fields.append("kind")
    if comment is not None:
        feedback.comment = comment[:COMMENT_MAX_CHARS]
        fields.append("comment")
    if expected_document is not None:
        feedback.expected_document = expected_document[:512]
        fields.append("expected_document")
    if expected_term is not None:
        feedback.expected_term = expected_term[:512]
        fields.append("expected_term")

    if fields:
        feedback.priority_score = priority.compute_base_priority(
            kind=feedback.kind,
            policy_hist=feedback.policy_hist,
            rating=feedback.rating,
            dedup_fingerprint=feedback.dedup_fingerprint,
            expected_document=feedback.expected_document,
            expected_term=feedback.expected_term,
            distinct_reporters=priority.count_distinct_reporters(
                feedback.dedup_fingerprint, including_pseudonym=feedback.user_pseudonym
            ),
        )
        fields.append("priority_score")
        feedback.save(update_fields=[*fields, "updated_at"])
    return feedback
