"""Construction du dataset Excel v2 OFFICIEL : migration + split + fuites + manifests.

Migre les cas ``accepted_retrieval_lookup`` (générateur) vers le schéma v2 stable
(document_ref par sha256, locator, evidence hashée), effectue un split STRICTEMENT
documentaire sans fuite, écrit les artefacts de façon ATOMIQUE et initialise l'état du
holdout (used-once). AUCUNE métrique retrieval.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from apps.rag_core.benchmark import dataset_manifest as dm
from apps.rag_core.benchmark import holdout_state as hs
from apps.rag_core.benchmark import manifest as mf
from apps.rag_core.benchmark import split_validation as sv
from apps.rag_core.benchmark import stable_ground_truth as sgt
from apps.rag_core.benchmark import stable_source_resolver as ssr
from apps.rag_core.models.chunk import Chunk
from apps.rag_core.models.document import Document

ACCEPTED_DECISION = "accepted_retrieval_lookup"


def current_corpus_manifest(rag_space) -> dict:
    """Reconstruit le manifeste COURANT du corpus (détection de dérive). Lecture seule,
    aucun contenu documentaire brut dans le résultat."""
    by_doc: dict[str, list[str]] = {}
    for doc_id, text in (Chunk.objects.filter(rag_space=rag_space)
                         .order_by("document_id", "chunk_index")
                         .values_list("document_id", "text")):
        by_doc.setdefault(str(doc_id), []).append(text or "")
    documents = []
    for d in Document.objects.filter(rag_space=rag_space).only("id", "filename", "status"):
        texts = by_doc.get(str(d.id), [])
        documents.append({
            "document_id": str(d.id), "filename": d.filename,
            "format": (d.filename or "").rsplit(".", 1)[-1].lower() if "." in (d.filename or "") else "",
            "checksum": mf.document_checksum(d.filename or "", texts),
            "status": d.status, "chunk_count": len(texts),
        })
    return mf.build_manifest(
        rag_space_id=str(rag_space.id), rag_space_name=rag_space.name,
        embedding_provider=(rag_space.embedding_provider or ""),
        embedding_model=(rag_space.embedding_model or ""),
        embedding_dimension=int(getattr(settings, "RAG_EMBEDDING_DIM", 768)),
        chunking_version="1", documents=documents, created_at="",
    )


class DatasetBuildError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class BuildResult:
    report: dict
    calibration: list[dict]
    holdout: list[dict]


def _excluded(case_id: str, code: str, extra: dict | None = None) -> dict:
    out = {"case_id": case_id, "decision": "excluded", "codes": [code],
           "source_category": (extra or {}).get("sub_category", "")}
    return out


def migrate_candidate_to_v2(candidate: dict, *, rag_space) -> dict:
    """Migre UN candidat accepté vers le schéma v2. Lève ``DatasetBuildError`` si la
    source ne peut être décrite/​résolue de façon stable (le cas sera exclu)."""
    gt = candidate.get("ground_truth") or {}
    doc_ids = candidate.get("expected_document_ids") or []
    if not doc_ids:
        raise DatasetBuildError("no_document_reference")
    doc = Document.objects.filter(id=doc_ids[0], rag_space=rag_space).only(
        "id", "filename", "sha256", "meta").first()
    if doc is None or not doc.sha256:
        raise DatasetBuildError(sgt.ERR_DOCUMENT_NOT_FOUND)

    sheet = gt.get("sheet_name") or ""
    cell_range = gt.get("cell_range") or ""
    try:
        c1, r1, c2, r2 = sgt.parse_cell_range(cell_range)
        locator = sgt.ExcelRegionLocator(
            kind=sgt.LOCATOR_KIND_XLSX, sheet_name=sheet, cell_range=sgt.normalize_cell_range(cell_range),
            row_start=r1, row_end=r2, column_start=sgt.num_to_col(c1), column_end=sgt.num_to_col(c2))
    except sgt.StableGroundTruthError as e:
        raise DatasetBuildError(e.code) from e

    chunks = list(Chunk.objects.filter(document=doc, meta__kind="excel_table").only("id", "meta", "text"))
    values = gt.get("expected_values") or ([gt.get("expected_value")] if gt.get("expected_value") else [])
    measures = gt.get("measure_columns") or ([gt.get("measure_column")] if gt.get("measure_column") else [])
    try:
        stable_gt = ssr.build_stable_ground_truth_from_chunks(
            document_ref=sgt.StableDocumentRef(
                content_sha256=doc.sha256, filename=doc.filename,
                relative_path=(doc.meta or {}).get("relative_path"), media_type=sgt.XLSX_MEDIA_TYPE),
            locator=locator, chunks=chunks,
            expected_value=str(values[0]) if values else None)
        # Prouve la résolvabilité (region_not_found / ambiguous_region… -> exclusion).
        ssr.resolve_stable_ground_truth(rag_space=rag_space, ground_truth=stable_gt)
    except sgt.StableGroundTruthError as e:
        raise DatasetBuildError(e.code) from e

    case_id = sgt.build_stable_case_id(
        document_sha256=doc.sha256, sheet_name=sheet, cell_range=cell_range,
        measure_columns=[str(m) for m in measures], row_identifier=str(gt.get("row_identifier") or ""))
    case = sgt.to_case_dict(case_id=case_id, question=candidate.get("question", ""),
                            rag_space=rag_space.name, ground_truth=stable_gt)
    # Métadonnées d'analyse NON canoniques pour la vérité terrain, mais utiles au split.
    case["difficulty"] = candidate.get("difficulty", "")
    case["category"] = candidate.get("category", "xlsx")
    case["sub_category"] = candidate.get("sub_category", "")
    case["fact_group_id"] = candidate.get("fact_group_id", "")
    return case


def migrate_accepted(rag_space, candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Migre uniquement les cas ``accepted_retrieval_lookup``. Renvoie (v2_cases, exclus)."""
    v2: list[dict] = []
    excluded: list[dict] = []
    seen_case_ids: set[str] = set()
    seen_gt: set[str] = set()
    for c in candidates:
        decision = ((c.get("methodology") or {}).get("decision")) or c.get("decision")
        if decision and decision != ACCEPTED_DECISION:
            excluded.append(_excluded(c.get("case_id", ""), decision, c))
            continue
        if c.get("task_type") not in (None, "retrieval_lookup"):
            excluded.append(_excluded(c.get("case_id", ""), "unsupported_task_type", c))
            continue
        try:
            case = migrate_candidate_to_v2(c, rag_space=rag_space)
        except DatasetBuildError as e:
            excluded.append(_excluded(c.get("case_id", ""), e.code, c))
            continue
        if case["case_id"] in seen_case_ids:
            excluded.append(_excluded(case["case_id"], "duplicate_case_id", c))
            continue
        gt_key = f"{sgt.dump_stable_case({'ground_truth': case['ground_truth']})}"
        if gt_key in seen_gt:
            excluded.append(_excluded(case["case_id"], "duplicate_stable_ground_truth", c))
            continue
        seen_case_ids.add(case["case_id"])
        seen_gt.add(gt_key)
        v2.append(case)
    return v2, excluded


def build_official_dataset(
    *, rag_space, corpus_manifest: dict, accepted_candidates: list[dict],
    out_dir: str | Path, seed: int, calibration_ratio: float, created_at: str,
) -> BuildResult:
    """Construit le dataset officiel ATOMIQUEMENT (répertoire temporaire -> renommage).
    Aucun artefact officiel partiel ne subsiste en cas d'échec."""
    out_dir = Path(out_dir)
    v2_cases, excluded = migrate_accepted(rag_space, accepted_candidates)
    if not v2_cases:
        raise DatasetBuildError("no_migratable_cases", f"{len(excluded)} exclus")

    cal, hold = sv.split_by_document(v2_cases, calibration_ratio=calibration_ratio, seed=seed)
    leaks = sv.find_leaks(cal, hold)
    if leaks:
        raise DatasetBuildError("dataset_leak", str(list(leaks.keys())))
    div = sv.diversity(cal, hold)
    usable = sv.holdout_is_officially_usable(div)

    cal_jsonl = dm.canonical_jsonl(cal)
    hold_jsonl = dm.canonical_jsonl(hold)
    cal_sha = dm.sha256_hex(cal_jsonl)
    hold_sha = dm.sha256_hex(hold_jsonl)

    manifest = dm.build_dataset_manifest(
        corpus_manifest_checksum=corpus_manifest.get("manifest_checksum", ""),
        seed=seed, calibration_ratio=calibration_ratio,
        calibration_cases=div.calibration_cases, holdout_cases=div.holdout_cases,
        calibration_documents=div.calibration_documents, holdout_documents=div.holdout_documents,
        calibration_sha256=cal_sha, holdout_sha256=hold_sha, created_at=created_at,
        limitations=([dm.LIMITATION_LOW_VOLUME] if len(v2_cases) < 30 else []),
    )
    manifest_sha = dm.manifest_canonical_sha256(manifest)

    report = {
        "source_candidates": len(accepted_candidates),
        "migrated_cases": len(v2_cases),
        "excluded": len(excluded),
        "calibration_cases": div.calibration_cases, "holdout_cases": div.holdout_cases,
        "calibration_documents": div.calibration_documents, "holdout_documents": div.holdout_documents,
        "calibration_sha256": cal_sha, "holdout_sha256": hold_sha,
        "dataset_manifest_sha256": manifest_sha,
        "corpus_manifest_checksum": corpus_manifest.get("manifest_checksum", ""),
        "leaks": {"document": 0, "case_id": 0, "fact_group": 0, "question": 0,
                  "locator": 0, "stable_ground_truth": 0},
        "qualification": manifest["qualification"],
        "holdout_officially_usable": usable,
        "seed": seed, "calibration_ratio": calibration_ratio,
    }

    # --- Écriture ATOMIQUE : tout dans un répertoire de construction puis renommage ---
    building = out_dir.with_name(out_dir.name + ".building")
    if building.exists():
        shutil.rmtree(building)
    building.mkdir(parents=True)
    try:
        (building / "manifest-corpus.json").write_text(
            _dump(corpus_manifest), encoding="utf-8")
        (building / "dataset-manifest.json").write_text(_dump(manifest), encoding="utf-8")
        (building / "calibration.jsonl").write_text(cal_jsonl, encoding="utf-8")
        (building / "holdout.jsonl").write_text(hold_jsonl, encoding="utf-8")
        (building / "rejected-or-excluded.jsonl").write_text(
            "".join(_dump(e) + "\n" for e in excluded), encoding="utf-8")
        (building / "split-report.json").write_text(_dump(report), encoding="utf-8")
        (building / "generation-report.md").write_text(_generation_report_md(report, div), encoding="utf-8")
        # État holdout : créé UNIQUEMENT après validation complète du dataset.
        hs.init_holdout_state(building, dataset_id=dm.DATASET_ID, dataset_sha256=cal_sha,
                              holdout_sha256=hold_sha, dataset_manifest_sha256=manifest_sha)
    except Exception:
        shutil.rmtree(building, ignore_errors=True)
        raise
    # Renommage atomique de la destination.
    if out_dir.exists():
        shutil.rmtree(out_dir)
    building.rename(out_dir)
    return BuildResult(report=report, calibration=cal, holdout=hold)


def _dump(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True)


def _generation_report_md(report: dict, div: sv.SplitDiversity) -> str:
    return (
        "# Dataset Excel v2 — rapport de génération\n\n"
        f"- cas source: {report['source_candidates']}\n"
        f"- migrés (retenus): {report['migrated_cases']}\n"
        f"- exclus: {report['excluded']}\n"
        f"- calibration: {div.calibration_cases} cas / {div.calibration_documents} documents\n"
        f"- holdout: {div.holdout_cases} cas / {div.holdout_documents} documents\n"
        f"- ratio: {report['calibration_ratio']} · seed: {report['seed']}\n"
        f"- fuites: 0 (document/case_id/fact_group/question/locator/evidence)\n"
        f"- calibration_sha256: {report['calibration_sha256'][:16]}…\n"
        f"- holdout_sha256: {report['holdout_sha256'][:16]}…\n"
        f"- qualification: **{report['qualification']}**"
        + (" (holdout non officiellement exploitable — volume)" if not report['holdout_officially_usable'] else "")
        + "\n\n_Aucune métrique retrieval dans ce lot._\n"
    )
