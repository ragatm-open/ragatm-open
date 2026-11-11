"""Préflight de résolution du benchmark v2 : JSONL v2 -> objets internes résolus.

Résout TOUTES les vérités terrain stables (par RagSpace, en lot anti-N+1) AVANT tout
appel retrieval. Un seul cas v2 non résolu **bloque** le run officiel (atomicité) :
on ne produit jamais de métrique partielle silencieuse.

Aucune mutation : lecture seule sur documents/chunks. Les UUID résolus restent en
mémoire et ne sont jamais réécrits dans le dataset canonique.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from apps.rag_core.benchmark import stable_ground_truth as sgt
from apps.rag_core.benchmark.dataset import BenchmarkCase, LoadedV2Case, load_mixed_dataset
from apps.rag_core.benchmark.stable_source_resolver import (
    ResolvedStableGroundTruth,
    resolve_many_stable_ground_truths,
)
from apps.rag_core.models.rag_space import RagSpace

ERR_RAG_SPACE_NOT_FOUND = "rag_space_not_found"
ERR_AMBIGUOUS_RAG_SPACE = "ambiguous_rag_space"
ERR_NO_RESOLVED_CHUNKS = "no_resolved_chunks"


@dataclass(frozen=True)
class BenchmarkPreflightIssue:
    case_id: str
    code: str
    detail: str
    rag_space: str | None = None

    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "code": self.code, "detail": self.detail,
                "rag_space": self.rag_space}


class BenchmarkDatasetPreflightError(Exception):
    """Au moins un cas v2 n'a pu être résolu : le run officiel est refusé."""

    def __init__(self, issues: list[BenchmarkPreflightIssue]):
        self.issues = tuple(issues)
        super().__init__(f"{len(self.issues)} cas v2 non résolu(s) — run officiel refusé")


@dataclass(frozen=True)
class PreflightOutcome:
    cases: tuple[BenchmarkCase, ...]        # v1 + v2 résolus, prêts pour le runner
    issues: tuple[BenchmarkPreflightIssue, ...]
    report: dict


def _resolve_space(name: str, *, forced_space_id: str | None, cache: dict) -> tuple[RagSpace | None, str | None]:
    """Résout un nom logique -> RagSpace, STRICTEMENT (jamais ``first()`` silencieux).
    Renvoie (space, code_erreur|None)."""
    if forced_space_id:
        if forced_space_id not in cache:
            sp = RagSpace.objects.filter(id=forced_space_id, deleted_at__isnull=True).first()
            cache[forced_space_id] = sp
        sp = cache[forced_space_id]
        return (sp, None) if sp else (None, ERR_RAG_SPACE_NOT_FOUND)
    if name in cache:
        return cache[name], (None if cache[name] else ERR_RAG_SPACE_NOT_FOUND)
    spaces = list(RagSpace.objects.filter(name=name, deleted_at__isnull=True)[:2])
    if not spaces:
        cache[name] = None
        return None, ERR_RAG_SPACE_NOT_FOUND
    if len(spaces) > 1:
        cache[name] = None
        return None, ERR_AMBIGUOUS_RAG_SPACE
    cache[name] = spaces[0]
    return spaces[0], None


def _resolved_case(v2: LoadedV2Case, resolved: ResolvedStableGroundTruth) -> BenchmarkCase:
    """Construit l'objet interne du runner : ``expected_chunk_ids`` = UUID COURANTS
    résolus (en mémoire). ``expected_documents`` volontairement vide -> le runner
    mesure le recall par chunk (vérité terrain v2), pas par filename (§23)."""
    return BenchmarkCase(
        id=v2.case_id,
        rag_space=v2.rag_space,
        question=v2.question,
        should_answer=True,
        expected_documents=(),
        expected_chunk_ids=tuple(str(cid) for cid in resolved.chunk_ids),
        format="xlsx",
        source_schema_version=2,
        match_policy=resolved.match_policy,
    )


def run_preflight(
    *,
    v1_cases: list[BenchmarkCase],
    v2_cases: list[LoadedV2Case],
    forced_space_id: str | None = None,
    dataset_path: str | Path | None = None,
) -> PreflightOutcome:
    """Résout tous les cas v2 (batch par espace). N'exécute AUCUN retrieval.

    Ne lève pas : renvoie un ``PreflightOutcome`` (cas résolus + issues + rapport).
    L'appelant décide de bloquer (run officiel) ou non (diagnostic)."""
    t0 = time.perf_counter()
    resolved: list[BenchmarkCase] = list(v1_cases)      # v1 : chemin historique, inchangé
    issues: list[BenchmarkPreflightIssue] = []
    space_cache: dict = {}

    # Groupement par espace résolu (uniquement les cas dont l'espace résout).
    by_space: dict[str, tuple[RagSpace, list[LoadedV2Case]]] = {}
    space_names: set[str] = set()
    for c in v2_cases:
        space_names.add(c.rag_space)
        space, err = _resolve_space(c.rag_space, forced_space_id=forced_space_id, cache=space_cache)
        if err:
            issues.append(BenchmarkPreflightIssue(c.case_id, err, f"rag_space={c.rag_space!r}", c.rag_space))
            continue
        key = str(space.id)
        by_space.setdefault(key, (space, []))[1].append(c)

    # Résolution batch par espace (réutilise l'anti-N+1 du résolveur).
    n_resolved = 0
    for space, cases in by_space.values():
        results = resolve_many_stable_ground_truths(
            rag_space=space, ground_truths=[c.ground_truth for c in cases]
        )
        for c, r in zip(cases, results, strict=True):
            if isinstance(r, sgt.StableGroundTruthError):
                issues.append(BenchmarkPreflightIssue(c.case_id, r.code, str(r), c.rag_space))
            elif not r.chunk_ids:
                issues.append(BenchmarkPreflightIssue(c.case_id, ERR_NO_RESOLVED_CHUNKS, "", c.rag_space))
            else:
                resolved.append(_resolved_case(c, r))
                n_resolved += 1

    duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    report = {
        "dataset": str(dataset_path) if dataset_path else None,
        "cases_total": len(v1_cases) + len(v2_cases),
        "schema_versions": {"1": len(v1_cases), "2": len(v2_cases)},
        "v2_resolved": n_resolved,
        "v2_failed": len(issues),
        "rag_spaces": sorted(space_names),
        "duration_ms": duration_ms,
        "official": len(issues) == 0,
    }
    if issues:
        report["issues"] = [i.to_dict() for i in issues]
    return PreflightOutcome(cases=tuple(resolved), issues=tuple(issues), report=report)


def load_and_preflight(
    path: str | Path, *, forced_space_id: str | None = None, allow_unresolved: bool = False
) -> PreflightOutcome:
    """Charge un JSONL mixte v1/v2 et exécute le préflight. Lève
    ``BenchmarkDatasetPreflightError`` si des cas v2 échouent et ``allow_unresolved``
    est faux (run officiel refusé, atomicité)."""
    v1, v2 = load_mixed_dataset(path)
    outcome = run_preflight(v1_cases=v1, v2_cases=v2, forced_space_id=forced_space_id, dataset_path=path)
    if outcome.issues and not allow_unresolved:
        raise BenchmarkDatasetPreflightError(list(outcome.issues))
    return outcome
