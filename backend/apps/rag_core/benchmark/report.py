"""Rendu des rapports de benchmark (JSON machine + Markdown lisible) et seuils.

Aucune dépendance Django : sérialisation pure pour pouvoir écrire les rapports
dans ``backend/benchmarks/results/`` ou les afficher en console.
"""
from __future__ import annotations

import json
from typing import Any

from .runner import BenchmarkReport

# Seuils de non-régression par défaut (calibrer après un premier run réel).
# Voir backend/benchmarks/README.md. Volontairement prudents.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "recall_at_5": 0.80,
    "mrr": 0.70,
    "no_answer_accuracy": 0.90,
}


def check_thresholds(
    report: BenchmarkReport, thresholds: dict[str, float] | None = None
) -> tuple[bool, list[dict[str, Any]]]:
    """Compare le résumé aux seuils. Renvoie ``(ok, violations)``.

    Clés supportées : ``recall_at_{k}``, ``mrr``, ``no_answer_accuracy``,
    ``answer_decision_accuracy``, ``answer_contains_mean``.
    Un seuil dont la métrique est ``None`` (non mesurée) est ignoré (non bloquant).
    """
    thr = DEFAULT_THRESHOLDS if thresholds is None else thresholds
    summary = report.summary
    violations: list[dict[str, Any]] = []

    for key, minimum in thr.items():
        actual = _metric_value(summary, key)
        if actual is None:
            continue
        if actual < minimum:
            violations.append({"metric": key, "min": minimum, "actual": actual})

    return (len(violations) == 0, violations)


def _metric_value(summary: dict[str, Any], key: str) -> float | None:
    if key.startswith("recall_at_"):
        k = key.rsplit("_", 1)[-1]
        return (summary.get("recall_at_k") or {}).get(k)
    return summary.get(key)


def to_json(report: BenchmarkReport, *, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=indent)


def to_markdown(report: BenchmarkReport, *, thresholds: dict[str, float] | None = None) -> str:
    s = report.summary
    meta = report.meta
    lines: list[str] = []

    lines.append("# Rapport Benchmark RAG")
    lines.append("")
    lines.append("## Contexte")
    for label, key in (
        ("Dataset", "dataset"),
        ("Généré le", "generated_at"),
        ("RagSpace(s)", "rag_spaces"),
        ("Provider embeddings", "embeddings_provider"),
        ("Modèle embeddings", "embeddings_model"),
        ("Dimension", "embedding_dim"),
        ("FTS config", "fts_config"),
        ("Rerank", "rerank"),
        ("LLM (answers)", "llm"),
    ):
        if key in meta and meta[key] not in (None, ""):
            lines.append(f"- **{label}** : {meta[key]}")
    lines.append("")

    lines.append("## Synthèse")
    lines.append("")
    lines.append("| Métrique | Valeur |")
    lines.append("| --- | --- |")
    lines.append(f"| Cas | {s.get('n_cases')} (positifs: {s.get('n_positive')}, no-answer: {s.get('n_no_answer')}, erreurs: {s.get('n_errors')}) |")
    for k, v in (s.get("recall_at_k") or {}).items():
        lines.append(f"| recall@{k} | {_fmt(v)} |")
    lines.append(f"| MRR | {_fmt(s.get('mrr'))} |")
    lines.append(f"| no-answer accuracy | {_fmt(s.get('no_answer_accuracy'))} |")
    lines.append(f"| faux refus (positifs) | {_fmt(s.get('false_refusal_rate'))} |")
    lines.append(f"| fausses réponses (no-answer) | {_fmt(s.get('false_answer_rate'))} |")
    lines.append(f"| décision répondre/refuser | {_fmt(s.get('answer_decision_accuracy'))} |")
    if s.get("answer_contains_mean") is not None:
        lines.append(f"| answer-contains (moyenne) | {_fmt(s.get('answer_contains_mean'))} |")
    if s.get("llm_cost_usd_total") is not None:
        lines.append(f"| coût LLM total (USD) | {s.get('llm_cost_usd_total'):.6f} |")
    lat = s.get("latency_ms") or {}
    lines.append(f"| latence p50 / p95 (ms) | {_fmt(lat.get('p50'))} / {_fmt(lat.get('p95'))} |")
    lines.append("")

    rk = s.get("rerank") or {}
    if rk.get("triggered"):
        rk_lat = rk.get("latency_ms") or {}
        lines.append("## Rerank (observabilité)")
        lines.append("")
        lines.append("| Métrique | Valeur |")
        lines.append("| --- | --- |")
        lines.append(f"| déclenchements | {rk.get('triggered')} |")
        lines.append(f"| providers effectifs | {', '.join(rk.get('effective_providers') or []) or '—'} |")
        lines.append(f"| fallbacks (cross_encoder→stub) | {rk.get('fallback_count')} |")
        lines.append(f"| chunks avant / après (moyenne) | {_fmt(rk.get('n_in_mean'))} / {_fmt(rk.get('n_out_mean'))} |")
        lines.append(f"| latence rerank p50 / p95 (ms) | {_fmt(rk_lat.get('p50'))} / {_fmt(rk_lat.get('p95'))} |")
        if rk.get("fallback_count"):
            lines.append("")
            lines.append("> ⚠️ Des fallbacks cross_encoder→stub ont eu lieu : reranking sémantique dégradé (vérifier sentence-transformers/CUDA).")
        lines.append("")

    ok, violations = check_thresholds(report, thresholds)
    lines.append("## Seuils de non-régression")
    lines.append("")
    if ok:
        lines.append("✅ Tous les seuils mesurés sont respectés.")
    else:
        lines.append("❌ Violations :")
        lines.append("")
        lines.append("| Métrique | Minimum | Obtenu |")
        lines.append("| --- | --- | --- |")
        for v in violations:
            lines.append(f"| {v['metric']} | {_fmt(v['min'])} | {_fmt(v['actual'])} |")
    lines.append("")

    failing = [c for c in report.cases if c.error or not c.no_answer_correct]
    if failing:
        lines.append("## Cas en échec / à inspecter")
        lines.append("")
        lines.append("| id | status | attendu | rang 1er doc | erreur |")
        lines.append("| --- | --- | --- | --- | --- |")
        for c in failing:
            expect = "répondre" if c.should_answer else "no-answer"
            rank = c.first_relevant_rank if c.first_relevant_rank is not None else "—"
            err = (c.error or "").replace("|", "/")[:60]
            lines.append(f"| {c.case_id} | {c.status} | {expect} | {rank} | {err} |")
        lines.append("")

    return "\n".join(lines)


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)
