# tests/tests_root/rag_core/test_entities.py
"""Tests P2 — extraction d'entités métier (plans / ferraillage / nomenclatures)."""
from __future__ import annotations

from apps.rag_core.services.ingest.entities import (
    aggregate_page_entities,
    extract_plan_entities,
    has_business_entities,
)


def test_plan_page_full_entities():
    txt = ("VUE EN PLAN MASSIF M01 NAPPE INFERIEURE COUPE A-A "
           "8 HA20 5 HA14 e=0.15 Ø720 platine 400x400 COUPE B-B")
    e = extract_plan_entities(txt)
    assert e["page_type"] == "plan"
    assert e["massifs"] == ["M01"]
    assert "vue_en_plan" in e["views"]
    assert "coupe_A-A" in e["views"] and "coupe_B-B" in e["views"]
    assert e["nappes"] == ["inferieure"]
    assert "8 HA20" in e["rebar"] and "5 HA14" in e["rebar"]
    assert e["spacing"] == ["e=0.15"]
    assert e["piles"] == ["Ø720"]
    assert e["plates"] == ["400x400"]
    assert has_business_entities(e)


def test_massif_variants():
    assert extract_plan_entities("M-06")["massifs"] == ["M06"]
    assert extract_plan_entities("MASSIF M01 et MASSIF M06")["massifs"] == ["M01", "M06"]
    # "Massif N°04" capté ; le "et N°03" enchaîné (sans "Massif") n'est volontairement
    # PAS capté pour éviter les faux positifs (N° peut désigner un détail/repère).
    assert extract_plan_entities("Modification AS Massif N°04 et N°03")["massifs"] == ["M04"]


def test_nomenclature_detection():
    e = extract_plan_entities("NOMENCLATURE GENERALE DES ACIERS — Repère 12 HA14")
    assert e["page_type"] == "nomenclature"
    e2 = extract_plan_entities("RECAPITULATIF DES ACIERS")
    assert e2["page_type"] == "nomenclature"


def test_steel_grade_not_confused_with_rebar():
    # "HA 500" (nuance acier) NE doit PAS être capturé comme une barre HA.
    e = extract_plan_entities("Aciers haute adherence HA 500 M Pa, Treillis soude TS HA 500")
    assert e["rebar"] == []
    assert e["page_type"] != "plan"


def test_cartouche_detection():
    e = extract_plan_entities("ECHELLE 1:50 FORMAT A3 INDICE 02 EMETTEUR TRIVERIO")
    assert e["page_type"] == "cartouche"


def test_empty_text():
    e = extract_plan_entities("")
    assert e["page_type"] == "empty"
    assert not has_business_entities(e)


def test_ocr_tolerant_coupe_without_dash():
    # OCR perd parfois le tiret : "COUPE A A".
    e = extract_plan_entities("COUPE A A et NAPPE SUPERIEURE")
    assert "coupe_A-A" in e["views"]
    assert e["nappes"] == ["superieure"]


def test_aggregate_page_entities():
    pages = [
        extract_plan_entities("MASSIF M01 VUE EN PLAN 8 HA20"),
        extract_plan_entities("MASSIF M02 COUPE A-A NAPPE SUPERIEURE 5 HA14"),
        extract_plan_entities("NOMENCLATURE GENERALE DES ACIERS"),
    ]
    agg = aggregate_page_entities(pages)
    assert set(agg["detected_massifs"]) == {"M01", "M02"}
    assert "vue_en_plan" in agg["detected_views"]
    assert agg["pages_by_type"]["plan"] == 2
    assert agg["pages_by_type"]["nomenclature"] == 1
