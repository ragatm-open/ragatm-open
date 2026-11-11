"""Sérialisation canonique + manifeste du dataset Excel v2.

Deux constructions identiques (même cas, même split) -> mêmes octets, mêmes SHA-256.
Aucun timestamp ni UUID courant dans les hashes canoniques.
"""
from __future__ import annotations

import json

from apps.rag_core.benchmark import stable_ground_truth as sgt

DATASET_ID = "dev-bench-excel-retrieval-v2"
DATASET_VERSION = 1
GENERATOR_VERSION = "1"
METHODOLOGY_VERSION = "1"
QUALIFICATION_PILOT = "pilot"
LIMITATION_LOW_VOLUME = "retrieval_lookup_cases_below_30"

LINE_TERMINATOR = "\n"   # documenté : LF, une ligne par cas, avec fin de ligne finale


def canonical_jsonl(cases: list[dict]) -> str:
    """JSONL déterministe : un cas canonique par ligne (clés triées), LF, fin finale."""
    return "".join(sgt.dump_stable_case(c) + LINE_TERMINATOR for c in cases)


def sha256_hex(text: str) -> str:
    return sgt.sha256_hex(text)


def build_dataset_manifest(
    *,
    corpus_manifest_checksum: str,
    seed: int,
    calibration_ratio: float,
    calibration_cases: int,
    holdout_cases: int,
    calibration_documents: int,
    holdout_documents: int,
    calibration_sha256: str,
    holdout_sha256: str,
    created_at: str,
    qualification: str = QUALIFICATION_PILOT,
    limitations: list[str] | None = None,
    human_validation_status: str = "pending",
    frozen_config: dict | None = None,
) -> dict:
    """Construit le manifeste du dataset (§18). ``created_at`` est présent pour info
    mais EXCLU du hash canonique (cf. ``manifest_canonical_sha256``)."""
    return {
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "schema_version": sgt.STABLE_GROUND_TRUTH_SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "seed": seed,
        "calibration_ratio": calibration_ratio,
        "split_unit": "document_sha256",
        "corpus_manifest_checksum": corpus_manifest_checksum,
        "calibration_cases": calibration_cases,
        "holdout_cases": holdout_cases,
        "calibration_documents": calibration_documents,
        "holdout_documents": holdout_documents,
        "calibration_sha256": calibration_sha256,
        "holdout_sha256": holdout_sha256,
        "qualification": qualification,
        "limitations": limitations or [],
        "human_validation_status": human_validation_status,
        "frozen_config": frozen_config or default_frozen_config(),
        "created_at": created_at,
    }


def manifest_canonical_sha256(manifest: dict) -> str:
    """SHA-256 du manifeste SANS ``created_at`` (le timestamp n'entre pas dans le hash)."""
    stable = {k: v for k, v in manifest.items() if k != "created_at"}
    return sgt.sha256_hex(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def default_frozen_config() -> dict:
    """Configuration de RÉFÉRENCE gelée (§33-34) — retrieval-only, reranker OFF.
    Aucune exécution dans cette mission ; toute divergence bloque le holdout."""
    return {
        "embedding_provider": "ollama",
        "embedding_model": "nomic-embed-text",
        "embedding_dimension": 768,
        "fts_config": "simple",
        "reranker_enabled": False,
        "candidate_k": 50,
        "top_k": [1, 3, 5, 10],
        "retrieval_profile": "retrieval_only",
        "runner_version": "1",
        "dataset_version": DATASET_VERSION,
    }
