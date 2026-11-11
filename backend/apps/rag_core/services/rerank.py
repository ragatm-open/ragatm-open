from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

from django.conf import settings

from apps.rag_core.domain.ranking import sort_candidates

logger = logging.getLogger("rag.rerank")


# =========================
# Utils communs
# =========================

def _normalize_question_terms(question: str) -> set[str]:
    q = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in question).split()
    return {t for t in q if len(t) >= 4}


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if max_chars <= 0:
        return text
    return text[:max_chars]


# =========================
# Providers reconnus
# =========================

#: Seules valeurs acceptées pour ``RAG_RERANK_PROVIDER``. Toute autre valeur est une
#: erreur de configuration : on retombe sur le stub, mais en le SIGNALANT
#: (``fallback_used=True``). Sans cela, un ``local`` ou un ``crossencoder`` mal
#: orthographié dégrade silencieusement la qualité du reranking, la trace étant
#: indistinguable d'un stub légitimement configuré (dette D1).
RERANK_PROVIDERS = frozenset({"stub", "cross_encoder"})


def normalize_rerank_provider(raw: Any) -> str:
    """Normalise la valeur brute du provider (casse et espaces indifférents)."""
    return str(raw or "stub").lower().strip()


# =========================
# Stub déterministe (fallback)
# =========================

def _stub_rerank(*, question: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Rerank minimal déterministe.
    Utilisé comme fallback safe si le reranker réel est indisponible.
    """
    q_terms = _normalize_question_terms(question)

    def bonus(text: str) -> float:
        t = (text or "").lower()
        hits = sum(1 for term in q_terms if term in t)
        return float(hits) * 0.05  # petit bonus stable

    reranked: list[dict[str, Any]] = []
    for c in chunks:
        base = float(c.get("score", 0.0) or 0.0)
        b = bonus(c.get("text", ""))
        c2 = dict(c)
        c2["rerank_score"] = base + b
        reranked.append(c2)

    # `rerank_score` puis `score` n'ordonnent pas totalement — le stub, chemin PAR
    # DÉFAUT, produit une grille très discrète (score + 0,05 x nb de termes trouvés) :
    # deux chunks au même ts_rank ex æquo et au même nombre de hits sont strictement
    # égaux sur les deux clés. Le départage documentaire clôt l'ordre.
    # Cf. domain/ranking.py.
    reranked = sort_candidates(
        reranked,
        relevance_key=lambda x: (
            float(x.get("rerank_score", 0.0) or 0.0),
            float(x.get("score", 0.0) or 0.0),
        ),
    )
    return reranked


# =========================
# Cross-encoder (GPU-ready)
# =========================

@dataclass(frozen=True)
class _CrossEncoderConfig:
    model_name: str
    device: str
    batch_size: int
    max_text_chars: int


_CROSS_ENCODER_MODEL: Any | None = None
_CROSS_ENCODER_SIGNATURE: tuple[str, str] | None = None


def _get_cross_encoder(cfg: _CrossEncoderConfig):
    """
    Lazy singleton du modèle cross-encoder.
    Rechargé uniquement si (model_name, device) changent.
    """
    global _CROSS_ENCODER_MODEL, _CROSS_ENCODER_SIGNATURE

    sig = (cfg.model_name, cfg.device)
    if _CROSS_ENCODER_MODEL is not None and _CROSS_ENCODER_SIGNATURE == sig:
        return _CROSS_ENCODER_MODEL

    # Import tardif (extra optionnel "rerank")
    from sentence_transformers import CrossEncoder  # type: ignore

    _CROSS_ENCODER_MODEL = CrossEncoder(cfg.model_name, device=cfg.device)
    _CROSS_ENCODER_SIGNATURE = sig
    return _CROSS_ENCODER_MODEL


def _iter_pairs(
    question: str,
    chunks: Iterable[dict[str, Any]],
    max_text_chars: int,
) -> list[tuple[str, str]]:
    return [
        (question, _truncate(str(ch.get("text", "")), max_text_chars))
        for ch in chunks
    ]


def _cross_encoder_rerank(*, question: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cfg = _CrossEncoderConfig(
        model_name=getattr(settings, "RAG_RERANK_MODEL_NAME", "BAAI/bge-reranker-base"),
        device=getattr(settings, "RAG_RERANK_DEVICE", "cuda"),
        batch_size=int(getattr(settings, "RAG_RERANK_BATCH_SIZE", 16)),
        max_text_chars=int(getattr(settings, "RAG_RERANK_MAX_TEXT_CHARS", 2000)),
    )

    model = _get_cross_encoder(cfg)
    pairs = _iter_pairs(question, chunks, cfg.max_text_chars)
    scores = model.predict(pairs, batch_size=cfg.batch_size)

    reranked: list[dict[str, Any]] = []
    for ch, s in zip(chunks, scores):
        c2 = dict(ch)
        try:
            c2["rerank_score"] = float(s)
        except Exception:
            c2["rerank_score"] = 0.0
        reranked.append(c2)

    # `rerank_score` puis `score` n'ordonnent pas totalement — le stub, chemin PAR
    # DÉFAUT, produit une grille très discrète (score + 0,05 x nb de termes trouvés) :
    # deux chunks au même ts_rank ex æquo et au même nombre de hits sont strictement
    # égaux sur les deux clés. Le départage documentaire clôt l'ordre.
    # Cf. domain/ranking.py.
    reranked = sort_candidates(
        reranked,
        relevance_key=lambda x: (
            float(x.get("rerank_score", 0.0) or 0.0),
            float(x.get("score", 0.0) or 0.0),
        ),
    )
    return reranked


# =========================
# Observabilité (Priorité B4)
# =========================

@dataclass
class RerankInfo:
    """Trace d'exécution d'un rerank, pour audit/observabilité.

    - ``requested_provider`` : provider demandé par la config ;
    - ``effective_provider`` : provider réellement utilisé (``stub`` si fallback) ;
    - ``fallback_used``      : True si cross_encoder a échoué et qu'on est retombé sur stub ;
    - ``model``              : modèle effectif (cross-encoder) ou ``""`` pour le stub ;
    - ``n_in`` / ``n_out``   : nombre de chunks en entrée / sortie du rerank ;
    - ``latency_ms``         : latence du rerank seul ;
    - ``error``              : message d'erreur si fallback déclenché.
    """

    requested_provider: str
    effective_provider: str
    fallback_used: bool = False
    model: str = ""
    n_in: int = 0
    n_out: int = 0
    latency_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_provider": self.requested_provider,
            "effective_provider": self.effective_provider,
            "fallback_used": self.fallback_used,
            "model": self.model,
            "n_in": self.n_in,
            "n_out": self.n_out,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
        }


# =========================
# API publique
# =========================

def rerank_chunks_observed(
    *, question: str, chunks: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], RerankInfo]:
    """Comme ``rerank_chunks`` mais renvoie aussi une ``RerankInfo`` observable.

    Détecte explicitement le fallback cross_encoder -> stub (dégradation
    silencieuse, le risque #1 du rerank en prod sans GPU) et le journalise.
    Ne lève jamais d'exception.
    """
    requested = normalize_rerank_provider(getattr(settings, "RAG_RERANK_PROVIDER", "stub"))
    n_in = len(chunks or [])
    start = time.perf_counter()

    if requested not in RERANK_PROVIDERS:
        # Valeur de configuration invalide : le stub tourne, mais c'est une DÉGRADATION,
        # pas un choix. On la signale comme telle pour qu'une alerte sur `fallback_used`
        # la voie, et que le log la distingue d'un stub légitime.
        reranked = _stub_rerank(question=question, chunks=chunks)
        info = RerankInfo(
            requested_provider=requested,
            effective_provider="stub",
            fallback_used=True,
            n_in=n_in,
            n_out=len(reranked),
            latency_ms=(time.perf_counter() - start) * 1000.0,
            error=(
                f"provider de rerank inconnu: {requested!r} "
                f"(valeurs acceptées: {', '.join(sorted(RERANK_PROVIDERS))})"
            ),
        )
        logger.warning(
            "rerank provider inconnu requested_provider=%s effective_provider=%s "
            "-> stub (dégradation silencieuse évitée) ; %s",
            info.requested_provider,
            info.effective_provider,
            info.error,
        )
    elif requested == "cross_encoder":
        try:
            reranked = _cross_encoder_rerank(question=question, chunks=chunks)
            info = RerankInfo(
                requested_provider=requested,
                effective_provider="cross_encoder",
                model=str(getattr(settings, "RAG_RERANK_MODEL_NAME", "")),
                n_in=n_in,
                n_out=len(reranked),
                latency_ms=(time.perf_counter() - start) * 1000.0,
            )
        except Exception as exc:  # dépendance absente / CUDA indispo / OOM…
            reranked = _stub_rerank(question=question, chunks=chunks)
            info = RerankInfo(
                requested_provider=requested,
                effective_provider="stub",
                fallback_used=True,
                n_in=n_in,
                n_out=len(reranked),
                latency_ms=(time.perf_counter() - start) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.warning(
                "rerank cross_encoder fallback->stub (%s) ; vérifier sentence-transformers/CUDA",
                info.error,
            )
    else:
        reranked = _stub_rerank(question=question, chunks=chunks)
        info = RerankInfo(
            requested_provider=requested,
            effective_provider="stub",
            n_in=n_in,
            n_out=len(reranked),
            latency_ms=(time.perf_counter() - start) * 1000.0,
        )

    # `requested_provider` est tracé : sans lui, "provider=stub fallback=False" est
    # indistinguable d'un stub légitimement configuré (dette D1).
    logger.info(
        "rerank done requested_provider=%s effective_provider=%s fallback=%s "
        "n_in=%d n_out=%d latency_ms=%.1f",
        info.requested_provider,
        info.effective_provider,
        info.fallback_used,
        info.n_in,
        info.n_out,
        info.latency_ms,
    )
    return reranked, info


def rerank_chunks(*, question: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Rerank chunks selon le provider configuré.

    Providers:
      - "stub"          : heuristique déterministe (fallback safe)
      - "cross_encoder" : rerank local HF (GPU-ready)

    Garantie:
      - ne lève jamais d’exception
      - fallback automatique vers le stub si dépendances absentes / CUDA indisponible

    Compat: conserve la signature historique (liste seule). Pour l'observabilité,
    préférer ``rerank_chunks_observed``.
    """
    reranked, _info = rerank_chunks_observed(question=question, chunks=chunks)
    return reranked
