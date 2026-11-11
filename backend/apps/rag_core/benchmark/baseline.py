"""Normalisation d'un rapport de benchmark en *baseline canonique* committable (P1.5).

Une baseline committée doit être **reproductible à l'octet** : régénérée dans les
mêmes conditions (même commit, même corpus stub/FTS), elle ne doit produire AUCUN
diff. On retire donc les champs *volatils* (horodatage, latences non déterministes,
chemins absolus) tout en conservant les métriques et la vérité terrain déterministes.

La comparaison CI (``rag_benchmark_compare``) ne s'appuie de toute façon jamais sur
ces champs volatils : ni pour la compatibilité (identité du benchmark, case_id,
chunking_version) ni pour la détection de régression (rangs, recall, erreurs).
"""
from __future__ import annotations

import json
from typing import Any

# Champs volatils retirés de la baseline canonique.
_VOLATILE_META_KEYS = ("generated_at",)
_VOLATILE_SUMMARY_KEYS = ("latency_ms",)  # latences non déterministes
# ``best_score`` / ``retrieved_docs`` / ``n_sources`` : diagnostics sensibles à l'ordre
# ANN (HNSW) sur les corpus > quelques chunks — l'ordonnancement de queue et le score
# fusionné peuvent varier légèrement d'un run à l'autre SANS changer les métriques ni les
# décisions par cas (first_relevant_rank, recall, status restent stables). On les exclut
# du format canonique pour préserver la reproductibilité à l'octet ; ils restent
# disponibles dans le rapport brut (--out) et dans les artefacts de calibration.
_VOLATILE_CASE_KEYS = ("latency_ms", "best_score", "retrieved_docs", "n_sources")


def _basename(path: Any) -> Any:
    if not isinstance(path, str) or not path:
        return path
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Retourne une copie canonique du rapport (déterministe, sans champ volatil)."""
    meta = dict(report.get("meta") or {})
    for k in _VOLATILE_META_KEYS:
        meta.pop(k, None)
    # chemin absolu -> basename (stable entre machines / runners CI).
    if "dataset" in meta:
        meta["dataset"] = _basename(meta["dataset"])
    # Profil explicite : cette baseline protège le chemin FTS/stub, PAS le sémantique.
    meta.setdefault("profile", "stub")
    meta.setdefault("semantic_embeddings", False)

    summary = {
        k: v for k, v in (report.get("summary") or {}).items()
        if k not in _VOLATILE_SUMMARY_KEYS
    }

    cases = []
    for c in report.get("cases") or []:
        if not isinstance(c, dict):
            continue
        cc = {k: v for k, v in c.items() if k not in _VOLATILE_CASE_KEYS}
        # answerability : ne garder que la DÉCISION stable (answerable + reason) ; la
        # confidence et les signals embarquent le score de retrieval (volatil ANN).
        a = cc.get("answerability")
        if isinstance(a, dict) and a:
            cc["answerability"] = {"answerable": a.get("answerable"), "reason": a.get("reason")}
        cases.append(cc)

    return {"meta": meta, "summary": summary, "cases": cases}


def dump_canonical(report: dict[str, Any]) -> str:
    """Sérialise la baseline canonique de façon stable (clés triées, newline final)."""
    canonical = normalize_report(report)
    return json.dumps(canonical, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
