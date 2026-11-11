"""Erreurs typées du client AI Gateway.

Hiérarchie dédiée pour que les consommateurs distinguent explicitement :
passerelle désactivée, mal configurée, timeout, ou réponse en erreur — sans
confondre avec ``LLMError`` (providers directs OpenAI/Ollama).

CLASSIFICATION DE REPLI (Lot 1 — P0-C)
--------------------------------------
Chaque classe déclare ``fallback_eligible``. La version historique déclenchait le
repli externe sur **tout** ``status_code >= 400`` : une erreur de configuration
(modèle absent, contrat rompu, clé invalide) provoquait donc exactement le même
egress qu'une panne réseau. C'est corrigé — désormais seules les défaillances
réellement **transitoires** peuvent motiver un repli, et encore : sous réserve
que la politique d'egress du périmètre l'autorise (cf. ``apps.llm_router.egress``).

Éligibles au repli (indisponibilité transitoire) :
  * timeout, connexion refusée, réseau indisponible ;
  * HTTP 500, 502, 503, 504 ;
  * HTTP 429 (la passerelle déleste — c'est précisément le cas d'usage du repli).

NON éligibles (erreur de configuration ou de contrat — doit remonter telle quelle) :
  * HTTP 400, 401, 403, 404, 409, 422, 501 ;
  * JSON invalide, payload non conforme au DTO ;
  * passerelle désactivée ou mal configurée.

Ne jamais rendre une erreur éligible « pour que ça marche » : une erreur de
contrat qui bascule chez un tiers est une fuite déguisée en résilience.
"""
from __future__ import annotations


class GatewayError(RuntimeError):
    """Base de toutes les erreurs du client AI Gateway.

    ``fallback_eligible`` vaut False par défaut : fail-closed. Une nouvelle
    sous-classe est non-éligible tant qu'on ne l'a pas explicitement déclarée.
    """

    #: Cette défaillance peut-elle justifier un repli vers un provider externe ?
    fallback_eligible: bool = False

    #: Code HTTP à l'origine de l'erreur, quand il y en a un (audit/diagnostic).
    status_code: int | None = None

    #: Refus PROPRE à AI Gateway Pool (``X-AIGP-Error``), ``None`` sinon — cf.
    #: ``apps.llm_router.gateway.pool_errors``. Diagnostic seulement.
    pool_error = None


class GatewayDisabledError(GatewayError):
    """Le client a été invoqué alors que AI_GATEWAY_ENABLED est False.

    Garde-fou : aucune requête réseau n'est émise dans ce cas.
    """


class GatewayConfigError(GatewayError):
    """Configuration invalide (ex. AI_GATEWAY_BASE_URL manquante).

    Non éligible au repli : une passerelle mal configurée est un défaut de
    déploiement à corriger, pas une panne à contourner en envoyant les données
    ailleurs.
    """


#: Alias explicite demandé par la spécification du Lot 1.
GatewayConfigurationError = GatewayConfigError


class GatewayTimeoutError(GatewayError):
    """La passerelle n'a pas répondu dans le délai imparti. ÉLIGIBLE au repli."""

    fallback_eligible = True


class GatewayResponseError(GatewayError):
    """Réponse réseau en erreur (HTTP >= 400, connexion échouée, payload invalide).

    Conservée comme base commune pour la compatibilité ascendante : le code
    existant qui capture ``GatewayResponseError`` continue de fonctionner. Les
    sous-classes ci-dessous portent la classification fine.
    """


class GatewayTemporaryUnavailable(GatewayResponseError):
    """Indisponibilité transitoire : connexion refusée, 500, 502, 503, 504.

    ÉLIGIBLE au repli — c'est le seul cas où un provider externe est un secours
    légitime plutôt qu'un contournement.
    """

    fallback_eligible = True


class GatewayRateLimited(GatewayTemporaryUnavailable):
    """HTTP 429 — la passerelle déleste. ÉLIGIBLE au repli."""

    fallback_eligible = True
    status_code = 429


class GatewayAuthenticationError(GatewayResponseError):
    """HTTP 401 / 403 — identifiants ou droits invalides. NON éligible.

    Basculer chez un tiers parce que l'authentification interne échoue
    transformerait un incident de sécurité en fuite de données.
    """


class GatewayContractError(GatewayResponseError):
    """Contrat rompu : 400, 404, 409, 422, 501, JSON invalide, DTO non conforme.

    NON éligible : le prompt serait rejeté de la même façon ailleurs, ou pire,
    accepté par un tiers alors que la passerelle l'avait refusé.
    """


class GatewayStreamError(GatewayTemporaryUnavailable):
    """Flux NDJSON rompu : événement ``error`` de la passerelle, ou fin de flux
    sans ``done``.

    Éligible au repli comme toute indisponibilité transitoire — MAIS l'éligibilité
    ne suffit pas : l'appelant (``router.generate_text_stream``) doit refuser tout
    repli dès qu'un premier delta a été émis, sous peine de recoller la fin d'une
    génération d'un modèle B au début d'un modèle A.
    """


class GatewayUnavailableError(GatewayError):
    """L'AI Gateway est indisponible et aucun repli contrôlé n'est possible.

    Levée quand ``AI_GATEWAY_FALLBACK_PROVIDER=none`` (repli interdit) après un
    échec Gateway. Distincte des erreurs de transport ci-dessus : elle signale une
    décision de politique (pas de fallback), et JAMAIS un repli Ollama direct.
    """


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

#: Codes HTTP considérés comme des indisponibilités transitoires.
TEMPORARY_STATUS_CODES: frozenset[int] = frozenset({500, 502, 503, 504})


#: Catégories de refus du POOL qui décrivent une indisponibilité (le statut HTTP décide
#: alors comme pour la Gateway). Toute autre catégorie — configuration, sécurité, contrat,
#: modèle, erreur interne, code inconnu — est un défaut à corriger : JAMAIS éligible au
#: repli, même sous un statut 500/501/503 (fail-closed).
from .pool_errors import AVAILABILITY_CATEGORIES as _POOL_AVAILABILITY_CATEGORIES  # noqa: E402


def classify_http_status(status_code: int, *, detail: str = "", pool=None) -> GatewayResponseError:
    """Construit l'erreur typée correspondant à un code HTTP de la passerelle.

    ``detail`` ne doit contenir que des métadonnées — jamais le prompt, jamais le
    corps de réponse du tiers (cf. constat P0-C : le corps repris dans l'exception
    pouvait ré-exposer l'entrée par écho).

    ``pool`` (``PoolErrorInfo``) : refus émis par AI Gateway Pool. Son code (MAJUSCULES)
    et son identifiant de requête sont des métadonnées du pool, jamais un écho de l'entrée.
    """
    if pool is not None:
        meta = f"pool_code={pool.code} request_id={pool.request_id or '-'}"
        detail = f"{detail}; {meta}" if detail else meta
    suffix = f": {detail}" if detail else ""
    if pool is not None and pool.category not in _POOL_AVAILABILITY_CATEGORIES:
        exc: GatewayResponseError = GatewayContractError(
            f"AI Gateway Pool refusal ({status_code}){suffix}"
        )
        exc.status_code = status_code
        exc.pool_error = pool
        return exc
    if status_code == 429:
        exc: GatewayResponseError = GatewayRateLimited(
            f"AI Gateway rate limited (429){suffix}"
        )
    elif status_code in (401, 403):
        exc = GatewayAuthenticationError(
            f"AI Gateway authentication/authorization error ({status_code}){suffix}"
        )
    elif status_code in TEMPORARY_STATUS_CODES:
        exc = GatewayTemporaryUnavailable(
            f"AI Gateway temporarily unavailable ({status_code}){suffix}"
        )
    else:
        # 400, 404, 409, 422, 501 et tout autre code : contrat/configuration.
        exc = GatewayContractError(
            f"AI Gateway contract error ({status_code}){suffix}"
        )
    exc.status_code = status_code
    exc.pool_error = pool
    return exc


class VisionGatewayError(GatewayError):
    """Échec normalisé d'un appel ``POST /v1/vision/analyze``.

    Le flot de contrôle ne lit que ``code`` et ``retryable`` — jamais un message.
    Le message du Gateway n'est PAS conservé : il peut refléter l'entrée. ``str(exc)``
    ne contient donc que des métadonnées.

    Jamais éligible au repli : aucune image ne part chez un tiers.
    """

    fallback_eligible = False

    def __init__(self, code: str, *, retryable: bool, http_status: int | None = None,
                 request_id: str | None = None) -> None:
        self.code = code
        self.retryable = bool(retryable)
        self.http_status = http_status
        self.status_code = http_status
        self.request_id = request_id
        super().__init__(
            f"AI Gateway vision error code={code} http_status={http_status} "
            f"retryable={self.retryable}"
        )


class VisionContractDrift(VisionGatewayError):
    """Réponse de succès non conforme au contrat V1 FERMÉ. Jamais relançable.

    ``reason`` désigne l'emplacement contractuel fautif (chemin de champ), jamais une
    valeur de la réponse.
    """

    CODE = "visual_contract_drift"

    def __init__(self, reason: str, *, http_status: int | None = None,
                 request_id: str | None = None) -> None:
        super().__init__(self.CODE, retryable=False, http_status=http_status,
                         request_id=request_id)
        self.reason = reason


def is_fallback_eligible(exc: BaseException) -> bool:
    """True si ``exc`` décrit une panne transitoire pouvant motiver un repli.

    Fail-closed : toute exception non reconnue est NON éligible.
    """
    return bool(getattr(exc, "fallback_eligible", False))
