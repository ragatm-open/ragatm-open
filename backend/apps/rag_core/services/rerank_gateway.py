"""Reranking de passages via l'AI Gateway — client durable, sous drapeau.

RagATM ne connaît **que** le contrat de la Gateway et un **nom logique** de modèle. Ni le
dépôt Hugging Face, ni sa révision, ni l'implémentation du backend, ni son port n'entrent
dans ce module : la Gateway reste la source de vérité de l'identité physique, et le produit
n'a pas à la dupliquer.

## Pourquoi ce module trie lui-même le pool

Le piège que la campagne a rencontré trois fois : après la fusion, l'étape d'hygiène
**re-trie** le pool par ``score`` brut. La liste ``chunks`` n'est donc **pas** dans l'ordre
RRF, et un ``chunks[:N]`` naïf prélève un autre sous-ensemble. Mesuré lors du replay :
2 questions sur 63 seulement recevaient le bon pool, et 17 vérités sur 59 n'atteignaient
jamais le reranker — la couverture tombait *sous* la baseline.

``select_rrf_pool`` impose donc la clé canonique ``rrf_sort_key``. Elle n'est pas une
commodité : c'est l'invariant qui rend la régression impossible.

## Politique d'échec

Configurable, et **jamais silencieuse**. En ``fail_open_rrf``, l'ordre RRF d'origine est
conservé et le repli est compté ET journalisé ; en ``fail_closed``, l'erreur remonte.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

from apps.rag_core.services import ops_counters
from apps.rag_core.services.query.diversify import rrf_sort_key
from apps.llm_router.gateway.request_id import correlation_headers

logger = logging.getLogger("rag.rerank.gateway")

#: Politiques d'échec acceptées. Toute autre valeur est refusée à la lecture.
FAILURE_POLICIES = frozenset({"fail_open_rrf", "fail_closed"})

#: Noms des compteurs exposés. Cardinalité BORNÉE par construction : aucune dimension
#: libre (ni question, ni identifiant de fragment, ni utilisateur).
COUNTER_NAMES = (
    "rag_reranker_attempted_total",
    "rag_reranker_success_total",
    "rag_reranker_failure_total",
    "rag_reranker_fallback_total",
    "rag_reranker_timeout_total",
    "rag_reranker_invalid_response_total",
    "rag_reranker_documents_total",
    "rag_reranker_duration_ms_total",
)


#: Catégories d'échec du client, dérivées de `error_kind`. Elles existaient déjà —
#: mais seulement comme compteurs agrégés, donc irrattachables à une requête.
#: `timeout` et `reponse non conforme` se corrigent différemment : les confondre sous
#: un « échec » générique fait perdre l'information au moment où elle sert.
OUTCOME_SUCCESS = "success"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_INVALID_RESPONSE = "invalid_response"
OUTCOME_FAILURE = "failure"
OUTCOME_NOT_ATTEMPTED = "not_attempted"

#: Préfixes d'erreur produits par `_validate` — une réponse structurellement fausse.
_INVALID_RESPONSE_PREFIXES = ("cardinalite", "identifiant", "score", "reponse")


def classify_outcome(info: dict[str, Any]) -> str:
    """Issue de l'appel, à partir de l'`info` déjà produite. Ne recalcule rien.

    Volontairement DISTINCTE de `fallback` : l'issue dit ce qui est arrivé à l'appel,
    `fallback` dit ce que le pipeline en a fait. Les fondre — un état « FALLBACK » —
    effacerait la cause, alors que c'est exactement ce qu'un exploitant cherche.
    """
    if info.get("success"):
        return OUTCOME_SUCCESS
    if not info.get("attempted"):
        return OUTCOME_NOT_ATTEMPTED
    kind = str(info.get("error_kind") or "")
    if kind == "timeout":
        return OUTCOME_TIMEOUT
    if kind.startswith(_INVALID_RESPONSE_PREFIXES):
        return OUTCOME_INVALID_RESPONSE
    return OUTCOME_FAILURE


class RerankGatewayError(RuntimeError):
    """Reranking impossible ou réponse non conforme."""


def _bump(name: str, value: int = 1) -> None:
    """Incrémente un compteur PARTAGÉ entre workers (cf. `ops_counters`)."""
    ops_counters.incr(name, value)


class _Counters:
    """Accumule les compteurs d'UN événement et les écrit en **une seule fois**.

    Pourquoi grouper : le serveur Redis persiste en `appendfsync everysec`, et une
    écriture peut attendre la fin d'une `fsync` en cours. Quatre écritures séparées
    offrent quatre occasions de heurter cette attente ; une seule n'en offre qu'une.
    S'y ajoute la disparition d'un `EXISTS` par incrément, que `django_redis.cache.incr`
    impose et que `INCRBY` rend inutile.

    Effet secondaire souhaitable : `attempted`, `success`, `failure` et `fallback` d'un
    même événement atteignent le magasin **ensemble**, donc un lecteur ne peut plus
    observer `attempted` sans son issue.
    """

    __slots__ = ("_p",)

    def __init__(self) -> None:
        self._p: dict[str, int] = {}

    def add(self, name: str, value: int = 1) -> None:
        self._p[name] = self._p.get(name, 0) + int(value)

    def flush(self) -> None:
        if not self._p:
            return
        pending, self._p = self._p, {}
        ops_counters.incr_many(pending)


def counters_snapshot() -> dict[str, int]:
    return ops_counters.snapshot(list(COUNTER_NAMES))


def reset_counters() -> None:
    """Remise à zéro — réservée aux tests."""
    ops_counters.reset(list(COUNTER_NAMES))


def reranker_enabled() -> bool:
    """Drapeau du reranking. OFF par défaut, partout."""
    return bool(getattr(settings, "RAG_RERANKER_ENABLED", False))


def failure_policy() -> str:
    raw = str(getattr(settings, "RAG_RERANKER_FAILURE_POLICY", "fail_open_rrf") or "").strip().lower()
    return raw if raw in FAILURE_POLICIES else "fail_open_rrf"


def model_logical() -> str:
    return str(getattr(settings, "RAG_RERANKER_MODEL_LOGICAL", "rerank-default") or "rerank-default")


def pool_k() -> int:
    try:
        return max(1, int(getattr(settings, "RAG_RERANKER_POOL_K", 40)))
    except (TypeError, ValueError):
        return 40


def max_per_group() -> int:
    """Plafond par document appliqué **uniquement** quand le reranking est actif.

    Séparé du plafond historique : le drapeau OFF ne doit rien changer à la baseline.
    """
    try:
        return max(1, int(getattr(settings, "RAG_RERANKER_MAX_PER_GROUP", 3)))
    except (TypeError, ValueError):
        return 3


def _base_url() -> str:
    base = str(getattr(settings, "RAG_RERANKER_BASE_URL", "") or "").rstrip("/")
    if not base:
        raise RerankGatewayError("RAG_RERANKER_BASE_URL absente")
    return base


def _timeout() -> float:
    try:
        return float(getattr(settings, "RAG_RERANKER_TIMEOUT_SECONDS", 2.0))
    except (TypeError, ValueError):
        return 2.0


def select_rrf_pool(chunks: list[dict[str, Any]], k: int | None = None) -> tuple[list, list]:
    """Rend ``(tête, reste)`` selon l'ordre RRF **canonique**, jamais l'ordre courant.

    C'est le point de passage obligé avant tout reranking : l'ordre de la liste reçue est
    celui de l'hygiène, pas celui du classement.
    """
    n = pool_k() if k is None else max(1, int(k))
    ordered = sorted(list(chunks or []), key=rrf_sort_key, reverse=True)
    return ordered[:n], ordered[n:]


def _pool_code(exc) -> str:
    """Code ``X-AIGP-Error`` d'une erreur HTTP urllib, ou ''. Le corps n'est pas lu."""
    from apps.llm_router.gateway.pool_errors import parse_pool_error

    try:
        info = parse_pool_error(int(exc.code), exc.headers, None)
    except Exception:  # noqa: BLE001 - diagnostic seulement
        return ""
    return info.code if info is not None else ""


def _post_rerank(question: str, docs: list[dict[str, str]]) -> tuple[dict, float, int]:
    body = {
        "query": str(question or "")[:2000],
        "documents": docs,
        "model": model_logical(),
        "top_n": len(docs),
    }
    req = urllib.request.Request(
        _base_url() + "/v1/rerank",
        data=json.dumps(body).encode("utf-8"),
        # Corrélation (qualification) : `X-Request-ID` de la requête HTTP en cours, si présent.
        headers={"Content-Type": "application/json", **correlation_headers()},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            status = resp.getcode()
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Refus PROPRE au pool : son code (MAJUSCULES, métadonnée du pool) est ajouté au
        # motif ; le préfixe `HTTP <statut>` des compteurs et journaux est inchangé.
        pool_code = _pool_code(exc)
        raise RerankGatewayError("HTTP %s" % exc.code + (" pool=%s" % pool_code if pool_code else "")) from exc
    except json.JSONDecodeError as exc:
        raise RerankGatewayError("reponse non JSON") from exc
    except TimeoutError as exc:
        raise RerankGatewayError("timeout") from exc
    except Exception as exc:  # noqa: BLE001 — DNS, socket, URLError…
        # `socket.timeout` est un alias de TimeoutError depuis 3.10, mais urllib peut
        # emballer le dépassement dans une URLError : on regarde le motif.
        if "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower():
            raise RerankGatewayError("timeout") from exc
        raise RerankGatewayError("%s" % type(exc).__name__) from exc
    return payload, (time.perf_counter() - t0) * 1000.0, status


def _validate(payload: dict, docs: list[dict[str, str]]) -> dict[str, float]:
    """Refuse toute réponse partielle. Un reranking incomplet se présente comme complet."""
    results = payload.get("results")
    if not isinstance(results, list):
        raise RerankGatewayError("reponse sans liste results")
    if len(results) != len(docs):
        raise RerankGatewayError("cardinalite %d/%d" % (len(results), len(docs)))
    sent = {d["id"] for d in docs}
    scores: dict[str, float] = {}
    for row in results:
        if not isinstance(row, dict):
            raise RerankGatewayError("resultat non-objet")
        rid = str(row.get("id") or "")
        if rid not in sent:
            raise RerankGatewayError("identifiant inconnu")
        if rid in scores:
            raise RerankGatewayError("identifiant duplique")
        try:
            val = float(row.get("relevance_score"))
        except (TypeError, ValueError) as exc:
            raise RerankGatewayError("score non numerique") from exc
        if val != val or val in (float("inf"), float("-inf")):
            raise RerankGatewayError("score non fini")
        scores[rid] = val
    if len(scores) != len(docs):
        raise RerankGatewayError("scores manquants")
    return scores


def rerank_chunks(*, question: str, chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Réordonne le pool par pertinence. UN SEUL appel HTTP pour N passages.

    Rend ``(chunks, info)``. En cas d'échec sous ``fail_open_rrf``, rend l'ordre RRF
    d'origine avec ``fallback=True`` — jamais silencieusement : le compteur et le journal
    le disent.
    """
    info: dict[str, Any] = {
        "attempted": False, "success": False, "fallback": False,
        "documents": 0, "duration_ms": None, "model_logical": model_logical(),
        "policy": failure_policy(), "error_kind": None,
    }
    head, tail = select_rrf_pool(chunks)
    if not head:
        return list(chunks or []), info

    docs, seen = [], set()
    for c in head:
        cid = str(c.get("chunk_id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        docs.append({"id": cid, "text": str(c.get("text") or "")[:4000]})
    if not docs:
        return list(chunks or []), info

    info["attempted"] = True
    info["documents"] = len(docs)
    info["pool_k"] = pool_k()
    counters = _Counters()
    counters.add("rag_reranker_attempted_total")
    try:
        payload, duration_ms, status = _post_rerank(question, docs)
        scores = _validate(payload, docs)
    except RerankGatewayError as exc:
        kind = str(exc)[:80]
        info["error_kind"] = kind
        counters.add("rag_reranker_failure_total")
        # Sous-catégories : un exploitant doit distinguer une Gateway lente d'une
        # réponse non conforme — les deux se corrigent différemment.
        if kind == "timeout":
            counters.add("rag_reranker_timeout_total")
        elif kind.startswith(("cardinalite", "identifiant", "score", "reponse")):
            counters.add("rag_reranker_invalid_response_total")
        logger.warning(
            "rag_rerank_failed policy=%s documents=%d error=%s",
            info["policy"], len(docs), info["error_kind"],
        )
        if info["policy"] == "fail_closed":
            counters.flush()
            raise
        info["fallback"] = True
        counters.add("rag_reranker_fallback_total")
        counters.flush()
        # Repli EXPLICITE sur l'ordre RRF canonique — pas sur l'ordre reçu.
        return head + tail, info

    info["success"] = True
    info["duration_ms"] = round(duration_ms, 1)
    info["http_status"] = status
    info["model"] = payload.get("model")
    info["model_revision"] = payload.get("model_revision")
    info["upstream_provider"] = payload.get("upstream_provider")
    counters.add("rag_reranker_success_total")
    counters.add("rag_reranker_documents_total", len(docs))
    counters.add("rag_reranker_duration_ms_total", int(duration_ms))
    counters.flush()
    logger.info(
        "rag_rerank_ok documents=%d duration_ms=%.1f model_logical=%s",
        len(docs), duration_ms, info["model_logical"],
    )
    reordered = _apply_order(head, scores)
    # OBSERVABILITÉ UNIQUEMENT : `info` s'enrichit, la liste rendue est inchangée.
    info["returned"] = len(scores)
    info["movements"] = _movements(head, reordered)
    return reordered + tail, info


def _movements(
    head: list[dict[str, Any]], reordered: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mouvements de rang RRF -> reranking, pour la trace. N'influence RIEN.

    Le rang AVANT est la position dans ``head``, c'est-à-dire le pool **canonique RRF**
    réellement soumis à la Gateway — jamais l'ordre physique de la liste reçue par
    l'orchestrateur, qui est celui de l'hygiène. C'est la confusion que
    ``select_rrf_pool`` existe pour empêcher, et la tracer autrement la réintroduirait.

    Le rang APRÈS est celui rendu par la Gateway (``rerank_rank``), donc **avant**
    diversification et avant tout reranking historique. Il ne dit pas la position
    finale au prompt.

    Volumétrie BORNÉE par construction : ``head`` vaut au plus ``pool_k`` (plafonné à
    64 par le registre). Aucune croissance libre.

    Identifiants seulement — aucun texte de passage, aucun contenu documentaire.
    """
    before = {str(c.get("chunk_id")): i + 1 for i, c in enumerate(head)}
    out: list[dict[str, Any]] = []
    for c in reordered:
        cid = str(c.get("chunk_id") or "")
        if not cid:
            continue
        rank_before = before.get(cid)
        rank_after = c.get("rerank_rank")
        entry: dict[str, Any] = {
            "chunk_id": cid,
            "document_id": str(c.get("document_id") or "") or None,
            "before_rank": rank_before,
            "after_rank": rank_after,
        }
        if isinstance(rank_before, int) and isinstance(rank_after, int):
            # Positif = remonté. Un candidat passé du rang 12 au rang 3 a gagné 9.
            entry["delta"] = rank_before - rank_after
        score = c.get("rerank_score")
        # Score NATIF, jamais normalisé ; absent plutôt que 0 quand la Gateway n'en
        # a pas rendu — un 0 se lirait comme « jugé non pertinent ».
        if score is not None:
            entry["score"] = score
        out.append(entry)
    return out


def _apply_order(head: list[dict[str, Any]], scores: dict[str, float]) -> list[dict[str, Any]]:
    """ENRICHIT le pool reranké : ``rerank_score`` réel et ``rerank_rank``, sur des copies.

    Le reranking n'écrase PLUS ``score`` ni ``fusion_score``. Il les laisse porter ce
    qu'ils ont toujours voulu dire — la pertinence du retrieval avant reranking — et
    ajoute ses propres champs à côté.

    Auparavant, la clé de tri était imposée en réécrivant ``score`` et ``fusion_score``
    avec un ordinal (``n - i``). C'était efficace mais faux : un rang déguisé en score.
    Un seuil aval qui lisait ``score`` comparait alors un rang à une pertinence — voir
    ``rerank_rank`` dans ``diversify._rrf_key``, qui porte désormais cet ordre
    EXPLICITEMENT.

    Départage : à ``rerank_score`` égal, l'ordre d'entrée est conservé — ``head`` arrive
    trié par la clé RRF canonique (``select_rrf_pool``) et ``sorted`` est stable. Le
    résultat est donc déterministe, sans dépendre de l'ordre d'un dictionnaire ni de
    celui de la réponse HTTP.
    """
    ordered = sorted(head, key=lambda c: scores.get(str(c.get("chunk_id")), float("-inf")), reverse=True)
    out = []
    for i, c in enumerate(ordered):
        cc = dict(c)
        cc["rerank_score"] = scores.get(str(c.get("chunk_id")))
        cc["rerank_rank"] = i + 1
        out.append(cc)
    return out


def provenance() -> dict[str, Any]:
    """Identité observable de la Gateway, par HTTP. Ne lève jamais.

    Diagnostic d'exploitation : une métadonnée absente ne doit pas faire échouer une
    requête utilisateur tant que le reranking fonctionne.

    Lit SEULEMENT ``/health/runtime`` (non inférentielle). ``/health/rerank`` calcule un vrai
    reclassement (contrat AI Gateway Pool, ``gpu_compute``) : jamais appelée par un diagnostic.
    ``rerank_health`` vaut ``declared`` quand le registre
    publie le reranking actif — une déclaration, pas un test.
    """
    out: dict[str, Any] = {"reachable": False}
    for path, key in (("/health/runtime", "runtime"),):
        try:
            with urllib.request.urlopen(_base_url() + path, timeout=_timeout()) as resp:
                out[key] = json.loads(resp.read().decode("utf-8"))
                out["reachable"] = True
        except Exception as exc:  # noqa: BLE001 - diagnostic, jamais bloquant
            out[key] = {"error": type(exc).__name__}
    rt = out.get("runtime") or {}
    build = rt.get("build") or {}
    models = ((rt.get("rerank") or {}).get("models") or [{}])
    out["summary"] = {
        "environment": rt.get("environment"),
        "gateway_git_sha": build.get("git_sha"),
        "gateway_version": build.get("version"),
        "gateway_image_digest": build.get("image_digest"),
        "backend_implementation": models[0].get("provider"),
        "model_artifact": models[0].get("artifact"),
        "model_revision": models[0].get("revision"),
        "rerank_health": ("declared" if (rt.get("rerank") or {}).get("enabled") and models[0].get("artifact")
                          else None),
    }
    return out


__all__ = [
    "COUNTER_NAMES", "FAILURE_POLICIES", "RerankGatewayError", "counters_snapshot", "failure_policy",
    "max_per_group", "model_logical", "pool_k", "provenance", "rerank_chunks",
    "reranker_enabled", "reset_counters", "select_rrf_pool",
]
