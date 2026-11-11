from __future__ import annotations

"""Permissions transverses (rôle propriétaire global ragATM).

Le « propriétaire global ragATM » est une notion STRICTEMENT distincte des rôles
de workspace (owner / admin / member / viewer, cf. apps.workspaces.models.Membership) :
- les rôles de workspace sont scopés à UN workspace et n'accordent aucun droit sur
  les corpus documentaires métier partagés ;
- le propriétaire global administre la couche transverse (domaines métier, corpus
  métier, documents de corpus, associations workspace/corpus, réindexations).

Choix d'implémentation : on mappe ce rôle sur ``User.is_superuser`` (le projet
utilise déjà l'admin Django comme propriétaire de l'instance). Aucune migration
n'est requise et les installations existantes restent compatibles. Si un flag
métier dédié devait être introduit plus tard (``is_ragatm_owner``), il suffirait
de l'ajouter dans ``is_ragatm_owner()`` ci-dessous : c'est l'unique point de
vérité utilisé par toutes les permissions et services.
"""

from rest_framework.permissions import BasePermission


def is_ragatm_owner(user) -> bool:
    """Vrai si ``user`` est le propriétaire global de l'instance ragATM.

    Volontairement conservateur : un utilisateur non authentifié, inactif, ou
    simple admin de workspace n'est JAMAIS propriétaire global.
    """
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "is_superuser", False)
    )


class IsRagATMOwner(BasePermission):
    """Réserve l'accès au propriétaire global ragATM.

    À appliquer sur TOUS les endpoints d'écriture ET de listing admin des corpus
    métier. Un admin/owner de workspace n'a aucun droit ici.
    """

    message = "Réservé au propriétaire global ragATM."

    def has_permission(self, request, view) -> bool:
        return is_ragatm_owner(getattr(request, "user", None))

    def has_object_permission(self, request, view, obj) -> bool:
        return is_ragatm_owner(getattr(request, "user", None))
