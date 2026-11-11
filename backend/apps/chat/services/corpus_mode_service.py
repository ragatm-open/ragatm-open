"""État conversationnel de la clarification « rapide / complet ».

## Pourquoi ce module existe

Quand une demande globale est détectée, RagATM répond par une clarification. Au
tour suivant, l'utilisateur répond « réponse rapide » — et il faut **retrouver la
question d'origine** sans la lui faire retaper.

## Comment l'état est reconstruit, sans migration

``ChatMessage`` n'a **aucun champ de métadonnée** (pas de ``answer_kind``, pas de
``meta``) : cf. ``apps/chat/models.py``. Plutôt que d'inventer une persistance
fragile (parsing du Markdown, sentinelle cachée dans le texte, état serveur en
mémoire), l'état est **reconstruit** à partir de ce qui est DÉJÀ persisté :

    [ user      ] « résume tous les documents »   <- la question d'origine, persistée
    [ assistant ] « ## Cette demande porte… »     <- clarification : sources=[] , log=None
    [ user      ] « réponse rapide »              <- le tour courant

Une clarification en attente est reconnue si, et seulement si :

1. le dernier message du fil est un ``assistant`` ;
2. il ne porte **aucune source** et **aucun ``rag_query_log_id``** (donc il n'est
   pas issu d'un chemin RAG) ;
3. le message ``user`` qui le précède immédiatement est reconnu par
   ``GlobalCorpusMatcher`` comme une demande globale.

Le **matcher reste l'unique autorité** : aucune règle n'est dupliquée, aucun texte
Markdown n'est analysé. La reconnaissance échoue *fermée* — un doute renvoie
``None``, et « réponse rapide » redevient une question ordinaire.

## Limites assumées

- Un message d'aide (``/aide``) a aussi ``sources=[]`` et ``log=None``, mais la
  question qui le précède n'est pas reconnue comme globale : pas de collision.
- Une demande globale posée **avant** ce lot est passée par le RAG et porte donc
  un ``rag_query_log_id`` : elle ne sera pas prise pour une clarification.
- L'état survit au rechargement du fil, puisqu'il ne dépend que de la base.
- Sans ``thread_id`` (premier message d'un fil), il n'y a par construction aucune
  clarification en attente.
"""
from __future__ import annotations

from apps.actions.matchers import (
    FullAnalysisMatcher,
    GlobalCorpusMatcher,
    QuickConfirmMatcher,
)
from apps.actions.normalize import normalize

_GLOBAL_MATCHER = GlobalCorpusMatcher()
_QUICK_MATCHER = QuickConfirmMatcher()
_FULL_MATCHER = FullAnalysisMatcher()


def is_global_corpus_request(question: str) -> bool:
    text = question or ""
    return _GLOBAL_MATCHER.match(norm=normalize(text), original=text) is not None


def is_quick_confirm(question: str) -> bool:
    text = question or ""
    return _QUICK_MATCHER.match(norm=normalize(text), original=text) is not None


def is_full_analysis_request(question: str) -> bool:
    text = question or ""
    return _FULL_MATCHER.match(norm=normalize(text), original=text) is not None


def pending_global_question(*, user, thread_id) -> str | None:
    """Question globale en attente de choix de mode, ou ``None``.

    Ne lève jamais. Échoue fermée : au moindre doute, ``None``.
    """
    if not thread_id:
        return None

    from apps.chat.models import ChatMessage, ChatThread

    thread = ChatThread.objects.filter(id=thread_id, user=user).first()
    if thread is None:
        return None

    last_two = list(
        ChatMessage.objects.filter(thread=thread).order_by("-created_at", "-id")[:2]
    )
    if len(last_two) < 2:
        return None

    assistant, previous = last_two[0], last_two[1]

    # 1) Le dernier message doit être une réponse assistant…
    if assistant.role != ChatMessage.ROLE_ASSISTANT:
        return None
    # 2) …déterministe : ni sources, ni journal RAG.
    if assistant.sources:
        return None
    if assistant.rag_query_log_id is not None:
        return None
    # 3) …précédée d'une demande globale reconnue par le matcher (autorité unique).
    if previous.role != ChatMessage.ROLE_USER:
        return None
    if not is_global_corpus_request(previous.content or ""):
        return None

    question = (previous.content or "").strip()
    return question or None


# ---------------------------------------------------------------------------
# Orchestration du tour — implémentation UNIQUE, partagée par la vue sync, la
# vue streaming et le wiring. Aucune règle n'est recopiée ailleurs.
# ---------------------------------------------------------------------------

TURN_MODE_CHOICE = "mode_choice"
TURN_FULL_UNAVAILABLE = "full_unavailable"
TURN_FULL_LAUNCHED = "full_launched"
TURN_QUICK = "quick"
#: Un choix a été envoyé, mais plus aucune clarification n'est en attente : le
#: choix a déjà été consommé, ou le fil a avancé. On n'exécute RIEN.
TURN_CHOICE_EXPIRED = "choice_expired"

ANSWER_KIND_MODE_CHOICE = "corpus_mode_choice"
ANSWER_KIND_FULL_UNAVAILABLE = "corpus_full_unavailable"
ANSWER_KIND_FULL_LAUNCHED = "corpus_analysis_launched"
ANSWER_KIND_QUICK = "corpus_quick_answer"

# ---------------------------------------------------------------------------
# Choix structuré (Lot 3) — les boutons remplacent la saisie « réponse rapide ».
#
# Identifiants STABLES : le libellé affiché peut changer sans rien casser. Ils sont
# la source de vérité unique, côté serveur comme côté client.
# ---------------------------------------------------------------------------
CHOICE_QUICK = "quick_response"
CHOICE_FULL = "global_analysis"
CHOICES = (CHOICE_QUICK, CHOICE_FULL)

#: Texte persisté comme message utilisateur quand le choix vient d'un bouton. Écrit
#: par le SERVEUR : le navigateur n'écrit jamais l'historique du fil, et aucun
#: identifiant technique (`quick_response`) n'apparaît dans le chat.
CHOICE_LABELS = {
    CHOICE_QUICK: "Réponse rapide",
    CHOICE_FULL: "Analyse complète",
}


def pending_corpus_choice(*, user, thread_id) -> dict | None:
    """Y a-t-il un choix de mode en attente sur ce fil ? Autorité de LECTURE.

    C'est ce qui permet aux boutons de réapparaître après un rechargement **sans
    migration et sans analyser le Markdown** : l'état n'est pas stocké, il est
    recalculé — exactement comme le fait déjà le chemin d'écriture.

    Un choix déjà consommé fait avancer le fil, donc ``pending_global_question``
    renvoie ``None``, donc les boutons ne reviennent pas. L'idempotence de
    l'affichage découle de la même règle que celle de l'exécution.
    """
    question = pending_global_question(user=user, thread_id=thread_id)
    if not question:
        return None
    return {
        "question": question,
        # Sans sujet identifiable, le moteur n'a rien à chercher : le bouton
        # « Analyse complète » ne doit pas être proposé.
        "global_search_available": supports_global_search(question),
    }


def supports_global_search(question: str) -> bool:
    """La demande globale est-elle traitable par le moteur L4 ?

    L4 est une recherche THÉMATIQUE : il lui faut un sujet. « analyse tous les
    documents » dit *où* chercher, jamais *quoi* — le traiter reviendrait à
    inventer un sujet. Ces formulations restent donc explicitement indisponibles,
    et aucun job n'est créé pour elles.

    Le juge est la validation de requête du moteur lui-même : aucune règle n'est
    dupliquée ici.
    """
    from apps.rag_core.analysis.global_search import has_topic, normalize_query

    if not is_global_corpus_request(question):
        return False
    return has_topic(normalize_query(question))


def resolve_corpus_mode_turn(
    *, user, rag_space, thread_id, question: str, choice: str | None = None
) -> dict | None:
    """Décide si ce tour relève de la clarification « rapide / complet ».

    Renvoie ``None`` si le tour est une question ordinaire — le pipeline normal
    reprend alors la main, inchangé.

    ``choice`` (Lot 3) est l'identifiant d'action envoyé par un bouton. Il remplace
    la reconnaissance du texte, **mais pas l'autorité du serveur** : la question
    d'origine reste résolue depuis la base, jamais reçue du navigateur. Un choix
    sans clarification en attente n'exécute RIEN.

    Les deux premières branches sont **purement déterministes** : elles ne touchent
    ni retrieval, ni embeddings, ni LLM, ni Celery, ni ``RagQueryLog``.
    """
    text = (question or "").strip()

    # 0) Choix structuré (bouton). Le texte affiché n'a aucune valeur d'autorité.
    if choice:
        pending = pending_global_question(user=user, thread_id=thread_id)
        if not pending:
            # Déjà consommé, ou le fil a avancé. On ne relance rien, et surtout on
            # ne laisse pas le libellé du bouton partir dans le RAG.
            return {"kind": TURN_CHOICE_EXPIRED}
        if choice == CHOICE_QUICK:
            return {"kind": TURN_QUICK, "original_question": pending}
        if choice == CHOICE_FULL:
            if not supports_global_search(pending):
                from apps.rag_core.domain.corpus_mode import (
                    render_full_analysis_unavailable_markdown,
                )

                return {
                    "kind": TURN_FULL_UNAVAILABLE,
                    "answer": render_full_analysis_unavailable_markdown(),
                    "answer_kind": ANSWER_KIND_FULL_UNAVAILABLE,
                }
            return _launch_global_search(
                user=user, rag_space=rag_space, thread_id=thread_id, question=pending
            )
        return {"kind": TURN_CHOICE_EXPIRED}  # choix inconnu : on n'invente rien

    if not text:
        return None

    # 1) Réponse à une clarification en attente. Vérifié AVANT la détection
    #    globale : « analyse complète » n'est pas une demande globale (aucune
    #    portée corpus), et « réponse rapide » non plus.
    #
    #    Ce chemin TEXTUEL reste le filet de compatibilité du Lot 3 : les anciens
    #    fils, le clavier et les appels API directs continuent de fonctionner. Il
    #    pourra être retiré quand plus aucun message de clarification sans boutons
    #    ne circulera.
    pending = pending_global_question(user=user, thread_id=thread_id)
    if pending:
        if is_quick_confirm(text):
            # La question d'origine est reprise telle quelle : l'utilisateur n'a
            # pas à la retaper.
            return {"kind": TURN_QUICK, "original_question": pending}
        if is_full_analysis_request(text):
            # L4 ne sait chercher qu'un SUJET. Une demande globale sans sujet
            # (« analyse tous les documents ») reste explicitement indisponible :
            # aucun job n'est créé, on n'invente pas ce qu'il faudrait chercher.
            if not supports_global_search(pending):
                from apps.rag_core.domain.corpus_mode import (
                    render_full_analysis_unavailable_markdown,
                )

                return {
                    "kind": TURN_FULL_UNAVAILABLE,
                    "answer": render_full_analysis_unavailable_markdown(),
                    "answer_kind": ANSWER_KIND_FULL_UNAVAILABLE,
                }

            return _launch_global_search(
                user=user, rag_space=rag_space, thread_id=thread_id, question=pending
            )

    # 2) Nouvelle demande globale -> clarification. Aucun traitement lancé.
    if not is_global_corpus_request(text):
        return None

    from apps.rag_core.domain.corpus_mode import render_corpus_mode_choice_markdown
    from apps.rag_core.domain.coverage import coverage_to_dict
    from apps.rag_core.services.coverage_service import compute_rag_space_statistics

    stats = compute_rag_space_statistics(rag_space.id)  # SQL agrégé, aucun LLM
    coverage = stats["coverage"]
    global_search_available = supports_global_search(text)
    return {
        "kind": TURN_MODE_CHOICE,
        "answer": render_corpus_mode_choice_markdown(
            statistics=stats,
            coverage=coverage,
            global_search_available=global_search_available,
        ),
        "answer_kind": ANSWER_KIND_MODE_CHOICE,
        "coverage": coverage_to_dict(coverage),
        # Le frontend n'affiche « Analyse complète » que si le moteur sait la faire.
        "global_search_available": global_search_available,
    }


def _launch_global_search(*, user, rag_space, thread_id, question: str) -> dict:
    """Crée (ou réutilise) un job ``corpus.global_search`` et accuse réception.

    Aucun traitement n'est fait ici : le job part sur la queue `analytics`. Le fil
    reste utilisable.
    """
    from apps.rag_core.analysis.concurrency import ConcurrencyLimitReached
    from apps.rag_core.analysis.global_search import GlobalSearchError, normalize_query
    from apps.rag_core.analysis.registry import GLOBAL_SEARCH
    from apps.rag_core.analysis.service import (
        PreflightLimitExceeded,
        create_analysis_job,
    )
    from apps.rag_core.domain.corpus_mode import render_analysis_launched_markdown
    from apps.rag_core.models.corpus_analysis_job import (
        ERROR_CONCURRENCY_LIMIT,
        ERROR_PREFLIGHT_LIMIT_EXCEEDED,
        SAFE_ERROR_MESSAGES,
    )
    from apps.rag_core.services.coverage_service import compute_rag_space_statistics

    def _refuse(code: str) -> dict:
        return {
            "kind": TURN_FULL_UNAVAILABLE,
            "answer": f"## Analyse globale\n\n{SAFE_ERROR_MESSAGES[code]}",
            "answer_kind": ANSWER_KIND_FULL_UNAVAILABLE,
            "error_code": code,
        }

    try:
        outcome = create_analysis_job(
            user=user,
            rag_space=rag_space,
            analysis_type=GLOBAL_SEARCH,
            parameters={"query": normalize_query(question), "language": "auto"},
            # Idempotence : la MÊME question globale sur le MÊME corpus ne crée
            # jamais deux jobs (la contrainte partielle L2 s'en charge).
            request_id="",
        )
    except ConcurrencyLimitReached:
        return _refuse(ERROR_CONCURRENCY_LIMIT)
    except PreflightLimitExceeded:
        return _refuse(ERROR_PREFLIGHT_LIMIT_EXCEEDED)
    except GlobalSearchError as e:
        return _refuse(e.code)

    job = outcome.job
    if thread_id and job.chat_thread_id is None:
        # Lien DURABLE : le frontend retrouvera le job après rechargement du fil,
        # sans parser le Markdown ni prendre « le dernier job de l'utilisateur ».
        from apps.rag_core.models.corpus_analysis_job import CorpusAnalysisJob

        CorpusAnalysisJob.objects.filter(pk=job.pk).update(chat_thread_id=thread_id)
        job.chat_thread_id = thread_id

    stats = compute_rag_space_statistics(rag_space.id)
    return {
        "kind": TURN_FULL_LAUNCHED,
        "answer": render_analysis_launched_markdown(
            statistics=stats, coverage=stats["coverage"]
        ),
        "answer_kind": ANSWER_KIND_FULL_LAUNCHED,
        "analysis_id": str(job.id),
        "analysis_status": job.status,
    }


def quick_answer_coverage(*, rag_space, sources: list) -> dict:
    """Couverture RÉELLE d'une réponse rapide, calculée sur les sources retenues.

    Jamais la couverture de ``/stats/`` : celle-ci décrirait ce que l'espace
    CONTIENT, pas ce que le LLM a LU.
    """
    from apps.rag_core.domain.corpus_mode import build_quick_answer_coverage
    from apps.rag_core.domain.coverage import coverage_to_dict
    from apps.rag_core.services.coverage_service import compute_rag_space_statistics

    stats = compute_rag_space_statistics(rag_space.id)
    base = stats["coverage"]
    return coverage_to_dict(
        build_quick_answer_coverage(
            rag_space_id=rag_space.id,
            generated_at=base.scope.generated_at,
            corpus_fingerprint=base.scope.corpus_fingerprint,
            documents_total=base.documents_total,
            documents_considered=base.documents_considered,
            sources=sources or [],
        )
    )
