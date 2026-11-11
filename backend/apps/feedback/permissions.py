"""Rôles et périmètres d'accès au feedback et au diagnostic.

Le principe qui gouverne ce fichier : **voir le signal n'est pas voir le
contenu, et voir le contenu n'est pas autoriser sa sortie**. Trois droits
distincts, trois rôles distincts. Les confondre est ce qui transforme un outil
de diagnostic en canal d'exfiltration.

    utilisateur      -> crée et relit SON feedback
    admin workspace  -> lit les MÉTADONNÉES de son périmètre (jamais d'extraits)
    admin plateforme -> lit les traces, qualifie, autorise les niveaux 1 et 2
    superuser        -> autorise le niveau 3 (fichier source) et télécharge
"""
from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.workspaces.models import Membership

#: Niveaux d'export (cf. lot 3).
EXPORT_LEVEL_METADATA = 1
EXPORT_LEVEL_FRAGMENTS = 2
EXPORT_LEVEL_SOURCE_FILE = 3


def is_platform_admin(user) -> bool:
    """Administrateur plateforme, au sens EXACT de la console plateforme.

    Réutilise ``is_ragatm_owner`` plutôt que de redéfinir un critère : la
    plateforme a déjà tranché qui gouverne l'instance, et une seconde définition
    finirait par diverger de la première.
    """
    from apps.accounts.permissions import is_ragatm_owner

    return is_ragatm_owner(user)


def is_workspace_admin(user, workspace_id) -> bool:
    """Owner/admin du workspace indiqué."""
    if not workspace_id or user is None or not getattr(user, "is_authenticated", False):
        return False
    try:
        m = Membership.objects.filter(user=user, workspace_id=int(workspace_id)).first()
    except (TypeError, ValueError):
        return False
    return bool(m and (m.role or "").lower() in ("owner", "admin"))


def can_view_feedback_metadata(user, feedback) -> bool:
    """Métadonnées : le propriétaire du signal, l'admin de son workspace, la plateforme."""
    if is_platform_admin(user):
        return True
    if getattr(feedback, "created_by_id", None) and feedback.created_by_id == getattr(user, "pk", None):
        return True
    return is_workspace_admin(user, feedback.workspace_id_hist)


def can_view_diagnostic_content(user, feedback=None) -> bool:
    """Extraits et traces : plateforme et superuser UNIQUEMENT.

    Un admin workspace voit qu'un incident existe et ce qu'il vaut ; il ne voit
    pas les extraits documentaires du snapshot. Cette frontière est le cœur de
    la séparation « accès aux données / autorisation d'export ».
    """
    return is_platform_admin(user)


def can_approve_export(user, *, level: int) -> bool:
    """Autorisation d'export, par niveau.

    Le niveau 3 (fichier source complet) est réservé au superuser : c'est le
    seul niveau qui fait sortir un document de production tel quel.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if int(level) >= EXPORT_LEVEL_SOURCE_FILE:
        return bool(getattr(user, "is_superuser", False))
    return is_platform_admin(user)


def can_download_package(user) -> bool:
    """Téléchargement du paquet : superuser uniquement."""
    return bool(getattr(user, "is_authenticated", False) and getattr(user, "is_superuser", False))


class IsPlatformAdmin(BasePermission):
    message = "Réservé à l'administration plateforme."

    def has_permission(self, request, view):
        return is_platform_admin(getattr(request, "user", None))
