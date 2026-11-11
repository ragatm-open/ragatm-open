"""Exécution du benchmark : passe chaque cas dans le pipeline réel et agrège.

Découplé de Django pour rester testable : le runner reçoit des *callables*
(``query_fn``, ``space_resolver``, ``answer_fn``) et ne touche ni aux modèles ni
à l'I/O. La commande ``manage.py rag_benchmark`` fournit les implémentations
réelles (``query_rag`` + résolution des RagSpace).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import metrics
from .dataset import BenchmarkCase

# Rangs de recall calculés par défaut.
DEFAULT_KS: tuple[int, ...] = (1, 3, 5, 10)

# Signatures des callables injectés.
QueryFn = Callable[[str, str], dict]          # (rag_space_id, question) -> pipeline dict
SpaceResolver = Callable[[str], str | None]   # nom logique -> rag_space_id (ou None)
AnswerFn = Callable[[str, str], dict]         # (rag_space_id, question) -> {answer, sources}
# (case, rag_space_id) -> objet de validation (duck-typé : .is_valid, .error_kind(),
# .primary_message(), .to_dict()) ou None. Injecté par la commande (P1-B).
ValidateFn = Callable[[BenchmarkCase, str], Any]
# (question, sources) -> objet de décision d'answerability (duck-typé : .answerable
# bool, .to_dict()) ou None. Injecté par la commande (P1.8). Décide, SANS LLM, si les
# preuves récupérées permettent réellement de répondre (≠ simple proximité retrieval).
AnswerabilityFn = Callable[[str, list], Any]

#: Collecteur de traces de candidats : ``(case_id, candidate_trace)``.
#: Le runner PRODUIT, le caller PERSISTE. Séparer les deux est ce qui permet de
#: capturer une trace sans utilisateur, sans session et sans `RagQueryLog` — le
#: manque exact qui avait laissé le diagnostic sans données.
CandidateSink = Callable[[str, dict], None]


def extract_doc_keys(sources: Sequence[dict[str, Any]]) -> list[str]:
    """Ordonne et déduplique les documents d'une liste de chunks (rang document).

    Utilise ``source.filename`` (vérité terrain du dataset = filenames) avec repli
    sur ``filename`` au top-level, puis ``document_id``.
    """
    keys: list[str] = []
    for ch in sources or []:
        if not isinstance(ch, dict):
            continue
        src = ch.get("source") if isinstance(ch.get("source"), dict) else {}
        key = (
            (src.get("filename") if src else None)
            or ch.get("filename")
            or (src.get("document_id") if src else None)
            or ch.get("document_id")
        )
        if key is not None:
            keys.append(str(key))
    return metrics.dedup_preserve_order(keys)


def extract_chunk_keys(sources: Sequence[dict[str, Any]]) -> list[str]:
    keys = [str(ch.get("chunk_id")) for ch in (sources or []) if isinstance(ch, dict) and ch.get("chunk_id")]
    return metrics.dedup_preserve_order(keys)


@dataclass
class CaseResult:
    case_id: str
    rag_space: str
    status: str
    should_answer: bool
    answered: bool
    no_answer_correct: bool
    latency_ms: float
    n_sources: int
    retrieved_docs: list[str]
    expected_docs: list[str]
    recall: dict[int, float]
    reciprocal_rank: float
    first_relevant_rank: int | None
    tags: list[str]
    # question du cas (exposée pour la comparaison baseline/candidat P1-A)
    question: str = ""
    # meilleur score des sources sélectionnées (calibration du seuil P1.6) ; None si
    # aucune source (cas rejeté / no-answer).
    best_score: float | None = None
    # catégorie / difficulté (analyse par catégorie, dataset étendu P1.7)
    category: str = ""
    difficulty: str = ""
    # format du document source (benchmark terrain P1.9) : pdf | docx | xlsx | …
    format: str = ""
    # décision d'answerability (P1.8) : {answerable, confidence, reason, signals} ou vide.
    answerability: dict[str, Any] = field(default_factory=dict)
    # facultatif (mode --with-answers)
    answer_contains: float | None = None
    llm_cost: float | None = None
    # observabilité rerank (B4) renvoyée par le pipeline (vide si non déclenché)
    rerank: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # catégorie d'erreur (P1-C) : None | retrieval_error | obsolete_reference |
    # invalid_dataset_case | comparison_error. Distingue un défaut du MOTEUR (le
    # candidat) d'un benchmark devenu INVALIDE (à régénérer).
    error_kind: str | None = None
    # validation documentaire (P1-B) : {status, errors[], warnings[]} ou vide.
    validation: dict[str, Any] = field(default_factory=dict)
    # résumé de trace de retrieval (P1-A0) : {schema_version, entrypoint, outcome,
    # degraded, legs} ou vide. Artefact d'OBSERVABILITÉ — n'entre dans AUCUNE métrique
    # ni verdict. Rend visible un retrieval DÉGRADÉ vs un miss réel.
    retrieval: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "rag_space": self.rag_space,
            "status": self.status,
            "should_answer": self.should_answer,
            "answered": self.answered,
            "no_answer_correct": self.no_answer_correct,
            "latency_ms": round(self.latency_ms, 2),
            "n_sources": self.n_sources,
            "retrieved_docs": self.retrieved_docs,
            "expected_docs": self.expected_docs,
            "recall": {str(k): round(v, 4) for k, v in self.recall.items()},
            "reciprocal_rank": round(self.reciprocal_rank, 4),
            "first_relevant_rank": self.first_relevant_rank,
            "question": self.question,
            "best_score": (None if self.best_score is None else round(self.best_score, 6)),
            "category": self.category,
            "difficulty": self.difficulty,
            "format": self.format,
            "n_expected": len(self.expected_docs),
            "answerability": self.answerability,
            "answer_contains": (
                None if self.answer_contains is None else round(self.answer_contains, 4)
            ),
            "llm_cost": self.llm_cost,
            "rerank": self.rerank,
            "tags": self.tags,
            "error": self.error,
            "error_kind": self.error_kind,
            "validation": self.validation,
            "retrieval": self.retrieval,
        }


def run_case(
    case: BenchmarkCase,
    *,
    query_fn: QueryFn,
    space_resolver: SpaceResolver,
    ks: Sequence[int] = DEFAULT_KS,
    answer_fn: AnswerFn | None = None,
    validate_fn: ValidateFn | None = None,
    answerability_fn: AnswerabilityFn | None = None,
    candidate_sink: "CandidateSink | None" = None,
) -> CaseResult:
    space_id = space_resolver(case.rag_space)
    base = CaseResult(
        case_id=case.id,
        rag_space=case.rag_space,
        status="error",
        should_answer=case.should_answer,
        answered=False,
        no_answer_correct=False,
        latency_ms=0.0,
        n_sources=0,
        retrieved_docs=[],
        expected_docs=list(case.expected_documents),
        recall={k: 0.0 for k in ks},
        reciprocal_rank=0.0,
        first_relevant_rank=None,
        tags=list(case.tags),
        question=case.question,
        category=case.category,
        difficulty=case.difficulty,
        format=case.format,
    )

    if space_id is None:
        # Espace référencé absent : le benchmark référence une cible disparue.
        base.error = f"RagSpace introuvable: {case.rag_space!r}"
        base.error_kind = "obsolete_reference"
        return base

    # Contrôles déterministes de cohérence documentaire (P1-B). Un cas obsolète /
    # invalide n'est PAS exécuté et ne compte PAS comme miss du moteur (P1-C).
    if validate_fn is not None:
        validation = validate_fn(case, space_id)
        if validation is not None:
            base.validation = validation.to_dict()
            if not validation.is_valid:
                base.error = validation.primary_message()
                base.error_kind = validation.error_kind()
                return base

    start = time.perf_counter()
    try:
        out = query_fn(space_id, case.question)
    except Exception as exc:  # le pipeline lève en cas d'erreur retrieval
        base.error = f"{type(exc).__name__}: {exc}"
        base.error_kind = "retrieval_error"
        base.latency_ms = (time.perf_counter() - start) * 1000.0
        return base
    latency_ms = (time.perf_counter() - start) * 1000.0

    # P1-A0 : capture du résumé de trace pour les artefacts (observabilité pure).
    # Distingue en sortie un retrieval DÉGRADÉ d'un miss réel, sans toucher aux
    # métriques (recall/MRR/verdict) calculées plus bas.
    retrieval_summary: dict[str, Any] = {}
    if isinstance(out, dict):
        _trace = out.get("retrieval_trace")
        if isinstance(_trace, dict):
            retrieval_summary = {
                "schema_version": _trace.get("schema_version"),
                "entrypoint": _trace.get("entrypoint"),
                "outcome": _trace.get("outcome"),
                "degraded": bool(_trace.get("degraded")),
                "legs": {
                    leg.get("name"): leg.get("status")
                    for leg in (_trace.get("legs") or [])
                    if isinstance(leg, dict) and leg.get("name")
                },
            }
        # la trace des candidats sort par un COLLECTEUR, jamais par
        # `CaseResult`. Deux raisons : elle ne doit entrer dans aucune métrique, et
        # sa persistance appartient au caller — le pipeline la produit, il ne
        # l'écrit pas. C'est ce découplage qui permet de la capturer sans
        # utilisateur, sans session et sans ligne d'audit.
        if candidate_sink is not None:
            _candidats = (_trace or {}).get("candidate_trace") if isinstance(_trace, dict) else None
            if isinstance(_candidats, dict):
                candidate_sink(case.id, _candidats)

    status = str(out.get("status") or "").strip().lower()
    sources = out.get("sources") or []
    retrieved = extract_doc_keys(sources)
    expected = list(case.expected_documents)
    if not expected and case.expected_chunk_ids:
        retrieved = extract_chunk_keys(sources)
        expected = list(case.expected_chunk_ids)

    # Meilleur score des sources retenues (calibration du seuil P1.6).
    _scores = [
        float(s.get("score"))
        for s in sources
        if isinstance(s, dict) and isinstance(s.get("score"), (int, float))
    ]
    best_score = max(_scores) if _scores else None

    answer_contains: float | None = None
    llm_cost: float | None = None
    if answer_fn is not None and case.should_answer:
        try:
            ans_out = answer_fn(space_id, case.question)
            answer_contains = metrics.answer_contains_score(
                ans_out.get("answer") or "", case.expected_answer_contains
            )
            llm_cost = ans_out.get("llm_cost_estimated")
        except Exception:
            answer_contains = None

    rerank_info = out.get("rerank") if isinstance(out.get("rerank"), dict) else {}

    # Answerability (P1.8) : décide, à partir des preuves récupérées, si la question est
    # réellement answerable — et SURCHARGE la décision répondre/refuser du benchmark.
    answerability_dict: dict[str, Any] = {}
    if answerability_fn is not None:
        decision = answerability_fn(case.question, sources)
        if decision is not None:
            answerability_dict = decision.to_dict()
            status = "ok" if decision.answerable else "no_answer"

    return CaseResult(
        case_id=case.id,
        rag_space=case.rag_space,
        status=status,
        should_answer=case.should_answer,
        answered=(status == "ok"),
        no_answer_correct=metrics.no_answer_correct(
            should_answer=case.should_answer, status=status
        ),
        latency_ms=latency_ms,
        n_sources=len(sources),
        retrieved_docs=retrieved,
        expected_docs=expected,
        # v2 : recall tenant compte de match_policy (any/all). v1 : sémantique
        # fractionnaire historique INCHANGÉE. MRR/first_relevant_rank (rang du premier
        # attendu trouvé) restent valides pour les deux politiques.
        recall=(
            {k: metrics.recall_at_k_policy(retrieved, expected, k, case.match_policy) for k in ks}
            if case.source_schema_version == 2
            else {k: metrics.recall_at_k(retrieved, expected, k) for k in ks}
        ),
        reciprocal_rank=metrics.reciprocal_rank(retrieved, expected),
        first_relevant_rank=metrics.first_relevant_rank(retrieved, expected),
        tags=list(case.tags),
        question=case.question,
        best_score=best_score,
        category=case.category,
        difficulty=case.difficulty,
        format=case.format,
        answerability=answerability_dict,
        answer_contains=answer_contains,
        llm_cost=llm_cost,
        rerank=rerank_info,
        validation=base.validation,
        retrieval=retrieval_summary,
    )


def aggregate(results: Sequence[CaseResult], *, ks: Sequence[int] = DEFAULT_KS) -> dict[str, Any]:
    """Agrège les résultats par cas en métriques globales.

    Politique d'erreur (P1-C) :
    - ``retrieval_error`` (défaut du MOTEUR) : compté comme échec retrieval → recall=0,
      RR=0, INCLUS dans le dénominateur du recall/MRR (ne gonfle pas les scores en
      s'excluant) ; visible via ``n_retrieval_errors`` ;
    - ``obsolete_reference`` / ``invalid_dataset_case`` (benchmark INVALIDE) : exclus du
      recall (ce n'est pas un miss du moteur) mais rendent le run non promouvable
      (``run_status = invalid_dataset``) ;
    - ``n_errors`` reste le total d'erreurs et un run avec erreurs ne peut pas être
      déclaré PASS silencieusement (cf. commande + comparaison).
    """
    total = len(results)
    errored = [r for r in results if r.error]
    n_retrieval_errors = sum(1 for r in results if r.error_kind == "retrieval_error")
    n_obsolete = sum(1 for r in results if r.error_kind == "obsolete_reference")
    n_invalid = sum(1 for r in results if r.error_kind == "invalid_dataset_case")

    positives = [r for r in results if r.should_answer and not r.error]
    negatives = [r for r in results if not r.should_answer and not r.error]

    # Cas positifs SCORÉS pour le recall/MRR : succès réels + erreurs retrieval (recall
    # 0). Les cas obsolètes/invalides sont exclus (benchmark à régénérer).
    positives_scored = [
        r
        for r in results
        if r.should_answer and r.error_kind in (None, "retrieval_error")
    ]

    recall_agg = {
        k: metrics.mean([r.recall.get(k, 0.0) for r in positives_scored]) for k in ks
    }
    mrr = metrics.mean([r.reciprocal_rank for r in positives_scored])

    # Multi-source (P1.7) : un cas answerable à ≥2 documents attendus n'est "couvert"
    # que si TOUTES les preuves sont dans le top-k de décision (k=5 si présent).
    decision_k = 5 if 5 in tuple(ks) else max(ks)
    multi_docs = [r for r in positives_scored if len(r.expected_docs) >= 2]
    multi_source_full_coverage = metrics.mean(
        [1.0 if r.recall.get(decision_k, 0.0) >= 1.0 else 0.0 for r in multi_docs]
    )
    multi_source_mean_coverage = metrics.mean(
        [r.recall.get(decision_k, 0.0) for r in multi_docs]
    )

    # no-answer accuracy = correct sur les cas négatifs ; faux refus sur positifs.
    no_answer_accuracy = metrics.mean(
        [1.0 if r.no_answer_correct else 0.0 for r in negatives]
    )
    false_refusal_rate = metrics.mean(
        [0.0 if r.answered else 1.0 for r in positives]
    )
    false_answer_rate = metrics.mean(
        [1.0 if r.answered else 0.0 for r in negatives]
    )
    answer_decision_accuracy = metrics.mean(
        [1.0 if r.no_answer_correct else 0.0 for r in (positives + negatives)]
    )

    latencies = [r.latency_ms for r in results if not r.error]
    answer_contains_vals = [
        r.answer_contains for r in positives if r.answer_contains is not None
    ]
    cost_vals = [r.llm_cost for r in results if r.llm_cost is not None]

    # Answerability (P1.8) : matrice de confusion prédiction (answered) vs vérité
    # (should_answer), sur les cas non-errorés. TP = answerable prédit answerable, etc.
    scored = [r for r in results if not r.error]
    tp = sum(1 for r in scored if r.should_answer and r.answered)
    fn = sum(1 for r in scored if r.should_answer and not r.answered)
    tn = sum(1 for r in scored if not r.should_answer and not r.answered)
    fp = sum(1 for r in scored if not r.should_answer and r.answered)
    a_prec = tp / (tp + fp) if (tp + fp) else None
    a_rec = tp / (tp + fn) if (tp + fn) else None
    a_f1 = (2 * a_prec * a_rec / (a_prec + a_rec)) if (a_prec and a_rec) else None
    a_acc = (tp + tn) / len(scored) if scored else None

    if n_obsolete or n_invalid:
        run_status = "invalid_dataset"
    elif n_retrieval_errors:
        run_status = "completed_with_errors"
    else:
        run_status = "completed"

    return {
        "run_status": run_status,
        "n_cases": total,
        "n_errors": len(errored),
        "n_retrieval_errors": n_retrieval_errors,
        "n_obsolete_references": n_obsolete,
        "n_invalid_cases": n_invalid,
        "n_positive": len(positives),
        "n_positive_scored": len(positives_scored),
        "n_no_answer": len(negatives),
        "recall_at_k": {str(k): _round(v) for k, v in recall_agg.items()},
        "mrr": _round(mrr),
        "no_answer_accuracy": _round(no_answer_accuracy),
        "false_refusal_rate": _round(false_refusal_rate),
        "false_answer_rate": _round(false_answer_rate),
        # Alias explicites (calibration sémantique P1.6) :
        # - no_answer_false_positive_rate : cas no-answer ayant produit une réponse
        #   / total no-answer (== false_answer_rate) ;
        # - answerable_false_negative_rate : cas answerable rejetés / total answerable
        #   (== false_refusal_rate).
        "no_answer_false_positive_rate": _round(false_answer_rate),
        "answerable_false_negative_rate": _round(false_refusal_rate),
        # Multi-source (P1.7) : couverture des cas answerable à ≥2 documents attendus.
        "n_multi_source": len(multi_docs),
        "multi_source_full_coverage": _round(multi_source_full_coverage),
        "multi_source_mean_coverage": _round(multi_source_mean_coverage),
        # Answerability (P1.8) : matrice de confusion + précision/rappel/F1.
        "answerability": {
            "confusion": {"tp": tp, "fn": fn, "tn": tn, "fp": fp},
            "accuracy": _round(a_acc),
            "precision": _round(a_prec),
            "recall": _round(a_rec),
            "f1": _round(a_f1),
        },
        "answer_decision_accuracy": _round(answer_decision_accuracy),
        "answer_contains_mean": (
            _round(metrics.mean(answer_contains_vals)) if answer_contains_vals else None
        ),
        "llm_cost_usd_total": (round(sum(cost_vals), 6) if cost_vals else None),
        "latency_ms": {
            "p50": _round(metrics.percentile(latencies, 50)),
            "p95": _round(metrics.percentile(latencies, 95)),
            "mean": _round(metrics.mean(latencies)),
        },
        "rerank": _aggregate_rerank(results),
    }


def aggregate_by_category(
    results: Sequence[CaseResult], *, ks: Sequence[int] = DEFAULT_KS
) -> dict[str, dict[str, Any]]:
    """Métriques par catégorie (P1.7) : un seuil ne doit pas masquer une régression
    sur une catégorie (identifiants forts, multi-doc, …)."""
    return aggregate_grouped(results, key=lambda r: r.category, ks=ks)


def aggregate_by_format(
    results: Sequence[CaseResult], *, ks: Sequence[int] = DEFAULT_KS
) -> dict[str, dict[str, Any]]:
    """Métriques par FORMAT source (P1.9) : pdf / docx / xlsx / …

    Les classeurs XLSX ne se comportent pas comme des paragraphes : mesurer le
    retrieval par format évite qu'une moyenne globale masque un format en échec.
    """
    return aggregate_grouped(results, key=lambda r: r.format, ks=ks)


def aggregate_grouped(
    results: Sequence[CaseResult],
    *,
    key: Callable[[CaseResult], str],
    ks: Sequence[int] = DEFAULT_KS,
) -> dict[str, dict[str, Any]]:
    """Agrège les métriques par groupe arbitraire (``(non étiqueté)`` si clé vide)."""
    cats: dict[str, list[CaseResult]] = {}
    for r in results:
        cats.setdefault(key(r) or "(non étiqueté)", []).append(r)

    out: dict[str, dict[str, Any]] = {}
    for cat, rs in sorted(cats.items()):
        pos = [r for r in rs if r.should_answer and r.error_kind in (None, "retrieval_error")]
        neg = [r for r in rs if not r.should_answer and not r.error]
        out[cat] = {
            "n": len(rs),
            "n_positive": len(pos),
            "n_no_answer": len(neg),
            "recall_at_k": {
                str(k): _round(metrics.mean([r.recall.get(k, 0.0) for r in pos])) for k in ks
            },
            "mrr": _round(metrics.mean([r.reciprocal_rank for r in pos])),
            "no_answer_accuracy": _round(
                metrics.mean([1.0 if r.no_answer_correct else 0.0 for r in neg])
            ),
            "no_answer_false_positive_rate": _round(
                metrics.mean([1.0 if r.answered else 0.0 for r in neg])
            ),
            "answerable_false_negative_rate": _round(
                metrics.mean([0.0 if r.answered else 1.0 for r in pos])
            ),
            "n_errors": sum(1 for r in rs if r.error),
        }
    return out


def _aggregate_rerank(results: Sequence[CaseResult]) -> dict[str, Any]:
    """Agrège l'observabilité rerank (B4) : déclenchements, fallback, latences, chunks."""
    triggered = [r for r in results if r.rerank]
    if not triggered:
        return {"triggered": 0}
    rk_lat = [float(r.rerank.get("latency_ms") or 0.0) for r in triggered]
    n_in = [int(r.rerank.get("n_in") or 0) for r in triggered]
    n_out = [int(r.rerank.get("n_out") or 0) for r in triggered]
    fallbacks = sum(1 for r in triggered if r.rerank.get("fallback_used"))
    providers = sorted({str(r.rerank.get("effective_provider") or "") for r in triggered})
    return {
        "triggered": len(triggered),
        "effective_providers": providers,
        "fallback_count": fallbacks,
        "n_in_mean": _round(metrics.mean(n_in)),
        "n_out_mean": _round(metrics.mean(n_out)),
        "latency_ms": {
            "p50": _round(metrics.percentile(rk_lat, 50)),
            "p95": _round(metrics.percentile(rk_lat, 95)),
        },
    }


def _round(v: float | None, ndigits: int = 4) -> float | None:
    return None if v is None else round(float(v), ndigits)


@dataclass
class BenchmarkReport:
    meta: dict[str, Any]
    summary: dict[str, Any]
    cases: list[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "summary": self.summary,
            "cases": [c.to_dict() for c in self.cases],
        }


def run_benchmark(
    cases: Sequence[BenchmarkCase],
    *,
    query_fn: QueryFn,
    space_resolver: SpaceResolver,
    ks: Sequence[int] = DEFAULT_KS,
    answer_fn: AnswerFn | None = None,
    validate_fn: ValidateFn | None = None,
    answerability_fn: AnswerabilityFn | None = None,
    candidate_sink: "CandidateSink | None" = None,
    meta: dict[str, Any] | None = None,
) -> BenchmarkReport:
    results = [
        run_case(
            c,
            query_fn=query_fn,
            space_resolver=space_resolver,
            ks=ks,
            answer_fn=answer_fn,
            validate_fn=validate_fn,
            answerability_fn=answerability_fn,
            candidate_sink=candidate_sink,
        )
        for c in cases
    ]
    summary = aggregate(results, ks=ks)
    return BenchmarkReport(meta=dict(meta or {}), summary=summary, cases=results)
