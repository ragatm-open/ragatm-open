"""Base commune de l'API d'administration PLATEFORME (superuser only).

Garde : `IsRagATMOwner` = utilisateur **actif superuser** (source de vérité unique,
cf. apps/accounts/permissions). Un rôle workspace owner/admin ne suffit JAMAIS.

DOCTRINE DE SÉCURITÉ : la console plateforme donne une vue de GOUVERNANCE (users,
workspaces, memberships, owners, owner-scopes, comptages, diagnostics) mais AUCUN
accès implicite au CONTENU (documents, chats, prompts, chunks, résultats d'analyse).
`is_superuser` ne crée aucun bypass de contenu : ces vues ne renvoient jamais de
contenu documentaire.
"""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated

from apps.accounts.permissions import IsRagATMOwner

# À appliquer à CHAQUE vue plateforme (lecture comme mutation).
PLATFORM_PERMS = [IsAuthenticated, IsRagATMOwner]

# Pagination : pas de framework DRF configuré dans le projet → enveloppe maison
# simple et bornée (les listes plateforme peuvent être volumineuses).
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def parse_page(request) -> tuple[int, int]:
    """Retourne (limit, offset) bornés à partir des query params."""
    try:
        limit = int(request.query_params.get("limit") or _DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    try:
        offset = int(request.query_params.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, _MAX_LIMIT)), max(0, offset)


def page_envelope(items: list, total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def user_display_name(u) -> str:
    full = (u.get_full_name() or "").strip() if hasattr(u, "get_full_name") else ""
    return full or getattr(u, "username", "") or getattr(u, "email", "") or f"#{u.pk}"


def user_public_dict(u) -> dict:
    """Identité minimale d'un utilisateur — JAMAIS de mot de passe/token/secret."""
    return {
        "id": u.id,
        "username": getattr(u, "username", "") or "",
        "email": getattr(u, "email", "") or "",
        "display_name": user_display_name(u),
        "is_active": bool(getattr(u, "is_active", False)),
        "is_staff": bool(getattr(u, "is_staff", False)),
        "is_superuser": bool(getattr(u, "is_superuser", False)),
    }
