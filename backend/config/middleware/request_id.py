"""Identifiant de corrélation HTTP (``X-Request-ID``).

Qualification (point d'exploitabilité « propagation X-Request-ID ») :

- la grammaire acceptée est **celle du contrat** ``gateway-api/1.0`` appliquée par l'AI Gateway
  (``app/main.py``) et par le Pool : 1 à 128 caractères dans ``[A-Za-z0-9._:-]``. Un identifiant
  externe non conforme n'est **jamais** relayé : il est remplacé par un UUID4 neuf (le client
  reçoit l'identifiant effectif dans la réponse) ;
- l'identifiant de la requête en cours est exposé par une ``ContextVar`` pour que les clients
  Gateway (génération, embeddings, reranking) le transmettent en en-tête, sans avoir à le
  faire voyager à travers toutes les signatures. Il est posé en entrée de requête et remis à
  zéro en sortie ; les réponses en flux (SSE), dont le corps est produit APRÈS
  ``process_response``, reçoivent l'identifiant explicitement via ``LlmExecutionContext``.

Aucune donnée (prompt, document, en-tête d'authentification) n'est lue ni journalisée ici.
"""
from __future__ import annotations

import contextvars
import re
import uuid
from typing import Optional

from django.utils.deprecation import MiddlewareMixin

REQUEST_ID_HEADER = "X-Request-ID"
# Même expression que l'AI Gateway Open (`app/main.py::_REQUEST_ID_RE`).
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ragatm_request_id", default=""
)


def is_valid_request_id(value: object) -> bool:
    """Vrai si ``value`` respecte la grammaire du contrat (chaîne, 1..128, alphabet fermé)."""
    return isinstance(value, str) and REQUEST_ID_RE.match(value) is not None


def new_request_id() -> str:
    return str(uuid.uuid4())


def get_current_request_id() -> str:
    """Identifiant de la requête HTTP en cours de traitement, ou ``""`` hors requête."""
    return _current_request_id.get()


def bind_request_id(request_id: str) -> contextvars.Token:
    return _current_request_id.set(request_id)


def unbind_request_id(token: Optional[contextvars.Token]) -> None:
    if token is None:
        return
    try:
        _current_request_id.reset(token)
    except ValueError:
        # Contexte différent (ASGI) : on efface plutôt que de laisser fuir l'identifiant.
        _current_request_id.set("")


class RequestIdMiddleware(MiddlewareMixin):
    """
    Ensures every request has a correlation id.

    - Reads incoming header: X-Request-ID (or x-request-id)
    - If missing OR not conforming to the contract grammar: generates UUID4
    - Exposes it as request.request_id (and via ``get_current_request_id()``)
    - Returns it back in response header: X-Request-ID
    """

    header_name = REQUEST_ID_HEADER

    def process_request(self, request):
        rid: Optional[str] = request.headers.get("x-request-id")
        if not is_valid_request_id(rid):
            rid = new_request_id()
        request.request_id = rid
        request._request_id_token = bind_request_id(rid)

    def process_response(self, request, response):
        rid = getattr(request, "request_id", None)
        if rid:
            response[self.header_name] = rid
        unbind_request_id(getattr(request, "_request_id_token", None))
        return response
