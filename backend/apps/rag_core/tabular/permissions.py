"""Périmètre d'accès du moteur tabulaire.

Aucune règle d'autorisation n'est réinventée ici : le moteur s'appuie sur
`require_rag_space_access`, la même porte que le chat et les documents. Un second
système d'autorisation serait un second endroit où se tromper, et les deux
divergeraient au premier changement.

La propriété importante est le **filtrage à la source** : les documents hors périmètre
ne sont pas récupérés puis écartés, ils ne figurent jamais dans le jeu de résultats.
Un document inaccessible et un document inexistant produisent donc la même réponse —
c'est ce qui empêche le moteur de servir d'oracle d'existence.
"""
from __future__ import annotations

from uuid import UUID

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

from apps.rag_core.tabular import errors as E

#: `require_rag_space_access` lève la `PermissionDenied` de DJANGO, pas celle de DRF.
#: N'en attraper qu'une laisserait l'autre remonter en 500 — une erreur serveur là où
#: la réponse correcte est un refus d'accès.
_PERMISSION_ERRORS = (DjangoPermissionDenied, DRFPermissionDenied)


def require_access(*, user, rag_space_id: UUID):
    """Vérifie l'accès en lecture au RagSpace. Lève une erreur tabulaire typée."""
    from apps.workspaces.services.access import require_rag_space_access

    try:
        return require_rag_space_access(user=user, rag_space_id=rag_space_id,
                                        permission="read")
    except _PERMISSION_ERRORS as exc:
        # Message volontairement générique : ne jamais confirmer l'existence d'un
        # RagSpace auquel l'appelant n'a pas droit.
        raise E.TabularPermissionDenied(
            E.ERR_PERMISSION_DENIED, "aucun accès à cet espace") from exc


def accessible_documents(*, user, rag_space_id: UUID):
    """QuerySet des documents interrogeables, déjà restreint au périmètre.

    L'accès au RagSpace est vérifié AVANT toute requête sur les documents : un appelant
    sans droit n'atteint jamais la table `Document`.
    """
    from apps.rag_core.domain.eligibility import eligible_documents_queryset
    from apps.rag_core.models import Document

    rag_space = require_access(user=user, rag_space_id=rag_space_id)

    # Lot 2 (P0-A) : l'ancien `.exclude(document_status="deleted")` était du CODE
    # MORT — la valeur "deleted" n'est écrite nulle part dans le dépôt, et le champ
    # est déclaré inerte par le modèle. Il donnait l'apparence d'une protection
    # sans en être une, et aucun filtre de `status` ne l'accompagnait : un classeur
    # en échec d'ingestion pouvait être sélectionné et calculé.
    return eligible_documents_queryset(
        Document.objects.filter(rag_space=rag_space)
    ).order_by("filename", "created_at")
