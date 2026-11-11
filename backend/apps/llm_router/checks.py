"""System checks — politique IA RagATM (AI Gateway obligatoire).

Refuse au démarrage (``manage.py check`` / runserver) les configurations qui
violeraient la policy Configuration IT :

- l'IA locale RagATM DOIT passer par l'AI Gateway (``AI_GATEWAY_ENABLED=1``,
  ``AI_GATEWAY_BASE_URL`` routable, jamais loopback) dans les environnements
  officiels (dev/staging/prod) ;
- RagATM ne DOIT JAMAIS appeler Ollama en direct (``RAG_LLM_PROVIDER=ollama`` refusé) ;
- le repli Gateway est CONTRÔLÉ : ``AI_GATEWAY_FALLBACK_PROVIDER`` = ``none`` (aucun repli).

Échappatoire tests : ``ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1`` dégrade les checks A/C
en Warning (tests unitaires / mode dégradé), JAMAIS en déploiement réel.

Aucune valeur sensible (clé, token, prompt) n'est jamais affichée par ces checks.
"""
from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.core.checks import Error
from django.core.checks import Warning as CheckWarning

OFFICIAL_ENVS = ("dev", "staging", "prod")
# Hôtes non routables : vus depuis un conteneur, le loopback n'est jamais la Gateway.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1"})


def _env() -> str:
    """Environnement de déploiement, via le registre.

    `DJANGO_ENV` y est déclarée avec `source=config.settings.base` : le repli
    historique sur `os.getenv` reproduisait ce que la projection fait déjà, en
    contournant type et énumération.
    """
    return str(_s("DJANGO_ENV") or "").strip().lower()


def _tests_tolerated() -> bool:
    return bool(getattr(settings, "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS", False))


def _s(name: str):
    """Réglage DÉCLARÉ du routeur (settings puis environnement, défaut du registre)."""
    from .config_access import router_setting

    return router_setting(name)


def _required_url() -> str:
    """URL de la Gateway rappelée dans les messages (indication) ; générique si non posée."""
    return str(_s("AI_GATEWAY_REQUIRED_BASE_URL") or "").strip() or "http://<ai-gateway-host>:8100"


def check_test_escape_hatch_scope(app_configs, **kwargs):
    """``llm_router.E006`` — l'échappatoire de TESTS ne peut pas servir en déploiement.

    ``ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1`` dégrade en avertissements les checks qui
    interdisent d'appeler Ollama en direct et exigent la Gateway. C'est légitime pour
    une suite unitaire, qui n'a aucune Gateway à joindre.

    Ce qui manquait : rien n'empêchait de la poser en staging ou en production. Elle y
    aurait éteint les gardes de gouvernance **en silence** — les checks seraient passés
    au vert en n'émettant qu'un Warning, que personne ne lit dans un journal de
    déploiement.

    La CI a besoin de ce drapeau pour exécuter la suite backend (sans lui, le job meurt
    aux system checks et aucun test ne tourne). Il devient donc strictement borné.
    """
    if not _tests_tolerated():
        return []
    env = _env()
    if env in ("staging", "prod", "production"):
        return [Error(
            f"ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1 avec DJANGO_ENV={env} : cette "
            "échappatoire est réservée aux tests. Elle dégrade en avertissements les "
            "gardes qui interdisent l'appel direct à Ollama et exigent l'AI Gateway.",
            hint="Retirer ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS de cet environnement.",
            id="llm_router.E006",
        )]
    return []


def check_ai_gateway_mandatory(app_configs, **kwargs):
    """Check A — Gateway obligatoire dans les environnements officiels."""
    env = _env()
    if env not in OFFICIAL_ENVS:
        return []
    if bool(_s("AI_GATEWAY_ENABLED")):
        return []
    if _tests_tolerated():
        return [
            CheckWarning(
                f"AI_GATEWAY_ENABLED=0 in an official environment (DJANGO_ENV={env}); "
                "tolerated only because ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1.",
                hint="Never set ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS in a real deployment.",
                id="llm_router.W001",
            )
        ]
    return [
        Error(
            "The AI Gateway is mandatory but AI_GATEWAY_ENABLED=0 "
            f"(DJANGO_ENV={env}).",
            hint=(
                f"Set AI_GATEWAY_ENABLED=1 and AI_GATEWAY_BASE_URL={_required_url()} — "
                "or ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1 for unit tests only."
            ),
            id="llm_router.E001",
        )
    ]


def check_ai_gateway_base_url(app_configs, **kwargs):
    """Check B — URL Gateway interdite (vide / loopback) quand la Gateway est active."""
    env = _env()
    if env not in OFFICIAL_ENVS:
        return []
    # Ne s'applique que si la Gateway est censée servir. Si désactivée, le Check A
    # gère déjà le cas (évite un double-signalement).
    if not bool(_s("AI_GATEWAY_ENABLED")):
        return []

    base_url = (_s("AI_GATEWAY_BASE_URL") or "").strip()
    if not base_url:
        return [
            Error(
                f"AI_GATEWAY_ENABLED=1 but AI_GATEWAY_BASE_URL is empty (DJANGO_ENV={env}).",
                hint=f"Set AI_GATEWAY_BASE_URL={_required_url()}.",
                id="llm_router.E002",
            )
        ]

    host = (urlparse(base_url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return [
            Error(
                f"AI_GATEWAY_BASE_URL points to a loopback/non-routable host ({host}) "
                f"in DJANGO_ENV={env}; from a container, loopback is not the AI Gateway "
                "(use the compose service name, e.g. http://ai-gateway:8100).",
                hint=f"Set AI_GATEWAY_BASE_URL={_required_url()}.",
                id="llm_router.E002",
            )
        ]
    return []


def check_no_direct_ollama(app_configs, **kwargs):
    """Check C — RAG_LLM_PROVIDER=ollama interdit dans les environnements officiels…

    …quand la Gateway est ÉTEINTE : c'est alors le chemin direct qui servirait la
    génération. Gateway active, la variable n'a aucun effet nominal (RAGatm Open ne
    connaît que la valeur ``ollama``) et le check est muet.
    """
    env = _env()
    if env not in OFFICIAL_ENVS:
        return []
    if bool(_s("AI_GATEWAY_ENABLED")):
        return []
    provider = (_s("RAG_LLM_PROVIDER") or "").strip().lower()
    if provider != "ollama":
        return []
    if _tests_tolerated():
        return [
            CheckWarning(
                f"RAG_LLM_PROVIDER=ollama in DJANGO_ENV={env}; tolerated only because "
                "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1.",
                hint="Local models must be served via the AI Gateway.",
                id="llm_router.W002",
            )
        ]
    return [
        Error(
            "RAG_LLM_PROVIDER=ollama is forbidden in an official environment "
            f"(DJANGO_ENV={env}): RagATM must never call Ollama directly.",
            hint=(
                "Enable the AI Gateway (AI_GATEWAY_ENABLED=1, AI_GATEWAY_BASE_URL=...); "
                "RAG_LLM_PROVIDER has no nominal effect once the Gateway serves generation."
            ),
            id="llm_router.E003",
        )
    ]


def check_ai_gateway_fallback_provider(app_configs, **kwargs):
    """Check D — ``llm_router.E004`` : le seul provider de repli admis est ``none``.

    RAGatm Open n'a aucun fournisseur externe : une autre valeur (``openai``, ``ollama``…)
    est une configuration invalide, refusée au démarrage plutôt que découverte lors d'une
    panne de Gateway.
    """
    fb = (_s("AI_GATEWAY_FALLBACK_PROVIDER") or "none").strip().lower()
    if fb != "none":
        return [
            Error(
                f"AI_GATEWAY_FALLBACK_PROVIDER={fb!r} is not allowed in RAGatm Open "
                "(only 'none': there is no fallback provider).",
                hint="Set AI_GATEWAY_FALLBACK_PROVIDER=none (or unset it).",
                id="llm_router.E004",
            )
        ]
    return []


#: Réglages de provider dont la valeur ne peut jamais désigner un fournisseur externe.
PROVIDER_SETTINGS = (
    "RAG_LLM_PROVIDER",
    "DEFAULT_LLM_PROVIDER",
    "EMBEDDINGS_PROVIDER",
    "DEFAULT_RAGSPACE_CHAT_PROVIDER",
    "DEFAULT_RAGSPACE_EMBEDDING_PROVIDER",
    "AI_GATEWAY_PROVIDER",
)


def check_no_external_provider_configuration(app_configs, **kwargs):
    """``llm_router.E009`` — aucune configuration de fournisseur d'IA externe (édition Open).

    Fail-closed et sans repli : un réglage de provider valant un fournisseur externe
    (``openai``, ``anthropic``, ``mistral``, ``cohere``, ``google``, ``azure_openai``) empêche
    le démarrage. Les variables d'environnement de ces fournisseurs (``OPENAI_API_KEY``…) sont
    refusées par ``config.E009`` (niveau projet). La sortie des données n'est pas une option
    de configuration de RAGatm Open.
    """
    from .egress import EXTERNAL_PROVIDERS, normalize_provider

    errors = []
    for name in PROVIDER_SETTINGS:
        value = normalize_provider(_s(name) or "")
        if value and value in EXTERNAL_PROVIDERS:
            errors.append(Error(
                f"{name}={value!r} designates an external AI provider; RAGatm Open serves "
                "inference exclusively through the AI Gateway.",
                hint=f"Set {name} to a local value (ollama / ai_gateway) or unset it.",
                id="llm_router.E009",
            ))
    return errors


# ---------------------------------------------------------------------------
# Check E — les TROIS URL d'AI Gateway
# ---------------------------------------------------------------------------
#: Les trois réglages qui désignent une AI Gateway (directe ou via AI Gateway Pool).
#: Sémantiques DISTINCTES (génération/vision/ASR, embeddings gouvernés, rerank) : elles
#: valent la même adresse aujourd'hui, mais aucune n'est déduite d'une autre.
GATEWAY_URL_SETTINGS = (
    "AI_GATEWAY_BASE_URL",
    "AI_GATEWAY_EMBEDDINGS_BASE_URL",
    "RAG_RERANKER_BASE_URL",
)
#: Port d'Ollama : une URL Gateway qui le vise est un appel Ollama DIRECT déguisé.
_OLLAMA_PORT = 11434
_PROD_ENVS = ("prod", "production")


def _gateway_url(name: str) -> str:
    if name == "RAG_RERANKER_BASE_URL":
        # Projeté par `os.getenv` dans les settings (hors registre du routeur).
        return str(getattr(settings, name, "") or "").strip()
    return str(_s(name) or "").strip()


def check_gateway_endpoints(app_configs, **kwargs):
    """``llm_router.E007`` — chacune des trois URL Gateway doit être une cible gouvernée.

    Accepte une Gateway jointe directement (``http://ai-gateway:8100``) comme un pool
    déclaré dans ``AI_GATEWAY_POOL_HOSTS`` : seules les cibles NON gouvernées sont
    refusées. Une URL VIDE n'est pas jugée ici (E002 pour la génération ; les checks de
    chaque capacité pour les autres, ex. ``rag_core`` pour le rerank).

    Refusé :
    - schéma autre que http/https, ou URL sans hôte ;
    - hôte loopback pour les embeddings et le rerank (E002 couvre déjà la génération) ;
    - port 11434 (Ollama direct) sur l'une des trois.
    """
    env = _env()
    if env not in OFFICIAL_ENVS + _PROD_ENVS[1:]:
        return []
    if not bool(_s("AI_GATEWAY_ENABLED")):
        return []
    errors = []
    for name in GATEWAY_URL_SETTINGS:
        raw = _gateway_url(name)
        if not raw:
            continue
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if parsed.scheme not in ("http", "https") or not host or port == -1:
            errors.append(Error(
                f"{name} is not a valid http(s) URL with a host (DJANGO_ENV={env}).",
                hint="Expected e.g. http://ai-gateway:8100 or http://ai-gateway-pool:8080.",
                id="llm_router.E007",
            ))
            continue
        if name != "AI_GATEWAY_BASE_URL" and host in _LOOPBACK_HOSTS:
            errors.append(Error(
                f"{name} points to a loopback/non-routable host ({host}) in DJANGO_ENV={env}.",
                hint="A container's loopback is not the AI Gateway (nor the pool on the host).",
                id="llm_router.E007",
            ))
        if port == _OLLAMA_PORT:
            errors.append(Error(
                f"{name} targets port {_OLLAMA_PORT} (Ollama) in DJANGO_ENV={env}: "
                "RagATM must never call Ollama directly.",
                hint="Use the AI Gateway (directly or through AI Gateway Pool).",
                id="llm_router.E007",
            ))
    return errors


def check_gateway_routes_consistent(app_configs, **kwargs):
    """``llm_router.E008`` — les trois URL Gateway suivent le MÊME chemin.

    une bascule partielle (génération par le pool,
    embeddings en direct, rerank par un autre pool…) est refusée au démarrage, jamais
    corrigée en silence. Deux pools différents sont aussi refusés. Une URL vide n'est pas
    jugée ici (E002 et les checks propres à chaque capacité).
    """
    from .gateway.endpoints import ROUTE_POOL, route_kind, url_host

    env = _env()
    if env not in OFFICIAL_ENVS + _PROD_ENVS[1:]:
        return []
    if not bool(_s("AI_GATEWAY_ENABLED")):
        return []
    urls = {name: _gateway_url(name) for name in GATEWAY_URL_SETTINGS}
    urls = {k: v for k, v in urls.items() if v}
    routes = {k: route_kind(v) for k, v in urls.items()}
    pool_hosts = {url_host(v) for k, v in urls.items() if routes[k] == ROUTE_POOL}
    if ROUTE_POOL in routes.values() and (len(set(routes.values())) > 1 or len(pool_hosts) > 1):
        detail = ", ".join(f"{k}={routes[k]}" for k in sorted(routes))
        return [Error(
            f"Gateway URLs are routed inconsistently (DJANGO_ENV={env}): {detail}.",
            hint="All three URLs must target the same AI Gateway Pool, or all the direct Gateway.",
            id="llm_router.E008",
        )]
    return []
