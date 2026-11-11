"""Lecture des erreurs PROPRES à AI Gateway Pool.

Le pool relaie à l'identique les réponses de la Gateway (``{"detail": …}``, statut,
``Retry-After``). Il émet aussi SES PROPRES refus, reconnaissables à deux marques
(contrat relevé dans le code déployé du pool, révision de référence,
``src/ai_gateway_pool/transport/proxy.py:pool_error_response``) :

- en-tête ``X-AIGP-Error: <CODE>`` — jamais posé sur une réponse relayée de la Gateway ;
- corps ``{"error": {"code": "<CODE>", "message": …, "request_id": …[, "compensation": …]}}``.

Les codes sont en MAJUSCULES (``JOB_AFFINITY_UNKNOWN``…). Les parseurs historiques de
RAGatm n'acceptent que ``^[a-z][a-z0-9_]…$`` : un refus du pool y devenait ``http_<status>``,
et — pour l'ASR — un 404 de routage devenait la mort définitive du job.

Ce module NE DÉCIDE RIEN (ni relance, ni terminalité) : il rend un constat typé. Chaque
client garde sa politique. Deux règles :

1. **Pas de synonymie.** Un code du pool est normalisé en ``pool_<code en minuscules>`` :
   le préfixe interdit toute collision avec un code de la Gateway (``job_not_found``,
   ``resource_saturated``…). ``JOB_AFFINITY_UNKNOWN`` ne vaut PAS ``job_not_found``.
2. **L'en-tête fait foi.** Sans ``X-AIGP-Error``, la réponse est traitée comme une réponse
   de la Gateway, exactement comme avant ce module. Un corps ``{"error": …}`` seul ne
   suffit pas : la Gateway peut elle-même répondre sous cette forme.

``outcome`` qualifie ce que le refus PROUVE sur l'exécution amont :

- ``not_executed`` : le pool a refusé AVANT tout envoi à un nœud (routage, modèle,
  capacité, chemin, sécurité, affinité inconnue) ;
- ``uncertain`` : la requête a pu atteindre la Gateway (connexion perdue, délai dépassé,
  affinité non persistée après création d'un job). Une réponse perdue ne prouve pas
  qu'une opération n'a pas eu lieu : AUCUN rejeu d'un POST n'est autorisé par ce constat.

Jamais lu ni conservé : le ``message`` du pool (texte libre).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

POOL_ERROR_HEADER = "X-AIGP-Error"
REQUEST_ID_HEADER = "X-Request-ID"

_POOL_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_COMPENSATION_VALUES = frozenset({"deleted", "failed"})

NOT_EXECUTED = "not_executed"
UNCERTAIN = "uncertain"

#: Vocabulaire du pool à la révision déployée en staging :
#: code -> (statut HTTP attendu, catégorie, issue amont prouvée, transitoire).
#: « transitoire » = une nouvelle LECTURE idempotente plus tard a du sens ; ce n'est
#: jamais une autorisation de rejouer un POST.
POOL_CODES: dict[str, tuple[int, str, str, bool]] = {
    "NO_ELIGIBLE_NODE": (503, "routing", NOT_EXECUTED, True),
    "NODE_UNAVAILABLE": (503, "routing", NOT_EXECUTED, True),
    "CAPABILITY_NOT_AVAILABLE": (503, "routing", NOT_EXECUTED, True),
    "MODEL_NOT_AVAILABLE": (404, "model", NOT_EXECUTED, False),
    "MODEL_REQUIRED": (400, "model", NOT_EXECUTED, False),
    "ROUTING_NOT_READY": (501, "configuration", NOT_EXECUTED, False),
    "CONFIGURATION_INVALID": (500, "configuration", NOT_EXECUTED, False),
    "INVALID_NODE_DESTINATION": (400, "security", NOT_EXECUTED, False),
    "UPSTREAM_OVERRIDE_DENIED": (400, "security", NOT_EXECUTED, False),
    "ROUTE_NOT_IN_CONTRACT": (404, "contract", NOT_EXECUTED, False),
    "INVALID_REQUEST_PATH": (400, "contract", NOT_EXECUTED, False),
    "INVALID_REQUEST_FRAMING": (400, "contract", UNCERTAIN, False),
    "UPSTREAM_UNAVAILABLE": (503, "transport", UNCERTAIN, True),
    "UPSTREAM_TIMEOUT": (504, "transport", UNCERTAIN, True),
    "POOL_ROUTING_ERROR": (500, "internal", UNCERTAIN, False),
    "AFFINITY_STORE_UNAVAILABLE": (503, "affinity", UNCERTAIN, True),
    "JOB_AFFINITY_UNKNOWN": (404, "affinity", NOT_EXECUTED, False),
    "JOB_AFFINITY_CONFLICT": (409, "affinity", UNCERTAIN, False),
}

JOB_AFFINITY_UNKNOWN = "JOB_AFFINITY_UNKNOWN"

#: Catégories décrivant une INDISPONIBILITÉ de l'acheminement : le statut HTTP décide alors
#: comme pour une réponse de la Gateway. Les autres (configuration, sécurité, contrat,
#: modèle, interne, inconnu) sont des défauts à corriger, jamais relançables ni éligibles
#: à un repli.
AVAILABILITY_CATEGORIES = frozenset({"routing", "transport", "affinity"})


@dataclass(frozen=True)
class PoolErrorInfo:
    """Refus émis par le pool lui-même (jamais une réponse relayée de la Gateway)."""

    code: str                  # code ORIGINAL, ex. ``JOB_AFFINITY_UNKNOWN``
    normalized: str            # ``pool_job_affinity_unknown``
    http_status: int
    known: bool                # code présent dans le vocabulaire connu
    category: str
    outcome: str               # ``not_executed`` | ``uncertain``
    transient: bool
    request_id: Optional[str] = None
    retry_after_s: Optional[int] = None
    compensation: Optional[str] = None   # ``deleted`` | ``failed`` (ASR, après création)

    @property
    def is_job_affinity_unknown(self) -> bool:
        return self.code == JOB_AFFINITY_UNKNOWN

    def log_fields(self) -> str:
        """Fragment de journal SANS contenu : codes, statut, identifiant technique."""
        return (f"pool_code={self.code} http_status={self.http_status} category={self.category} "
                f"outcome={self.outcome} pool_request_id={self.request_id or '-'}")


def attach_pool_error(err: Any, pool: Optional[PoolErrorInfo]) -> Any:
    """Rattache le constat ``pool`` à une erreur typée d'un client (vision, StructX…).

    Ne change NI le ``code`` du client (consommé ailleurs), NI la relance d'une
    indisponibilité ; retire seulement la relance d'un refus qui n'en est pas une
    (configuration, contrat, modèle…). Rend ``err``.
    """
    if pool is None:
        return err
    err.pool_error = pool
    if getattr(err, "request_id", None) is None and pool.request_id is not None:
        err.request_id = pool.request_id
    if pool.category not in AVAILABILITY_CATEGORIES and hasattr(err, "retryable"):
        err.retryable = False
    return err


def normalize_pool_code(code: str) -> str:
    return "pool_" + code.lower()


def _header(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    try:
        val = headers.get(name)
        if val is None and isinstance(headers, Mapping):
            low = name.lower()
            for k, v in headers.items():
                if str(k).lower() == low:
                    val = v
                    break
    except Exception:  # noqa: BLE001 - en-têtes exotiques : on ne conclut rien
        return None
    return str(val).strip() if val is not None else None


def _retry_after(headers: Any) -> Optional[int]:
    raw = _header(headers, "Retry-After")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 <= value <= 3600 else None


def parse_pool_error(status: int, headers: Any, body: Any) -> Optional[PoolErrorInfo]:
    """Constat typé si la réponse est un refus du pool, sinon ``None``.

    ``body`` est le JSON déjà décodé (ou ``None``) ; il n'est consulté que pour
    ``request_id`` et ``compensation``, jamais pour le message.
    """
    code = _header(headers, POOL_ERROR_HEADER)
    if not code or not _POOL_CODE_RE.match(code):
        return None
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    rid = err.get("request_id")
    if not (isinstance(rid, str) and _REQUEST_ID_RE.match(rid)):
        rid = _header(headers, REQUEST_ID_HEADER)
        if not (rid and _REQUEST_ID_RE.match(rid)):
            rid = None
    comp = err.get("compensation")
    comp = comp if comp in _COMPENSATION_VALUES else None
    spec = POOL_CODES.get(code)
    if spec is None:
        # Code inconnu : on garde le statut, on ne présume ni non-exécution ni transitoire.
        category, outcome, transient, known = "unknown", UNCERTAIN, False, False
    else:
        _, category, outcome, transient = spec
        known = True
    return PoolErrorInfo(
        code=code, normalized=normalize_pool_code(code), http_status=int(status), known=known,
        category=category, outcome=outcome, transient=transient, request_id=rid,
        retry_after_s=_retry_after(headers), compensation=comp,
    )


def pool_error_from_response(resp: Any) -> Optional[PoolErrorInfo]:
    """Variante pour un objet réponse ``requests`` (ou compatible). Ne lève jamais."""
    headers = getattr(resp, "headers", None)
    if _header(headers, POOL_ERROR_HEADER) is None:
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - corps illisible : l'en-tête suffit
        body = None
    return parse_pool_error(int(getattr(resp, "status_code", 0) or 0), headers, body)


__all__ = [
    "AVAILABILITY_CATEGORIES",
    "attach_pool_error",
    "JOB_AFFINITY_UNKNOWN",
    "NOT_EXECUTED",
    "POOL_CODES",
    "POOL_ERROR_HEADER",
    "PoolErrorInfo",
    "REQUEST_ID_HEADER",
    "UNCERTAIN",
    "normalize_pool_code",
    "parse_pool_error",
    "pool_error_from_response",
]
