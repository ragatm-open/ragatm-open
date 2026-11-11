"""Plafond STRICT des chunks Excel (fix/excel-chunk-size-and-provenance).

Le défaut historique : la branche narrative de `render_block` émettait le bloc ENTIER
en un seul chunk (jusqu'à ~95 k caractères observés en base), et une ligne unique trop
large était conservée entière (~12-26 k). Ces tests figent l'invariant :

    pour 100 % des chunks Excel : 0 < len(text) <= chunk_max_chars

par découpage STRUCTUREL (lignes, colonnes, narratif, cellule) — jamais `texte[:max]`.
Provenance (feuille, plage, colonnes, en-têtes) préservée et RESTREINTE au contenu réel.
"""
from __future__ import annotations

import re

from apps.rag_core.services.ingest.extraction.excel_blocks import (
    BLOCK_NARRATIVE,
    BLOCK_TABLE,
    Block,
)
from apps.rag_core.services.ingest.extraction.excel_render import render_block

MAX = 4000

_COMMON = dict(
    sheet_name="Feuille", sheet_index=0, sheet_hidden=False, sheet_state="visible",
    rows_per_chunk=40, chunk_max_chars=MAX, max_columns_markdown=20,
    wide_threshold=16, max_columns_per_chunk=8, max_chunks=500,
)


def _render(block, **over):
    kw = {**_COMMON, **over}
    return render_block(block, has_formulas=over.pop("has_formulas", False), **{
        k: v for k, v in kw.items() if k != "has_formulas"
    })


def _table(rows, headers, *, row_start=1, header_rows=(1,), inferred=False, title=None):
    ncols = len(headers)
    return Block(
        kind=BLOCK_TABLE, row_start=row_start, row_end=row_start + len(rows),
        col_start=1, col_end=ncols, rows=rows, headers=list(headers),
        headers_inferred=inferred, header_rows=list(header_rows) if not inferred else [],
        index=1, title=title,
    )


def _md_data_line_count(text: str) -> int:
    """Compte les lignes de DONNÉES Markdown (hors ligne d'en-tête et séparateur)."""
    n = 0
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("|") and not re.match(r"^\|[\s:\-|]+\|$", s):
            n += 1
    return max(0, n - 1)  # retire la ligne d'en-tête


# --------------------------------------------------------------------------- #
# 20.1 — Tableau HAUT : 1000 lignes × 10 colonnes                             #
# --------------------------------------------------------------------------- #
def test_tall_table_windowed_under_cap():
    headers = [f"H{i}" for i in range(10)]
    rows = [[f"r{r}c{c}" for c in range(10)] for r in range(1000)]
    block = _table(rows, headers)
    chunks, _ = _render(block)

    assert len(chunks) > 1
    assert all(0 < len(c["text"]) <= MAX for c in chunks)

    # Ranges de lignes contiguës, sans trou ni chevauchement, couverture complète.
    spans = sorted((c["meta"]["row_start"], c["meta"]["row_end"]) for c in chunks)
    assert spans[0][0] == block.data_row_start
    covered = 0
    prev_end = block.data_row_start - 1
    for lo, hi in spans:
        assert lo == prev_end + 1, f"trou/chevauchement à {lo} (prev_end={prev_end})"
        covered += hi - lo + 1
        prev_end = hi
    assert covered == len(rows)             # aucune ligne perdue ni dupliquée
    # En-têtes présents dans chaque chunk.
    assert all("H0" in c["text"] and "H9" in c["text"] for c in chunks)


# --------------------------------------------------------------------------- #
# 20.2 — Ligne très LARGE : 1 ligne × 200 colonnes                            #
# --------------------------------------------------------------------------- #
def test_wide_row_split_by_columns():
    ncols = 200
    headers = [f"col{i + 1}" for i in range(ncols)]
    row = [f"v{i}_{'x' * 20}" for i in range(ncols)]
    block = _table([row], headers, row_start=3, inferred=True)
    chunks, warns = _render(block)

    assert len(chunks) > 1
    assert "excel_wide_row_split" in warns
    assert all(0 < len(c["text"]) <= MAX for c in chunks)

    # Couverture complète : la colonne d'IDENTIFICATION (col A) est reportée dans chaque
    # chunk (identity_columns), les colonnes de DONNÉES partitionnent le reste (col B..).
    def _num(letter):
        acc = 0
        for ch in letter:
            acc = acc * 26 + (ord(ch) - 64)
        return acc

    data_ranges = sorted((_num(c["meta"]["column_start"]), _num(c["meta"]["column_end"]))
                         for c in chunks)
    identity_cols = {_num(x) for c in chunks for x in (c["meta"].get("identity_columns") or [])}

    covered = set(identity_cols)
    prev = max(identity_cols) if identity_cols else 0   # les données commencent après l'identité
    for lo, hi in data_ranges:
        assert lo == prev + 1, f"colonnes de données non contiguës à {lo}"
        covered |= set(range(lo, hi + 1))
        prev = hi
    assert covered == set(range(1, ncols + 1))   # aucune colonne perdue ni dupliquée

    # Chaque valeur reste associée à son en-tête dans le MÊME chunk.
    for probe in (0, 137, ncols - 1):
        hit = [c for c in chunks if f"col{probe + 1}" in c["text"] and f"v{probe}_" in c["text"]]
        assert hit, f"col{probe + 1} séparée de sa valeur"


# --------------------------------------------------------------------------- #
# 20.3 — Tableau HAUT et LARGE : découpage 2D                                 #
# --------------------------------------------------------------------------- #
def test_tall_and_wide_two_dimensional():
    ncols, nrows = 40, 300
    headers = ["Libellé"] + [f"M{i}" for i in range(ncols - 1)]
    rows = [[f"ligne{r}"] + [f"{r}-{c}" for c in range(ncols - 1)] for r in range(nrows)]
    # En-têtes FIABLES -> chemin de découpage bidimensionnel dédié.
    block = _table(rows, headers, inferred=False)
    chunks, _ = _render(block)

    assert len(chunks) > 1
    assert all(0 < len(c["text"]) <= MAX for c in chunks)
    # Découpage sur les DEUX axes présent.
    assert any(c["meta"].get("chunk_axis") == "rows_and_columns" for c in chunks)


# --------------------------------------------------------------------------- #
# 20.4 — Bloc NARRATIF long                                                   #
# --------------------------------------------------------------------------- #
def test_narrative_split_no_text_loss():
    rows = [[("Paragraphe %d. " % i) * 25] for i in range(1, 120)]
    block = Block(kind=BLOCK_NARRATIVE, row_start=105, row_end=105 + len(rows) - 1,
                  col_start=1, col_end=1, rows=[[r[0]] for r in rows],
                  headers=[], headers_inferred=True, index=1)
    chunks, warns = _render(block)

    assert len(chunks) > 1
    assert "excel_narrative_split" in warns
    assert all(0 < len(c["text"]) <= MAX for c in chunks)
    # part_index cohérent, 1..N contigus.
    idxs = [c["meta"]["narrative_part_index"] for c in chunks]
    assert idxs == list(range(1, len(chunks) + 1))
    assert all(c["meta"]["narrative_part_count"] == len(chunks) for c in chunks)
    assert all(c["meta"]["kind"] == "excel_narrative" for c in chunks)

    # Reconstruction : aucun mot du texte source perdu (comparaison normalisée).
    src = " ".join(r[0] for r in rows)
    got = " ".join(
        "\n".join(c["text"].splitlines()[2:]) for c in chunks  # retire contexte + ligne vide
    )

    def _norm(s):
        return re.sub(r"\s+", " ", s).strip()

    assert set(_norm(src).split()) <= set(_norm(got).split())


# --------------------------------------------------------------------------- #
# 20.5 — Cellule GÉANTE (une seule cellule > max_chars)                       #
# --------------------------------------------------------------------------- #
def test_giant_cell_hard_wrapped():
    giant = "MOT" + "a" * (MAX * 2)          # une cellule ~2× le plafond
    headers = ["ID", "Contenu"]
    rows = [["X1", giant]]
    block = _table(rows, headers, inferred=False)
    chunks, _ = _render(block)

    assert all(0 < len(c["text"]) <= MAX for c in chunks)
    assert len(chunks) > 1                   # la cellule a bien été fractionnée
    # Reconstruction du contenu normalisé de la cellule.
    body = re.sub(r"\s+", "", "".join(c["text"] for c in chunks))
    assert body.count("a") >= MAX * 2 - 5    # aucune fin perdue silencieusement


# --------------------------------------------------------------------------- #
# 20.6 — Le PRÉFIXE (contexte + en-têtes) compte dans le plafond              #
# --------------------------------------------------------------------------- #
def test_prefix_counts_toward_cap():
    # En-têtes très longs : le préfixe seul est massif ; le corps doit s'y adapter.
    headers = [f"colonne_au_nom_tres_long_numero_{i}" for i in range(30)]
    rows = [[f"valeur_{r}_{c}" for c in range(30)] for r in range(200)]
    block = _table(rows, headers, inferred=False)
    chunks, _ = _render(block, chunk_max_chars=2000)
    assert all(0 < len(c["text"]) <= 2000 for c in chunks)


# --------------------------------------------------------------------------- #
# 20.7 — Formules liées à la bonne cellule après découpage colonnes           #
# --------------------------------------------------------------------------- #
def test_formula_stays_with_its_column():
    ncols = 120
    headers = [f"col{i + 1}" for i in range(ncols)]
    row = [f"=SOMME(A{i}:Z{i})_{'y' * 15}" for i in range(ncols)]
    block = _table([row], headers, row_start=5, inferred=True)
    chunks, _ = _render(block, has_formulas=True)
    assert all(0 < len(c["text"]) <= MAX for c in chunks)
    # La formule de colX est dans le chunk qui porte colX (jamais séparée).
    for probe in (2, 60, ncols - 1):
        hit = [c for c in chunks
               if f"col{probe + 1}" in c["text"] and f"=SOMME(A{probe}:Z{probe})" in c["text"]]
        assert hit, f"formule col{probe + 1} orpheline"


# --------------------------------------------------------------------------- #
# 20.8 — En-têtes multi-lignes / fusionnés : pas de crash, provenance saine   #
# --------------------------------------------------------------------------- #
def test_multirow_headers_no_crash():
    headers = ["Groupe / Sous1", "Groupe / Sous2", "Autre / X", "Autre / Y"]
    rows = [[f"a{r}", f"b{r}", f"c{r}", f"d{r}"] for r in range(50)]
    block = Block(kind=BLOCK_TABLE, row_start=1, row_end=52, col_start=1, col_end=4,
                  rows=rows, headers=headers, headers_inferred=False,
                  header_rows=[1, 2], index=1)
    chunks, _ = _render(block)
    assert all(0 < len(c["text"]) <= MAX for c in chunks)
    assert all(c["meta"]["cell_range"] for c in chunks)
    assert all(c["meta"]["header_depth"] == 2 for c in chunks)


# --------------------------------------------------------------------------- #
# 21 — INVARIANT GLOBAL sur plusieurs fixtures                                #
# --------------------------------------------------------------------------- #
def _fixtures():
    yield _table([[f"r{r}c{c}" for c in range(6)] for r in range(500)],
                 [f"H{i}" for i in range(6)])
    yield _table([[f"v{i}_{'z' * 25}" for i in range(226)]],
                 [f"col{i}" for i in range(226)], row_start=3, inferred=True)
    yield Block(kind=BLOCK_NARRATIVE, row_start=1, row_end=80, col_start=1, col_end=1,
                rows=[[("Phrase %d. " % i) * 30] for i in range(80)],
                headers=[], headers_inferred=True, index=1)
    yield _table([["ID", "A" * (MAX * 3)]], ["k", "v"], inferred=False)  # cellule géante
    yield _table([[f"{r}-{c}" for c in range(50)] for r in range(400)],
                 ["Nom"] + [f"M{i}" for i in range(49)], inferred=False)  # haut & large


def test_global_invariant_all_fixtures():
    for block in _fixtures():
        for max_chars in (1500, 4000):
            chunks, _ = _render(block, chunk_max_chars=max_chars)
            for c in chunks:
                assert c["text"], "chunk vide interdit"
                assert len(c["text"]) <= max_chars, (
                    f"{block.kind} chunk={len(c['text'])} > {max_chars} "
                    f"range={c['meta'].get('cell_range')}"
                )


# --------------------------------------------------------------------------- #
# 22 — RECONSTRUCTION : aucune ligne perdue sur un tableau haut               #
# --------------------------------------------------------------------------- #
def test_reconstruction_no_row_lost():
    headers = ["ID", "Val"]
    rows = [[f"ID{r}", f"val{r}"] for r in range(777)]
    block = _table(rows, headers, inferred=False)
    chunks, _ = _render(block)
    joined = "\n".join(c["text"] for c in chunks)
    for r in range(777):
        assert f"ID{r}" in joined and f"val{r}" in joined, f"ligne {r} perdue"
