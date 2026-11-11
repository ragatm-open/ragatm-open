# backend/apps/actions/handlers.py
"""Dispatcher de handlers (audit §7) — signature commune, imports paresseux.

⚠️ Ce module définit le *seam* d'exécution mais n'est PAS branché sur le chemin de
chat live dans ce lot (lot 3). Les adaptateurs importent les services historiques
**paresseusement** (dans le corps de la fonction) pour éviter les imports
circulaires et garder ``apps.actions`` léger/testable sans DB.

Tous les handlers respectent :  ``handle(*, context, params) -> ActionResult``.
"""
from __future__ import annotations

from typing import Callable

from .plan import ActionContext, ActionResult

Handler = Callable[..., ActionResult]


def _not_wired(name: str) -> Handler:
    def _h(*, context: ActionContext, params: dict) -> ActionResult:  # pragma: no cover
        raise NotImplementedError(
            f"handler {name!r} déclaré mais non câblé (lot 3). "
            "Ce lot fournit la détection/validation, pas l'exécution centralisée."
        )
    return _h


# Le registre pointe vers ces clés. Tant que le câblage (lot 3) n'est pas fait,
# ils lèvent explicitement plutôt que d'exécuter un comportement partiel.
ACTION_HANDLERS: dict[str, Handler] = {
    "search_answer": _not_wired("search_answer"),
    "list_documents": _not_wired("list_documents"),
    "search_news": _not_wired("search_news"),
    "summarize_document": _not_wired("summarize_document"),
    # Mutations : volontairement absentes du dispatcher NL (sécurité).
}


def get_handler(handler_key: str) -> Handler:
    h = ACTION_HANDLERS.get(handler_key)
    if h is None:
        raise KeyError(f"handler non disponible pour clé {handler_key!r}")
    return h
