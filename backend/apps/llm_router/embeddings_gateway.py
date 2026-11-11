"""Client d'embeddings GOUVERNÉS — AI Gateway ``POST {base}/v1/embeddings``.

Ce module est le seul endroit de RagATM qui parle au service d'embeddings gouverné.
Il ne fait rien d'autre : ni chunking, ni persistance, ni décision de routage. Le
routage vit dans ``embeddings.py``, la persistance dans le service d'indexation.

## Ce que « gouverné » veut dire ici

La Gateway ne rend pas seulement des vecteurs : elle rend l'**identité du pipeline**
qui les a produits — un ``pipeline_id`` OPAQUE (``emb-v2:<empreinte>``) — et les champs
qui l'attestent (``model_logical``, ``model_digest``, ``identity_verified``). RagATM
stocke l'identifiant tel quel, le **compare par égalité** à la référence que la Gateway
publie sur ``/health/embeddings`` (cf. ``embedding_reference``), et dérive la **famille**
de comparabilité des champs attestés — jamais de l'identifiant lui-même, qui ne porte
aucune sémantique lisible. Deux vecteurs ne sont comparés que si leurs familles sont
égales.

Conséquence directe sur ce client : **toute réponse incomplète ou incohérente est
refusée**. Un vecteur sans identité vérifiable est plus dangereux qu'une erreur — il
s'écrit en base, se compare à tort, et rend des résultats plausibles et faux.

## Absence délibérée de repli

Il n'y a **aucun fallback** vers Ollama direct, ni vers un autre modèle. Un RagSpace
gouverné qui échoue lève. Un repli silencieux produirait des vecteurs d'un AUTRE
pipeline dans une colonne qui prétend porter celui-ci — exactement ce que
``pipeline_id`` existe pour empêcher.

## Journalisation

Aucun texte de chunk, aucune question, aucun vecteur n'est journalisé (§22). Seuls
des identifiants, des compteurs et des durées.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .config_access import router_setting
from .embedding_errors import GatewayEmbeddingsError
from .embedding_pipeline import (
    ALLOWED_TASKS,
    TASK_DOCUMENT,
    TASK_QUERY,
    attested_pipeline_family,
    pipeline_family,
)
from .embedding_reference import PipelineReference, pipeline_reference_for
from .gateway.pool_errors import pool_error_from_response
from .gateway.request_id import REQUEST_ID_HEADER, resolve_request_id

logger = logging.getLogger("rag.embeddings.gateway")

#: Nom du provider d'embeddings gouverné. EXPLICITE : il ne détourne pas `ollama`.
PROVIDER_AI_GATEWAY = "ai_gateway"


# ---------------------------------------------------------------------------
# Compteurs
# ---------------------------------------------------------------------------
# RagATM n'embarque aucun backend de métriques (ni prometheus_client, ni statsd) :
# inventer une infrastructure sortirait du périmètre du lot. Les compteurs sont donc
# tenus EN PROCESSUS et exposés par `counters_snapshot()`, que la commande de
# cohérence lit. Ils complètent les logs structurés, ils ne les remplacent pas.
_COUNTERS: dict[str, int] = {
    "embedding_gateway_requests_total": 0,
    "embedding_gateway_errors_total": 0,
    "embedding_gateway_texts_total": 0,
    "embedding_pipeline_mismatch_total": 0,
    "embedding_chunks_rejected_total": 0,
}


def _bump(name: str, value: int = 1) -> None:
    _COUNTERS[name] = _COUNTERS.get(name, 0) + int(value)


def counters_snapshot() -> dict[str, int]:
    """Copie des compteurs du processus courant (workers compris, chacun les siens)."""
    return dict(_COUNTERS)


def reset_counters() -> None:
    """Remise à zéro — réservée aux tests."""
    for k in list(_COUNTERS):
        _COUNTERS[k] = 0


# ---------------------------------------------------------------------------
# Résultat
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EmbeddingBatch:
    """Vecteurs + identité du pipeline qui les a produits.

    ``pipeline_id`` est la valeur RENDUE par la Gateway, jamais reconstruite ni
    interprétée. Pour le chemin historique (non gouverné), c'est une identité legacy
    explicitement marquée — voir ``embedding_pipeline.legacy_pipeline_id``.

    ``family`` est la clé de COMPARABILITÉ des vecteurs (``modèle_logique@digest8``).
    Sur le chemin gouverné elle est dérivée des champs ATTESTÉS de la réponse par le
    validateur (``attested_pipeline_family``) ; la question et le fragment ont des
    ``pipeline_id`` différents mais la même famille. Une identité legacy est sa propre
    famille (posée automatiquement). Un identifiant gouverné sans famille attestée
    reste à ``""`` : les appelants gouvernés refusent, ils ne devinent pas.
    """

    vectors: list[list[float]]
    pipeline_id: str
    dimension: int
    task: str = ""
    model_logical: str = ""
    model_digest: str = ""
    governed: bool = False
    latency_ms: int = 0
    truncated: list[bool] = field(default_factory=list)
    family: str = ""

    def __post_init__(self) -> None:
        if not self.family:
            # Seule une identité LEGACY porte sa famille en elle-même ; une identité
            # gouvernée opaque ne livre rien ici (cf. embedding_pipeline.pipeline_family).
            object.__setattr__(self, "family", pipeline_family(self.pipeline_id))

    def __len__(self) -> int:
        return len(self.vectors)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def gateway_embeddings_enabled() -> bool:
    return bool(router_setting("AI_GATEWAY_EMBEDDINGS_ENABLED"))


def gateway_embeddings_base_url() -> str:
    return str(router_setting("AI_GATEWAY_EMBEDDINGS_BASE_URL") or "").rstrip("/")


def gateway_embeddings_default_model() -> str:
    return str(router_setting("AI_GATEWAY_EMBEDDINGS_MODEL") or "").strip()


def gateway_embeddings_timeout_s() -> float:
    return float(router_setting("AI_GATEWAY_EMBEDDINGS_TIMEOUT_SECONDS"))


# ---------------------------------------------------------------------------
# Validation de la réponse
# ---------------------------------------------------------------------------
def _require(condition: bool, message: str, *, code: str) -> None:
    if not condition:
        _bump("embedding_gateway_errors_total")
        raise GatewayEmbeddingsError(message, code=code)


def _validate_vector(vec: Any, *, index: int, dimension: int) -> list[float]:
    """Un vecteur valide : liste de flottants finis, de la bonne dimension, non nul.

    Le vecteur NUL est refusé explicitement : il passe toutes les gardes naïves
    (bonne longueur, valeurs finies) mais sa similarité cosinus est indéfinie — en
    pratique il se compare à tout et à rien.
    """
    _require(
        isinstance(vec, list) and len(vec) == dimension,
        f"vecteur {index} de dimension inattendue "
        f"({len(vec) if isinstance(vec, list) else type(vec).__name__} != {dimension})",
        code="vector_dimension_mismatch",
    )
    out: list[float] = []
    for x in vec:
        try:
            f = float(x)
        except (TypeError, ValueError) as exc:
            _bump("embedding_gateway_errors_total")
            raise GatewayEmbeddingsError(
                f"vecteur {index} : valeur non numérique", code="vector_not_numeric"
            ) from exc
        _require(math.isfinite(f), f"vecteur {index} : valeur non finie", code="vector_not_finite")
        out.append(f)
    _require(any(v != 0.0 for v in out), f"vecteur {index} nul", code="vector_zero")
    return out


ReferenceResolver = Callable[..., PipelineReference]


def _validate_response(
    data: Any,
    *,
    expected_model_logical: str,
    expected_task: str,
    expected_dimension: int,
    n_texts: int,
    resolve_reference: ReferenceResolver,
) -> EmbeddingBatch:
    """Refuse tout ce qui n'est pas un lot gouverné, complet et cohérent (§7).

    Chaque garde correspond à un mode de défaillance nommé dans le lot. Aucune n'est
    dégradable en avertissement : une réponse partiellement valide écrirait des
    vecteurs dont l'identité serait fausse.

    ``resolve_reference(model_logical, force_refresh=False)`` rend la référence
    publiée par la Gateway pour ce modèle (cf. ``embedding_reference``) ; elle lève si
    elle ne peut pas l'établir — la validation échoue alors, elle ne suppose rien.
    """
    _require(isinstance(data, dict), "réponse Gateway : objet JSON attendu", code="bad_payload")

    _require(
        data.get("governed") is True,
        "réponse Gateway non gouvernée (governed != true)",
        code="not_governed",
    )

    model_logical = str(data.get("model_logical") or "")
    _require(
        model_logical == expected_model_logical,
        f"modèle logique inattendu ({model_logical!r} != {expected_model_logical!r})",
        code="model_logical_mismatch",
    )

    task = str(data.get("task") or "")
    _require(
        task == expected_task,
        f"tâche inattendue ({task!r} != {expected_task!r})",
        code="task_mismatch",
    )

    dimension = data.get("dimension")
    _require(
        isinstance(dimension, int) and dimension == expected_dimension,
        f"dimension inattendue ({dimension!r} != {expected_dimension})",
        code="dimension_mismatch",
    )

    _require(
        data.get("normalized") is True,
        "réponse Gateway non normalisée (normalized != true)",
        code="not_normalized",
    )

    pipeline_id = str(data.get("pipeline_id") or "").strip()
    _require(bool(pipeline_id), "pipeline_id absent de la réponse Gateway", code="pipeline_missing")

    # --- Identité ATTESTÉE : la Gateway dit elle-même si les poids servis sont ceux
    # du registre. Sans cette attestation, le digest rendu n'est qu'une déclaration.
    _require(
        data.get("identity_verified") is True,
        f"identité de modèle non vérifiée par la Gateway (identity_verified != true) "
        f"pour model_logical={model_logical!r}",
        code="identity_not_verified",
    )
    model_digest = str(data.get("model_digest") or "").strip().lower()
    _require(bool(model_digest), "model_digest absent de la réponse Gateway", code="digest_missing")
    expected_digest = str(data.get("expected_digest") or "").strip().lower()
    _require(
        not expected_digest or expected_digest == model_digest,
        f"digest servi ({model_digest[:12]}…) différent du digest attendu "
        f"({expected_digest[:12]}…) pour model_logical={model_logical!r}",
        code="digest_mismatch",
    )

    # --- Paire (task, pipeline_id) : ÉGALITÉ avec la référence publiée par la Gateway
    # pour ce modèle logique. L'identifiant est opaque : il ne se découpe pas, il se
    # compare. Un désaccord avec une référence en cache déclenche UNE relecture (la
    # Gateway a pu être redéployée) ; un identifiant toujours inconnu est refusé.
    reference = resolve_reference(model_logical)
    declared = reference.pipeline_id_for(task)
    if declared is not None and declared != pipeline_id:
        reference = resolve_reference(model_logical, force_refresh=True)
        declared = reference.pipeline_id_for(task)
    _require(
        declared is not None,
        f"référence de pipeline Gateway : aucune identité déclarée pour "
        f"model_logical={model_logical!r}, task={task!r}",
        code="pipeline_reference_missing_task",
    )
    if declared != pipeline_id:
        _bump("embedding_pipeline_mismatch_total")
        logger.warning(
            "embedding_pipeline_mismatch",
            extra={
                "embedding_model": model_logical,
                "task": task,
                "pipeline_id": pipeline_id,
                "declared_pipeline_id": declared,
            },
        )
    _require(
        declared == pipeline_id,
        f"pipeline_id inattendu pour model_logical={model_logical!r}, task={task!r} : "
        f"reçu {pipeline_id!r}, référence Gateway {declared!r}",
        code="pipeline_id_mismatch",
    )
    _require(
        not reference.expected_digest or reference.expected_digest == model_digest,
        f"digest servi ({model_digest[:12]}…) différent du digest déclaré au registre "
        f"({reference.expected_digest[:12]}…) pour model_logical={model_logical!r}",
        code="digest_mismatch",
    )

    # --- Famille de comparabilité : champs attestés, jamais l'identifiant.
    family = attested_pipeline_family(model_logical=model_logical, model_digest=model_digest)
    _require(
        bool(family),
        f"famille de pipeline non dérivable des champs attestés (model_logical={model_logical!r})",
        code="pipeline_family_missing",
    )

    embeddings = data.get("embeddings")
    _require(isinstance(embeddings, list), "champ `embeddings` absent ou invalide", code="bad_payload")
    _require(
        len(embeddings) == n_texts,
        f"nombre de vecteurs inattendu ({len(embeddings)} != {n_texts})",
        code="vector_count_mismatch",
    )

    # `truncated` : une troncature SILENCIEUSE fabriquerait un vecteur pour un texte
    # amputé. Le profil Granite est en `on_overflow: reject`, donc la Gateway devrait
    # refuser en amont — cette garde couvre le cas où le profil changerait sans nous.
    truncated = data.get("truncated")
    _require(
        isinstance(truncated, list) and len(truncated) == n_texts,
        f"champ `truncated` absent ou de taille incohérente "
        f"({len(truncated) if isinstance(truncated, list) else type(truncated).__name__} != {n_texts})",
        code="truncated_shape_invalid",
    )
    flagged = [i for i, t in enumerate(truncated) if bool(t)]
    if flagged:
        _bump("embedding_chunks_rejected_total", len(flagged))
    _require(
        not flagged,
        f"{len(flagged)} texte(s) tronqué(s) par la Gateway (positions {flagged[:5]}"
        f"{'…' if len(flagged) > 5 else ''}) — aucun vecteur tronqué n'est accepté",
        code="truncated",
    )

    vectors = [
        _validate_vector(v, index=i, dimension=expected_dimension)
        for i, v in enumerate(embeddings)
    ]

    return EmbeddingBatch(
        vectors=vectors,
        pipeline_id=pipeline_id,
        dimension=expected_dimension,
        task=task,
        model_logical=model_logical,
        model_digest=model_digest,
        governed=True,
        truncated=[bool(t) for t in truncated],
        family=family,
    )


# ---------------------------------------------------------------------------
# Corps de requête CANONIQUE
# ---------------------------------------------------------------------------
def build_gateway_embeddings_request(
    *,
    texts: list[str],
    model: str,
    task: str,
    keep_alive: Any | None = None,
) -> dict[str, Any]:
    """Seule forme de corps que RagATM émette sur ``POST /v1/embeddings``.

    Le schéma ``EmbeddingsRequest`` de la Gateway v2 est FERMÉ
    (``additionalProperties: false``) et n'accepte que ``texts``, ``model``, ``task``
    et ``keep_alive``. Tout autre champ est refusé en 400 ``extra_forbidden``.

    ``provider`` n'a JAMAIS appartenu à ce schéma. Un build plus ancien l'ignorait en
    silence : une requête invalide passait donc pour une requête au contrat, jusqu'à
    ce qu'un build à schéma fermé la rejette et fasse échouer l'ingestion. Le provider
    AMONT appartient à la Gateway, qui le choisit ; ``RagSpace.embedding_provider`` est
    un concept de ROUTAGE applicatif et ne part pas sur le fil.

    Les DEUX chemins RagATM (historique et gouverné) passent par ici : il ne peut pas
    exister deux formes de requête susceptibles de diverger à nouveau.
    """
    _require(task in ALLOWED_TASKS, f"task invalide: {task!r}", code="invalid_task")
    model_name = str(model or "").strip()
    _require(bool(model_name), "modèle d'embeddings vide", code="model_missing")

    payload: dict[str, Any] = {"model": model_name, "task": task, "texts": list(texts)}
    # `keep_alive` est au contrat mais RagATM ne le pilote pas : on ne l'émet que s'il
    # est fourni EXPLICITEMENT. Émettre `null` par défaut ajouterait au corps un champ
    # sans intention, et laisserait croire à une politique de résidence côté client.
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    return payload


# ---------------------------------------------------------------------------
# Appel
# ---------------------------------------------------------------------------
def embed_via_gateway(
    texts: list[str],
    *,
    model: str,
    task: str,
    dim: int,
    rag_space_id: Any = None,
    document_id: Any = None,
) -> EmbeddingBatch:
    """Encode ``texts`` via la Gateway gouvernée. Lève plutôt que de dégrader.

    ``model`` est le modèle **logique** (ex. ``fr-en-retrieval``), jamais le nom
    Ollama sous-jacent : c'est le registre de la Gateway qui fait la correspondance,
    et c'est ce qui permet de changer les poids sans changer la configuration RagATM
    — le digest, lui, changerait, donc l'identité aussi.
    """
    _require(task in ALLOWED_TASKS, f"task invalide: {task!r}", code="invalid_task")

    if not texts:
        return EmbeddingBatch(vectors=[], pipeline_id="", dimension=int(dim), task=task)

    _require(
        gateway_embeddings_enabled(),
        "AI_GATEWAY_EMBEDDINGS_ENABLED=0 : le provider `ai_gateway` est refusé "
        "(aucun repli silencieux vers Ollama)",
        code="gateway_embeddings_disabled",
    )

    base_url = gateway_embeddings_base_url()
    _require(
        bool(base_url),
        "AI_GATEWAY_EMBEDDINGS_BASE_URL est vide alors que le provider `ai_gateway` est demandé",
        code="base_url_missing",
    )

    model_logical = (model or "").strip() or gateway_embeddings_default_model()
    _require(bool(model_logical), "modèle logique d'embeddings non résolu", code="model_missing")

    url = f"{base_url}/v1/embeddings"
    payload = build_gateway_embeddings_request(texts=texts, model=model_logical, task=task)
    timeout_s = gateway_embeddings_timeout_s()

    _bump("embedding_gateway_requests_total")
    _bump("embedding_gateway_texts_total", len(texts))
    start = time.perf_counter()
    # Corrélation (qualification) : identifiant de la requête HTTP en cours (retrieval), sinon
    # un identifiant neuf par appel (ingestion Celery) — le journal local ci-dessous et celui de
    # la Gateway partagent ainsi la même clé. En-tête seulement ; corps du contrat inchangé.
    request_id = resolve_request_id(generate=True)

    def _log(status: str, *, error_class: str = "", latency_ms: int = 0,
             pipeline_id: str = "", family: str = "") -> None:
        # §22 : identifiants et compteurs uniquement. Ni texte, ni vecteur, ni secret.
        logger.info(
            "embedding_gateway_call",
            extra={
                "embedding_provider": PROVIDER_AI_GATEWAY,
                "embedding_model": model_logical,
                "pipeline_id": pipeline_id,
                "pipeline_family": family,
                "task": task,
                "rag_space_id": str(rag_space_id) if rag_space_id is not None else "",
                "document_id": str(document_id) if document_id is not None else "",
                "n_texts": len(texts),
                "dimension": int(dim),
                "gateway_latency_ms": latency_ms,
                "status": status,
                "error_class": error_class,
                "request_id": request_id,
            },
        )

    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json", REQUEST_ID_HEADER: request_id},
            json=payload,
            timeout=timeout_s,
        )
    except requests.RequestException as exc:
        _bump("embedding_gateway_errors_total")
        latency_ms = int((time.perf_counter() - start) * 1000)
        _log("error", error_class=type(exc).__name__, latency_ms=latency_ms)
        raise GatewayEmbeddingsError(
            f"appel Gateway /v1/embeddings échoué: {type(exc).__name__}", code="transport_error"
        ) from exc

    latency_ms = int((time.perf_counter() - start) * 1000)

    if resp.status_code >= 400:
        _bump("embedding_gateway_errors_total")
        # Le corps peut contenir le message métier de la Gateway (modèle non autorisé,
        # texte trop long, batch trop grand). Il est borné et ne contient pas le texte
        # soumis — la Gateway ne renvoie que l'index et la longueur.
        snippet = (resp.text or "")[:500]
        # Refus PROPRE au pool (`X-AIGP-Error`) : code d'origine et identifiant gardés pour
        # le diagnostic ; `code` reste `http_<status>` (contrat des appelants inchangé).
        pool = pool_error_from_response(resp)
        _log("error", error_class=f"HTTP{resp.status_code}" + (f":{pool.code}" if pool else ""),
             latency_ms=latency_ms)
        raise GatewayEmbeddingsError(
            f"Gateway /v1/embeddings HTTP {resp.status_code}"
            + (f" pool_code={pool.code} request_id={pool.request_id or '-'}" if pool else "")
            + f": {snippet}",
            code=f"http_{resp.status_code}",
            pool_error=pool,
        )

    try:
        data = resp.json()
    except ValueError as exc:
        _bump("embedding_gateway_errors_total")
        _log("error", error_class="InvalidJSON", latency_ms=latency_ms)
        raise GatewayEmbeddingsError("réponse Gateway non JSON", code="bad_payload") from exc

    def _resolve(model: str, force_refresh: bool = False) -> PipelineReference:
        return pipeline_reference_for(
            model, base_url=base_url, timeout_s=timeout_s, force_refresh=force_refresh
        )

    try:
        batch = _validate_response(
            data,
            expected_model_logical=model_logical,
            expected_task=task,
            expected_dimension=int(dim),
            n_texts=len(texts),
            resolve_reference=_resolve,
        )
    except GatewayEmbeddingsError as exc:
        _log("error", error_class=exc.code, latency_ms=latency_ms)
        raise
    batch = EmbeddingBatch(
        vectors=batch.vectors,
        pipeline_id=batch.pipeline_id,
        dimension=batch.dimension,
        task=batch.task,
        model_logical=batch.model_logical,
        model_digest=batch.model_digest,
        governed=True,
        latency_ms=latency_ms,
        truncated=batch.truncated,
        family=batch.family,
    )
    _log("ok", latency_ms=latency_ms, pipeline_id=batch.pipeline_id, family=batch.family)
    return batch


__all__ = [
    "PROVIDER_AI_GATEWAY",
    "EmbeddingBatch",
    "PipelineReference",
    "GatewayEmbeddingsError",
    "TASK_DOCUMENT",
    "TASK_QUERY",
    "build_gateway_embeddings_request",
    "counters_snapshot",
    "embed_via_gateway",
    "gateway_embeddings_enabled",
    "reset_counters",
]
