"""Catalogue des modèles et pipelines — projection GOUVERNÉE du registre Gateway.

## Les quatre notions, et pourquoi elles doivent rester distinctes

    available    présent dans `GET /v1/models`
    allowed      compatible avec le rôle demandé ET les invariants de CET environnement
    qualified    RagATM l'a mesuré (cf. `qualification.py`)
    recommended  le choix par défaut parmi les qualifiés

Les confondre a un coût concret. Avant ce module, la seule validation d'un nom de
modèle était une regex de caractères : `bge-m3` était accepté à la configuration d'un
espace, marquait tous ses documents `reindex_required`, puis échouait à la première
ingestion sur `dimension mismatch (got 1024, expected 768)`. L'espace se retrouvait
avec un leg vectoriel vide et aucune réindexation possible — un état que l'utilisateur
avait provoqué en croyant configurer son espace.

La Gateway publiait pourtant tout ce qu'il fallait pour le refuser : `capabilities`,
`dimension`, `registry_status`. Personne ne le lisait.

## Le piège que ce module doit rendre impossible

Le lot GOVERNED-EMBEDDING-PIPELINE-ENABLEMENT a mesuré que le pipeline gouverné
`legacy-768` coûte **26,8 ms** et `fr-en-retrieval` **170,5 ms**, pour des poids
différents mais une gouvernance identique. Le choix structurant n'est donc pas
« legacy ou gouverné » — c'est **quel pipeline gouverné**. Le catalogue expose les deux
comme deux entrées distinctes, chacune avec sa mesure.

## Génération : `/v1/models` ne dit pas ce que `/v1/generate` accepte

`/v1/models` relaie les capacités d'Ollama. `gpt-oss:20b` et
`qwen3:30b-a3b-thinking-2507` y portent `completion`, alors que le registre de la
Gateway les inscrit en `chat` seul : `/v1/generate` — le SEUL endpoint du chat RagATM —
les refuse en 400 `completion_not_supported`. Un
espace configuré ainsi échouait à chaque question.

La compatibilité de génération se lit donc dans `GET /health/runtime`
(`llm_models[].capabilities`), le registre que la Gateway applique elle-même :

    supported       inscrit, `completion` déclarée, `allowed_here`
    unsupported     inscrit sans `completion` (ou interdit sur cet environnement)
    unsafe_output   politique de sortie RagATM (`qualification.CHAT_OUTPUT_POLICY_REJECTED`)
    not_verified    absent du registre, ou registre illisible — FAIL-CLOSED

Seul `supported` est sélectionnable. Un modèle non inscrit répond peut-être (le
registre n'est pas strict), mais rien ne le garantit : il n'est pas proposé.

## Ce qui n'est PAS gouvernable ici

Les modèles OpenAI n'apparaissent pas dans le registre de la Gateway : le catalogue ne
peut ni les confirmer ni les infirmer. Ils conservent la validation lexicale seule, et
le catalogue le dit explicitement (`reason=not_in_gateway_registry`) plutôt que de
prétendre les avoir vérifiés.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from django.conf import settings

from .model_defaults import default_ragspace_chat_model
from .qualification import (
    QUALIFICATION_REVISION,
    RECOMMENDED_CHAT_REASON,
    RECOMMENDED_EMBEDDING_PIPELINE,
    RECOMMENDED_EMBEDDING_REASON,
    chat_output_policy_rejection,
    chat_qualification,
    embedding_qualification,
)

logger = logging.getLogger(__name__)

KIND_CHAT = "chat"
KIND_EMBEDDING = "embedding"

#: Durée de vie du catalogue en mémoire. Le catalogue n'est PAS sur le chemin chaud :
#: il sert à la création/modification d'un espace et à l'administration. Un TTL court
#: suffirait ; un TTL long éviterait des allers-retours inutiles. 300 s est un
#: compromis qui laisse un nouveau modèle apparaître dans les cinq minutes.
CATALOG_TTL_SECONDS = 300

#: Codes d'erreur exposés par l'API. Stables : le frontend et l'exploitation les lisent.
ERR_UNAVAILABLE = "model_catalog_unavailable"
ERR_UNKNOWN_MODEL = "model_not_in_catalog"
ERR_WRONG_CAPABILITY = "model_capability_mismatch"
ERR_DIMENSION = "model_dimension_mismatch"
ERR_NOT_READY = "model_registry_not_ready"
ERR_NOT_QUALIFIED = "model_not_qualified"
ERR_GENERATION_UNSUPPORTED = "model_generation_not_supported"
ERR_GENERATION_NOT_VERIFIED = "model_generation_not_verified"
ERR_OUTPUT_POLICY = "model_output_policy_rejected"
#: Profil d'embeddings absent de ceux que DÉCLARE le pool visé :
#: le pool répondrait 404 MODEL_NOT_AVAILABLE, il ne substitue jamais un autre modèle.
ERR_NOT_DECLARED_BY_POOL = "embedding_model_not_declared_by_pool"

#: Classification de génération d'un modèle de chat (cf. docstring du module).
GEN_SUPPORTED = "supported"
GEN_UNSUPPORTED = "unsupported"
GEN_UNSAFE_OUTPUT = "unsafe_output"
GEN_NOT_VERIFIED = "not_verified"

_GEN_ERROR = {
    GEN_UNSUPPORTED: ERR_GENERATION_UNSUPPORTED,
    GEN_UNSAFE_OUTPUT: ERR_OUTPUT_POLICY,
    GEN_NOT_VERIFIED: ERR_GENERATION_NOT_VERIFIED,
}


# ---------------------------------------------------------------------------
# Entrée de catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    """Une option proposable, avec ce qui la rend proposable — ou non."""

    id: str
    kind: str
    display_name: str
    available: bool
    allowed: bool
    qualified: bool
    recommended: bool
    capabilities: tuple[str, ...] = ()
    dimension: int | None = None
    registry_status: str = ""
    upstream_model: str = ""
    #: Motif du refus quand `allowed` est faux. Vide sinon. C'est ce qui permet à un
    #: exploitant de comprendre POURQUOI une option n'est pas offerte ici, alors
    #: qu'elle l'est ailleurs.
    reason: str = ""
    qualification: dict[str, Any] | None = None
    #: Chat seulement : compatibilité avec `/v1/generate` (GEN_*). Vide pour les embeddings.
    generation_status: str = ""

    def as_public_dict(self) -> dict[str, Any]:
        """Vue PUBLIQUE — sans digest, sans URL interne, sans topologie.

        Le digest complet reste accessible à l'API avancée (superuser) ; il n'a rien
        à faire dans une liste déroulante.
        """
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "display_name": self.display_name,
            "available": self.available,
            "allowed": self.allowed,
            "qualified": self.qualified,
            "recommended": self.recommended,
            "capabilities": list(self.capabilities),
            "registry_status": self.registry_status,
            "upstream_model": self.upstream_model,
        }
        if self.dimension is not None:
            d["dimension"] = self.dimension
        if self.reason:
            d["reason"] = self.reason
        if self.qualification:
            d["qualification"] = self.qualification
        if self.generation_status:
            d["generation_status"] = self.generation_status
        return d


# ---------------------------------------------------------------------------
# Source : le registre de la Gateway
# ---------------------------------------------------------------------------

#: Cache mémoire process-local : (timestamp, modèles). Volontairement PAS Redis —
#: le §38 du lot l'exclut, et une dépendance externe sur ce chemin transformerait une
#: panne de cache en panne de configuration.
_CACHE: dict[str, Any] = {"fetched_at": 0.0, "models": None, "stale": False}


def _gateway_base_url() -> str:
    from apps.rag_core.core import runtime_config

    return str(runtime_config.get("AI_GATEWAY_BASE_URL", default="") or "").rstrip("/")


def _fetch_gateway_models(timeout_s: float = 5.0) -> list[dict[str, Any]]:
    base = _gateway_base_url()
    if not base:
        raise RuntimeError("AI_GATEWAY_BASE_URL vide")
    resp = requests.get(f"{base}/v1/models", timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise RuntimeError("réponse /v1/models inattendue")
    return models


def catalog_applicable() -> bool:
    """Cet environnement déclare-t-il une passerelle dont on puisse lire le catalogue ?

    Distinction NÉCESSAIRE, et elle n'est pas cosmétique :

    - **aucune passerelle déclarée** (dev, tests, `AI_GATEWAY_BASE_URL` vide) : la
      gouvernance de catalogue ne s'applique pas. Elle n'a rien à gouverner, et la
      validation lexicale historique reste seule en vigueur.
    - **passerelle déclarée mais injoignable** : c'est une panne. On refuse plutôt que
      d'accepter sans vérifier — le fail-closed du §22.

    Les confondre ferait soit exploser les environnements sans passerelle, soit ouvrir
    une porte pendant une panne. Ce ne sont pas les mêmes situations.
    """
    return bool(_gateway_base_url())


def gateway_models(*, refresh: bool = False) -> tuple[list[dict[str, Any]] | None, bool]:
    """(modèles, périmé) — ``None`` si aucun état fiable n'a jamais été obtenu.

    Politique d'indisponibilité (§22), et elle est délibérée :

    - un catalogue **périmé** vaut mieux qu'un refus : le registre de la Gateway
      change rarement, et bloquer toute configuration parce qu'une passerelle est
      momentanément injoignable serait disproportionné ;
    - **aucun** catalogue vaut refus : accepter n'importe quel modèle « parce qu'on ne
      sait pas » est exactement le comportement que ce module remplace.

    Le caractère périmé est propagé jusqu'à la réponse d'API — l'appelant doit pouvoir
    savoir qu'il lit une photo, pas le direct.
    """
    now = time.monotonic()
    fresh = (
        _CACHE["models"] is not None
        and not refresh
        and (now - float(_CACHE["fetched_at"])) < CATALOG_TTL_SECONDS
    )
    if fresh:
        return _CACHE["models"], bool(_CACHE["stale"])

    try:
        models = _fetch_gateway_models()
    except Exception as exc:  # noqa: BLE001 - toute panne de transport
        if _CACHE["models"] is not None:
            _CACHE["stale"] = True
            logger.warning(
                "model_catalog_stale error=%s (catalogue précédent conservé)",
                type(exc).__name__,
            )
            return _CACHE["models"], True
        logger.warning("model_catalog_unavailable error=%s", type(exc).__name__)
        return None, False

    _CACHE.update(fetched_at=now, models=models, stale=False)
    return models, False


#: Registre de génération (`/health/runtime`), même politique que `_CACHE`.
_RUNTIME_CACHE: dict[str, Any] = {"fetched_at": 0.0, "models": None, "stale": False}


def _fetch_gateway_runtime_models(timeout_s: float = 5.0) -> dict[str, dict[str, Any]]:
    """`llm_models` de `/health/runtime`, indexés par nom amont (`ollama_name`)."""
    base = _gateway_base_url()
    if not base:
        raise RuntimeError("AI_GATEWAY_BASE_URL vide")
    resp = requests.get(f"{base}/health/runtime", timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()
    llm = data.get("llm_models") if isinstance(data, dict) else None
    if not isinstance(llm, list):
        raise RuntimeError("réponse /health/runtime sans llm_models")
    out: dict[str, dict[str, Any]] = {}
    for m in llm:
        if not isinstance(m, dict) or not m.get("ollama_name"):
            continue
        out[str(m["ollama_name"])] = m
        # le nom LOGIQUE du registre Compact (`compact-generation-8`) est
        # aussi une clé. C'est lui que la configuration Open désigne et que la Gateway
        # résout en premier (`registry.resolve_llm`, nom logique avant nom Ollama).
        logical = str(m.get("model_logical") or "").strip()
        if logical:
            out.setdefault(logical, m)
    return out


def gateway_runtime_models(
    *, refresh: bool = False
) -> tuple[dict[str, dict[str, Any]] | None, bool]:
    """(registre de génération, périmé) — ``None`` si jamais obtenu.

    Même politique que `gateway_models` : un état périmé est servi et signalé, une
    absence totale rend TOUS les modèles de chat `not_verified` — donc non
    sélectionnables. Accepter un modèle « parce que le registre ne répond pas » serait
    accepter gpt-oss:20b.
    """
    now = time.monotonic()
    fresh = (
        _RUNTIME_CACHE["models"] is not None
        and not refresh
        and (now - float(_RUNTIME_CACHE["fetched_at"])) < CATALOG_TTL_SECONDS
    )
    if fresh:
        return _RUNTIME_CACHE["models"], bool(_RUNTIME_CACHE["stale"])
    try:
        models = _fetch_gateway_runtime_models()
    except Exception as exc:  # noqa: BLE001 - toute panne de transport
        if _RUNTIME_CACHE["models"] is not None:
            _RUNTIME_CACHE["stale"] = True
            logger.warning("model_runtime_registry_stale error=%s", type(exc).__name__)
            return _RUNTIME_CACHE["models"], True
        logger.warning("model_runtime_registry_unavailable error=%s", type(exc).__name__)
        return None, False
    _RUNTIME_CACHE.update(fetched_at=now, models=models, stale=False)
    return models, False


def invalidate_catalog_cache() -> None:
    """Purge le cache. Appelé par le rafraîchissement manuel (§39)."""
    _CACHE.update(fetched_at=0.0, models=None, stale=False)
    _RUNTIME_CACHE.update(fetched_at=0.0, models=None, stale=False)


def generation_status(name: str, runtime: dict[str, dict[str, Any]] | None) -> str:
    """Compatibilité de ``name`` avec `/v1/generate` (GEN_*), d'après le registre Gateway.

    La politique de sortie RagATM passe EN PREMIER : un modèle dont la réponse expose
    son raisonnement reste exclu même s'il est un jour inscrit, tant qu'il n'a pas été
    requalifié.
    """
    if chat_output_policy_rejection(name):
        return GEN_UNSAFE_OUTPUT
    if runtime is None:
        return GEN_NOT_VERIFIED
    reg = runtime.get(name)
    if reg is None:
        return GEN_NOT_VERIFIED
    if "completion" not in (reg.get("capabilities") or ()):
        return GEN_UNSUPPORTED
    if reg.get("allowed_here") is False:
        return GEN_UNSUPPORTED
    if (reg.get("reasoning") or {}).get("expose_thinking") is True:
        return GEN_UNSAFE_OUTPUT
    return GEN_SUPPORTED


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _dimension_of(raw: dict[str, Any]) -> int | None:
    """Dimension d'un modèle d'embedding, du plus autoritatif au plus déductif.

    Le champ de premier niveau `dimension` n'est publié que pour les modèles INSCRITS
    au registre gouverné. `bge-m3` ne l'a pas — mais son `details.embedding_length`
    vaut 1024, et c'est cette valeur qui permet de le refuser AVANT toute ingestion.
    """
    for value in (raw.get("dimension"), (raw.get("details") or {}).get("embedding_length")):
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


def _expected_dimension() -> int:
    return int(getattr(settings, "RAG_EMBEDDING_DIM", 768))


def _governed_embeddings_ready() -> str | None:
    """Code d'indisponibilité du pipeline gouverné sur CET environnement, ou None."""
    from .embeddings_gateway import gateway_embeddings_base_url, gateway_embeddings_enabled

    if not gateway_embeddings_enabled():
        return "gateway_embeddings_not_enabled"
    if not gateway_embeddings_base_url():
        return "gateway_embeddings_base_url_missing"
    return None


def _chat_entry(raw: dict[str, Any], runtime: dict[str, dict[str, Any]] | None) -> CatalogEntry:
    name = str(raw.get("name") or "")
    logical = str(raw.get("model_logical") or "").strip()
    caps = tuple(raw.get("capabilities") or ())
    qual = chat_qualification(name)
    gen = generation_status(name, runtime)
    default = (default_ragspace_chat_model() or "").strip()
    if "completion" not in caps:
        allowed, reason = False, ERR_WRONG_CAPABILITY
    elif gen != GEN_SUPPORTED:
        allowed, reason = False, _GEN_ERROR[gen]
    else:
        allowed, reason = True, ""
    return CatalogEntry(
        id=name,
        kind=KIND_CHAT,
        display_name=str(raw.get("model_logical") or name),
        available=True,
        allowed=allowed,
        qualified=qual is not None,
        # Recommandé = le défaut EFFECTIF de l'environnement, s'il est utilisable. Une
        # seule source pour la présélection et le badge : elles ne peuvent plus diverger.
        # Le défaut peut être écrit sous son nom LOGIQUE (profil Compact) ou sous son
        # nom amont : les deux désignent la même entrée.
        recommended=(allowed and bool(default) and default in (name, logical)),
        capabilities=caps,
        registry_status=str(raw.get("registry_status") or ""),
        upstream_model=name,
        reason=reason,
        qualification=qual.as_dict() if qual else None,
        generation_status=gen,
    )


def default_chat_status(chat: list[CatalogEntry]) -> str:
    """État du défaut de chat de l'environnement : ``available`` ou le motif du refus.

    ``model_not_in_catalog`` si la passerelle ne le publie pas. Jamais de substitution :
    l'interface affiche cet état, elle ne choisit pas un autre modèle à sa place.
    """
    default = (default_ragspace_chat_model() or "").strip()
    for e in chat:
        # `display_name` porte le nom LOGIQUE du registre quand il existe.
        if default and default in (e.id, e.display_name):
            return "available" if e.allowed else (e.reason or ERR_WRONG_CAPABILITY)
    return ERR_UNKNOWN_MODEL


def _embedding_entry(raw: dict[str, Any], *, governed_block: str | None) -> CatalogEntry:
    name = str(raw.get("name") or "")
    logical = str(raw.get("model_logical") or "")
    caps = tuple(raw.get("capabilities") or ())
    dim = _dimension_of(raw)
    status = str(raw.get("registry_status") or "")
    qual = embedding_qualification(logical) if logical else None

    # L'identifiant d'un pipeline d'embeddings est son modèle LOGIQUE. Un modèle
    # d'embedding sans modèle logique n'est PAS un pipeline gouvernable : il est
    # présent sur la machine, pas inscrit au registre.
    entry_id = logical or name

    reason = ""
    allowed = True
    if "embedding" not in caps:
        allowed, reason = False, ERR_WRONG_CAPABILITY
    elif not logical or status != "ready":
        allowed, reason = False, ERR_NOT_READY
    elif dim != _expected_dimension():
        allowed, reason = False, ERR_DIMENSION
    elif governed_block:
        # Le pipeline existe et est conforme, mais CET environnement n'expose pas la
        # passerelle d'embeddings gouvernés. C'est la distinction du §46 : `available`
        # sans `allowed`, avec le motif.
        allowed, reason = False, governed_block

    return CatalogEntry(
        id=entry_id,
        kind=KIND_EMBEDDING,
        display_name=logical or name,
        available=True,
        allowed=allowed,
        qualified=qual is not None,
        recommended=(entry_id == RECOMMENDED_EMBEDDING_PIPELINE and allowed and qual is not None),
        capabilities=caps,
        dimension=dim,
        registry_status=status,
        upstream_model=name,
        reason=reason,
        qualification=qual.as_dict() if qual else None,
    )


def build_catalog(*, refresh: bool = False) -> dict[str, Any]:
    """Catalogue public : ``{"chat": [...], "embeddings": [...], ...}``.

    ``available`` est vrai pour toute entrée rendue : ce qui n'est pas dans le
    registre de la Gateway n'apparaît pas du tout.
    """
    if not catalog_applicable():
        return {
            "chat": [], "embeddings": [],
            "available": False, "stale": False,
            "error": "model_catalog_not_configured",
            "qualification_revision": QUALIFICATION_REVISION,
            "expected_embedding_dimension": _expected_dimension(),
        }
    models, stale = gateway_models(refresh=refresh)
    if models is None:
        return {
            "chat": [],
            "embeddings": [],
            "available": False,
            "stale": False,
            "error": ERR_UNAVAILABLE,
            "qualification_revision": QUALIFICATION_REVISION,
            "expected_embedding_dimension": _expected_dimension(),
        }

    runtime, runtime_stale = gateway_runtime_models(refresh=refresh)
    governed_block = _governed_embeddings_ready()
    chat, emb = [], []
    for raw in models:
        caps = tuple(raw.get("capabilities") or ())
        if "completion" in caps:
            chat.append(_chat_entry(raw, runtime))
        if "embedding" in caps:
            emb.append(_embedding_entry(raw, governed_block=governed_block))

    return {
        "chat": [e.as_public_dict() for e in chat],
        "embeddings": [e.as_public_dict() for e in emb],
        "available": True,
        "stale": stale or runtime_stale,
        # Registre de génération illisible : les modèles restent LISTÉS, aucun n'est
        # sélectionnable. L'interface doit pouvoir dire pourquoi.
        "generation_registry_available": runtime is not None,
        "default_chat_model": (default_ragspace_chat_model() or "").strip(),
        "default_chat_status": default_chat_status(chat),
        "recommended_chat_reason": RECOMMENDED_CHAT_REASON,
        "qualification_revision": QUALIFICATION_REVISION,
        "expected_embedding_dimension": _expected_dimension(),
        "recommended_embedding_reason": RECOMMENDED_EMBEDDING_REASON,
    }


# ---------------------------------------------------------------------------
# Validation — appelée AVANT tout effet de bord
# ---------------------------------------------------------------------------


def _entries_by_id(kind: str) -> dict[str, CatalogEntry] | None:
    models, _ = gateway_models()
    if models is None:
        return None
    governed_block = _governed_embeddings_ready()
    runtime = gateway_runtime_models()[0] if kind == KIND_CHAT else None
    out: dict[str, CatalogEntry] = {}
    for raw in models:
        caps = tuple(raw.get("capabilities") or ())
        if kind == KIND_CHAT and "completion" in caps:
            e = _chat_entry(raw, runtime)
            out[e.id] = e
            # Alias par nom LOGIQUE : `compact-generation-8` désigne la même
            # entrée que `qwen2.5:1.5b-instruct`. La Gateway accepte les deux formes sur
            # /v1/generate ; le catalogue ne doit pas refuser ce que la Gateway sert.
            logical = str(raw.get("model_logical") or "").strip()
            if logical:
                out.setdefault(logical, e)
        elif kind == KIND_EMBEDDING and "embedding" in caps:
            e = _embedding_entry(raw, governed_block=governed_block)
            out[e.id] = e
            # Un modèle d'embedding est aussi adressable par son nom AMONT, avec ou
            # sans tag : c'est ainsi que le chemin historique le désigne
            # (`ollama` / `nomic-embed-text`). La même entrée sert alors à vérifier la
            # dimension et la capacité — sans prétendre que l'espace est gouverné pour
            # autant (§28-29).
            #
            # L'alias sans tag n'est pas cosmétique : `bge-m3` (sans `:latest`) doit
            # être refusé sur sa dimension, pas rendu « inconnu ». Un message qui envoie
            # chercher une faute de frappe là où il y a une incompatibilité de 1024
            # contre 768 coûte du temps à qui le lit.
            if e.upstream_model:
                out.setdefault(e.upstream_model, e)
                out.setdefault(e.upstream_model.split(":", 1)[0], e)
    return out


def validate_chat_model(model: str) -> str | None:
    """Code d'erreur si ``model`` ne peut pas servir au chat, sinon ``None``.

    Un modèle VIDE est accepté : il signifie « défaut du provider », résolu en aval.
    Le §24 impose que le modèle **résolu** soit admissible ; c'est le défaut
    d'environnement qui porte cette garantie, vérifiée par test.
    """
    m = (model or "").strip()
    if not m or not catalog_applicable():
        return None
    entries = _entries_by_id(KIND_CHAT)
    if entries is None:
        return ERR_UNAVAILABLE
    entry = entries.get(m)
    if entry is None:
        # Peut-être un modèle d'embedding utilisé comme modèle de chat : le dire
        # précisément vaut mieux qu'un « inconnu » qui enverrait chercher une faute
        # de frappe là où il y a une erreur de rôle.
        emb = _entries_by_id(KIND_EMBEDDING) or {}
        return ERR_WRONG_CAPABILITY if m in emb else ERR_UNKNOWN_MODEL
    return None if entry.allowed else (entry.reason or ERR_WRONG_CAPABILITY)


def validate_embedding_model(provider: str, model: str) -> str | None:
    """Code d'erreur si le couple (provider, modèle) est inexploitable, sinon ``None``.

    ``provider`` compte : `ai_gateway` désigne un **pipeline logique**, `ollama` un
    **modèle amont**. Les deux peuvent pointer les mêmes poids et ne sont pas la même
    chose — c'est précisément la confusion que le §28 demande d'éviter.

    Édition Open : aucun fournisseur externe n'existe ; tout provider est jugé par le
    registre de la Gateway.
    """
    p = (provider or "").strip().lower()
    m = (model or "").strip()
    if not m or not catalog_applicable():
        return None
    entries = _entries_by_id(KIND_EMBEDDING)
    if entries is None:
        return ERR_UNAVAILABLE
    entry = entries.get(m)
    if entry is None:
        chat = _entries_by_id(KIND_CHAT) or {}
        return ERR_WRONG_CAPABILITY if m in chat else ERR_UNKNOWN_MODEL

    # Dimension et capacité valent pour TOUS les providers : ce sont des invariants de
    # la colonne pgvector, pas des règles de gouvernance.
    if "embedding" not in entry.capabilities:
        return ERR_WRONG_CAPABILITY
    if entry.dimension is not None and entry.dimension != _expected_dimension():
        return ERR_DIMENSION

    if p == "ai_gateway":
        # Ici seulement, l'inscription au registre et la disponibilité de la
        # passerelle gouvernée sont exigées.
        if entry.registry_status != "ready" or entry.id != m:
            return ERR_NOT_READY
        block = _governed_embeddings_ready()
        if block:
            return block
        # Chemin RÉELLEMENT sélectionné : un pool qui déclare ses profils (production) refuse
        # tout autre profil, même si la Gateway en amont le publie. En accès direct, ou via un
        # pool sans déclaration restrictive, la politique existante est inchangée.
        from django.conf import settings as _settings

        from .gateway.endpoints import declared_embedding_models

        declared = declared_embedding_models(str(getattr(_settings, "AI_GATEWAY_EMBEDDINGS_BASE_URL", "") or ""))
        if declared is not None and m not in declared:
            return ERR_NOT_DECLARED_BY_POOL
    return None


def is_qualified(kind: str, identifier: str) -> bool:
    if kind == KIND_CHAT:
        return chat_qualification(identifier) is not None
    return embedding_qualification(identifier) is not None
