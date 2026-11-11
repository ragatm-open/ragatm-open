# backend/apps/rag_core/services/ingest/extraction/excel_headers.py
from __future__ import annotations

"""
Reconstruction des en-têtes de tableau — **multi-lignes et fusionnés**. Module PUR.

Le défaut que ceci corrige, découvert sur un vrai classeur financier :

    Ligne 1 :        [ Premoney Capitalization (fusionné B1:D1) ]
    Ligne 2 :  Security | Shares Outstanding | Fully Diluted Shares | Ownership %
    Ligne 3 :  Common   | 1250000            | 1500000              | 62.5%

L'ancien détecteur ne regardait que **la première ligne du bloc**. Il prenait donc le
bandeau pour l'en-tête — avec des trous `col1`, `col3`, `col4` là où la fusion laisse
des cellules vides — et reléguait les **vrais intitulés** (« Shares Outstanding »…) au
rang de ligne de données.

Les valeurs étaient toutes là. La **sémantique des colonnes**, elle, était perdue : le
texte indexé ne contenait plus « Shares Outstanding » comme un en-tête, le retrieval
lexical ne pouvait plus s'y accrocher, et une question sur une colonne précise devenait
irrépondable.

Principes, dans l'ordre d'importance :

1. **Ne rien inventer.** En cas de doute, on retombe sur `col1..colN` (`headers_inferred`).
   Un en-tête faux est pire que pas d'en-tête : il ment sur la nature de la colonne.
2. **La fusion se propage UNIQUEMENT dans sa plage**, et **UNIQUEMENT** pendant la
   reconstruction de l'en-tête. Jamais dans les données, jamais persistée.
3. **Titre ≠ niveau d'en-tête.** Un bandeau qui couvre un GROUPE de colonnes est un
   niveau parent. Un libellé seul qui couvre TOUTE la largeur ne discrimine aucune
   colonne : c'est un titre. (Le préfixer partout n'ajouterait que du bruit.)
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: Séparateur hiérarchique. Constante testée : « Parent / Enfant ».
#: Ni retour à la ligne (casserait le Markdown), ni `>` (idem), ni concaténation nue.
HEADER_SEPARATOR = " / "

#: Au-delà, un « en-tête » n'en est plus un. Borne dure, jamais dépassée.
DEFAULT_MAX_HEADER_ROWS = 4

MAX_LABEL_CHARS = 120
#: Part de cellules TEXTUELLES au-delà de laquelle une ligne peut être un en-tête.
_TEXTUAL_RATIO = 0.6

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_FALLBACK = "fallback"

STRATEGY_SINGLE_ROW = "single_row"
STRATEGY_MULTI_ROW = "multi_row"
STRATEGY_MULTI_ROW_MERGED = "multi_row_merged"
STRATEGY_INFERRED = "inferred"

_WS = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Libellés
# --------------------------------------------------------------------------- #
def normalize_header_label(value: str) -> str:
    """Nettoie un intitulé SANS le dénaturer.

    On normalise les blancs (« Shares\\nOutstanding » -> « Shares Outstanding ») et on
    borne la longueur. On ne traduit pas, on ne change pas la casse : `EBITDA`, `ARR`,
    `FY2026` doivent rester tels quels. `%`, `#`, `/`, `&`, `-` sont porteurs de sens
    métier — on les garde.
    """
    if not isinstance(value, str):
        return ""
    s = _WS.sub(" ", value).strip()
    return s[:MAX_LABEL_CHARS]


# --------------------------------------------------------------------------- #
# Carte des cellules fusionnées
# --------------------------------------------------------------------------- #
class MergedHeaderMap:
    """`(ligne, colonne)` -> cellule MAÎTRE de la fusion. Consultation en O(1).

    Construite **uniquement** sur la fenêtre des lignes candidates à l'en-tête : une
    feuille peut porter des centaines de fusions dans ses données, elles ne nous
    intéressent pas. On évite ainsi le piège classique — « pour chaque cellule,
    parcourir toutes les fusions » — qui rendrait le coût quadratique.
    """

    __slots__ = ("_master", "_ranges", "truncated")

    def __init__(
        self,
        ranges: Sequence[tuple],
        *,
        row_min: int,
        row_max: int,
        max_cells: int = 200_000,
    ):
        self._master: Dict[Tuple[int, int], Tuple[int, int]] = {}
        self._ranges: List[tuple] = []
        self.truncated = False

        budget = max_cells
        for box in ranges or []:
            if not box:
                continue
            c1, r1, c2, r2 = box
            # Hors de la fenêtre d'en-tête : on ignore, sans même la développer.
            if r2 < row_min or r1 > row_max:
                continue
            if r1 == r2 and c1 == c2:
                continue                       # « fusion » d'une seule cellule : rien à faire

            span = (min(r2, row_max) - max(r1, row_min) + 1) * (c2 - c1 + 1)
            if span > budget:
                self.truncated = True
                break
            budget -= span

            self._ranges.append(box)
            for r in range(max(r1, row_min), min(r2, row_max) + 1):
                for c in range(c1, c2 + 1):
                    self._master[(r, c)] = (r1, c1)

    def master_of(self, row: int, col: int) -> Optional[Tuple[int, int]]:
        return self._master.get((row, col))

    def covers(self, row: int, col: int) -> bool:
        return (row, col) in self._master

    @property
    def ranges(self) -> List[tuple]:
        return self._ranges

    def intersects_rows(self, rows: Sequence[int]) -> bool:
        rs = set(rows)
        return any(any(r in rs for r in range(box[1], box[3] + 1)) for box in self._ranges)


# --------------------------------------------------------------------------- #
# Détection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HeaderDetection:
    """Résultat de la détection. `header_row_indices` est BLOC-LOCAL (0-based)."""

    header_row_indices: Tuple[int, ...]
    confidence: str
    reason: str
    headers_inferred: bool
    strategy: str = STRATEGY_INFERRED
    #: Ligne (bloc-locale) extraite comme TITRE plutôt que comme niveau d'en-tête.
    title_row_index: Optional[int] = None
    warnings: Tuple[str, ...] = field(default_factory=tuple)


def _effective_label(
    cells, i: int, j: int, *, merged: MergedHeaderMap, first_row: int, first_col: int
) -> str:
    """Libellé VU par la colonne `j` à la ligne `i`, fusion propagée.

    La propagation est **virtuelle** : elle ne touche ni la grille, ni les données, ni
    quoi que ce soit de persisté. Elle n'existe que le temps de reconstruire l'en-tête.
    """
    row = cells[i]
    if j < len(row) and row[j].text:
        return normalize_header_label(row[j].text)

    m = merged.master_of(first_row + i, first_col + j)
    if not m:
        return ""
    mi, mj = m[0] - first_row, m[1] - first_col
    if 0 <= mi < len(cells) and 0 <= mj < len(cells[mi]):
        return normalize_header_label(cells[mi][mj].text)
    return ""


def build_effective_header_grid(
    cells,
    header_rows: Sequence[int],
    *,
    merged: MergedHeaderMap,
    first_row: int,
    first_col: int,
    width: int,
) -> List[List[str]]:
    """Grille des libellés d'en-tête, bandeaux propagés. VIRTUELLE et bornée.

    Deux mécanismes de propagation, dans cet ordre :

    1. **La fusion**, quand elle existe. Elle est explicite et fait autorité.

    2. **La PORTÉE**, sinon. 🔴 Découverte du terrain : le classeur financier réel ne
       contient **aucune** cellule fusionnée. Ses bandeaux sont mis en forme par
       « Centrer sur plusieurs colonnes » — visuellement identiques à une fusion,
       structurellement différents : la valeur vit dans **une seule** cellule (`B1`),
       les voisines sont vides. Sans ce second mécanisme, seule la colonne B héritait
       du bandeau ; C et D restaient orphelines.

    La propagation par portée est **strictement bornée**, et ne s'applique qu'aux
    niveaux PARENTS (jamais à la ligne feuille) : un libellé s'étend vers la droite
    tant que, dans SA ligne, les cellules sont vides **ET** que la colonne possède un
    intitulé propre dans la ligne feuille. Elle s'arrête donc net :

    - au bandeau suivant (`Anti-Dilution Variables`) ;
    - sur une colonne séparatrice vide (pas d'intitulé feuille -> rien à qualifier) ;
    - au bord du bloc.

    C'est la convention universelle de lecture d'un en-tête groupé — et elle n'invente
    rien : on ne crée aucun libellé, on rattache une colonne au groupe qui la surplombe.
    """
    if not header_rows:
        return []

    leaf = header_rows[-1]

    def raw(i: int) -> List[str]:
        return [
            _effective_label(cells, i, j, merged=merged, first_row=first_row, first_col=first_col)
            for j in range(width)
        ]

    leaf_labels = raw(leaf)
    grid: List[List[str]] = []

    for i in header_rows:
        labels = raw(i)
        if i != leaf:
            for j in range(width):
                if not labels[j]:
                    continue
                k = j + 1
                while k < width and not labels[k] and leaf_labels[k]:
                    labels[k] = labels[j]
                    k += 1
        grid.append(labels)

    return grid


def _row_is_header_like(cells, i: int, *, merged, first_row, first_col, width) -> bool:
    """Une ligne d'en-tête est **majoritairement textuelle**.

    On raisonne sur les valeurs EFFECTIVES (fusion propagée) : sans cela, un bandeau
    fusionné n'aurait qu'une cellule remplie sur quatre et serait jugé trop vide.
    """
    labels = [
        _effective_label(cells, i, j, merged=merged, first_row=first_row, first_col=first_col)
        for j in range(width)
    ]
    filled = [j for j, t in enumerate(labels) if t]
    if not filled:
        return False

    numeric = sum(
        1 for j in filled
        if j < len(cells[i]) and cells[i][j].text and cells[i][j].is_numeric
    )
    return (len(filled) - numeric) >= len(filled) * _TEXTUAL_RATIO


def _row_has_data_cell(cells, i: int) -> bool:
    """La ligne porte-t-elle une donnée TYPÉE (nombre, date, booléen) ?

    C'est le signal d'arrêt de la profondeur d'en-tête : au-dessus des données, on est
    dans l'en-tête ; dès qu'une valeur typée apparaît, on n'y est plus.
    """
    return any(c.text and c.is_numeric for c in cells[i])


def _is_title_row(labels: Sequence[str], width: int) -> bool:
    """Un TITRE, pas un niveau d'en-tête.

    On juge sur les libellés EFFECTIFS (portée et fusion déjà propagées) — sinon un
    bandeau « Financial Metrics » posé dans une seule cellule passerait pour un titre
    alors qu'il surplombe trois colonnes.

    Est un titre :
    - un libellé UNIQUE couvrant TOUTE la largeur du bloc : il ne discrimine aucune
      colonne, le préfixer partout n'ajouterait que du bruit (`X / a`, `X / b`, …) ;
    - un libellé seul n'ayant qualifié AUCUNE autre colonne (« Financing Overview »
      posé au-dessus d'un tableau autonome).

    Un bandeau qui couvre un GROUPE — mais pas tout — est un vrai niveau parent.
    """
    distinct = {t for t in labels if t}
    if len(distinct) != 1:
        return False
    covered = sum(1 for t in labels if t)
    return covered == width or covered == 1


def detect_header_rows(
    cells,
    *,
    merged: MergedHeaderMap,
    first_row: int,
    first_col: int,
    width: int,
    max_depth: int = DEFAULT_MAX_HEADER_ROWS,
) -> HeaderDetection:
    """Combien de lignes d'en-tête, et lesquelles ?

    On avance tant que la ligne est **majoritairement textuelle** et l'on s'arrête à la
    première ligne de DONNÉES (nombres, dates, booléens). Prudence assumée : sans ligne
    de données identifiable, on ne reconstruit rien.
    """
    n = len(cells)
    warnings: List[str] = []

    def header_like(i: int) -> bool:
        return _row_is_header_like(
            cells, i, merged=merged, first_row=first_row, first_col=first_col, width=width
        )

    # La PREMIÈRE ligne est jugée sur sa proportion de texte (règle historique : un
    # en-tête peut porter un millésime — « Projet | 2025 | 2026 »).
    depth = 1 if n and header_like(0) else 0

    # Au-delà, on est plus STRICT : une ligne d'en-tête intermédiaire ne contient
    # AUCUNE donnée typée. Sans cette exigence, une ligne de données majoritairement
    # textuelle (« Atlas | x | 100 ») serait absorbée dans l'en-tête — et sa valeur
    # perdue. C'est exactement le piège qu'un test de non-régression a levé.
    while depth < n and header_like(depth) and not _row_has_data_cell(cells, depth):
        depth += 1

    if depth == 0:
        # La première ligne est déjà de la donnée : aucun en-tête.
        return HeaderDetection((), CONFIDENCE_FALLBACK, "first_row_is_data", True)

    if depth == n:
        # Aucune ligne de données : c'est un bloc de texte, pas un tableau à en-tête.
        # (Comportement historique : `col1..colN`, toutes les lignes en données.)
        return HeaderDetection((), CONFIDENCE_FALLBACK, "no_data_row", True)

    if depth > max_depth:
        # On ne consomme JAMAIS dix lignes comme en-tête : on garde les plus PROCHES
        # des données (les feuilles les plus spécifiques), et on le signale.
        warnings.append("excel_header_depth_truncated")
        rows = tuple(range(depth - max_depth, depth))
    else:
        rows = tuple(range(depth))

    # Un titre en tête de pile n'est pas un niveau : on l'extrait. La décision se prend
    # sur la grille EFFECTIVE (portée propagée), jamais sur la cellule brute.
    title_idx: Optional[int] = None
    if len(rows) > 1:
        eff0 = build_effective_header_grid(
            cells, rows, merged=merged, first_row=first_row, first_col=first_col, width=width
        )
        if eff0 and _is_title_row(eff0[0], width):
            title_idx = rows[0]
            rows = rows[1:]

    if not rows:
        return HeaderDetection((), CONFIDENCE_FALLBACK, "title_only", True,
                               title_row_index=title_idx, warnings=tuple(warnings))

    has_merge = merged.intersects_rows([first_row + i for i in rows])

    if len(rows) == 1:
        strategy = STRATEGY_SINGLE_ROW
        confidence = CONFIDENCE_HIGH
        reason = "single_textual_header_row"
    else:
        warnings.append("excel_multirow_headers_detected")
        if has_merge:
            strategy = STRATEGY_MULTI_ROW_MERGED
            confidence = CONFIDENCE_HIGH
            reason = "merged_banner_above_column_titles"
        else:
            strategy = STRATEGY_MULTI_ROW
            # Sans fusion, l'empilement de lignes textuelles est plausible mais moins sûr.
            confidence = CONFIDENCE_MEDIUM
            reason = "stacked_textual_rows"

    return HeaderDetection(
        header_row_indices=rows,
        confidence=confidence,
        reason=reason,
        headers_inferred=False,
        strategy=strategy,
        title_row_index=title_idx,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- #
# Reconstruction
# --------------------------------------------------------------------------- #
def build_headers(
    cells,
    detection: HeaderDetection,
    *,
    merged: MergedHeaderMap,
    first_row: int,
    first_col: int,
    width: int,
) -> tuple:
    """Empile les niveaux, colonne par colonne. Renvoie ``(headers, warnings)``.

    Pour chaque colonne : les libellés effectifs des lignes d'en-tête, de haut en bas,
    **doublons adjacents supprimés** (« Revenue / Revenue » -> « Revenue »), niveaux
    vides ignorés (une colonne d'identification n'a pas de bandeau parent — elle garde
    son seul intitulé).
    """
    warnings: List[str] = []

    if detection.headers_inferred or not detection.header_row_indices:
        return [f"col{j + 1}" for j in range(width)], warnings

    eff = build_effective_header_grid(
        cells, detection.header_row_indices, merged=merged,
        first_row=first_row, first_col=first_col, width=width,
    )

    headers: List[str] = []
    for j in range(width):
        stack: List[str] = []
        for row in eff:
            label = row[j]
            if not label:
                continue
            if stack and stack[-1] == label:      # « Revenue / Revenue » -> « Revenue »
                continue
            stack.append(label)
        headers.append(HEADER_SEPARATOR.join(stack) if stack else f"col{j + 1}")

    headers, dupes = _disambiguate(headers)
    if dupes:
        warnings.append("excel_duplicate_headers_disambiguated")

    # Un bandeau fusionné sans valeur au maître : on ne l'invente pas, la colonne garde
    # son niveau feuille (ou `colN`). On le signale, c'est un classeur bancal.
    if any(h.startswith("col") and h[3:].isdigit() for h in headers) and merged.ranges:
        warnings.append("excel_merged_header_without_value")

    return headers, warnings


def _disambiguate(headers: List[str]) -> tuple:
    """Deux colonnes au même intitulé : suffixe stable ``[2]``, ``[3]``…

    On ne recourt JAMAIS à la lettre de colonne dans le texte principal : elle n'a aucun
    sens pour un lecteur, et elle pollue l'embedding.
    """
    seen: Dict[str, int] = {}
    out: List[str] = []
    dupes = False
    for h in headers:
        n = seen.get(h, 0) + 1
        seen[h] = n
        if n == 1:
            out.append(h)
        else:
            dupes = True
            out.append(f"{h} [{n}]")
    return out, dupes
