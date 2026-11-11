"""Résolveur STRICT de vérité terrain stable -> IDs courants de l'ingestion.

Traduit une référence LOGIQUE (`StableGroundTruth`, indépendante des UUID) vers le
document et les chunks de l'ingestion COURANTE. Ne fait AUCUN retrieval (pas de
vectoriel, FTS, reranker, LLM) : il n'utilise que le document, les métadonnées de
chunks et le contenu normalisé.

Règle d'or : jamais de choix arbitraire (`first()`, `[0]`, tri par UUID). Toute
pluralité est soit résolue par une clé logique, soit rejetée comme ambiguë.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

from apps.rag_core.benchmark import stable_ground_truth as gt
from apps.rag_core.benchmark.stable_ground_truth import (
    RegionCells,
    StableGroundTruth,
    StableGroundTruthError,
    range_relation,
)
from apps.rag_core.models.chunk import Chunk
from apps.rag_core.models.document import Document


@dataclass(frozen=True)
class ResolvedStableGroundTruth:
    document_id: UUID
    chunk_ids: tuple[UUID, ...]
    match_policy: str
    resolution_kind: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Parsing markdown -> cellules Excel (métadonnées d'abord, texte en fallback)
# --------------------------------------------------------------------------- #
_SEP_RE = re.compile(r"^\|[\s:\-|]+\|$")


def _parse_markdown_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Extrait (en-têtes, lignes de données) d'un chunk Markdown Excel. Les lignes de
    contexte (« Feuille : … », « Identifiants : … », vide) sont ignorées."""
    lines = [ln for ln in (text or "").splitlines()]
    table = [ln for ln in lines if ln.strip().startswith("|")]
    if not table:
        return [], []

    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    headers = cells(table[0])
    data = [cells(ln) for ln in table[1:] if not _SEP_RE.match(ln.strip())]
    return headers, data


def _chunk_column_map(meta: dict, n_md_cols: int) -> list[int]:
    """Colonne Markdown i -> numéro de colonne Excel. Gère le chunk standard
    (colonnes contiguës à partir de ``column_start``) et le chunk à découpage par
    COLONNES (1re colonne = identifiant, puis fenêtre contiguë)."""
    col_start = gt.col_to_num(meta["column_start"])
    identity = [gt.col_to_num(c) for c in (meta.get("identity_columns") or [])]
    axis = meta.get("chunk_axis")
    if identity and axis in ("columns", "rows_and_columns"):
        # rendu = [identité…, fenêtre contiguë column_start..]
        out = list(identity)
        i = col_start
        while len(out) < n_md_cols:
            out.append(i)
            i += 1
        return out[:n_md_cols]
    return [col_start + i for i in range(n_md_cols)]


def _chunk_rect(meta: dict) -> tuple[int, int, int, int]:
    return (gt.col_to_num(meta["column_start"]), int(meta["row_start"]),
            gt.col_to_num(meta["column_end"]), int(meta["row_end"]))


def extract_region_cells(chunks: list, locator, *, warnings: list[str]) -> RegionCells:
    """Reconstruit les cellules NON VIDES de la région (clip au locator), depuis les
    chunks couvrants — INDÉPENDAMMENT du découpage. Détecte les conflits (même cellule,
    valeurs différentes) -> ``ambiguous_region``."""
    lc1, lr1, lc2, lr2 = locator.rect
    cells: dict[tuple[int, int], str] = {}
    for ch in chunks:
        meta = ch.meta or {}
        r0 = int(meta["row_start"])
        headers, rows = _parse_markdown_table(ch.text or "")
        colmap = _chunk_column_map(meta, max((len(r) for r in rows), default=len(headers)))
        for j, row in enumerate(rows):
            excel_row = r0 + j
            if excel_row < lr1 or excel_row > lr2:
                continue
            for i, val in enumerate(row):
                if i >= len(colmap):
                    continue
                col = colmap[i]
                if col < lc1 or col > lc2:
                    continue
                norm = gt.normalize_benchmark_evidence_text(val)
                if not norm:
                    continue
                key = (excel_row, col)
                if key in cells and cells[key] != norm:
                    raise StableGroundTruthError(
                        gt.ERR_AMBIGUOUS_REGION,
                        f"cellule {gt.num_to_col(col)}{excel_row} couverte par deux contenus",
                    )
                cells[key] = norm
    return RegionCells(cells=cells)


# --------------------------------------------------------------------------- #
# Résolution document (par SHA-256, jamais par UUID/nom seul)
# --------------------------------------------------------------------------- #
def _resolve_document(rag_space, ref) -> Document:
    ref.validate()
    docs = list(
        Document.objects.filter(rag_space=rag_space, sha256=ref.content_sha256)
        .only("id", "filename", "meta")
        .order_by("id")  # ordre stable pour un diagnostic reproductible, PAS un choix
    )
    if not docs:
        raise StableGroundTruthError(gt.ERR_DOCUMENT_NOT_FOUND, ref.content_sha256)
    if len(docs) == 1:
        return docs[0]
    # Pluralité : désambiguïsation par champs SECONDAIRES uniquement (jamais first()).
    cand = docs
    if ref.relative_path:
        cand = [d for d in cand if (d.meta or {}).get("relative_path") == ref.relative_path]
    if len(cand) != 1 and ref.filename:
        cand = [d for d in cand if d.filename == ref.filename]
    if len(cand) == 1:
        return cand[0]
    raise StableGroundTruthError(
        gt.ERR_AMBIGUOUS_DOCUMENT_REFERENCE,
        f"{len(docs)} documents de même sha256, non départageables",
    )


def _canonical_chunk_key(ch) -> tuple:
    meta = ch.meta or {}
    return (
        gt.normalize_sheet_name(meta.get("sheet_name", "")),
        int(meta.get("row_start", 0)),
        gt.col_to_num(meta.get("column_start", "A")),
        int(meta.get("row_end", 0)),
        gt.col_to_num(meta.get("column_end", "A")),
        int(meta.get("column_window_index", 0) or 0),
        int(meta.get("narrative_part_index", 0) or 0),
        int(meta.get("chunk_index", 0) or 0),
        str(ch.id),  # ultime départage, jamais discriminant logique
    )


def resolve_stable_ground_truth(
    *, rag_space, ground_truth: StableGroundTruth
) -> ResolvedStableGroundTruth:
    """Résout une vérité terrain stable vers les IDs courants. Lève
    ``StableGroundTruthError`` (code stable) sur toute impossibilité/ambiguïté."""
    ground_truth.validate()
    loc = ground_truth.locator
    doc = _resolve_document(rag_space, ground_truth.document_ref)

    sheet_norm = gt.normalize_sheet_name(loc.sheet_name)
    doc_chunks = list(
        Chunk.objects.filter(document=doc, meta__kind="excel_table").only("id", "meta", "text")
    )
    in_sheet = [c for c in doc_chunks if gt.normalize_sheet_name((c.meta or {}).get("sheet_name", "")) == sheet_norm]
    if not in_sheet:
        raise StableGroundTruthError(gt.ERR_SHEET_NOT_FOUND, loc.sheet_name)

    # Chunks recouvrant (tout ou partie) la région.
    covering = [c for c in in_sheet if range_relation(loc.rect, _chunk_rect(c.meta or {})) != "disjoint"]
    if not covering:
        raise StableGroundTruthError(gt.ERR_REGION_NOT_FOUND, loc.cell_range)

    # Couverture COMPLÈTE : chaque cellule du locator doit appartenir à un chunk.
    lc1, lr1, lc2, lr2 = loc.rect
    n_cells = (lr2 - lr1 + 1) * (lc2 - lc1 + 1)
    if n_cells > 200_000:
        raise StableGroundTruthError(gt.ERR_INVALID_LOCATOR, "région trop vaste pour un cas lookup")
    covered: set[tuple[int, int]] = set()
    for c in covering:
        cc1, cr1, cc2, cr2 = _chunk_rect(c.meta or {})
        for r in range(max(lr1, cr1), min(lr2, cr2) + 1):
            for col in range(max(lc1, cc1), min(lc2, cc2) + 1):
                covered.add((r, col))
    full = all((r, col) in covered for r in range(lr1, lr2 + 1) for col in range(lc1, lc2 + 1))
    if not full:
        raise StableGroundTruthError(gt.ERR_PARTIAL_REGION_COVERAGE, loc.cell_range)

    # Evidence : reconstruire le contenu canonique et comparer le hash.
    warnings: list[str] = []
    region = extract_region_cells(covering, loc, warnings=warnings)
    got = region.sha256()
    if got != ground_truth.evidence.region_text_sha256:
        raise StableGroundTruthError(
            gt.ERR_EVIDENCE_HASH_MISMATCH,
            f"attendu={ground_truth.evidence.region_text_sha256[:12]}… calculé={got[:12]}…",
        )

    ordered = sorted(covering, key=_canonical_chunk_key)
    kind = "single_chunk" if len(ordered) == 1 else "multi_chunk"
    return ResolvedStableGroundTruth(
        document_id=doc.id,
        chunk_ids=tuple(c.id for c in ordered),
        match_policy=ground_truth.match_policy,
        resolution_kind=kind,
        warnings=tuple(warnings),
    )


def resolve_many_stable_ground_truths(
    *, rag_space, ground_truths: list[StableGroundTruth]
) -> list[ResolvedStableGroundTruth | StableGroundTruthError]:
    """Résolution en lot : précharge documents (par sha) et chunks (par document) pour
    éviter le N+1. Renvoie, pour chaque entrée, un résultat OU l'erreur (jamais None)."""
    shas = {g.document_ref.content_sha256 for g in ground_truths}
    docs_by_sha: dict[str, list[Document]] = {}
    for d in Document.objects.filter(rag_space=rag_space, sha256__in=shas).only("id", "filename", "meta"):
        docs_by_sha.setdefault(d.sha256, []).append(d)
    doc_ids = [d.id for lst in docs_by_sha.values() for d in lst]
    chunks_by_doc: dict[UUID, list[Chunk]] = {}
    for c in Chunk.objects.filter(document_id__in=doc_ids, meta__kind="excel_table").only("id", "document_id", "meta", "text"):
        chunks_by_doc.setdefault(c.document_id, []).append(c)

    out: list[ResolvedStableGroundTruth | StableGroundTruthError] = []
    for g in ground_truths:
        try:
            out.append(_resolve_one_cached(g, docs_by_sha, chunks_by_doc))
        except StableGroundTruthError as e:
            out.append(e)
    return out


def build_stable_ground_truth_from_chunks(
    *, document_ref, locator, chunks: list, match_policy: str = gt.MATCH_ANY,
    expected_value: str | None = None,
) -> StableGroundTruth:
    """Construit une vérité terrain v2 depuis des chunks COURANTS (prépa migration §42,
    et fixtures de test). L'evidence est bâtie par la MÊME extraction que le résolveur
    vérifie -> génération et résolution concordent par construction.

    Ne câble PAS le générateur officiel (hors périmètre de ce lot)."""
    covering = [
        c for c in chunks
        if range_relation(locator.rect, _chunk_rect(c.meta or {})) != "disjoint"
    ]
    region = extract_region_cells(covering, locator, warnings=[])
    ev = gt.StableEvidence(
        normalization_version=gt.NORMALIZATION_VERSION,
        region_text_sha256=region.sha256(),
        expected_value_sha256=(
            gt.sha256_hex(gt.normalize_benchmark_evidence_text(expected_value))
            if expected_value is not None else None
        ),
    )
    out = StableGroundTruth(document_ref=document_ref, locator=locator, evidence=ev,
                            match_policy=match_policy)
    out.validate()
    return out


def _resolve_one_cached(g: StableGroundTruth, docs_by_sha, chunks_by_doc) -> ResolvedStableGroundTruth:
    g.validate()
    ref = g.document_ref
    docs = sorted(docs_by_sha.get(ref.content_sha256, []), key=lambda d: str(d.id))
    if not docs:
        raise StableGroundTruthError(gt.ERR_DOCUMENT_NOT_FOUND, ref.content_sha256)
    if len(docs) == 1:
        doc = docs[0]
    else:
        cand = docs
        if ref.relative_path:
            cand = [d for d in cand if (d.meta or {}).get("relative_path") == ref.relative_path]
        if len(cand) != 1 and ref.filename:
            cand = [d for d in cand if d.filename == ref.filename]
        if len(cand) != 1:
            raise StableGroundTruthError(gt.ERR_AMBIGUOUS_DOCUMENT_REFERENCE, ref.content_sha256)
        doc = cand[0]

    loc = g.locator
    sheet_norm = gt.normalize_sheet_name(loc.sheet_name)
    in_sheet = [
        c for c in chunks_by_doc.get(doc.id, [])
        if gt.normalize_sheet_name((c.meta or {}).get("sheet_name", "")) == sheet_norm
    ]
    if not in_sheet:
        raise StableGroundTruthError(gt.ERR_SHEET_NOT_FOUND, loc.sheet_name)
    covering = [c for c in in_sheet if range_relation(loc.rect, _chunk_rect(c.meta or {})) != "disjoint"]
    if not covering:
        raise StableGroundTruthError(gt.ERR_REGION_NOT_FOUND, loc.cell_range)
    lc1, lr1, lc2, lr2 = loc.rect
    covered: set[tuple[int, int]] = set()
    for c in covering:
        cc1, cr1, cc2, cr2 = _chunk_rect(c.meta or {})
        for r in range(max(lr1, cr1), min(lr2, cr2) + 1):
            for col in range(max(lc1, cc1), min(lc2, cc2) + 1):
                covered.add((r, col))
    if not all((r, col) in covered for r in range(lr1, lr2 + 1) for col in range(lc1, lc2 + 1)):
        raise StableGroundTruthError(gt.ERR_PARTIAL_REGION_COVERAGE, loc.cell_range)
    region = extract_region_cells(covering, loc, warnings=[])
    if region.sha256() != g.evidence.region_text_sha256:
        raise StableGroundTruthError(gt.ERR_EVIDENCE_HASH_MISMATCH, loc.cell_range)
    ordered = sorted(covering, key=_canonical_chunk_key)
    return ResolvedStableGroundTruth(
        document_id=doc.id,
        chunk_ids=tuple(c.id for c in ordered),
        match_policy=g.match_policy,
        resolution_kind="single_chunk" if len(ordered) == 1 else "multi_chunk",
    )
