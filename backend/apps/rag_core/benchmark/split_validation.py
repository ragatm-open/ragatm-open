"""Split STRICTEMENT DOCUMENTAIRE (par content_sha256) + contrôles de fuite.

L'unité de split est le **document** : un classeur entier appartient soit à la
calibration, soit au holdout, jamais aux deux. Jamais de split par question / feuille /
fact_group seul (deux lignes d'un même classeur ne doivent pas se retrouver de part et
d'autre). Déterministe : indépendant de l'ordre PostgreSQL, des UUID, de l'heure, du FS.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from apps.rag_core.benchmark import stable_ground_truth as sgt

# Codes de fuite STABLES.
LEAK_DOCUMENT = "document_leak"
LEAK_CASE_ID = "case_id_leak"
LEAK_FACT_GROUP = "fact_group_leak"
LEAK_QUESTION = "question_leak"
LEAK_LOCATOR = "locator_leak"
LEAK_STABLE_GROUND_TRUTH = "stable_ground_truth_leak"


def _doc_sha(case: dict) -> str:
    return ((case.get("ground_truth") or {}).get("document_ref") or {}).get("content_sha256", "")


def _locator_key(case: dict) -> str:
    loc = (case.get("ground_truth") or {}).get("locator") or {}
    return f"{_doc_sha(case)}|{sgt.normalize_sheet_name(loc.get('sheet_name', ''))}|{loc.get('cell_range', '')}"


def _evidence_key(case: dict) -> str:
    ev = (case.get("ground_truth") or {}).get("evidence") or {}
    return f"{_doc_sha(case)}|{ev.get('region_text_sha256', '')}"


def canonical_order(cases: list[dict]) -> list[dict]:
    """Ordre canonique documenté (§20) : sha256 doc -> feuille -> plage -> case_id."""
    def key(c: dict):
        loc = (c.get("ground_truth") or {}).get("locator") or {}
        return (
            _doc_sha(c),
            sgt.normalize_sheet_name(loc.get("sheet_name", "")),
            loc.get("cell_range", ""),
            c.get("case_id", ""),
        )
    return sorted(cases, key=key)


def split_by_document(
    cases: list[dict], *, calibration_ratio: float = 0.70, seed: int = 1789
) -> tuple[list[dict], list[dict]]:
    """Affecte chaque DOCUMENT (par sha256) à la calibration ou au holdout. Reproductible
    (population triée AVANT la seed ; jamais d'ordre DB/UUID/heure)."""
    docs = sorted({_doc_sha(c) for c in cases if _doc_sha(c)})
    rng = random.Random(seed)
    rng.shuffle(docs)
    n_cal = int(round(len(docs) * calibration_ratio))
    cal_docs = set(docs[:n_cal])
    cal = canonical_order([c for c in cases if _doc_sha(c) in cal_docs])
    hold = canonical_order([c for c in cases if _doc_sha(c) not in cal_docs])
    return cal, hold


def find_leaks(calibration: list[dict], holdout: list[dict]) -> dict[str, list[str]]:
    """Contrôles de fuite (§14). Renvoie {code: [détails]} — vide = aucune fuite."""
    leaks: dict[str, list[str]] = {}

    def _shared(key_fn) -> list[str]:
        a = {key_fn(c) for c in calibration if key_fn(c)}
        b = {key_fn(c) for c in holdout if key_fn(c)}
        return sorted(a & b)

    checks = [
        (LEAK_DOCUMENT, _doc_sha),
        (LEAK_CASE_ID, lambda c: c.get("case_id", "")),
        (LEAK_FACT_GROUP, lambda c: c.get("fact_group_id", "")),
        (LEAK_QUESTION, lambda c: sgt.normalize_benchmark_evidence_text(c.get("question", ""))),
        (LEAK_LOCATOR, _locator_key),
        (LEAK_STABLE_GROUND_TRUTH, _evidence_key),
    ]
    for code, fn in checks:
        shared = _shared(fn)
        if shared:
            leaks[code] = shared[:20]
    return leaks


@dataclass(frozen=True)
class SplitDiversity:
    calibration_cases: int
    holdout_cases: int
    calibration_documents: int
    holdout_documents: int
    calibration_sheets: int
    holdout_sheets: int
    calibration_difficulties: int
    holdout_difficulties: int


def _sheets(cases: list[dict]) -> set[str]:
    return {sgt.normalize_sheet_name(((c.get("ground_truth") or {}).get("locator") or {}).get("sheet_name", ""))
            for c in cases}


def diversity(calibration: list[dict], holdout: list[dict]) -> SplitDiversity:
    return SplitDiversity(
        calibration_cases=len(calibration), holdout_cases=len(holdout),
        calibration_documents=len({_doc_sha(c) for c in calibration}),
        holdout_documents=len({_doc_sha(c) for c in holdout}),
        calibration_sheets=len(_sheets(calibration)), holdout_sheets=len(_sheets(holdout)),
        calibration_difficulties=len({c.get("difficulty", "") for c in calibration}),
        holdout_difficulties=len({c.get("difficulty", "") for c in holdout}),
    )


# Seuils minimaux d'un holdout officiel (§40) — sinon dataset PILOTE sans holdout.
MIN_CALIBRATION_CASES = 12
MIN_HOLDOUT_CASES = 5
MIN_DOCUMENTS_PER_SPLIT = 3


def holdout_is_officially_usable(div: SplitDiversity) -> bool:
    return (
        div.calibration_cases >= MIN_CALIBRATION_CASES
        and div.holdout_cases >= MIN_HOLDOUT_CASES
        and div.calibration_documents >= MIN_DOCUMENTS_PER_SPLIT
        and div.holdout_documents >= MIN_DOCUMENTS_PER_SPLIT
    )
