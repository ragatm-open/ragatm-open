"""Refus PRÉCOCE — décider avant de lire, pas après avoir tout chargé.

Le défaut corrigé est opérationnel : un fichier d'un million de lignes était lu,
normalisé et partiellement transféré pendant plusieurs minutes avant d'être refusé.
Le refus était juste ; son coût ne l'était pas.
"""
from __future__ import annotations

import csv
import io
import time
import zipfile
from dataclasses import dataclass

import pytest
from openpyxl import Workbook

from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import preflight as PF


@dataclass(frozen=True)
class _Limits:
    max_rows_scanned: int = 1_000
    max_columns: int = 256
    max_input_file_mb: float = 200.0
    max_uncompressed_mb: float = 500.0


def _csv_bytes(n: int, *, multiline: bool = False, cols: int = 3) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"c{i}" for i in range(cols)])
    for i in range(n):
        valeur = "texte\nsur deux lignes" if multiline and i % 100 == 0 else "x"
        w.writerow([f"{i:08d}", valeur] + [i] * (cols - 2))
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def test_un_csv_dans_la_limite_est_accepte():
    verdict = PF.check(_csv_bytes(1_000), is_csv=True, delimiter=";", limits=_Limits())
    assert verdict.observed_rows == 1_000
    assert verdict.rows_are_exact is True


def test_une_ligne_de_trop_suffit_a_refuser():
    with pytest.raises(E.TabularLimitExceeded) as exc:
        PF.check(_csv_bytes(1_001), is_csv=True, delimiter=";", limits=_Limits())
    assert exc.value.code == E.ERR_INPUT_ROW_LIMIT
    assert exc.value.details.get("limit") == 1_000


def test_un_fichier_enorme_est_refuse_sans_etre_parcouru_entierement():
    """Le cœur du correctif : le coût du refus ne doit pas suivre la taille du fichier.

    Cent fois la limite doit coûter à peu près le même temps qu'une fois la limite —
    le scan s'arrête à `max_rows + 1`.
    """
    petit = _csv_bytes(1_001)
    enorme = _csv_bytes(100_000)

    depart = time.perf_counter()
    with pytest.raises(E.TabularLimitExceeded):
        PF.check(petit, is_csv=True, delimiter=";", limits=_Limits())
    cout_petit = time.perf_counter() - depart

    depart = time.perf_counter()
    with pytest.raises(E.TabularLimitExceeded):
        PF.check(enorme, is_csv=True, delimiter=";", limits=_Limits())
    cout_enorme = time.perf_counter() - depart

    # Le décodage initial reste proportionnel au fichier ; le SCAN, lui, est borné.
    # Sans la borne, le rapport suivrait le rapport des tailles (×100).
    assert cout_enorme < max(0.5, cout_petit * 20), (
        f"le scan n'est pas borné : {cout_petit:.3f}s pour 1 001 lignes, "
        f"{cout_enorme:.3f}s pour 100 000")


def test_les_champs_multi_lignes_ne_gonflent_pas_le_compte():
    """Compter les `\\n` surestimerait : une cellule peut en contenir entre guillemets.

    C'est le genre d'erreur qui refuse un document parfaitement valide, et qui ne se
    voit que sur le fichier d'un client.
    """
    verdict = PF.check(_csv_bytes(500, multiline=True), is_csv=True,
                       delimiter=";", limits=_Limits())
    assert verdict.observed_rows == 500


def test_trop_de_colonnes_est_refuse_des_len_tete():
    with pytest.raises(E.TabularLimitExceeded) as exc:
        PF.check(_csv_bytes(10, cols=40), is_csv=True, delimiter=";",
                 limits=_Limits(max_columns=20))
    assert exc.value.code == E.ERR_INPUT_COLUMN_LIMIT


def test_un_fichier_trop_gros_est_refuse_sur_sa_taille():
    with pytest.raises(E.TabularLimitExceeded) as exc:
        PF.check(_csv_bytes(100), is_csv=True, delimiter=";",
                 limits=_Limits(max_input_file_mb=0.001))
    assert exc.value.code == E.ERR_INPUT_SIZE_LIMIT


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #
def _xlsx_bytes(n: int) -> bytes:
    buf = io.BytesIO()
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("F")
    ws.append(["a", "b"])
    for i in range(n):
        ws.append([i, i * 2])
    wb.save(buf)
    return buf.getvalue()


def test_un_xlsx_dans_la_limite_est_accepte():
    verdict = PF.check(_xlsx_bytes(500), is_csv=False, limits=_Limits())
    assert verdict.exceeded is False


def test_un_xlsx_hors_limite_est_refuse():
    with pytest.raises(E.TabularLimitExceeded) as exc:
        PF.check(_xlsx_bytes(1_500), is_csv=False, limits=_Limits())
    assert exc.value.code == E.ERR_INPUT_ROW_LIMIT


def test_une_dimension_declaree_mensongere_ne_fait_pas_accepter():
    """Une dimension n'est qu'un SIGNAL : elle sert à refuser vite, jamais à accepter.

    Un classeur qui déclare `A1:B5` mais contient 1 500 lignes doit être refusé sur
    le scan, pas accepté sur sa déclaration. C'est le même piège que celui que
    `reset_dimensions()` corrige à l'ingestion.
    """
    brut = _xlsx_bytes(1_500)
    sortie = io.BytesIO()
    source = zipfile.ZipFile(io.BytesIO(brut))
    with zipfile.ZipFile(sortie, "w") as dest:
        for item in source.infolist():
            contenu = source.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet"):
                contenu = contenu.replace(
                    b"<sheetData>", b'<dimension ref="A1:B5"/><sheetData>', 1)
            dest.writestr(item, contenu)

    with pytest.raises(E.TabularLimitExceeded) as exc:
        PF.check(sortie.getvalue(), is_csv=False, limits=_Limits())
    assert exc.value.code == E.ERR_INPUT_ROW_LIMIT


def test_le_verdict_dit_comment_il_a_decide():
    """La méthode est journalisée : un refus doit pouvoir s'expliquer."""
    verdict = PF.check(_csv_bytes(100), is_csv=True, delimiter=";", limits=_Limits())
    assert verdict.method in ("scan_complet", "scan_borne", "dimension_declaree")
