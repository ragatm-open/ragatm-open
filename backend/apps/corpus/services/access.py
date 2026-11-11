"""Résolution d'accès à un document de **corpus métier**.

Pendant strict de ``api.v1.documents.views_common.load_accessible_document``, mais
pour l'autre famille de documents — et surtout : **jamais dans le même appel**.

Pourquoi deux résolveurs plutôt qu'un polymorphe
------------------------------------------------
``load_accessible_document`` est la porte d'accès de sept vues privées (aperçu,
téléchargement, suppression, réindexation, résumé…). Lui faire tenter un second
modèle en repli reviendrait à ce que TOUTES ces vues acceptent silencieusement un
identifiant de corpus — y compris celles qui suppriment ou réindexent. Un
identifiant appartient à un espace de noms ; le résoudre « par essais successifs »
est exactement ce que l'appelant doit décider en amont, à partir de
``source_type``.

Modèle d'autorisation
---------------------
Un corpus métier n'appartient à aucun workspace. Le seul mécanisme d'accès est
``WorkspaceDomainCorpusAccess`` (associé **et** activé, corpus actif), déjà
exprimé par ``allowed_corpus_ids_for_workspace`` — la même fonction que le
retrieval. Elle n'est pas réécrite ici : si le retrieval et le résumé divergeaient,
un utilisateur pourrait résumer un corpus qu'il n'a pas le droit d'interroger.

Anti-énumération
----------------
Inexistant, corpus inactif, corpus non associé, association désactivée : **une
seule** réponse publique, ``404 not_found``. La cause réelle part dans un
événement structuré (``summary_events``), jamais dans le corps de la réponse.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from apps.corpus.services import summary_events as events
from apps.corpus.services.retrieval import allowed_corpus_ids_for_workspace

#: Causes internes, jamais exposées publiquement. Le 404 est unique ; le journal
#: ne l'est pas — sans cette distinction, un incident d'habilitation est
#: indiscernable d'une faute de frappe dans un identifiant.
REASON_DOCUMENT_NOT_FOUND = "document_not_found"
REASON_CORPUS_NOT_AUTHORIZED = "corpus_not_authorized"
REASON_WORKSPACE_MISSING = "workspace_missing"


def _deny(reason: str, *, user, workspace, document_id, corpus_id=None, **extra) -> NotFound:
    """Construit le refus PUBLIC unique, après avoir journalisé la cause réelle."""
    event = (
        events.EVENT_NOT_FOUND
        if reason == REASON_DOCUMENT_NOT_FOUND
        else events.EVENT_ACCESS_DENIED
    )
    events.emit(
        event,
        reason=reason,
        source_type="domain_corpus",
        user_id=getattr(user, "id", None),
        workspace_id=getattr(workspace, "id", None),
        corpus_id=corpus_id,
        document_id=document_id,
        **extra,
    )
    return NotFound("not_found")


def workspace_can_read_corpus(workspace, corpus_id) -> bool:
    """Le workspace dispose-t-il d'un accès ACTIF à ce corpus ?

    Unique point de vérité de ce module — appelé aussi bien à la résolution qu'à la
    **revérification** avant toute restitution issue du cache ou d'une tâche
    asynchrone. Une habilitation retirée entre la demande et la livraison doit
    bloquer la livraison.
    """
    if workspace is None or corpus_id is None:
        return False
    return str(corpus_id) in allowed_corpus_ids_for_workspace(getattr(workspace, "id", None))


def load_accessible_domain_corpus_document(*, user, workspace, document_id):
    """Charge un ``DomainCorpusDocument`` après contrôle d'accès du workspace.

    Ne contrôle PAS le statut d'ingestion : un document accessible mais non prêt
    est une information que l'utilisateur a le droit de recevoir (``409
    document_not_ready``), et la confondre avec « inexistant » rendrait l'écran
    incompréhensible. Le contrôle de statut appartient au service de résumé.

    Lève ``rest_framework.exceptions.NotFound`` (404 ``{"detail": "not_found"}``)
    pour toute cause d'inaccessibilité, sans jamais les distinguer publiquement.
    """
    from apps.corpus.models import DomainCorpusDocument

    if workspace is None:
        raise _deny(
            REASON_WORKSPACE_MISSING, user=user, workspace=None, document_id=document_id
        )

    doc = (
        DomainCorpusDocument.objects.select_related("corpus", "corpus__domain")
        .filter(id=document_id)
        .first()
    )
    if doc is None:
        raise _deny(
            REASON_DOCUMENT_NOT_FOUND, user=user, workspace=workspace, document_id=document_id
        )

    # `allowed_corpus_ids_for_workspace` couvre en une requête : association
    # existante, `enabled=True`, et `corpus.is_active=True`. Un corpus désactivé
    # est donc refusé ici, sans branche séparée qui pourrait diverger du retrieval.
    if not workspace_can_read_corpus(workspace, doc.corpus_id):
        raise _deny(
            REASON_CORPUS_NOT_AUTHORIZED,
            user=user,
            workspace=workspace,
            document_id=document_id,
            corpus_id=doc.corpus_id,
        )

    return doc
