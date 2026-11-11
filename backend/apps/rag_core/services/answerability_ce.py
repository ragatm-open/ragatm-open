"""Answerability par cross-encoder — P2.0.

**Séparé du reranking, et ce n'est pas un détail.** Le reranking ORDONNE des candidats ;
l'answerability décide s'il faut RÉPONDRE. Les deux peuvent utiliser le même modèle, mais
les confondre reviendrait à dire « le meilleur candidat est forcément une réponse » — ce
qui est faux précisément dans le cas qui nous occupe : une question plausible dont la
propriété demandée est absente du corpus. Ce module s'active donc indépendamment de
`RAG_RERANK_ENABLED` et ne réordonne jamais rien.

Pourquoi ce levier après l'échec de `RAG_MIN_TEXT_RANK` (P1.9-f) : un filtre lexical compare
la question au **corpus** ; sur le corpus terrain les questions no-answer y matchent *mieux*
que les answerable (médiane 0.530 vs 0.236), car elles portent sur des entités réellement
présentes. Un cross-encoder compare la question à la **réponse candidate** — la seule
information que ni le lexique ni le vecteur ne portent.

Modèle : `BAAI/bge-reranker-base`, **local**, `num_labels=1`, activation **sigmoïde** →
score dans [0, 1] (et non un logit : la grille de seuils en dépend). Aucun accès réseau à
l'exécution, aucun repli vers une API distante, aucun repli vers le LLM génératif.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from django.conf import settings

logger = logging.getLogger(__name__)

# Stratégies d'agrégation (§6).
STRATEGY_MAX = "max"                     # A — score maximal
STRATEGY_MEAN_TOP2 = "mean_top2"         # B — moyenne des 2 meilleurs
STRATEGY_MAX_GUARDED = "max_guarded"     # C — max + garde retrieval
STRATEGY_COMPOSITE = "composite"         # D — max + protection identifiant fort
STRATEGIES = (STRATEGY_MAX, STRATEGY_MEAN_TOP2, STRATEGY_MAX_GUARDED, STRATEGY_COMPOSITE)

# Décisions
ANSWERABLE = "answerable"
NO_ANSWER = "no_answer"
UNAVAILABLE = "unavailable"


class AnswerabilityUnavailable(RuntimeError):
    """Le modèle est absent ou inutilisable. JAMAIS « answerable par défaut » en silence."""


# --------------------------------------------------------------------------
# Chargement du modèle : paresseux, une fois par processus, verrouillé
# --------------------------------------------------------------------------
_MODEL: Any | None = None
_SIGNATURE: tuple[str, str] | None = None
_LOCK = threading.Lock()


def _resolve_device(requested: str) -> str:
    """`cuda` demandé mais absent → CPU, tracé. Ne jamais échouer pour ça : le modèle
    tourne très bien sur CPU (~27 ms/paire), et un refus massif serait pire."""
    if requested != "cuda":
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


def get_model(model_name: str, device: str):
    """Singleton paresseux. Rechargé seulement si (modèle, device) change."""
    global _MODEL, _SIGNATURE

    sig = (model_name, device)
    if _MODEL is not None and _SIGNATURE == sig:
        return _MODEL
    with _LOCK:
        if _MODEL is not None and _SIGNATURE == sig:
            return _MODEL
        try:
            from sentence_transformers import CrossEncoder
        except Exception as exc:  # noqa: BLE001
            raise AnswerabilityUnavailable(
                "sentence-transformers absent : installez l'extra « rerank » "
                '(pip install -e ".[rerank]").'
            ) from exc
        try:
            _MODEL = CrossEncoder(model_name, device=device)
        except Exception as exc:  # noqa: BLE001
            raise AnswerabilityUnavailable(
                f"modèle d'answerability « {model_name} » indisponible localement : {exc}"
            ) from exc
        _SIGNATURE = sig
        logger.info("answerability cross-encoder chargé: %s (%s)", model_name, device)
        return _MODEL


def reset_model_cache() -> None:
    """Réinitialise le singleton (tests)."""
    global _MODEL, _SIGNATURE
    with _LOCK:
        _MODEL, _SIGNATURE = None, None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AnswerabilityConfig:
    enabled: bool = False
    provider: str = "cross_encoder"
    model: str = "BAAI/bge-reranker-base"
    device: str = "cpu"
    threshold: float = 0.5
    top_k: int = 5
    max_text_chars: int = 2000
    timeout_s: float = 3.0
    fail_open: bool = True
    strategy: str = STRATEGY_MAX

    @classmethod
    def from_settings(cls) -> AnswerabilityConfig:
        g = lambda k, d: getattr(settings, k, d)  # noqa: E731
        return cls(
            enabled=bool(g("RAG_ANSWERABILITY_ENABLED", False)),
            provider=str(g("RAG_ANSWERABILITY_PROVIDER", "cross_encoder")),
            model=str(g("RAG_ANSWERABILITY_MODEL", "BAAI/bge-reranker-base")),
            device=_resolve_device(str(g("RAG_ANSWERABILITY_DEVICE", "cpu"))),
            threshold=float(g("RAG_ANSWERABILITY_THRESHOLD", 0.5)),
            top_k=int(g("RAG_ANSWERABILITY_TOP_K", 5)),
            max_text_chars=int(g("RAG_ANSWERABILITY_MAX_TEXT_CHARS", 2000)),
            timeout_s=float(g("RAG_ANSWERABILITY_TIMEOUT_SECONDS", 3.0)),
            fail_open=bool(g("RAG_ANSWERABILITY_FAIL_OPEN", True)),
            strategy=str(g("RAG_ANSWERABILITY_STRATEGY", STRATEGY_MAX)),
        )


@dataclass
class AnswerabilityResult:
    decision: str
    score: float | None
    threshold: float
    strategy: str
    model: str
    device: str
    top_k: int
    n_pairs: int
    latency_ms: float
    fallback_used: bool = False
    error: str | None = None
    strong_id_override: bool = False
    scores: list[float] = field(default_factory=list)

    def as_log(self) -> dict[str, Any]:
        """Observabilité (§22) : jamais de contenu de chunk ni de question."""
        return {
            "answerability_provider": "cross_encoder",
            "answerability_model": self.model,
            "answerability_device": self.device,
            "threshold": self.threshold,
            "top_k": self.top_k,
            "strategy": self.strategy,
            "max_score": self.score,
            "decision": self.decision,
            "latency_ms": round(self.latency_ms, 1),
            "n_pairs": self.n_pairs,
            "fallback_used": self.fallback_used,
            "strong_id_override": self.strong_id_override,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Identifiants forts (§13)
# --------------------------------------------------------------------------
_STRONG_ID_RE = re.compile(
    r"\b(?:[A-Z]{1,10}[- ]?\d{1,4}(?:[-/]\d{1,4})+|[A-Z]{2,6}\d{2,4}(?:[-/][A-Z0-9]{1,8})*"
    r"|Series\s+[A-Z]-\d+|[A-Z]{2,5}-\d{4}-\d{2})\b"
)


def extract_strong_ids(text: str) -> set[str]:
    return {m.group(0).strip() for m in _STRONG_ID_RE.finditer(text or "")}


def has_exact_strong_id_match(question: str, chunks: Sequence[dict[str, Any]]) -> bool:
    """Un identifiant fort de la question figure-t-il EXACTEMENT dans un chunk candidat ?

    Un identifiant exact est une preuve dure : un score cross-encoder faible ne doit pas
    seul provoquer un refus dans ce cas. La règle est mesurée (stratégie D vs A), jamais
    posée d'office.
    """
    ids = extract_strong_ids(question)
    if not ids:
        return False
    for ch in chunks:
        text = str(ch.get("text") or "")
        if any(i in text for i in ids):
            return True
    return False


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def _truncate(text: str, limit: int) -> str:
    t = text or ""
    return t if len(t) <= limit else t[:limit]


def score_pairs(question: str, chunks: Sequence[dict[str, Any]], cfg: AnswerabilityConfig) -> list[float]:
    """Scores sigmoïdes [0,1] pour chaque (question, chunk). Ordre préservé."""
    if not chunks:
        return []
    model = get_model(cfg.model, cfg.device)
    pairs = [(question, _truncate(str(ch.get("text") or ""), cfg.max_text_chars)) for ch in chunks]
    raw = model.predict(pairs, batch_size=min(len(pairs), 16), show_progress_bar=False)
    out: list[float] = []
    for s in raw:
        try:
            v = float(s)
        except (TypeError, ValueError):
            raise AnswerabilityUnavailable(f"score invalide renvoyé par le modèle : {s!r}")
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
            raise AnswerabilityUnavailable(f"score non fini renvoyé par le modèle : {v!r}")
        out.append(v)
    return out


def aggregate(scores: Sequence[float], strategy: str) -> float:
    """Agrège les scores des candidats en un score d'answerability."""
    vals = [float(s) for s in scores]
    if not vals:
        return 0.0
    if strategy == STRATEGY_MEAN_TOP2:
        top = sorted(vals, reverse=True)[:2]
        return sum(top) / len(top)
    # max, max_guarded et composite s'appuient tous sur le maximum : un SEUL chunk qui
    # répond suffit à rendre la question answerable.
    return max(vals)


def decide(
    *,
    question: str,
    chunks: Sequence[dict[str, Any]],
    cfg: AnswerabilityConfig,
    retrieval_ok: bool = True,
) -> AnswerabilityResult:
    """Décide answerable / no_answer. Ne réordonne JAMAIS les chunks.

    `retrieval_ok` : verdict des gardes retrieval existantes (`should_answer`), utilisé par
    la stratégie C. Le cross-encoder ne les remplace pas, il les resserre.
    """
    t0 = time.perf_counter()
    base = dict(threshold=cfg.threshold, strategy=cfg.strategy, model=cfg.model,
                device=cfg.device, top_k=cfg.top_k)

    candidates = list(chunks)[: max(1, cfg.top_k)]
    if not candidates:
        return AnswerabilityResult(decision=NO_ANSWER, score=None, n_pairs=0,
                                   latency_ms=(time.perf_counter() - t0) * 1000, **base)

    try:
        scores = score_pairs(question, candidates, cfg)
    except AnswerabilityUnavailable as exc:
        # Panne du modèle (§23). Fail-open par défaut : transformer une panne
        # d'infrastructure en refus massif serait un dégât bien pire qu'un filtre inactif.
        # La trace est explicite — jamais un « answerable » muet.
        latency = (time.perf_counter() - t0) * 1000
        if cfg.fail_open:
            logger.warning("answerability indisponible (fail-open): %s", exc)
            return AnswerabilityResult(decision=UNAVAILABLE, score=None, n_pairs=0,
                                       latency_ms=latency, fallback_used=True,
                                       error=str(exc)[:200], **base)
        logger.error("answerability indisponible (fail-closed): %s", exc)
        return AnswerabilityResult(decision=NO_ANSWER, score=None, n_pairs=0,
                                   latency_ms=latency, fallback_used=True,
                                   error=str(exc)[:200], **base)

    score = aggregate(scores, cfg.strategy)
    latency = (time.perf_counter() - t0) * 1000
    if latency > cfg.timeout_s * 1000:
        logger.warning("answerability lente: %.0fms > %.0fms", latency, cfg.timeout_s * 1000)

    decision = ANSWERABLE if score >= cfg.threshold else NO_ANSWER
    override = False

    if cfg.strategy == STRATEGY_MAX_GUARDED and decision == ANSWERABLE and not retrieval_ok:
        # C : le cross-encoder ne peut pas RATTRAPER un retrieval qui a déjà renoncé.
        decision = NO_ANSWER
    elif cfg.strategy == STRATEGY_COMPOSITE and decision == NO_ANSWER:
        # D : un identifiant fort exact est une preuve dure. Ne pas refuser sur le seul
        # score du modèle quand le chunk contient littéralement la référence demandée.
        if has_exact_strong_id_match(question, candidates):
            decision, override = ANSWERABLE, True

    return AnswerabilityResult(decision=decision, score=score, n_pairs=len(candidates),
                               latency_ms=latency, strong_id_override=override,
                               scores=[round(s, 6) for s in scores], **base)
