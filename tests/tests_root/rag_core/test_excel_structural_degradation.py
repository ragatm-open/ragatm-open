"""M2 — une perte d'information structurelle ne doit JAMAIS être silencieuse.

Le défaut corrigé : `xl/_rels/workbook.xml.rels` illisible faisait `return {}`. Sans
relations, aucune `SheetInfo.part` n'est résolue, donc le scan XML est sauté pour TOUT
le classeur : ni formule, ni fusion, ni dimension déclarée. openpyxl ouvrait le fichier
malgré tout et produisait des chunks — l'extraction paraissait réussie.

C'est le scénario le plus dangereux du pipeline : un succès apparent qui a perdu la
connaissance structurelle. Ces tests verrouillent le signalement.
"""
from __future__ import annotations

import io
import zipfile

import pytest
from openpyxl import Workbook

from apps.rag_core.services.ingest.extraction import excel_workbook as xw


def _workbook_bytes(*, with_formula: bool = True) -> bytes:
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "Feuille"
    ws.append(["Poste", "Montant"])
    ws.append(["A", 100])
    ws.append(["B", 250])
    if with_formula:
        ws.append(["Total", "=SUM(B2:B3)"])
    wb.save(buf)
    return buf.getvalue()


def _replace_part(data: bytes, part: str, payload: bytes) -> bytes:
    out = io.BytesIO()
    src = zipfile.ZipFile(io.BytesIO(data))
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename == part:
                content = payload
            dst.writestr(item, content)
    return out.getvalue()


# --------------------------------------------------------------------------- #
# Référence : un classeur sain n'invente aucune dégradation
# --------------------------------------------------------------------------- #
def test_classeur_sain_aucune_degradation():
    inv = xw.inspect_workbook(_workbook_bytes())

    assert inv.warnings == []
    assert inv.relationships_unreadable is False
    assert inv.structurally_degraded is False
    assert inv.degraded_cause == ""
    # La preuve que le scan a bien eu lieu : la formule est vue.
    assert inv.formula_count == 1
    assert all(s.part for s in inv.sheets)


# --------------------------------------------------------------------------- #
# M2 — relations illisibles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [
    b'<?xml version="1.0"?><Relationships><PAS_FERME>',   # XML malformé
    b"",                                                   # partie vide
    b"\x00\x01\x02 pas du xml",                            # binaire
])
def test_rels_illisible_produit_un_warning_stable(payload):
    inv = xw.inspect_workbook(_replace_part(
        _workbook_bytes(), "xl/_rels/workbook.xml.rels", payload))

    assert xw.WARN_WORKBOOK_RELS_UNREADABLE in inv.warnings
    assert inv.warnings.count(xw.WARN_WORKBOOK_RELS_UNREADABLE) == 1  # jamais dupliqué
    assert inv.relationships_unreadable is True
    assert inv.structurally_degraded is True


def test_rels_illisible_conserve_la_cause():
    inv = xw.inspect_workbook(_replace_part(
        _workbook_bytes(), "xl/_rels/workbook.xml.rels",
        b'<?xml version="1.0"?><Relationships><PAS_FERME>'))

    assert inv.degraded_cause, "la cause doit être conservée, pas jetée"


def test_rels_illisible_la_perte_est_reelle_et_signalee():
    """Le cœur de M2 : la connaissance structurelle DISPARAÎT vraiment.

    Sans le correctif, ce même classeur rapportait `formula_count == 0` et
    `warnings == []` — indiscernable d'un classeur sans aucune formule.
    """
    sain = xw.inspect_workbook(_workbook_bytes())
    casse = xw.inspect_workbook(_replace_part(
        _workbook_bytes(), "xl/_rels/workbook.xml.rels",
        b'<?xml version="1.0"?><Relationships><PAS_FERME>'))

    assert sain.formula_count == 1
    assert casse.formula_count == 0          # la perte est bien réelle…
    assert casse.warnings                    # …et elle est signalée.
    assert all(not s.part for s in casse.sheets)


def test_code_de_warning_est_celui_du_contrat():
    """Le code est exposé en API et journalisé : il ne doit pas dériver."""
    assert xw.WARN_WORKBOOK_RELS_UNREADABLE == "workbook_relationships_unreadable"


# --------------------------------------------------------------------------- #
# Causes préservées (`raise ... from e`)
# --------------------------------------------------------------------------- #
def test_archive_invalide_conserve_la_cause_dorigine():
    from apps.rag_core.services.ingest.extraction.base import CorruptedFile

    with pytest.raises(CorruptedFile) as exc:
        xw.inspect_workbook(b"ceci n'est pas une archive zip")

    assert exc.value.__cause__ is not None, (
        "sans `from e`, un MemoryError deviendrait un « classeur corrompu » indébogable")
