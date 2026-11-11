"""Tâches asynchrones du corpus métier.

Symétrique de ``apps.rag_core.tasks.generate_document_summary_task`` pour l'autre
famille de documents. La différence structurante est le contrôle d'accès : une
tâche enfilée porte une décision d'habilitation prise plus tôt. Pour le privé, la
revalidation passe par ``task_guard`` (périmètre owner) ; pour le corpus métier,
c'est l'association workspace ↔ corpus qui doit être reconfirmée — et elle l'est
dans ``run_corpus_summary_generation_for_row``, au tout début de l'exécution.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def generate_domain_corpus_summary_task(self, *, summary_id: str, user_id=None) -> dict:
    """Génère un résumé de document de corpus métier (long) en arrière-plan.

    La ligne ``DomainCorpusDocumentSummary`` (statut PENDING) passe à READY avec le
    contenu, ou à FAILED avec un ``error_code`` stable. Jamais de blocage
    silencieux en PENDING : un échec explicite est préférable, et le reaper
    (``reap_stale_corpus_summaries``) rattrape les tâches perdues.

    Retry avec backoff exponentiel sur erreur provider transitoire uniquement. Les
    erreurs permanentes (document non prêt, sans contenu, accès révoqué) échouent
    immédiatement : les retenter ne ferait que retarder l'affichage de l'échec.
    """
    from celery.exceptions import MaxRetriesExceededError

    from apps.corpus.models import DomainCorpusDocumentSummary
    from apps.corpus.services.summary import (
        SummaryError,
        SummaryProviderError,
        mark_row_failed,
        run_corpus_summary_generation_for_row,
    )

    row = (
        DomainCorpusDocumentSummary.objects.select_related(
            "document", "document__corpus", "rag_space", "workspace"
        )
        .filter(id=summary_id)
        .first()
    )
    if row is None:
        logger.warning(
            "generate_domain_corpus_summary_task: résumé introuvable (id=%s)", summary_id
        )
        return {"ok": False, "summary_id": str(summary_id), "error": "not_found"}

    def _fail(message: str, code: str) -> dict:
        mark_row_failed(row, code=code, message=message)
        return {"ok": False, "summary_id": str(summary_id), "error": code}

    try:
        run_corpus_summary_generation_for_row(row, user_id=user_id)
        return {"ok": True, "summary_id": str(summary_id)}
    except SummaryProviderError as e:
        try:
            backoff_s = min(60, 2 ** (self.request.retries + 1))
            logger.warning(
                "corpus summary provider error (id=%s), retry %d/3 in %ss: %s",
                summary_id, self.request.retries + 1, backoff_s, e,
            )
            raise self.retry(countdown=backoff_s, exc=e)
        except MaxRetriesExceededError:
            logger.error(
                "corpus summary provider error exhausted retries (id=%s): %s", summary_id, e
            )
            return _fail(str(e), e.code)
    except SummaryError as e:
        # Permanent : document non prêt, sans contenu, ou accès révoqué depuis
        # l'enqueue. Le message du fil bascule en échec, sans détail interne.
        return _fail(str(e), e.code)
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "generate_domain_corpus_summary_task failed (id=%s): %s", summary_id, e
        )
        return _fail(str(e) or "unexpected_error", "summary_failed")


@shared_task(bind=True, max_retries=0)
def reap_stale_domain_corpus_summaries_task(self, *, timeout_s: int | None = None) -> dict:
    """Reap périodique des résumés métier bloqués en PENDING (tâche perdue → FAILED).

    Même filet que le reaper privé, sur l'autre table. Idempotent et sans effet si
    rien n'est bloqué.
    """
    from apps.corpus.services.summary import reap_stale_corpus_summaries
    from apps.rag_core.services.periodic import LockNotAcquired, periodic_run

    try:
        with periodic_run("reap_stale_domain_corpus_summaries"):
            reaped = reap_stale_corpus_summaries(timeout_s=timeout_s)
        return {"ok": True, "reaped": reaped}
    except LockNotAcquired:
        return {"ok": True, "skipped": "lock_not_acquired"}
