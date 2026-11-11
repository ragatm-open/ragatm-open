"""Résolveur de vérité terrain stable -> IDs courants. Fixtures SYNTHÉTIQUES.

Couvre : résolution par SHA (jamais UUID/nom seul), réingestion à UUID différents,
rechunking compatible, changement de contenu, ambiguïtés, couverture partielle,
evidence mismatch. Aucun appel retrieval.
"""
from __future__ import annotations

import pytest

from apps.rag_core.benchmark import stable_ground_truth as gt
from apps.rag_core.benchmark import stable_source_resolver as res
from apps.rag_core.models.chunk import Chunk
from apps.rag_core.models.document import Document, DocumentStatus
from apps.rag_core.models.rag_space import RagSpace
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

SHA1 = "1" * 64
SHA2 = "2" * 64


@pytest.fixture
def space(db):
    ws = Workspace.objects.create(name="w-gt")
    return RagSpace.objects.create(name="gt-space", workspace=ws)


def _doc(space, sha=SHA1, filename="rapport.xlsx", status=DocumentStatus.READY, relative_path=None):
    d = Document.objects.create(
        rag_space=space, filename=filename, sha256=sha, status=status,
        content_type=gt.XLSX_MEDIA_TYPE, meta=({"relative_path": relative_path} if relative_path else {}),
    )
    return d


def _chunk(doc, sheet, col_start, row_start, headers, rows, *, chunk_index=0,
           identity_cols=None, chunk_axis=None):
    ncols = len(headers)
    col_end = gt.num_to_col(gt.col_to_num(col_start) + ncols - 1)
    row_end = row_start + len(rows) - 1
    lines = [f"Feuille : {sheet} ({col_start}{row_start}:{col_end}{row_end})", ""]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    meta = {
        "kind": "excel_table", "sheet_name": sheet,
        "cell_range": f"{col_start}{row_start}:{col_end}{row_end}",
        "row_start": row_start, "row_end": row_end,
        "column_start": col_start, "column_end": col_end, "chunk_index": chunk_index,
    }
    if identity_cols:
        meta["identity_columns"] = identity_cols
    if chunk_axis:
        meta["chunk_axis"] = chunk_axis
    return Chunk.objects.create(rag_space=doc.rag_space, document=doc,
                                chunk_index=chunk_index, text="\n".join(lines), meta=meta)


def _loc(sheet, rng):
    c1, r1, c2, r2 = gt.parse_cell_range(rng)
    return gt.ExcelRegionLocator(kind=gt.LOCATOR_KIND_XLSX, sheet_name=sheet, cell_range=rng,
                                 row_start=r1, row_end=r2,
                                 column_start=gt.num_to_col(c1), column_end=gt.num_to_col(c2))


# --------------------------------------------------------------------------- #
def test_resolve_single_chunk(space):
    d = _doc(space)
    c = _chunk(d, "Produits", "A", 12, ["Nom", "Code SI"], [["ABC", "X1"]])
    loc = _loc("Produits", "A12:B12")
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1, filename="rapport.xlsx"),
        locator=loc, chunks=[c], expected_value="X1")
    r = res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert r.document_id == d.id
    assert r.chunk_ids == (c.id,)
    assert r.resolution_kind == "single_chunk"


def test_resolve_multi_chunk_row_split_rechunking_stable(space):
    """§27 : même région, chunking différent (1 chunk -> 2). Même evidence, résolution
    complète."""
    d = _doc(space)
    # « Avant » : un seul chunk A12:B14.
    big = _chunk(d, "S", "A", 12, ["Nom", "V"], [["a", "1"], ["b", "2"], ["c", "3"]])
    loc = _loc("S", "A12:B14")
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1), locator=loc, chunks=[big])
    # « Après » rechunking : deux chunks A12:B13 + A14:B14 (mêmes cellules).
    big.delete()
    c1 = _chunk(d, "S", "A", 12, ["Nom", "V"], [["a", "1"], ["b", "2"]], chunk_index=0)
    c2 = _chunk(d, "S", "A", 14, ["Nom", "V"], [["c", "3"]], chunk_index=1)
    r = res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert set(r.chunk_ids) == {c1.id, c2.id}
    assert r.resolution_kind == "multi_chunk"


def test_reingestion_new_uuids_same_reference(space):
    """§26 : suppression/recréation -> UUID différents, MÊME référence v2, résolution OK."""
    d1 = _doc(space)
    c1 = _chunk(d1, "S", "A", 5, ["Nom", "V"], [["x", "42"]])
    loc = _loc("S", "A5:B5")
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1), locator=loc, chunks=[c1])
    old_doc_uuid, old_chunk_uuid = d1.id, c1.id
    Chunk.objects.filter(document=d1).delete()
    d1.delete()
    d2 = _doc(space)                       # nouveaux UUID
    c2 = _chunk(d2, "S", "A", 5, ["Nom", "V"], [["x", "42"]])
    assert d2.id != old_doc_uuid and c2.id != old_chunk_uuid
    r = res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert r.document_id == d2.id and r.chunk_ids == (c2.id,)


def test_document_not_found(space):
    _doc(space, sha=SHA1)
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA2),
        locator=_loc("S", "A1:B1"), chunks=[])
    # (evidence vide car pas de chunk ; on force une evidence valide en la remplaçant)
    g = gt.StableGroundTruth(document_ref=g.document_ref, locator=g.locator,
                             evidence=gt.StableEvidence(1, "a" * 64), match_policy="any")
    with pytest.raises(gt.StableGroundTruthError) as e:
        res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert e.value.code == gt.ERR_DOCUMENT_NOT_FOUND


def test_content_change_detected_via_sha(space):
    """§28 : contenu modifié -> sha256 différent -> la référence ne résout plus."""
    d = _doc(space, sha=SHA1)
    c = _chunk(d, "S", "A", 1, ["Nom", "V"], [["x", "old"]])
    loc = _loc("S", "A1:B1")
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1), locator=loc, chunks=[c])
    # Réingestion avec contenu modifié -> nouveau sha256.
    Chunk.objects.filter(document=d).delete()
    d.delete()
    d2 = _doc(space, sha=SHA2)
    _chunk(d2, "S", "A", 1, ["Nom", "V"], [["x", "new"]])
    with pytest.raises(gt.StableGroundTruthError) as e:
        res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert e.value.code == gt.ERR_DOCUMENT_NOT_FOUND


def test_same_name_different_content_resolves_by_hash(space):
    """§29 : deux fichiers de même nom, hashes différents -> jamais de choix par nom."""
    dA = _doc(space, sha=SHA1, filename="rapport.xlsx")
    cA = _chunk(dA, "S", "A", 1, ["Nom", "V"], [["x", "A"]])
    dB = _doc(space, sha=SHA2, filename="rapport.xlsx")
    _chunk(dB, "S", "A", 1, ["Nom", "V"], [["x", "B"]])
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1, filename="rapport.xlsx"),
        locator=_loc("S", "A1:B1"), chunks=[cA])
    r = res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert r.document_id == dA.id


def test_ambiguous_document_reference(space):
    """§30 : deux documents de même sha (statut FAILED, hors contrainte d'unicité) sans
    relative_path -> ambiguïté ; avec relative_path -> unique."""
    dA = _doc(space, sha=SHA1, status=DocumentStatus.FAILED, relative_path="a/rapport.xlsx")
    _doc(space, sha=SHA1, status=DocumentStatus.FAILED, relative_path="b/rapport.xlsx")
    ref_amb = gt.StableDocumentRef(content_sha256=SHA1)
    with pytest.raises(gt.StableGroundTruthError) as e:
        res._resolve_document(space, ref_amb)
    assert e.value.code == gt.ERR_AMBIGUOUS_DOCUMENT_REFERENCE
    # Désambiguïsation par relative_path.
    ref_ok = gt.StableDocumentRef(content_sha256=SHA1, relative_path="a/rapport.xlsx")
    assert res._resolve_document(space, ref_ok).id == dA.id


def test_sheet_not_found(space):
    d = _doc(space)
    c = _chunk(d, "Produits", "A", 1, ["Nom", "V"], [["x", "1"]])
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1),
        locator=_loc("Produits", "A1:B1"), chunks=[c])
    g = gt.StableGroundTruth(document_ref=g.document_ref,
                             locator=_loc("Absente", "A1:B1"), evidence=g.evidence)
    with pytest.raises(gt.StableGroundTruthError) as e:
        res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert e.value.code == gt.ERR_SHEET_NOT_FOUND


def test_region_not_found(space):
    d = _doc(space)
    _chunk(d, "S", "A", 1, ["Nom", "V"], [["x", "1"]])   # A1:B1
    g = gt.StableGroundTruth(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1),
        locator=_loc("S", "A50:B50"), evidence=gt.StableEvidence(1, "a" * 64))
    with pytest.raises(gt.StableGroundTruthError) as e:
        res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert e.value.code == gt.ERR_REGION_NOT_FOUND


def test_partial_region_coverage(space):
    d = _doc(space)
    c = _chunk(d, "S", "A", 1, ["Nom", "V"], [["x", "1"]])   # couvre A1:B1
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1),
        locator=_loc("S", "A1:B10"), chunks=[c])   # demande A1:B10
    with pytest.raises(gt.StableGroundTruthError) as e:
        res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert e.value.code == gt.ERR_PARTIAL_REGION_COVERAGE


def test_evidence_hash_mismatch(space):
    d = _doc(space)
    c = _chunk(d, "S", "A", 1, ["Nom", "V"], [["x", "1"]])
    loc = _loc("S", "A1:B1")
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1), locator=loc, chunks=[c])
    # Le chunk change de valeur après coup -> hash de région diffère.
    c.text = c.text.replace("| x | 1 |", "| x | 999 |")
    c.save(update_fields=["text"])
    with pytest.raises(gt.StableGroundTruthError) as e:
        res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert e.value.code == gt.ERR_EVIDENCE_HASH_MISMATCH


def test_ambiguous_region(space):
    d = _doc(space)
    # Deux chunks recouvrant la MÊME cellule avec des valeurs différentes.
    _chunk(d, "S", "A", 1, ["Nom", "V"], [["x", "1"]], chunk_index=0)
    _chunk(d, "S", "A", 1, ["Nom", "V"], [["x", "2"]], chunk_index=1)
    g = gt.StableGroundTruth(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1),
        locator=_loc("S", "A1:B1"), evidence=gt.StableEvidence(1, "a" * 64))
    with pytest.raises(gt.StableGroundTruthError) as e:
        res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    assert e.value.code == gt.ERR_AMBIGUOUS_REGION


def test_canonical_chunk_order_stable(space):
    d = _doc(space)
    c2 = _chunk(d, "S", "A", 14, ["N", "V"], [["c", "3"]], chunk_index=1)
    c1 = _chunk(d, "S", "A", 12, ["N", "V"], [["a", "1"], ["b", "2"]], chunk_index=0)
    g = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1),
        locator=_loc("S", "A12:B14"), chunks=[c1, c2])
    r = res.resolve_stable_ground_truth(rag_space=space, ground_truth=g)
    # Ordre canonique : row_start croissant -> c1 (12) avant c2 (14).
    assert r.chunk_ids == (c1.id, c2.id)


def test_resolve_many_batch(space):
    d = _doc(space)
    c = _chunk(d, "S", "A", 1, ["N", "V"], [["x", "1"]])
    good = res.build_stable_ground_truth_from_chunks(
        document_ref=gt.StableDocumentRef(content_sha256=SHA1), locator=_loc("S", "A1:B1"), chunks=[c])
    bad = gt.StableGroundTruth(document_ref=gt.StableDocumentRef(content_sha256=SHA2),
                               locator=_loc("S", "A1:B1"), evidence=gt.StableEvidence(1, "a" * 64))
    out = res.resolve_many_stable_ground_truths(rag_space=space, ground_truths=[good, bad])
    assert isinstance(out[0], res.ResolvedStableGroundTruth)
    assert isinstance(out[1], gt.StableGroundTruthError) and out[1].code == gt.ERR_DOCUMENT_NOT_FOUND
