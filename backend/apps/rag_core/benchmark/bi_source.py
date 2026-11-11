# backend/apps/rag_core/benchmark/bi_source.py
from __future__ import annotations

"""Benchmark de retrieval BI-SOURCE (privé + corpus métier).

Ce que ce benchmark mesure, et pourquoi il existe
------------------------------------------------
Les datasets de benchmark existants (``demo-fr-extended*``) seedent un RagSpace PRIVÉ :
ils ne peuvent pas, par construction, mesurer la contribution d'un corpus métier. Or les
deux correctifs de ce lot portent précisément là :

- D1 (repli de plan FTS sur le leg corpus) — change le RAPPEL du corpus ;
- D2 (fusion de rangs unifiée, derrière flag) — change le CLASSEMENT bi-source.

Un correctif de classement ne se valide pas à l'intuition. Ce module fournit donc une
mesure reproductible, exécutable avec le stub d'embeddings (donc déterministe et sans
provider), qui compare la MÊME question dans les deux configurations du flag.

Ce qu'il ne fait pas : remplacer les benchmarks de qualité de réponse. Il mesure le
retrieval — rappel, rang, MRR, composition des sources, doublons, requêtes SQL, latence.

Jeu de données : synthétique, dans le domaine du venture capital (le signalement qui a
motivé le lot). Il ne colore AUCUN code produit — c'est une donnée de test, comme
``demo_fr_extended`` l'est pour le français métier.
"""

import time
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Callable, Sequence

from django.db import connection
from django.test.utils import CaptureQueriesContext

DATASET_ID = "bi-source-vc"
DATASET_VERSION = "v1"

WORKSPACE_NAME = "Bench Bi-Source"
SPACE_NAME = "bench-bi-source"
DOMAIN_SLUG = "bench-venture-capital"
CORPUS_SLUG = "bench-vc-reference"
CORPUS_NAME = "Bench VC Reference"

#: Documents PRIVÉS : génériques, ils parlent du sujet sans le définir. Plusieurs
#: documents distincts : le plafond de diversité par document (2) ne doit pas être le
#: facteur limitant de la mesure.
PRIVATE_DOCUMENTS: dict[str, str] = {
    f"press-review-{i}.txt": (
        f"Press review number {i}. This internal review collects headlines about "
        "financing rounds, startup announcements and market sentiment. It mentions "
        "venture capital as a topic several times without defining it, and offers no "
        "detail on instruments, actors, dilution or exit mechanics. The remainder "
        "covers unrelated operational matters: office moves, supplier renewals, "
        "training plans and travel policy updates."
    )
    for i in range(1, 7)
}

#: Documents du CORPUS MÉTIER : la définition et les notions distinctives. Chaque
#: document porte UNE notion, pour que le rang mesure quelque chose.
CORPUS_DOCUMENTS: dict[str, str] = {
    "vc-definition.txt": (
        "Venture capital is a form of equity financing provided to unlisted, privately "
        "held companies with high growth potential and a high risk of failure. The "
        "investor takes an equity participation in the company capital rather than "
        "lending money, and accepts dilution across successive financing rounds."
    ),
    "vc-partners.txt": (
        "A venture capital fund is structured around general partners, who manage the "
        "fund and select the investments, and limited partners, who commit the capital "
        "and remain passive. The general partners are remunerated through a management "
        "fee and a carried interest on realised gains."
    ),
    "vc-exit.txt": (
        "Venture capital investors realise their return through an exit, over a horizon "
        "of typically five to ten years: a trade sale, a secondary sale or an initial "
        "public offering. No interest is paid along the way; the entire return depends "
        "on the value of the equity participation at exit."
    ),
    "vc-vs-bank-loan.txt": (
        "Unlike a conventional bank loan, venture capital carries no collateral, no "
        "repayment schedule and no interest. The bank lender is repaid regardless of "
        "performance; the venture capital investor is repaid only through an exit, and "
        "loses the capital if the company fails. High risk is compensated by high "
        "growth expectations, not by a coupon."
    ),
}

#: Questions du signalement. Capitalisées : c'est la forme qui déclenchait D1.
QUESTIONS: tuple[str, ...] = (
    "What is venture capital?",
    "What is the role of general partners and limited partners?",
    "How do venture capital investors realise their returns?",
    "How is venture capital different from a bank loan?",
)


def _is_corpus(source: dict) -> bool:
    src = source.get("source") if isinstance(source.get("source"), dict) else {}
    return (source.get("source_type") or src.get("source_type")) == "domain_corpus"


@dataclass
class QuestionMeasure:
    """Mesure d'UNE question dans UNE configuration."""

    question: str
    status: str
    source_count: int = 0
    corpus_source_count: int = 0
    private_source_count: int = 0
    first_corpus_rank: int | None = None
    duplicate_source_count: int = 0
    sql_query_count: int = 0
    latency_ms: float = 0.0
    corpus_fts_plan_index: int | None = None
    corpus_drop_stage: str | None = None
    selected_chunk_ids: tuple[str, ...] = ()
    private_chunk_ids: tuple[str, ...] = ()

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.first_corpus_rank if self.first_corpus_rank else 0.0


@dataclass
class ConfigMeasure:
    """Agrégat d'une configuration (flag OFF ou ON)."""

    label: str
    questions: list[QuestionMeasure] = field(default_factory=list)

    def _mean(self, fn: Callable[[QuestionMeasure], float]) -> float:
        return round(sum(fn(q) for q in self.questions) / len(self.questions), 4) if self.questions else 0.0

    def as_dict(self) -> dict[str, Any]:
        ranks = [q.first_corpus_rank for q in self.questions if q.first_corpus_rank]
        latencies = sorted(q.latency_ms for q in self.questions)
        return {
            "label": self.label,
            "questions": len(self.questions),
            # Rappel corpus : proportion de questions où AU MOINS une source corpus est
            # citée. C'est la métrique que D1 doit faire bouger.
            "corpus_recall": self._mean(lambda q: 1.0 if q.corpus_source_count else 0.0),
            "mrr_corpus": self._mean(lambda q: q.reciprocal_rank),
            "first_corpus_rank_mean": round(sum(ranks) / len(ranks), 3) if ranks else None,
            "corpus_sources_mean": self._mean(lambda q: q.corpus_source_count),
            "private_sources_mean": self._mean(lambda q: q.private_source_count),
            "sources_mean": self._mean(lambda q: q.source_count),
            "no_answer_rate": self._mean(lambda q: 1.0 if q.status != "ok" else 0.0),
            "duplicate_sources_total": sum(q.duplicate_source_count for q in self.questions),
            "sql_queries_mean": self._mean(lambda q: q.sql_query_count),
            "latency_p50_ms": round(median(latencies), 1) if latencies else 0.0,
            # Avec 4 questions, « p95 » est le pire cas : on le nomme pour ce qu'il est.
            "latency_max_ms": round(latencies[-1], 1) if latencies else 0.0,
            "corpus_fts_plan_index": sorted(
                {q.corpus_fts_plan_index for q in self.questions if q.corpus_fts_plan_index is not None}
            ),
            "corpus_drop_stages": sorted({q.corpus_drop_stage or "?" for q in self.questions}),
        }


def measure_question(*, rag_space, user, question: str) -> QuestionMeasure:
    """Exécute une question et en extrait des métriques de RETRIEVAL uniquement."""
    from apps.rag_core.services.query import query_rag

    started = time.perf_counter()
    with CaptureQueriesContext(connection) as captured:
        result = query_rag(
            rag_space_id=rag_space.id, rag_space=rag_space, user=user, question=question
        )
    latency_ms = (time.perf_counter() - started) * 1000.0

    sources = result.get("sources") or []
    chunk_ids = [str(s.get("chunk_id")) for s in sources if s.get("chunk_id")]
    private_ids = [str(s.get("chunk_id")) for s in sources if s.get("chunk_id") and not _is_corpus(s)]
    corpus_ranks = [i for i, s in enumerate(sources, start=1) if _is_corpus(s)]
    corpus_trace = (result.get("retrieval_trace") or {}).get("business_corpus") or {}

    return QuestionMeasure(
        question=question,
        status=str(result.get("status") or ""),
        source_count=len(sources),
        corpus_source_count=len(corpus_ranks),
        private_source_count=len(sources) - len(corpus_ranks),
        first_corpus_rank=corpus_ranks[0] if corpus_ranks else None,
        duplicate_source_count=len(chunk_ids) - len(set(chunk_ids)),
        sql_query_count=len(captured.captured_queries),
        latency_ms=latency_ms,
        corpus_fts_plan_index=corpus_trace.get("corpus_fts_plan_index"),
        corpus_drop_stage=corpus_trace.get("corpus_drop_stage"),
        selected_chunk_ids=tuple(chunk_ids),
        private_chunk_ids=tuple(private_ids),
    )


def measure_config(*, label: str, rag_space, user, questions: Sequence[str] = QUESTIONS) -> ConfigMeasure:
    out = ConfigMeasure(label=label)
    for question in questions:
        out.questions.append(measure_question(rag_space=rag_space, user=user, question=question))
    return out


def private_retention(baseline: ConfigMeasure, candidate: ConfigMeasure) -> dict[str, Any]:
    """Les sources PRIVÉES de la baseline survivent-elles dans la configuration candidate ?

    Critère §9 « n'élimine pas les documents privés pertinents ». On compare les
    ``chunk_id`` retenus, question par question — pas les compteurs, qui masqueraient une
    substitution.
    """
    kept = total = 0
    for base_q, cand_q in zip(baseline.questions, candidate.questions):
        base_private = set(base_q.private_chunk_ids)
        total += len(base_private)
        kept += len(base_private & set(cand_q.selected_chunk_ids))
    return {
        "baseline_private_sources": total,
        "kept_in_candidate": kept,
        "retention_rate": round(kept / total, 4) if total else None,
    }
