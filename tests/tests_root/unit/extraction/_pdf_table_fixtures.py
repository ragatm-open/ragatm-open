"""fabriques de PDF tabulaires SYNTHÉTIQUES et NEUTRES.

Ces fixtures définissent le contrat GÉNÉRIQUE. Leur vocabulaire est délibérément
banal (`ID`, `Description`, `Amount`, `A`, `B`, `Q1`, `Q2`) : aucun terme du corpus
de qualification ne doit pouvoir influencer, même par accident, la logique de
production. Un moteur qui ne passerait ces cas qu'avec le vocabulaire d'un corpus particulier serait
un moteur spécialisé déguisé.

Aucun PDF binaire n'est versionné : chaque cas est reconstruit par reportlab, ce
qui rend la cause d'un échec lisible.

Convention : `GRID` visible. pdfplumber détecte les tableaux par les lignes
(stratégie « lines » par défaut) ; sans grille les fabriques produiraient du texte
libre et les tests ne mesureraient plus rien.

Géométrie : A4 paysage, comme le corpus. Deux aides portent tout le contraste des
cas de continuation, parce que le moteur chaîne deux fragments sur la CONTINUITÉ
VERTICALE (le tableau descend au bas d'une page et reprend en haut de la suivante)
et JAMAIS sur la seule égalité des en-têtes :

- `_full_page_block()` produit un tableau qui occupe la page entière — géométrie
  d'un vrai tableau paginé, donc chaînable ;
- `_short_block()` produit un tableau court laissé en haut de page — aucune
  continuité verticale, donc jamais chaîné même à en-tête identique.
"""
from __future__ import annotations

from io import BytesIO
from typing import Iterable, List, Optional, Sequence

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PAGE = landscape(A4)          # 841.89 x 595.28
PAGE_W, PAGE_H = PAGE
MARGIN = 24.0

#: Schéma plat neutre utilisé par la majorité des cas.
SCHEMA_3 = ["ID", "Description", "Amount"]
SCHEMA_5 = ["ID", "Label", "Quantity", "Unit price", "Total"]

_STYLES = getSampleStyleSheet()


def _doc(flowables: Sequence) -> bytes:
    buf = BytesIO()
    SimpleDocTemplate(
        buf, pagesize=PAGE,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
    ).build(list(flowables))
    return buf.getvalue()


def _grid(extra: Optional[Iterable] = None) -> TableStyle:
    cmds = [("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "TOP")]
    cmds.extend(extra or [])
    return TableStyle(cmds)


def _table(data: List[List], *, style_extra=None, col_widths=None, row_heights=None) -> Table:
    t = Table(data, colWidths=col_widths, rowHeights=row_heights)
    t.setStyle(_grid(style_extra))
    return t


def para(text: str) -> Paragraph:
    return Paragraph(text, _STYLES["BodyText"])


#: Hauteur d'une rangée d'en-tête, fixée pour rendre la géométrie prévisible.
ROW_H = 26.0

#: Largeurs de colonnes FIXES. Un vrai tableau paginé conserve sa géométrie d'une
#: page à l'autre : c'est même ce qui permet d'affirmer que c'est le même tableau.
#: Sans cette contrainte, reportlab dimensionne chaque page d'après son contenu et
#: fabrique deux géométries différentes — un artefact de fixture, pas une réalité.
COLS_3 = [70.0, 260.0, 70.0]
COLS_5 = [60.0, 90.0, 70.0, 80.0, 80.0]

#: Hauteur visée d'un bloc « pleine page ». Volontairement SOUS la hauteur du cadre
#: (595 - 2*24 = 547, moins le rembourrage interne de reportlab) : au-delà, le
#: moteur de mise en page scinde le tableau et fabrique des pages parasites, ce qui
#: détruirait précisément la géométrie que ces fixtures doivent produire.
_FULL_PAGE_TARGET_H = 515.0


def _widths_for(data: List[List]) -> Optional[List[float]]:
    n = max((len(r) for r in data), default=0)
    if n == len(COLS_3):
        return list(COLS_3)
    if n == len(COLS_5):
        return list(COLS_5)
    return None


def _full_page_block(rows: List[List[str]], *, with_header=True, schema=None) -> Table:
    """Tableau qui occupe la page ENTIÈRE, du haut au bas de la zone de texte.

    C'est la géométrie d'un vrai tableau paginé, et c'est elle qui porte le signal de
    CONTINUITÉ VERTICALE dont le moteur se sert pour chaîner deux fragments. Un
    tableau court laissé en haut de page (cf. `independent_tables_*`) ne présente pas
    ce signal et ne doit donc jamais être chaîné, même à en-tête identique."""
    data = ([list(schema or SCHEMA_3)] if with_header else []) + rows
    head_h = ROW_H if with_header else 0.0
    body_h = max(1.0, (_FULL_PAGE_TARGET_H - head_h) / max(1, len(rows)))
    heights = ([ROW_H] if with_header else []) + [body_h] * len(rows)
    return _table(data, row_heights=heights, col_widths=_widths_for(data))


# --------------------------------------------------------------------------- #
# A — tableau plat 3 colonnes (§4.A, §62)
# --------------------------------------------------------------------------- #
def flat_three_column() -> bytes:
    data = [
        SCHEMA_3,
        ["R-1", "First item description", "10.00"],
        ["R-2", "Second item description", "20.50"],
    ]
    return _doc([_table(data)])


# --------------------------------------------------------------------------- #
# B — tableau 5 colonnes mixte (§4.B)
# --------------------------------------------------------------------------- #
def flat_five_column() -> bytes:
    data = [
        SCHEMA_5,
        ["R-1", "Alpha", "2", "5.00", "10.00"],
        ["R-2", "Beta", "3", "4.00", "12.00"],
        ["R-3", "Gamma", "1", "7.25", "7.25"],
    ]
    return _doc([_table(data)])


# --------------------------------------------------------------------------- #
# §63 — grille SANS en-tête textuel
# --------------------------------------------------------------------------- #
def headerless_grid() -> bytes:
    """Toutes les rangées ont le même profil (numérique) : aucune n'est un en-tête."""
    data = [
        ["1", "2", "3"],
        ["4", "5", "6"],
        ["7", "8", "9"],
    ]
    return _doc([_table(data)])


# --------------------------------------------------------------------------- #
# G — colonnes d'espacement physiques (§4.G, §64)
# --------------------------------------------------------------------------- #
def _pad_row(cells: Sequence[str], physical_cols: int) -> List[str]:
    n = len(cells)
    assert physical_cols >= n and physical_cols % n == 0, (physical_cols, n)
    stride = physical_cols // n
    out = [""] * physical_cols
    for i, c in enumerate(cells):
        out[i * stride] = c
    return out


def spacer_columns(physical_cols: int) -> bytes:
    """Le MÊME tableau logique à 3 colonnes, rendu sur 3, 6 ou 9 colonnes physiques."""
    rows = [SCHEMA_3,
            ["R-1", "First item description", "10.00"],
            ["R-2", "Second item description", "20.50"]]
    return _doc([_table([_pad_row(r, physical_cols) for r in rows])])


# --------------------------------------------------------------------------- #
# C — multi-page, en-tête répétée (§4.C, §66)
# --------------------------------------------------------------------------- #
def _short_block(rows: List[List[str]], *, with_header=True, schema=None) -> Table:
    """Tableau COURT laissé là où le flux le place : pas de continuité verticale."""
    data = ([list(schema or SCHEMA_3)] if with_header else []) + rows
    return _table(data, row_heights=[ROW_H] * len(data), col_widths=_widths_for(data))


def repeated_header_two_pages() -> bytes:
    """Chaque page porte l'en-tête ; la page 1 occupe toute la hauteur utile."""
    p1_rows = [[f"R-{i}", f"Description {i}", f"{i}.00"] for i in range(1, 4)]
    p2_rows = [[f"R-{i}", f"Description {i}", f"{i}.00"] for i in range(4, 6)]
    return _doc([_full_page_block(p1_rows), PageBreak(), _full_page_block(p2_rows)])


# --------------------------------------------------------------------------- #
# D — ligne logique à cheval sur deux pages (§4.D, §67)
# --------------------------------------------------------------------------- #
def row_continuation_two_pages() -> bytes:
    """La dernière ligne de la page 1 se poursuit page 2, cellule d'ancrage VIDE."""
    p1_rows = [["R-1", "First part of the description", "10.00"]]
    p2_rows = [["", "Second part of the same description", ""]]
    return _doc([_full_page_block(p1_rows), PageBreak(), _full_page_block(p2_rows)])


# --------------------------------------------------------------------------- #
# E — continuation PUIS nouvelle ligne sur la page suivante (§4.E, §68)
# --------------------------------------------------------------------------- #
def continuation_then_new_row() -> bytes:
    p1_rows = [["R-1", "First part of the description", "10.00"]]
    p2_rows = [["", "Second part of the same description", ""],
               ["R-2", "An entirely different item", "20.50"]]
    return _doc([_full_page_block(p1_rows), PageBreak(), _full_page_block(p2_rows)])


# --------------------------------------------------------------------------- #
# §69 — nouvelle ligne à PREMIÈRE CELLULE VIDE, en milieu de tableau
# --------------------------------------------------------------------------- #
def blank_first_cell_new_row() -> bytes:
    """Cellule d'ancrage vide MAIS en plein milieu d'un fragment : ce n'est PAS une
    continuation. C'est le test qui prouve que la règle n'est pas naïve."""
    data = [
        SCHEMA_3,
        ["R-1", "First item description", "10.00"],
        ["", "A legitimate row with no identifier", "15.00"],
        ["R-3", "Third item description", "30.00"],
    ]
    return _doc([_table(data)])


# --------------------------------------------------------------------------- #
# F / O — cellules vides légitimes (§4.F, §4.O)
# --------------------------------------------------------------------------- #
def legitimate_blank_cells() -> bytes:
    data = [
        SCHEMA_3,
        ["R-1", "", "10.00"],
        ["R-2", "Second item description", ""],
    ]
    return _doc([_table(data)])


# --------------------------------------------------------------------------- #
# §74 — ligne éparse (valeurs en colonnes 2 et 4 seulement)
# --------------------------------------------------------------------------- #
def sparse_row() -> bytes:
    data = [
        SCHEMA_5,
        ["R-1", "Alpha", "2", "5.00", "10.00"],
        ["", "Beta", "", "4.00", ""],
    ]
    return _doc([_table(data)])


# --------------------------------------------------------------------------- #
# N — aucun identifiant textuel de ligne (§4.N)
# --------------------------------------------------------------------------- #
def no_row_identifier() -> bytes:
    data = [
        ["Description", "Amount"],
        ["First item description", "10.00"],
        ["Second item description", "20.50"],
    ]
    return _doc([_table(data)])


# --------------------------------------------------------------------------- #
# J — deux tableaux INDÉPENDANTS, même schéma, pages consécutives (§4.J, §70)
# --------------------------------------------------------------------------- #
def independent_tables_consecutive_pages() -> bytes:
    """Le tableau de la page 1 s'arrête HAUT : aucune continuité verticale, donc
    deux tableaux logiques malgré des en-têtes identiques."""
    t1 = _short_block([["R-1", "First table row", "10.00"]])
    t2 = _short_block([["R-2", "Second table row", "20.50"]])
    return _doc([t1, PageBreak(), t2])


# --------------------------------------------------------------------------- #
# K — deux tableaux INDÉPENDANTS, même schéma, MÊME page (§4.K, §71)
# --------------------------------------------------------------------------- #
def independent_tables_same_page() -> bytes:
    t1 = _short_block([["R-1", "First table row", "10.00"]])
    t2 = _short_block([["R-2", "Second table row", "20.50"]])
    return _doc([t1, Spacer(1, 120), t2])


# --------------------------------------------------------------------------- #
# L — tableau / prose / tableau de même schéma (§4.L, §72)
# --------------------------------------------------------------------------- #
PROSE_BETWEEN = ("This paragraph separates two unrelated tables that happen to share "
                 "the same column headers.")
PROSE_BEFORE = "The following table lists the items under review."
PROSE_AFTER = "The preceding table is indicative only."


def tables_separated_by_prose() -> bytes:
    t1 = _short_block([["R-1", "First table row", "10.00"]])
    t2 = _short_block([["R-2", "Second table row", "20.50"]])
    return _doc([t1, Spacer(1, 18), para(PROSE_BETWEEN), Spacer(1, 18), t2])


# --------------------------------------------------------------------------- #
# §72 bis / T — prose autour d'un tableau (suppression de la zone tabulaire)
# --------------------------------------------------------------------------- #
def prose_around_table() -> bytes:
    data = [SCHEMA_3, ["R-1", "A distinctive cell sentence for duplication checks", "10.00"]]
    return _doc([para(PROSE_BEFORE), _table(data), para(PROSE_AFTER)])


# --------------------------------------------------------------------------- #
# H / I — en-tête hiérarchique à deux niveaux, matrice (§4.H, §4.I, §65, §75)
# --------------------------------------------------------------------------- #
def hierarchical_matrix() -> bytes:
    """Matrice neutre du lot ::

                    Q1          Q2
                 A     B     A     B
        Row1     1     2     3     4
        Row2     5     6     7     8

    `Q1` est fusionné sur les deux colonnes `A`/`B` de gauche, `Q2` sur celles de
    droite. Aucun sens métier : seule la topologie compte."""
    data = [
        ["", "Q1", "", "Q2", ""],
        ["", "A", "B", "A", "B"],
        ["Row1", "1", "2", "3", "4"],
        ["Row2", "5", "6", "7", "8"],
    ]
    style = [("SPAN", (1, 0), (2, 0)), ("SPAN", (3, 0), (4, 0))]
    return _doc([_table(data, style_extra=style)])


# --------------------------------------------------------------------------- #
# §44 — rangée séparatrice pleine largeur
# --------------------------------------------------------------------------- #
SEPARATOR_LABEL = "First group"


def separator_row() -> bytes:
    data = [
        SCHEMA_3,
        [SEPARATOR_LABEL, "", ""],
        ["R-1", "First item description", "10.00"],
    ]
    style = [("SPAN", (0, 1), (-1, 1)),
             ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#DDDDDD"))]
    return _doc([_table(data, style_extra=style)])


# --------------------------------------------------------------------------- #
# M — encadré à une colonne (§4.M, §73)
# --------------------------------------------------------------------------- #
def one_column_box() -> bytes:
    """Une seule colonne sémantique : jamais un tableau, quelle que soit la hauteur."""
    data = [
        ["This boxed paragraph is prose inside a framed region."],
        ["It is stacked over several ruled rows by the layout engine."],
        ["It carries a single column and is not a table schema."],
        ["It must remain ordinary page text."],
    ]
    return _doc([_table(data, col_widths=[400])])


# --------------------------------------------------------------------------- #
# §77 — candidat ambigu -> repli sûr
# --------------------------------------------------------------------------- #
def ambiguous_ragged() -> bytes:
    """Grille dont les rangées n'ont pas de colonnes cohérentes : le moteur ne doit
    ni structurer, ni supprimer le texte de page."""
    t1 = _table([["only one cell"]], col_widths=[120])
    t2 = _table([["a", "b", "c", "d", "e", "f"]], col_widths=[40] * 6)
    return _doc([para("Ambiguous region follows."), t1, Spacer(1, 6), t2])


# --------------------------------------------------------------------------- #
# §36 / N — ligne logique sur trois pages
# --------------------------------------------------------------------------- #
def row_across_three_pages() -> bytes:
    p1 = _full_page_block([["R-1", "Part one of the description", "10.00"]])
    p2 = _full_page_block([["", "Part two of the description", ""]])
    p3 = _full_page_block([["", "Part three of the description", ""]])
    return _doc([p1, PageBreak(), p2, PageBreak(), p3])


# =========================================================================== #
# fabriques de ROBUSTESSE
# =========================================================================== #
"""Deux familles s'ajoutent ici.

**Continuation typographique (B1).** Une cellule coupée par un saut de page
reprend SANS espacement de paragraphe, alors qu'une nouvelle ligne en ouvre un et
porte donc son espace avant. reportlab n'honore pas `spaceBefore` pour le premier
paragraphe d'une cellule : on produit donc les deux modes par le rembourrage haut,
qui est exactement la grandeur que l'extracteur mesure.

**Partition de matrice (B2).** Fabriques à coin vide, rempli, fusionné, et à
profondeurs d'en-tête variables — plus les négatifs plats qui ne doivent jamais
devenir des matrices.
"""

from reportlab.lib.pagesizes import LETTER, legal  # noqa: E402

#: Formats de page couverts par les tests de robustesse.
PAGE_SIZES = {"A4": A4, "Letter": LETTER, "Legal": legal, "Custom": (500.0, 720.0)}

R1_SCHEMA = ["Item", "Description", "Notes", "Amount"]


def _r1_style(size: float, leading: Optional[float]):
    from reportlab.lib.styles import ParagraphStyle

    return ParagraphStyle("r1", fontName="Helvetica", fontSize=size,
                          leading=leading or size * 1.2, spaceBefore=0, spaceAfter=0)


def _r1_table(rows, widths, *, pad_ordinary: float, continuation_rows=(),
              pad_continuation: float = 3.0, row_heights=None) -> Table:
    """`pad_ordinary` = espace au-dessus du premier glyphe d'une rangée ORDINAIRE ;
    les rangées de `continuation_rows` n'ont que le rembourrage nu."""
    t = Table(rows, colWidths=widths, rowHeights=row_heights)
    cmds = [("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), pad_ordinary),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    for ri in continuation_rows:
        cmds.append(("TOPPADDING", (0, ri), (-1, ri), pad_continuation))
    t.setStyle(TableStyle(cmds))
    return t


def two_page_case(*, case="continuation", page="A4", orient="landscape",
                  top=24.0, bottom=24.0, size=10.0, leading=None,
                  space_before=9.0, split_col=1, fill_ratio=0.75,
                  delayed_top=False, n_rows=8) -> bytes:
    """Deux pages, même schéma. `case` pilote ce que porte la page 2.

    - `continuation`        : reprise d'une cellule coupée (ancrage NON vide si
                              `split_col` vaut 0) ;
    - `independent`         : tableau indépendant, ligne complète ;
    - `blank_anchor_new_row`: nouvelle ligne SANS identifiant (piège du §18) ;
    - `cont_then_new`       : reprise puis nouvelle ligne sur la même page.
    """
    base = PAGE_SIZES[page]
    ps = landscape(base) if orient == "landscape" else base
    W, H = ps
    st = _r1_style(size, leading)
    widths = [(W - 48) / 4.0] * 4
    usable = H - top - bottom

    def cells(texts):
        return [Paragraph(t, st) if t else "" for t in texts]

    head = [Paragraph(f"<b>{h}</b>", st) for h in R1_SCHEMA]
    body1 = [cells([f"A-{i}", f"Description {i}", f"Notes {i}", f"{i}.00"])
             for i in range(1, n_rows + 1)]
    # Une reprise de paragraphe EN MILIEU de page 1 : elle donne à la calibration la
    # population basse dont elle a besoin, y compris quand la page 2 ne contient
    # aucune continuation. Sans elle, le document serait typographiquement uniforme
    # et la règle s'abstiendrait — ce qui ne testerait plus rien.
    body1.insert(1, cells(["", "overflow line of the previous cell", "", ""]))
    target = usable * fill_ratio
    hs = [size * 2.4] + [max(size * 2.2, (target - size * 2.4) / len(body1))] * len(body1)
    overflow_row = 2   # en-tête + 1re ligne de données

    flow = [_r1_table([head] + body1, widths, pad_ordinary=space_before,
                      continuation_rows=[overflow_row], row_heights=hs),
            PageBreak()]
    if delayed_top:
        flow.append(Paragraph("Repeated document heading line.", st))

    cont = [""] * 4
    cont[split_col] = "continued text of the same cell without a new paragraph"
    p2 = [head]
    if case == "continuation":
        p2.append(cells(cont))
    elif case == "independent":
        p2.append(cells(["B-1", "Other description", "Other notes", "99.00"]))
    elif case == "blank_anchor_new_row":
        p2.append(cells(["", "A brand new row without identifier", "Notes", "42.00"]))
    elif case == "cont_then_new":
        p2.append(cells(cont))
        p2.append(cells(["B-1", "A genuinely new row", "Notes", "99.00"]))
    else:
        raise ValueError(case)
    cont_rows = [1] if case in ("continuation", "cont_then_new") else []
    flow.append(_r1_table(p2, widths, pad_ordinary=space_before,
                          continuation_rows=cont_rows,
                          row_heights=[size * 2.4] * len(p2)))

    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=ps, leftMargin=24, rightMargin=24,
                      topMargin=top, bottomMargin=bottom).build(flow)
    return buf.getvalue()


def tiny_table() -> bytes:
    """Trop peu de rangées mesurables : la calibration doit s'abstenir."""
    return _doc([_table([R1_SCHEMA, ["A-1", "Description", "Notes", "1.00"]])])


def uniform_gaps_table() -> bytes:
    """Assez de rangées, mais AUCUNE ne commence plus haut : abstention aussi."""
    rows = [R1_SCHEMA] + [[f"A-{i}", f"Description {i}", f"Notes {i}", f"{i}.00"]
                          for i in range(1, 12)]
    return _doc([_r1_table(rows, None, pad_ordinary=9.0)])


# --------------------------------------------------------------------------- #
# Matrices neutres (B2)
# --------------------------------------------------------------------------- #
def matrix(rows: List[List[str]], spans=()) -> bytes:
    """Matrice arbitraire : `spans` liste des ((c0,r0),(c1,r1)) fusionnés."""
    return _doc([_table(rows, style_extra=[("SPAN", a, b) for a, b in spans])])


_Q = [((1, 0), (2, 0)), ((3, 0), (4, 0))]

#: coin VIDE, h=2 v=1
MATRIX_EMPTY_CORNER = ([["", "Q1", "", "Q2", ""], ["", "A", "B", "A", "B"],
                        ["R1", "1", "2", "3", "4"], ["R2", "5", "6", "7", "8"]], _Q)
#: coin REMPLI, h=2 v=1
MATRIX_FILLED_CORNER = ([["Region", "Q1", "", "Q2", ""], ["", "A", "B", "A", "B"],
                         ["R1", "1", "2", "3", "4"], ["R2", "5", "6", "7", "8"]], _Q)
#: coin FUSIONNÉ sur deux rangées, h=2 v=1
MATRIX_MERGED_CORNER = ([["Axis", "Q1", "", "Q2", ""], ["", "A", "B", "A", "B"],
                         ["R1", "1", "2", "3", "4"], ["R2", "5", "6", "7", "8"]],
                        [((0, 0), (0, 1))] + _Q)
#: coin MULTI-CELLULES (étiquette aussi dans la rangée feuille), h=2 v=1
MATRIX_MULTICELL_CORNER = ([["Axis", "Q1", "", "Q2", ""], ["Unit", "A", "B", "A", "B"],
                            ["R1", "1", "2", "3", "4"], ["R2", "5", "6", "7", "8"]], _Q)
#: h=1 v=1
MATRIX_H1_V1 = ([["", "A", "B", "C"], ["R1", "1", "2", "3"], ["R2", "4", "5", "6"]], [])
#: h=2 v=2
MATRIX_H2_V2 = ([["", "", "Q1", "", "Q2", ""], ["", "", "A", "B", "A", "B"],
                 ["G1", "R1", "1", "2", "3", "4"], ["G1", "R2", "5", "6", "7", "8"]],
                [((2, 0), (3, 0)), ((4, 0), (5, 0))])
#: h=3 v=2, en-tête MAJORITAIRE (3 rangées de tête pour 1 de corps)
MATRIX_H3_V2 = ([["", "", "H", "", "", ""], ["", "", "Q1", "", "Q2", ""],
                 ["", "", "A", "B", "A", "B"], ["G1", "R1", "1", "2", "3", "4"]],
                [((2, 0), (5, 0)), ((2, 1), (3, 1)), ((4, 1), (5, 1))])
#: h=4 v=1
MATRIX_H4_V1 = ([["", "T", "", "", ""], ["", "H", "", "", ""], ["", "Q1", "", "Q2", ""],
                 ["", "A", "B", "A", "B"], ["R1", "1", "2", "3", "4"]],
                [((1, 0), (4, 0)), ((1, 1), (4, 1)), ((1, 2), (2, 2)), ((3, 2), (4, 2))])
#: corps TEXTE seul
MATRIX_TEXT_BODY = ([["", "Q1", "", "Q2", ""], ["", "A", "B", "A", "B"],
                     ["R1", "aa", "bb", "cc", "dd"], ["R2", "ee", "ff", "gg", "hh"]], _Q)
#: corps MIXTE
MATRIX_MIXED_BODY = ([["", "Q1", "", "Q2", ""], ["", "A", "B", "A", "B"],
                      ["R1", "12", "ok", "3.5", "n/a"], ["R2", "x", "6", "7%", "hi"]], _Q)
#: corps ÉPARS
MATRIX_SPARSE_BODY = ([["", "Q1", "", "Q2", ""], ["", "A", "B", "A", "B"],
                       ["R1", "1", "", "", "4"], ["R2", "", "6", "", ""]], _Q)
#: corps MULTI-LIGNES
MATRIX_MULTILINE_BODY = ([["", "Q1", "", "Q2", ""], ["", "A", "B", "A", "B"],
                          ["R1", "line one\nline two", "b", "c", "d"],
                          ["R2", "e", "f", "g", "h"]], _Q)
#: fusions IRRÉGULIÈRES (3 feuilles puis 2) sur grille feuille régulière
MATRIX_IRREGULAR = ([["", "H1", "", "", "H2", ""], ["", "A", "B", "C", "A", "B"],
                     ["R1", "1", "2", "3", "4", "5"], ["R2", "6", "7", "8", "9", "10"]],
                    [((1, 0), (3, 0)), ((4, 0), (5, 0))])
#: hiérarchie INÉGALE : une branche à 2 niveaux, une à 1
MATRIX_UNEVEN = ([["", "Q1", "", "Solo"], ["", "A", "B", ""],
                  ["R1", "1", "2", "3"], ["R2", "4", "5", "6"]],
                 [((1, 0), (2, 0)), ((3, 0), (3, 1))])
#: en-tête de LIGNES fusionné sur deux rangées de corps
MATRIX_MERGED_ROW_HEADER = ([["", "Q1", "", "Q2", ""], ["", "A", "B", "A", "B"],
                             ["G", "1", "2", "3", "4"], ["", "5", "6", "7", "8"]],
                            _Q + [((0, 2), (0, 3))])

#: --- NÉGATIFS : doivent rester PLATS -------------------------------------- #
FLAT_MERGED_TITLE = ([["Title", "", ""], ["ID", "Desc", "Amount"], ["R-1", "x", "1"]],
                     [((0, 0), (2, 0))])
FLAT_SUBTOTAL = ([["ID", "Desc", "Amount"], ["R-1", "x", "1"], ["Total", "", "9"]],
                 [((0, 2), (1, 2))])
FLAT_SECTION = ([["ID", "Desc", "Amount"], ["Group A", "", ""], ["R-1", "x", "1"]],
                [((0, 1), (2, 1))])
FLAT_SPARSE = ([["ID", "Desc", "Amount"], ["R-1", "", "1"], ["", "x", ""]], [])
FLAT_FILLED_CORNER = ([["ID", "Desc", "Amount"], ["R-1", "x", "1"], ["R-2", "y", "2"]], [])
FLAT_MERGED_BODY_CELL = ([["ID", "Desc", "Amount"], ["R-1", "x", "1"], ["R-2", "m", ""]],
                         [((1, 2), (2, 2))])


def duplicate_header_labels(*, with_leaf_row: bool = True) -> bytes:
    """NATIVE-TABLE-GUARD-1 — deux colonnes d'en-tête au MÊME libellé (« Grade » ×2) ; la rangée
    suivante, textuelle, porte les feuilles qui les distinguent (``Low`` / ``High``). Sans cette
    rangée, les étiquettes restent ambiguës."""
    head = [["Indicator", "Project", "Level 1", "Level 2", "Grade", "Grade"]]
    if with_leaf_row:
        head.append(["", "", "", "", "Low", "High"])
    body = [["Item A", "866.2", "1050", "750", "900", "800"],
            ["Item B", "1000.7", "4780.6", "1451.1", "n/a", "n/a"],
            ["Item C", "12.5", "40", "35", "30", "25"]]
    return _doc([_table(head + body)])
