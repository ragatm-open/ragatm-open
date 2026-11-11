"""Schéma de vérité terrain STABLE v2 — validation, normalisation, case_id, evidence.

Tests PURS (aucun DB, aucun retrieval). Fixtures synthétiques, non sensibles.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from apps.rag_core.benchmark import stable_ground_truth as gt


# --------------------------------------------------------------------------- #
# Coordonnées Excel
# --------------------------------------------------------------------------- #
def test_column_roundtrip_beyond_z():
    for n in (1, 26, 27, 52, 53, 702, 703):
        assert gt.col_to_num(gt.num_to_col(n)) == n
    assert gt.num_to_col(1) == "A"
    assert gt.num_to_col(27) == "AA"
    assert gt.num_to_col(703) == "AAA"


def test_normalize_cell_range_and_columns():
    assert gt.normalize_cell_range("$a$1:$h$12") == "A1:H12"
    assert gt.normalize_excel_column("$aa") == "AA"


@pytest.mark.parametrize("bad", ["A1", "A1:B", "1:2", "A0:B2", "B2:A1", "A5:A3"])
def test_invalid_cell_ranges_rejected(bad):
    with pytest.raises(gt.StableGroundTruthError) as e:
        gt.parse_cell_range(bad)
    assert e.value.code == gt.ERR_INVALID_CELL_RANGE


# --------------------------------------------------------------------------- #
# Normalisation textuelle
# --------------------------------------------------------------------------- #
def test_evidence_text_normalization_stable():
    a = gt.normalize_benchmark_evidence_text("Coût\r\n  2 024,50   \r\n")
    b = gt.normalize_benchmark_evidence_text("Coût\n2 024,50")
    assert a == b == "Coût\n2 024,50"


def test_evidence_text_nfkc_unifies():
    # Chiffres pleine largeur + espace insécable -> forme compatible NFKC.
    assert gt.normalize_benchmark_evidence_text("１ ２") == gt.normalize_benchmark_evidence_text("1 2")


def test_unknown_normalization_version_rejected():
    with pytest.raises(gt.StableGroundTruthError) as e:
        gt.normalize_benchmark_evidence_text("x", version=99)
    assert e.value.code == gt.ERR_UNSUPPORTED_NORMALIZATION_VERSION


# --------------------------------------------------------------------------- #
# SHA-256 documentaire
# --------------------------------------------------------------------------- #
def test_document_sha_validation():
    assert gt.is_valid_sha256("a" * 64)
    assert not gt.is_valid_sha256("A" * 64)   # pas lowercase
    assert not gt.is_valid_sha256("a" * 63)   # mauvaise longueur
    assert not gt.is_valid_sha256("z" * 64)   # non hexadécimal
    with pytest.raises(gt.StableGroundTruthError) as e:
        gt.StableDocumentRef(content_sha256="nope").validate()
    assert e.value.code == gt.ERR_INVALID_DOCUMENT_SHA256


# --------------------------------------------------------------------------- #
# Locator
# --------------------------------------------------------------------------- #
def _loc(rng="A12:H12", rs=12, re_=12, cs="A", ce="H"):
    return gt.ExcelRegionLocator(kind=gt.LOCATOR_KIND_XLSX, sheet_name="Produits",
                                 cell_range=rng, row_start=rs, row_end=re_,
                                 column_start=cs, column_end=ce)


def test_locator_valid():
    _loc().validate()  # ne lève pas


def test_locator_bounds_must_match_cell_range():
    with pytest.raises(gt.StableGroundTruthError) as e:
        _loc(rng="A1:B10", rs=5, re_=20, cs="A", ce="B").validate()
    assert e.value.code == gt.ERR_INVALID_LOCATOR


def test_locator_unsupported_kind():
    loc = gt.ExcelRegionLocator(kind="pdf_page", sheet_name="S", cell_range="A1:B2",
                                row_start=1, row_end=2, column_start="A", column_end="B")
    with pytest.raises(gt.StableGroundTruthError) as e:
        loc.validate()
    assert e.value.code == gt.ERR_UNSUPPORTED_LOCATOR_KIND


# --------------------------------------------------------------------------- #
# range_relation
# --------------------------------------------------------------------------- #
def test_range_relation():
    a = (1, 1, 8, 10)   # A1:H10
    assert gt.range_relation(a, (1, 1, 8, 10)) == "exact"
    assert gt.range_relation(a, (1, 1, 8, 5)) == "contains"       # a contient b
    assert gt.range_relation((1, 1, 8, 5), a) == "contained_by"   # b contient a
    assert gt.range_relation(a, (1, 5, 8, 20)) == "overlaps"
    assert gt.range_relation(a, (1, 11, 8, 20)) == "disjoint"
    assert gt.range_relation(a, (9, 1, 12, 10)) == "disjoint"     # autre colonne


# --------------------------------------------------------------------------- #
# case_id stable
# --------------------------------------------------------------------------- #
def test_case_id_stable_and_uuid_independent():
    kw = dict(document_sha256="a" * 64, sheet_name="Produits", cell_range="A12:H12",
              measure_columns=["Code SI"], row_identifier="ABC")
    id1 = gt.build_stable_case_id(**kw)
    id2 = gt.build_stable_case_id(**kw)
    assert id1 == id2 and id1.startswith("xlsx-")
    # Normalisation : casse/accents des champs logiques n'altèrent pas l'identité.
    kw2 = {**kw, "sheet_name": "Produits", "row_identifier": "abc"}
    assert gt.build_stable_case_id(**kw2) != id1  # row id différent -> id différent
    # sha différent -> id différent
    assert gt.build_stable_case_id(**{**kw, "document_sha256": "b" * 64}) != id1


def test_case_id_not_affected_by_pythonhashseed():
    snippet = (
        "from apps.rag_core.benchmark import stable_ground_truth as g;"
        "print(g.build_stable_case_id(document_sha256='a'*64, sheet_name='Réf',"
        "cell_range='A1:C3', measure_columns=['Coût'], row_identifier='X'))"
    )
    outs = []
    for hs in ("0", "1", "random"):
        env = {**__import__("os").environ, "PYTHONHASHSEED": hs, "DJANGO_SETTINGS_MODULE": "config.settings.dev"}
        r = subprocess.run([sys.executable, "-c", "import django;django.setup();" + snippet],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr[-400:]
        outs.append(r.stdout.strip())
    assert len(set(outs)) == 1, outs


# --------------------------------------------------------------------------- #
# schema_version + sérialisation canonique
# --------------------------------------------------------------------------- #
def _gt():
    return gt.StableGroundTruth(
        document_ref=gt.StableDocumentRef(content_sha256="a" * 64, filename="x.xlsx"),
        locator=_loc(),
        evidence=gt.StableEvidence(normalization_version=1, region_text_sha256="b" * 64),
        match_policy=gt.MATCH_ANY,
    )


def test_parse_case_rejects_unknown_schema_version():
    case = gt.to_case_dict(case_id="xlsx-x", question="q?", rag_space="s", ground_truth=_gt())
    assert case["schema_version"] == 2
    case["schema_version"] = 3
    with pytest.raises(gt.StableGroundTruthError) as e:
        gt.parse_case(case)
    assert e.value.code == gt.ERR_INVALID_SCHEMA_VERSION


def test_roundtrip_and_canonical_bytes_key_order_independent():
    case = gt.to_case_dict(case_id="xlsx-x", question="q?", rag_space="s", ground_truth=_gt())
    parsed = gt.parse_case(case)
    assert parsed.document_ref.content_sha256 == "a" * 64
    # Dump canonique indépendant de l'ordre des clés du dict source.
    shuffled = dict(reversed(list(case.items())))
    assert gt.dump_stable_case(case) == gt.dump_stable_case(shuffled)


def test_region_cells_canonical_text_order_independent():
    a = gt.RegionCells(cells={(12, 8): "H", (12, 1): "A"})
    b = gt.RegionCells(cells={(12, 1): "A", (12, 8): "H"})
    assert a.canonical_text() == b.canonical_text()
    assert a.sha256() == b.sha256()
