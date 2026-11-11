# backend/apps/chat/services/summary_messages.py
"""Persistance des résumés de document dans le fil de discussion.

Point d'entrée UNIQUE, partagé par les trois chemins qui produisent un résumé :
la vue REST synchrone, la tâche Celery, et le routeur d'actions. Toute la logique
d'idempotence, de statut et de validation d'accès vit ici — pas dans les vues.

Invariant structurant
---------------------
``DocumentSummary`` est un **cache partagé**, unique sur ``(document, variant_key)``
et non par requête : deux utilisateurs, dans deux fils différents, qui demandent le
même résumé pointent la MÊME ligne. Le lien est donc porté par le message
(``ChatMessage.summary_id``), jamais l'inverse, et la finalisation procède par
**fan-out** : un résumé qui devient READY termine *tous* les messages en attente
qui le référencent. Stocker un ``result_message_id`` sur ``DocumentSummary``
casserait le cas multi-utilisateur en n'en servant qu'un seul.
"""
from __future__ import annotations

import logging
import uuid

from django.db import IntegrityError, transaction

from apps.chat.models import ChatMessage, ChatThread

logger = logging.getLogger(__name__)

# Contenu affiché tant que la génération n'a pas abouti. Volontairement du texte
# lisible et non une chaîne vide : un client qui ignore ``status`` (ancien
# frontend, export, copie du fil) doit voir quelque chose de sensé.
PENDING_PLACEHOLDER = "⏳ Résumé en cours de génération…"
FAILED_PLACEHOLDER = "La génération du résumé a échoué. Vous pouvez relancer la demande."

# Variantes anglaises. Une réponse anglaise assortie d'un placeholder français
# produit un fil mixte FR/EN — le défaut le plus visible une fois la politique de
# langue activée. Le français reste la valeur par défaut : `answer_language` vaut
# `None` partout où la politique est OFF, et les octets historiques sont conservés.
PENDING_PLACEHOLDER_EN = "⏳ Summary being generated…"
FAILED_PLACEHOLDER_EN = "Summary generation failed. You can start the request again."


def pending_placeholder(answer_language: str | None = None) -> str:
    return PENDING_PLACEHOLDER_EN if answer_language == "en" else PENDING_PLACEHOLDER


def failed_placeholder(answer_language: str | None = None) -> str:
    return FAILED_PLACEHOLDER_EN if answer_language == "en" else FAILED_PLACEHOLDER


def _log(event_status: str, msg: ChatMessage, *, is_async: bool | None = None, **extra) -> None:
    """Journal structuré. Ne journalise JAMAIS le contenu du résumé ni le prompt."""
    payload = {
        "event": "document_summary_chat_message",
        "outcome": event_status,
        "thread_id": str(msg.thread_id),
        "message_id": str(msg.id),
        "summary_id": str(msg.summary_id) if msg.summary_id else None,
        "document_id": str(msg.document_id) if msg.document_id else None,
        "request_id": msg.request_id or None,
        "status": msg.status,
    }
    if is_async is not None:
        payload["async"] = is_async
    payload.update(extra)
    logger.info("document_summary_chat_message %s", payload)


#: Provenances connues d'un message de résumé. ``workspace_document`` est le
#: DÉFAUT de lecture pour les messages antérieurs à ``ChatMessage.meta`` : ils ne
#: pouvaient référencer qu'un document privé, la valeur est donc exacte, pas
#: devinée.
SOURCE_TYPE_WORKSPACE = "workspace_document"
SOURCE_TYPE_DOMAIN_CORPUS = "domain_corpus"

MESSAGE_TYPE_DOCUMENT_SUMMARY = "document_summary"


def build_summary_meta(*, source: dict, scope: str, mode: str, language: str) -> dict:
    """Contrat de métadonnées persisté sur un message de résumé.

    Forme stable, documentée et lue par le frontend :
    ``{"message_type", "source": {...}, "summary": {...}}``. Ne contient jamais de
    texte de document — le contenu du résumé vit dans ``ChatMessage.content``.
    """
    return {
        "message_type": MESSAGE_TYPE_DOCUMENT_SUMMARY,
        "source": {k: v for k, v in (source or {}).items() if v is not None},
        "summary": {"scope": scope, "mode": mode, "language": language},
    }


def source_ref_of(msg: ChatMessage) -> dict:
    """Référence de source DISCRIMINÉE d'un message, avec repli historique.

    Un message écrit avant ``ChatMessage.meta`` n'a pas de provenance explicite.
    Le repli ``workspace_document`` n'est pas une supposition : à cette date, le
    résumé n'existait que pour les documents privés. Retirer l'action sur ces
    messages punirait l'historique sans rien sécuriser.
    """
    meta = msg.meta if isinstance(msg.meta, dict) else {}
    source = meta.get("source") if isinstance(meta.get("source"), dict) else {}
    return {
        "source_type": source.get("source_type") or SOURCE_TYPE_WORKSPACE,
        "document_id": source.get("document_id")
        or (str(msg.document_id) if msg.document_id else None),
        **{k: v for k, v in source.items() if k not in ("source_type", "document_id")},
    }


class SummaryRequestConflict(Exception):
    """Le ``request_id`` est déjà pris par une AUTRE cible dans ce fil.

    Les UUID de documents privés et de documents de corpus vivent dans deux
    espaces de noms disjoints : rien n'interdit à un client de réutiliser un
    ``request_id`` pour deux cibles différentes. Réutiliser l'échange existant
    répondrait alors le résumé d'un autre document sous la demande courante — on
    refuse explicitement plutôt que de servir la mauvaise réponse.
    """


def resolve_thread_for_summary(
    *, user, thread_id, document=None, expected_rag_space_id=None
) -> ChatThread | None:
    """Valide que le fil existe, appartient à l'utilisateur et vit dans le même espace.

    Ne JAMAIS faire confiance au ``thread_id`` fourni par le client : sans ce
    contrôle, un identifiant deviné permettrait d'écrire dans le fil d'autrui.
    Renvoie ``None`` pour « inaccessible », que le fil soit inexistant, étranger
    ou d'un autre RagSpace — l'appelant répond 404 dans les trois cas, ce qui rend
    l'énumération impossible (même convention que ``load_accessible_document``).

    ``expected_rag_space_id`` — espace attendu quand la cible n'en porte pas
    elle-même. Un document de CORPUS MÉTIER n'appartient à aucun RagSpace : c'est
    l'espace CONSOMMATEUR qui doit être confronté au fil. Sans ce paramètre, un
    utilisateur membre de deux workspaces pourrait présenter le fil du workspace A
    tout en résumant sous le contexte du workspace B.
    """
    if not thread_id:
        return None
    try:
        uuid.UUID(str(thread_id))
    except (TypeError, ValueError):
        return None

    thread = (
        ChatThread.objects.filter(id=thread_id, user=user)
        .select_related("rag_space")
        .first()
    )
    if thread is None:
        return None

    expected = expected_rag_space_id
    if expected is None and document is not None:
        expected = getattr(document, "rag_space_id", None)
    if expected is None:
        # Aucune référence d'espace à confronter : on refuse plutôt que d'accepter
        # un fil au hasard. Un appelant qui ne sait pas dans quel espace il opère
        # ne doit rien écrire dans un fil.
        return None

    # Isolation d'espace : un fil ne peut pas accueillir le résumé d'un document
    # rattaché (ou consommé) dans un autre RagSpace, même si les deux sont
    # lisibles par l'utilisateur.
    if str(thread.rag_space_id) != str(expected):
        return None
    return thread


def _same_target(msg: ChatMessage, source: dict | None) -> bool:
    """Le message existant vise-t-il la MÊME cible que la demande courante ?

    Comparaison sur le couple ``(source_type, document_id)`` — le seul qui
    identifie une cible sans ambiguïté. Comparer le seul ``document_id`` laisserait
    passer la collision entre les deux espaces de noms d'UUID.
    """
    if not source:
        return True  # appelant historique : rien à confronter
    ref = source_ref_of(msg)
    return (
        str(ref.get("source_type") or "") == str(source.get("source_type") or SOURCE_TYPE_WORKSPACE)
        and str(ref.get("document_id") or "") == str(source.get("document_id") or "")
    )


@transaction.atomic
def get_or_create_summary_exchange(
    *, thread: ChatThread, request_id: str, document, user_label: str,
    answer_language: str | None = None, source: dict | None = None,
    meta: dict | None = None,
) -> tuple[ChatMessage, bool]:
    """Crée (ou retrouve) le couple message utilisateur + message assistant `pending`.

    Le message assistant est écrit **à la date de la demande**, avant toute
    génération : c'est ce qui le place au bon rang chronologique dans le fil,
    y compris quand la génération asynchrone se termine bien plus tard.

    Idempotent sur ``(thread, request_id, role)``, garanti par la contrainte
    partielle ``uniq_chat_message_summary_request``. En cas de course, l'un des
    deux appelants prend l'IntegrityError, relit la ligne du gagnant et la
    renvoie : aucun doublon, et surtout aucune 500 pour une course normale.

    ``source`` — référence DISCRIMINÉE de la cible (``source_type`` +
    ``document_id`` + métadonnées de provenance). Participe à la clé logique
    d'idempotence : un ``request_id`` réutilisé pour une AUTRE cible lève
    ``SummaryRequestConflict`` au lieu de renvoyer le résumé du mauvais document.
    Absent = appelant historique, cible privée.

    Retourne ``(message_assistant, created)``.
    """
    existing = ChatMessage.objects.filter(
        thread=thread,
        request_id=request_id,
        role=ChatMessage.ROLE_ASSISTANT,
        kind=ChatMessage.KIND_DOCUMENT_SUMMARY,
    ).first()
    if existing is not None:
        if not _same_target(existing, source):
            _log("conflict", existing)
            raise SummaryRequestConflict(request_id)
        _log("reused", existing)
        return existing, False

    try:
        with transaction.atomic():
            # Le message utilisateur est persisté parce que l'interface affiche
            # DÉJÀ une entrée utilisateur explicite pour cette action. On ne
            # fabrique pas une entrée pour une simple symétrie technique.
            ChatMessage.objects.create(
                thread=thread,
                role=ChatMessage.ROLE_USER,
                content=user_label,
                sources=[],
                request_id=request_id,
                kind=ChatMessage.KIND_DOCUMENT_SUMMARY,
                status=ChatMessage.STATUS_COMPLETED,
                document_id=document.id,
                meta=meta,
            )
            msg = ChatMessage.objects.create(
                thread=thread,
                role=ChatMessage.ROLE_ASSISTANT,
                content=pending_placeholder(answer_language),
                sources=[],
                request_id=request_id,
                kind=ChatMessage.KIND_DOCUMENT_SUMMARY,
                status=ChatMessage.STATUS_PENDING,
                document_id=document.id,
                meta=meta,
            )
    except IntegrityError:
        # Course perdue : le concurrent a déjà écrit le couple. On relit le sien.
        msg = ChatMessage.objects.filter(
            thread=thread,
            request_id=request_id,
            role=ChatMessage.ROLE_ASSISTANT,
            kind=ChatMessage.KIND_DOCUMENT_SUMMARY,
        ).first()
        if msg is None:  # pragma: no cover - contrainte violée pour une autre raison
            raise
        if not _same_target(msg, source):
            _log("conflict", msg, race=True)
            raise SummaryRequestConflict(request_id)
        _log("reused", msg, race=True)
        return msg, False

    _log("created", msg)
    return msg, True


def reopen_message(msg: ChatMessage, *, answer_language: str | None = None) -> ChatMessage:
    """Repasse un message en ``pending`` pour une nouvelle tentative.

    Une relance après échec réutilise le MÊME message plutôt que d'en créer un
    second : c'est ce qui permet au client de conserver son ``request_id`` d'un
    essai à l'autre, et donc d'éviter la paire de résumés jumeaux qu'un retry
    après timeout produirait sinon.
    """
    if msg.status != ChatMessage.STATUS_FAILED:
        return msg
    msg.content = pending_placeholder(answer_language or _language_of_summary(msg.summary_id))
    msg.status = ChatMessage.STATUS_PENDING
    msg.summary_id = None
    msg.save(update_fields=["content", "status", "summary_id", "updated_at"])
    _log("reopened", msg)
    return msg


def attach_summary(msg: ChatMessage, summary_id) -> None:
    """Rattache le message au DocumentSummary qui le finalisera (fan-out)."""
    if not summary_id or str(msg.summary_id or "") == str(summary_id):
        return
    msg.summary_id = summary_id
    msg.save(update_fields=["summary_id", "updated_at"])


def complete_message(msg: ChatMessage, *, content: str, summary_id=None) -> ChatMessage:
    """Passe le message en ``completed``. Ne crée jamais un second message."""
    text = (content or "").strip()
    if not text:
        # Un `completed` sans contenu serait un mensonge affiché à l'utilisateur.
        return fail_message(msg, error="empty_summary")
    msg.content = text
    msg.status = ChatMessage.STATUS_COMPLETED
    if summary_id:
        msg.summary_id = summary_id
    msg.save(update_fields=["content", "status", "summary_id", "updated_at"])
    _log("completed", msg)
    return msg


def _language_of_summary(summary_id) -> str | None:
    """Langue concrète du résumé rattaché, ou ``None`` (fail-safe : jamais bloquant).

    Les deux tables de résumé sont interrogées : un identifiant de résumé métier
    resterait sinon sans langue, et un échec sur un résumé anglais s'afficherait en
    français. Les deux espaces d'UUID étant disjoints, l'ordre n'a pas d'incidence.
    """
    if not summary_id:
        return None
    try:
        from apps.rag_core.models.summary import DocumentSummary

        lang = (
            DocumentSummary.objects.filter(id=summary_id)
            .values_list("language", flat=True)
            .first()
        )
        if lang:
            return lang

        from apps.corpus.models import DomainCorpusDocumentSummary

        return (
            DomainCorpusDocumentSummary.objects.filter(id=summary_id)
            .values_list("language", flat=True)
            .first()
        )
    except Exception:  # pragma: no cover - le message d'échec prime sur sa langue
        return None


def fail_message(msg: ChatMessage, *, error: str = "", answer_language: str | None = None) -> ChatMessage:
    """Passe le message en ``failed`` avec un contenu sûr (aucun détail interne).

    ``answer_language`` non fourni : on le déduit du résumé rattaché, de sorte qu'un
    échec sur un résumé anglais s'affiche en anglais.
    """
    lang = answer_language or _language_of_summary(msg.summary_id)
    msg.content = failed_placeholder(lang)
    msg.status = ChatMessage.STATUS_FAILED
    msg.save(update_fields=["content", "status", "updated_at"])
    _log("failed", msg, error_code=(error or "")[:64])
    return msg


# --- Fan-out : un résumé partagé termine TOUS les messages qui l'attendent ----
def complete_messages_for_summary(*, summary_id, content: str) -> int:
    """Termine tous les messages ``pending`` rattachés à ce résumé.

    Appelé à la finalisation (synchrone comme Celery). Le filtre sur
    ``status=pending`` rend l'opération naturellement idempotente : un retry
    Celery, une reprise de job ou une double livraison ne retouchent rien.
    """
    text = (content or "").strip()
    if not text:
        return fail_messages_for_summary(summary_id=summary_id, error="empty_summary")

    pending = list(
        ChatMessage.objects.filter(
            summary_id=summary_id, status=ChatMessage.STATUS_PENDING
        )
    )
    for msg in pending:
        complete_message(msg, content=text)
    return len(pending)


def fail_messages_for_summary(*, summary_id, error: str = "") -> int:
    """Marque ``failed`` tous les messages ``pending`` rattachés à ce résumé."""
    pending = list(
        ChatMessage.objects.filter(
            summary_id=summary_id, status=ChatMessage.STATUS_PENDING
        )
    )
    # Langue résolue UNE fois : tous ces messages portent le même résumé.
    lang = _language_of_summary(summary_id)
    for msg in pending:
        fail_message(msg, error=error, answer_language=lang)
    return len(pending)


def _load_private_summary_row(summary_id):
    from apps.rag_core.models.summary import DocumentSummary

    return DocumentSummary.objects.filter(id=summary_id).first()


def settle_for_result(msg: ChatMessage, res, *, load_row=None) -> ChatMessage:
    """Aligne le message du fil sur le résultat d'un service de résumé.

    Chemin UNIQUE partagé par les vues REST (privée et corpus métier) et le
    routeur d'actions : tous doivent produire exactement le même état persisté
    pour la même action métier.

    ``load_row`` — chargeur de la ligne de cache, injecté par l'appelant. Par
    défaut ``DocumentSummary`` (privé) ; le résumé de corpus métier passe le sien.
    ``SummaryResult`` étant commun aux deux, c'est le SEUL point de ce module où
    la table concernée doit être connue.
    """
    if res.status == "ready":
        return complete_message(msg, content=res.summary, summary_id=res.summary_id)
    if res.status == "failed":
        return fail_message(
            msg, error=res.error or "summary_failed", answer_language=getattr(res, "language", None)
        )

    # ``pending`` : la génération se poursuit dans Celery. On rattache le message
    # au résumé pour que le fan-out de finalisation le termine, PUIS on relit la
    # ligne : la tâche a pu aboutir dans l'intervalle, avant que le rattachement
    # ne soit visible. Sans cette relecture, un résumé rapide laisserait le
    # message bloqué en « en cours ».
    attach_summary(msg, res.summary_id)
    row = (load_row or _load_private_summary_row)(res.summary_id)
    if row is not None:
        msg = settle_from_summary_row(msg, row)
    return msg


def settle_from_summary_row(msg: ChatMessage, row) -> ChatMessage:
    """Aligne un message ``pending`` sur l'état COURANT de sa ligne DocumentSummary.

    Ferme la fenêtre de course du chemin asynchrone : entre le retour de
    ``summarize_document`` (qui a répondu ``pending``) et le rattachement du
    ``summary_id``, la tâche Celery a pu terminer sans voir notre message. On
    relit donc la ligne après rattachement — soit le fan-out nous a vus, soit
    cette relecture nous rattrape. Dans les deux cas le message se termine.
    """
    if msg.status != ChatMessage.STATUS_PENDING:
        return msg
    if row.status == "READY":
        return complete_message(msg, content=row.summary or "", summary_id=row.id)
    if row.status == "FAILED":
        return fail_message(
            msg, error=row.error_message or "summary_failed", answer_language=row.language
        )
    return msg
