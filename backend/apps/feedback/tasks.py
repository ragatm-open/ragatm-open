"""Tâches asynchrones du feedback.

Ce qui se passe APRÈS le clic, jamais pendant. La règle qui gouverne ce module :
tout ce qui coûte — lecture du tampon, relecture du journal, replay du
retrieval, vérification objective d'un terme — se fait ici, hors du chemin de
réponse de l'utilisateur.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.db import IntegrityError

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    acks_late=True,
)
def capture_feedback_snapshot_task(self, feedback_id: str) -> dict:
    """Promeut la trace du tampon en snapshot durable.

    Trois issues possibles, toujours ÉTIQUETÉES :

    - ``buffer``  — la trace exacte était encore là. C'est le cas nominal quand
      le signalement arrive dans la fenêtre du TTL ;
    - ``replay``  — le tampon a expiré et le retrieval a été rejoué. Utile, mais
      ce n'est PAS l'état d'origine, et le snapshot le dit ;
    - ``partial`` — ni l'un ni l'autre : on conserve ce que le journal contient
      encore, et on ne prétend pas à davantage.
    """
    from apps.feedback.diagnostics.policy import diagnostics_enabled
    from apps.feedback.models import AnswerFeedback

    # SECONDE garde — celle qui tient l'invariant. Le producteur en pose déjà une
    # pour éviter la tâche inutile, mais elle ne couvre pas : une tâche en retard,
    # un appel direct, un producteur d'une version antérieure, ou une bascule de
    # configuration entre l'enfilement et l'exécution. Sans ce contrôle ici,
    # désactiver le diagnostic ne le désactiverait pas vraiment.
    if not diagnostics_enabled():
        logger.info(
            "feedback_snapshot_skipped feedback_id=%s reason=diagnostics_disabled",
            feedback_id,
        )
        return {"status": "diagnostics_disabled", "feedback_id": str(feedback_id)}

    feedback = AnswerFeedback.objects.filter(id=feedback_id).first()
    if feedback is None:
        logger.warning("feedback_snapshot_skipped feedback_id=%s error_code=NotFound", feedback_id)
        return {"status": "not_found"}

    if hasattr(feedback, "diagnostic_snapshot"):
        # Idempotence : le snapshot est immuable, il ne se recrée pas.
        return {"status": "already_captured", "feedback_id": str(feedback.id)}

    try:
        return _build_snapshot(feedback)
    except IntegrityError:
        # Le test ci-dessus lit une relation MISE EN CACHE sur l'instance : il ne
        # protège pas d'une exécution concurrente ni d'un réessai Celery, où le
        # snapshot a pu être créé entre le chargement et l'écriture.
        #
        # La contrainte d'unicité est le VRAI garde-fou — encore faut-il la
        # reconnaître. Sans cela, un doublon partait en `autoretry_for=Exception`,
        # échouait trois fois, et finissait en tâche en erreur alors que le
        # travail était déjà fait. Constaté en qualification staging le
        # 2026-07-28, worker réel.
        logger.info(
            "feedback_snapshot_already_captured feedback_id=%s (course gagnée ailleurs)",
            feedback.id,
        )
        return {"status": "already_captured", "feedback_id": str(feedback.id)}


def _build_snapshot(feedback) -> dict:
    from apps.audit.models import RagQueryLog
    from apps.feedback.diagnostics import buffer, conversation, snapshot as snap
    from apps.feedback.services import priority

    log = None
    if feedback.query_log_id:
        log = RagQueryLog.objects.filter(id=feedback.query_log_id).first()

    rag_space = None
    if feedback.rag_space_id_hist:
        from apps.rag_core.models import RagSpace

        rag_space = RagSpace.objects.filter(id=feedback.rag_space_id_hist).first()

    # --- 1. Tampon : la seule capture EXACTE -----------------------------
    trace = buffer.read_trace(feedback.query_log_id_hist)
    capture_mode = snap.FeedbackDiagnosticSnapshot.MODE_BUFFER if trace else None

    # --- 2. Replay : utile, mais jamais présenté comme la capture --------
    if trace is None:
        logger.info(
            "feedback_buffer_miss feedback_id=%s log_id=%s",
            feedback.id,
            feedback.query_log_id_hist,
        )
        trace = _replay_retrieval(feedback, log=log, rag_space=rag_space)
        capture_mode = (
            snap.FeedbackDiagnosticSnapshot.MODE_REPLAY
            if trace is not None
            else snap.FeedbackDiagnosticSnapshot.MODE_PARTIAL
        )

    if trace is None:
        trace = {
            "schema_version": 1,
            "candidates": [],
            "selection": {},
            "unavailable_reason": "buffer_expired_and_replay_unavailable",
        }

    # --- 3. Contexte conversationnel -------------------------------------
    include_text = snap.prompt_capture_allowed()
    context = conversation.capture_conversation_context(
        thread_id=feedback.thread_id,
        before_message_id=feedback.message_id,
        include_text=include_text,
    )

    request_payload = snap.build_request_payload(
        log,
        language_decision=_language_decision(log),
        conversation=context,
    )
    effective_config = snap.build_effective_config(rag_space=rag_space, trace=trace)
    generation_metadata = snap.build_generation_metadata(log, prompt=None)
    selected_chunks = snap.build_selected_chunks(log)

    if log is None:
        # Le journal a été purgé ou l'espace supprimé : le signal survit, le
        # matériel non. On l'écrit explicitement plutôt que de livrer un
        # snapshot silencieusement vide.
        capture_mode = snap.FeedbackDiagnosticSnapshot.MODE_PARTIAL
        request_payload["question_unavailable"] = True

    row = snap.create_snapshot(
        feedback=feedback,
        capture_mode=capture_mode,
        request_payload=request_payload,
        effective_config=effective_config,
        retrieval_trace=trace,
        selected_chunks=selected_chunks,
        generation_metadata=generation_metadata,
    )

    # Priorité affinée par les signaux VÉRIFIÉS (terme réellement indexé,
    # terme présent dans le contexte récent) — hors du chemin du clic.
    context_bonus = 1 if conversation.term_appears_in_context(context, feedback.expected_term) else 0
    new_score = priority.recompute_priority(feedback, context_bonus=context_bonus)
    if new_score != feedback.priority_score:
        feedback.priority_score = new_score
        feedback.save(update_fields=["priority_score", "updated_at"])

    if capture_mode == snap.FeedbackDiagnosticSnapshot.MODE_BUFFER:
        # Promue : la trace n'a plus de raison d'occuper le tampon.
        buffer.delete_trace(feedback.query_log_id_hist)

    if capture_mode == snap.FeedbackDiagnosticSnapshot.MODE_PARTIAL:
        logger.info(
            "feedback_snapshot_partial feedback_id=%s snapshot_id=%s", feedback.id, row.id
        )

    return {
        "status": "captured",
        "feedback_id": str(feedback.id),
        "snapshot_id": str(row.id),
        "capture_mode": capture_mode,
        "candidate_count": len(trace.get("candidates") or []),
        "priority_score": new_score,
    }


def _language_decision(log) -> dict:
    """Décision de langue, RECALCULÉE et marquée comme telle.

    La langue résolue n'est pas persistée par le pipeline : seule la langue
    détectée est recalculable à partir de la question. On expose donc ce qu'on
    sait, en disant d'où ça vient — un champ « langue résolue » reconstruit
    passerait pour une observation alors qu'il n'en est pas une.
    """
    if log is None:
        return {}
    try:
        from apps.rag_core.services.answer_language import detect_question_language

        return {
            "source": "recomputed",
            "detected_question_language": detect_question_language(
                getattr(log, "question", "") or ""
            ),
            "resolved_answer_language": None,
            "resolved_available": False,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("feedback_language_decision_failed error_code=%s", type(exc).__name__)
        return {}


def _replay_retrieval(feedback, *, log, rag_space) -> dict | None:
    """Rejoue le retrieval pour reconstituer une trace. JAMAIS une capture.

    Contraintes tenues :

    - aucun appel LLM (``query_rag`` ne génère pas) ;
    - aucun ``RagQueryLog`` métier créé — l'appel se fait sans utilisateur, et
      le journal n'est écrit que si un utilisateur est fourni ;
    - aucun fil ni message touché ;
    - divergence de modèle d'embedding signalée.
    """
    if not bool(getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED", False)):
        return None
    if log is None or rag_space is None:
        return None
    question = getattr(log, "question", "") or ""
    if not question or question.startswith("[redacted"):
        # Minimisation active : la question n'existe plus en clair, il n'y a
        # rien à rejouer. Mieux vaut un `partial` assumé qu'un replay sur une
        # question fabriquée.
        return None

    captured: dict = {}

    def _sink(payload: dict) -> None:
        captured.update(payload)

    try:
        from django.utils import timezone

        from apps.rag_core.services.query.orchestrator import query_rag

        query_rag(
            rag_space_id=rag_space.id,
            question=question,
            user=None,  # <- aucun RagQueryLog n'est écrit sans utilisateur
            rag_space=rag_space,
            entrypoint="internal",
            trace_sink=_sink,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "feedback_replay_failed feedback_id=%s error_code=%s",
            feedback.id,
            type(exc).__name__,
        )
        return None

    if not captured:
        return None

    captured["replayed_at"] = timezone.now().isoformat()
    captured["replay_disclaimer"] = (
        "Trace REJOUÉE après coup : l'index, la configuration et les modèles ont pu "
        "changer depuis la réponse d'origine. Ce n'est pas la capture de l'incident."
    )
    current_embedding = str(getattr(rag_space, "embedding_model", "") or "")
    captured["embedding_model_at_replay"] = current_embedding
    captured["embedding_model_divergent"] = bool(
        captured.get("embedding_model")
        and current_embedding
        and captured["embedding_model"] != current_embedding
    )
    logger.info(
        "feedback_replay_completed feedback_id=%s candidate_count=%s",
        feedback.id,
        len(captured.get("candidates") or []),
    )
    return captured


@shared_task
def purge_expired_feedback_snapshots_task(apply: bool = True) -> dict:
    """Purge des snapshots expirés. Le feedback, lui, n'est jamais touché."""
    from apps.feedback.retention import purge_expired_snapshots

    return purge_expired_snapshots(apply=apply)
