# backend/apps/rag_core/services/ingest/extraction/pdf_tables.py
from __future__ import annotations

"""
Tableaux PDF GÉNÉRIQUES — reconstruction structurelle.

Module PUR : aucun accès DB, aucun import Django, aucun appel réseau, aucun modèle
de langage. Il transforme la sortie de mise en page de ``pdfplumber`` en tableaux
LOGIQUES porteurs de trois informations que l'indexation par page détruit :

    à quelle LIGNE, à quelle COLONNE et à quelle PAGE SOURCE appartient une valeur.

Principe directeur — GÉNÉRICITÉ
-------------------------------
Toutes les décisions reposent sur des signaux STRUCTURELS : topologie de fusion des
cellules, occupation, géométrie des colonnes, continuité verticale entre pages,
similarité d'en-tête normalisée. Aucune ne repose sur le vocabulaire, le titre, le
nom de fichier, un numéro de page, ni un format d'identifiant de ligne. Un corpus
peut servir d'oracle ; il ne configure jamais le moteur.

Deux pièges que ce module refuse explicitement
----------------------------------------------
1. **« Première cellule vide = continuation »** est FAUX : une ligne peut
   légitimement n'avoir pas d'identifiant. La continuation exige un faisceau de
   signaux (cf. `_continuation_decision`), dont la position de la ligne en tête d'un
   fragment lui-même chaîné géométriquement.
2. **« Même en-tête = même tableau »** est FAUX : deux tableaux indépendants
   partagent souvent leur schéma. Le chaînage exige en plus une CONTINUITÉ
   VERTICALE (le tableau descend au bas d'une page et reprend en haut de la
   suivante) et une compatibilité géométrique des colonnes.

En cas de doute, le module ne structure pas : il rend un résultat `fallback` et
laisse le texte de page d'origine intact (jamais de perte de contenu, jamais
d'association inventée).
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Vocabulaire du contrat (neutre)
# --------------------------------------------------------------------------- #
TABLE_TYPE_GENERIC = "generic"
TABLE_TYPE_MATRIX = "generic_matrix"

ROW_KIND_DATA = "data"
ROW_KIND_SEPARATOR = "separator"

OUTCOME_STRUCTURED = "structured"
OUTCOME_REJECTED = "rejected"
OUTCOME_FALLBACK = "fallback_unstructured"

WARNING_UNSTRUCTURED = "pdf_table_unstructured"

#: Étiquette neutre des colonnes d'un tableau sans en-tête textuel.
NEUTRAL_COLUMN = "Column {}"
#: Séparateur d'un chemin de colonne hiérarchique.
PATH_SEP = " > "

# --------------------------------------------------------------------------- #
# Seuils STRUCTURELS (aucun n'encode une propriété d'un document particulier)
# --------------------------------------------------------------------------- #
#: Bande basse considérée comme habillage de page (pied de page, pagination). Le
#: texte qui s'y trouve ne compte pas comme « contenu après le tableau ».
PAGE_FURNITURE_BAND = 0.88
#: Un tableau qui n'atteint pas cette fraction de la page n'a pas « rempli » sa page,
#: même si rien ne le suit : une page nue en dessous n'est pas une continuation.
#: Seuil LARGE, choisi pour séparer un tableau qui occupe le bas de page d'un
#: tableau court resté en haut — pas pour coller à une mise en page particulière.
MIN_BOTTOM_RATIO = 0.70
#: Un fragment « reprend en haut de page » si son bord supérieur est au-dessus de
#: cette fraction. Au-delà, quelque chose le précède (titre, prose) : nouveau tableau.
TOP_CONTINUITY_RATIO = 0.25
#: Tolérance relative de comparaison des bords de colonnes entre deux fragments.
COLUMN_GEOMETRY_TOLERANCE = 0.02
#: Recouvrement minimal, en points, pour qu'une cellule appartienne à une colonne.
#: Deux colonnes voisines partagent un bord, et les coordonnées sont arrondies : sans
#: cette marge, une cellule déborderait dans la colonne d'à côté.
OVERLAP_TOLERANCE_PT = 0.5

# --- Continuation typographique (B1) --------------------------------------- #
# Une cellule coupée par un saut de page reprend SANS espacement de paragraphe ;
# une nouvelle ligne en ouvre un et porte donc son espace avant. L'écart entre le
# bord haut d'une rangée et son premier glyphe distingue donc les deux — mais sa
# valeur dépend de la fonte et des styles du document, jamais d'une constante
# universelle. On la calibre DANS le document.
#
# Les trois réglages ci-dessous ne sont pas des seuils de décision : ils bornent la
# calibration. Ils ont été éprouvés sur une large plage (échantillon 6 à 12, plancher
# 0,5 à 3,0 pt, facteur 1,5 à 10) sans changer un seul verdict.
#: En deçà, la population est trop faible pour situer la rangée ordinaire.
MIN_GAP_SAMPLE = 8
#: Écart minimal exigé sous la rangée ordinaire, en points. Évite qu'une dispersion
#: nulle rende la règle hypersensible au bruit de mesure.
GAP_CUT_FLOOR_PT = 1.5
#: Multiplicateur de l'écart absolu médian.
GAP_CUT_MAD_FACTOR = 3.0

# --- Bandeau d'en-tête réglé (NATIVE-TABLE-RECOVERY-2) ---------------------- #
# Un corps de tableau sans rangée d'en-tête n'est nommé que si un BANDEAU réglé lui
# appartient géométriquement : chaque bord intérieur de colonne du corps y porte un
# filet vertical, aux mêmes abscisses. Aucun mot n'intervient dans la décision.
#: Écart maximal, en points, entre deux abscisses de filets tenues pour identiques.
RULING_TOLERANCE_PT = 2.0
#: Écart vertical maximal entre le bas d'un bandeau et le haut du corps (même page).
HEADER_BAND_MAX_GAP_PT = 12.0
#: Lignes de texte au plus par niveau de bandeau : au-delà, ce sont des données.
HEADER_BAND_MAX_LINES = 3
#: Part minimale des mots du moteur natif que la reconstruction pdfplumber doit
#: retrouver pour qu'on lui confie le texte résiduel d'une page (sinon : mots natifs).
RESIDUAL_FIDELITY_MIN = 0.85

# --- Grille réglée sans filets horizontaux (NATIVE-TABLE-GROUNDING-3) ------- #
# Colonnes prouvées par des filets VERTICAUX dessinés (jamais par un simple alignement de
# mots) ; rangées prouvées par une ligne d'ANCRAGE sur laquelle chaque cellule de la rangée
# est centrée verticalement (mise en page centrée des cellules multilignes).
#: Écart maximal, en points, entre deux ordonnées de lignes de texte tenues pour une seule.
LINE_TOLERANCE_PT = 2.0
#: Écart maximal entre le centre vertical d'une cellule et celui de sa ligne d'ancrage.
#: Même ordre que la tolérance des filets : c'est une erreur d'arrondi, pas un jeu.
ROW_CENTERING_TOLERANCE_PT = 2.0
#: Une ligne est d'ancrage si elle occupe au moins cette part des colonnes.
ANCHOR_MIN_COLUMN_SHARE = 0.5
#: Rangées certifiées au minimum pour publier une grille : une seule ne prouve aucune régularité.
RULELESS_MIN_ROWS = 2
#: Base structurelle déclarée par les fragments de ces grilles (`meta.table.structure`).
STRUCTURE_RULED_COLUMNS_ANCHOR_ROWS = "ruled_columns_anchor_rows"
#: NATIVE-TABLE-GEOMETRY-12 : grille à filets de colonnes SEGMENTÉS (rectangles fins à double bord, filets
#: interrompus par des rangées fusionnées), en-tête prouvé par un segment commun, rangées ancrées.
STRUCTURE_SEGMENTED_RULED_COLUMNS = "segmented_ruled_columns_anchor_rows"
#: Deux bords verticaux plus proches que cela sont un SEUL filet (les deux côtés d'un rectangle fin) : aucune
#: colonne de texte ne tient dans cet écart.
SEGMENTED_EDGE_MERGE_PT = 2.0


# --------------------------------------------------------------------------- #
# Contrats de données
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SourceFragment:
    """Contribution d'UNE page physique à une ligne logique."""

    page_number: int
    physical_row_index: int
    #: étiquette de colonne (ou chemin hiérarchique) -> texte. Les cellules vides
    #: sont ABSENTES : une valeur manquante reste manquante, jamais inventée.
    cells: Dict[str, str]
    #: étiquette de colonne -> statut de lecture, ADDITIF.
    #: Vide pour le rail natif : absence = valeur exacte, ou cellule vide si la
    #: colonne manque aussi dans `cells`. Une cellule ILLISIBLE n'a pas de clé dans
    #: `cells` mais porte ``"unreadable"`` ici — elle ne se confond plus avec le vide.
    cell_states: Dict[str, str] = field(default_factory=dict)
    #: étiquette de colonne -> enveloppe (x0, top, x1, bottom) des cellules physiques
    #: lues pour cette valeur. Provenance seulement : n'entre dans aucune décision.
    cell_boxes: Dict[str, Tuple[float, float, float, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = set(self.cell_states.values()) - CELL_STATES
        if unknown:
            raise ValueError(f"cell_states: statut inconnu {sorted(unknown)}")


#: Statuts de lecture d'une cellule. `exact` n'est jamais stocké : c'est l'absence.
CELL_STATE_UNREADABLE = "unreadable"
CELL_STATE_APPROXIMATE = "approximate"
CELL_STATE_AMBIGUOUS = "ambiguous"
CELL_STATES = frozenset({CELL_STATE_UNREADABLE, CELL_STATE_APPROXIMATE, CELL_STATE_AMBIGUOUS})
#: Statuts qui appellent une vérification visuelle du fragment. `approximate` en est
#: exclu : la valeur est étiquetée comme approximative dans le texte indexé.
CELL_STATES_REQUIRING_CHECK = frozenset({CELL_STATE_UNREADABLE, CELL_STATE_AMBIGUOUS})
UNREADABLE_CELL_MARKER = "[unreadable]"


@dataclass
class LogicalRow:
    logical_row_id: str
    row_kind: str
    fragments: List[SourceFragment] = field(default_factory=list)
    #: valeur de la première colonne sémantique si elle existe — simple report du
    #: texte, sans interprétation de sa syntaxe.
    row_label: Optional[str] = None
    #: étiquette de la dernière rangée séparatrice rencontrée avant cette ligne.
    context_label: Optional[str] = None

    @property
    def source_pages(self) -> List[int]:
        return sorted({f.page_number for f in self.fragments})

    @property
    def is_continued(self) -> bool:
        return len(self.source_pages) > 1


@dataclass
class LogicalTable:
    table_id: str
    table_type: str
    #: étiquettes de colonnes « feuilles » (chemin joint pour une matrice).
    schema: List[str]
    #: chemin complet par colonne sémantique (un seul niveau pour un tableau plat).
    column_paths: List[List[str]]
    rows: List[LogicalRow] = field(default_factory=list)
    source_pages: List[int] = field(default_factory=list)
    #: provenance des étiquettes quand elles ne viennent pas d'une rangée du corps
    #: (bandeau réglé : page, enveloppe, relation au corps). ``None`` sinon.
    header_source: Optional[dict] = None
    #: base structurelle quand ce n'est pas la grille complète de l'analyseur
    #: (`STRUCTURE_RULED_COLUMNS_ANCHOR_ROWS`). ``None`` : grille de l'analyseur.
    structure: Optional[str] = None

    @property
    def page_start(self) -> int:
        return self.source_pages[0] if self.source_pages else 0

    @property
    def page_end(self) -> int:
        return self.source_pages[-1] if self.source_pages else 0


@dataclass
class TableExtractionReport:
    tables: List[LogicalTable] = field(default_factory=list)
    #: un enregistrement par candidat physique : page, issue, motif.
    outcomes: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: page -> bboxes des régions STRUCTURÉES, seules retirables du texte de page.
    suppressed_regions: Dict[int, List[Tuple[float, float, float, float]]] = field(default_factory=dict)
    #: page -> texte de page PRIVÉ des régions structurées, reconstruit depuis les
    #: positions de mots. Ne contient que les pages effectivement concernées.
    residual_page_text: Dict[int, str] = field(default_factory=dict)
    #: page -> moteur dont les MOTS ont servi au texte résiduel (« pdfplumber » ou
    #: « pymupdf », cf. `_residual_text`).
    residual_engine: Dict[int, str] = field(default_factory=dict)
    #: identités (``id``) des fragments physiques REJETÉS comme `single_semantic_column` : ils ne masquent
    #: pas leur région aux certificateurs de grilles réglées, qui y apportent une preuve indépendante.
    rejected_fragment_ids: set = field(default_factory=set)


@dataclass
class PageGeometry:
    """Filets d'une page, fusionnés par abscisse (verticaux) ou ordonnée (horizontaux).

    Sert à prouver qu'un bandeau d'en-tête et un corps partagent la même grille même
    quand l'analyseur n'a pas reconnu la région du bandeau comme un tableau."""

    page_number: int
    width: float
    height: float
    #: (x, top, bottom), segments colinéaires contigus fusionnés.
    v_segments: List[Tuple[float, float, float]] = field(default_factory=list)


@dataclass
class TableLimits:
    """Bornes génériques contre les PDF dégénérés ou hostiles."""

    max_candidates: int = 400
    max_cells_per_table: int = 20_000
    max_rows_per_table: int = 2_000
    max_logical_tables: int = 200
    max_cell_chars: int = 4_000


# --------------------------------------------------------------------------- #
# Normalisation de texte (conservatrice)
# --------------------------------------------------------------------------- #
_WS = re.compile(r"\s+")
#: Césure de fin de ligne : lettre + tiret + saut + minuscule. Recollage seulement
#: dans ce cas déterministe ; aucun autre tiret n'est touché.
_HYPHEN_BREAK = re.compile(r"([A-Za-zÀ-ÿ])-\s*\n\s*([a-zà-ÿ])")
_NUMERICISH = re.compile(r"^[\d\s.,%+\-/()]+$")


def normalize_cell(text: Optional[str], *, max_chars: int = 4_000) -> str:
    """Espaces normalisés et césures de fin de ligne recollées. Rien d'autre."""
    if not text:
        return ""
    s = _HYPHEN_BREAK.sub(r"\1\2", str(text))
    s = _WS.sub(" ", s).strip()
    return s[:max_chars]


def _is_numericish(s: str) -> bool:
    return bool(s) and bool(_NUMERICISH.match(s)) and any(c.isdigit() for c in s)


def _norm_key(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


# --------------------------------------------------------------------------- #
# Couche physique
# --------------------------------------------------------------------------- #
@dataclass
class PhysicalTableFragment:
    """Sortie brute de l'analyseur de mise en page pour UN tableau d'UNE page.

    Ce n'est PAS encore un tableau logique : un tableau logique se construit à
    partir d'un ou plusieurs fragments."""

    page_number: int
    bbox: Tuple[float, float, float, float]
    page_height: float
    #: matrice de texte normalisé, indexée [rangée][colonne physique].
    cells: List[List[str]]
    #: topologie de fusion : [rangée] -> [(index de colonne, nombre de colonnes couvertes)].
    spans: List[List[Tuple[int, int]]]
    #: bbox de chaque cellule, ``None`` là où une fusion couvre la position.
    boxes: List[List[Optional[Tuple[float, float, float, float]]]]
    #: bords x des colonnes physiques, pour comparer deux fragments.
    column_edges: List[float]
    #: nombre de mots situés SOUS le fragment, hors bande d'habillage de page.
    #: Zéro signifie que le tableau va jusqu'au bas de la zone de texte.
    words_below: int = 0

    @property
    def n_physical_cols(self) -> int:
        return max((len(r) for r in self.cells), default=0)

    @property
    def reaches_page_bottom(self) -> bool:
        """Le tableau occupe-t-il le bas de sa page ?

        Un tableau qui s'arrête nettement plus haut a fini : ce qui suit sur la page
        suivante est un autre tableau, fût-il de schéma identique. Le seuil est
        volontairement LARGE et sépare deux situations très éloignées (un tableau
        paginé descend au-delà des trois quarts de la page, un tableau court reste
        dans le premier quart) ; il n'est calé sur aucune mise en page particulière."""
        return self.bottom_ratio >= MIN_BOTTOM_RATIO

    @property
    def bottom_ratio(self) -> float:
        return self.bbox[3] / self.page_height if self.page_height else 0.0

    @property
    def top_ratio(self) -> float:
        return self.bbox[1] / self.page_height if self.page_height else 1.0


def _row_spans(row_cells: Sequence) -> List[Tuple[int, int]]:
    """Topologie de fusion d'une rangée.

    ``pdfplumber`` rend ``None`` pour les positions COUVERTES par une cellule
    fusionnée. Le nombre de ``None`` consécutifs après une cellule donne donc
    directement sa portée, sans aucun calcul géométrique ni tolérance."""
    out: List[Tuple[int, int]] = []
    n = len(row_cells)
    for i, c in enumerate(row_cells):
        if c is None:
            continue
        j = i + 1
        while j < n and row_cells[j] is None:
            j += 1
        out.append((i, j - i))
    return out


def _collect_fragments(pdf, wanted: Optional[set], limits: TableLimits,
                       dropped: Optional[List[dict]] = None,
                       geometry: Optional[Dict[int, "PageGeometry"]] = None) -> List[PhysicalTableFragment]:
    frags: List[PhysicalTableFragment] = []
    dropped = dropped if dropped is not None else []
    for page_index, page in enumerate(pdf.pages, start=1):
        if wanted is not None and page_index not in wanted:
            continue
        if geometry is not None:
            # Lue pour TOUTE page analysée, tableau reconnu ou non : le bandeau d'en-tête
            # d'un corps peut se trouver sur une page où l'analyseur n'a rien reconnu.
            geometry[page_index] = _page_geometry(page, page_index)
        try:
            found = page.find_tables() or []
        except Exception:  # analyseur en défaut sur cette page : on l'ignore
            continue
        raw: List[Tuple] = []
        for tobj in found:
            try:
                matrix = tobj.extract() or []
            except Exception:
                continue
            raw.append((tobj, matrix))

        # Sous-fragments de l'analyseur : une bbox strictement contenue dans une
        # autre décrit une RÉGION du même tableau, pas un tableau indépendant.
        boxes = [t.bbox for t, _ in raw]
        for idx, (tobj, matrix) in enumerate(raw):
            if _is_contained_in_other(tobj.bbox, boxes, idx):
                # Région INTERNE à un autre candidat : l'analyseur a redécoupé une
                # portion du même tableau. Son contenu est déjà porté par le
                # candidat englobant, donc l'écarter ne perd rien.
                dropped.append({"page": page_index, "outcome": OUTCOME_REJECTED,
                                "reason": "nested_parser_fragment"})
                continue
            cells = [[normalize_cell(c, max_chars=limits.max_cell_chars) for c in row]
                     for row in matrix]
            spans = [_row_spans(r.cells) for r in tobj.rows]
            bottom = float(tobj.bbox[3])
            furniture = float(page.height) * PAGE_FURNITURE_BAND
            try:
                below = sum(1 for w in (page.extract_words() or [])
                            if float(w["top"]) > bottom and float(w["top"]) < furniture)
            except Exception:
                below = 0
            frags.append(PhysicalTableFragment(
                page_number=page_index,
                bbox=tuple(float(v) for v in tobj.bbox),
                page_height=float(page.height),
                cells=cells,
                spans=spans,
                boxes=[[(tuple(float(v) for v in c) if c is not None else None)
                        for c in r.cells] for r in tobj.rows],
                column_edges=_column_edges(tobj),
                words_below=below,
            ))
            if len(frags) >= limits.max_candidates:
                return frags
    return frags


def _is_contained_in_other(bbox, boxes, self_index: int) -> bool:
    x0, top, x1, bot = bbox
    for i, other in enumerate(boxes):
        if i == self_index:
            continue
        ox0, otop, ox1, obot = other
        area = (x1 - x0) * (bot - top)
        oarea = (ox1 - ox0) * (obot - otop)
        if oarea <= area:
            continue
        if ox0 - 1 <= x0 and otop - 1 <= top and x1 <= ox1 + 1 and bot <= obot + 1:
            return True
    return False


def page_text_excluding(page, boxes: Sequence[Tuple[float, float, float, float]],
                        *, line_tolerance: float = 3.0) -> str:
    """Texte d'une page PRIVÉ des régions données, par position des mots.

    On retire les MOTS dont le centre tombe dans une région structurée, puis on
    reconstruit les lignes par ordonnée. C'est la seule méthode sûre : soustraire
    des chaînes du texte aplati supprimerait aussi les occurrences identiques
    situées hors du tableau, et dépendrait de l'ordre de lecture du moteur."""
    try:
        words = page.extract_words() or []
    except Exception:
        return ""
    return text_from_words(words, boxes, line_tolerance=line_tolerance)


def text_from_words(words: Sequence[dict], boxes: Sequence[Tuple[float, float, float, float]],
                    *, line_tolerance: float = 3.0) -> str:
    """Lignes reconstruites depuis des mots positionnés (``x0``, ``x1``, ``top``,
    ``bottom``, ``text``), privées des mots centrés dans ``boxes``. Indépendant du
    moteur qui a produit les mots."""
    kept = []
    for w in words:
        cx = (float(w["x0"]) + float(w["x1"])) / 2.0
        cy = (float(w["top"]) + float(w["bottom"])) / 2.0
        if any(x0 <= cx <= x1 and top <= cy <= bot for (x0, top, x1, bot) in boxes):
            continue
        kept.append(w)
    if not kept:
        return ""
    lines: List[Tuple[float, List[dict]]] = []
    for w in sorted(kept, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(w["top"])
        if lines and abs(lines[-1][0] - top) <= line_tolerance:
            lines[-1][1].append(w)
        else:
            lines.append((top, [w]))
    out = []
    for _, ws in lines:
        out.append(" ".join(w["text"] for w in sorted(ws, key=lambda w: float(w["x0"]))))
    return "\n".join(out).strip()


def _page_geometry(page, page_index: int) -> "PageGeometry":
    """Filets verticaux d'une page, segments colinéaires contigus fusionnés.

    Un même filet est souvent dessiné en plusieurs traits (une cellule après
    l'autre) : sans fusion, la continuité d'une grille ne se lirait pas."""
    raw: Dict[float, List[Tuple[float, float]]] = {}
    try:
        edges = page.edges or []
    except Exception:
        edges = []
    for e in edges:
        if e.get("orientation") != "v":
            continue
        x = round(float(e["x0"]) * 2) / 2.0
        raw.setdefault(x, []).append((float(e["top"]), float(e["bottom"])))
    merged: List[Tuple[float, float, float]] = []
    for x, spans in raw.items():
        spans.sort()
        top, bot = spans[0]
        for a, b in spans[1:]:
            if a <= bot + 1.0:
                bot = max(bot, b)
            else:
                merged.append((x, top, bot))
                top, bot = a, b
        merged.append((x, top, bot))
    return PageGeometry(page_number=page_index, width=float(page.width),
                        height=float(page.height), v_segments=sorted(merged))


def _column_edges(tobj) -> List[float]:
    edges = set()
    for row in tobj.rows:
        for c in row.cells:
            if c is None:
                continue
            edges.add(round(float(c[0]), 1))
            edges.add(round(float(c[2]), 1))
    return sorted(edges)


# --------------------------------------------------------------------------- #
# Normalisation des colonnes : physique -> sémantique
# --------------------------------------------------------------------------- #
def _occupied_columns(frag: PhysicalTableFragment) -> List[int]:
    """Colonnes physiques portant du texte quelque part (tableaux SANS en-tête).

    Une colonne n'est écartée que si elle est VIDE partout : c'est le seul cas où
    il est certain qu'elle ne porte rien. Une colonne dont beaucoup de cellules
    sont vides mais qui possède une valeur quelque part est CONSERVÉE (§12)."""
    n = frag.n_physical_cols
    return [c for c in range(n)
            if any(len(row) > c and row[c] for row in frag.cells)]


def _groups_from_span_row(frag: PhysicalTableFragment, ri: int, *,
                          attach_orphans: bool = True) -> List[List[int]]:
    """Colonnes SÉMANTIQUES d'après la rangée `ri` : une cellule et sa PORTÉE.

    C'est la définition structurelle correcte d'une colonne. Déduire les colonnes
    de l'occupation globale est un piège : dans un tableau réel, une rangée
    séparatrice ou une ligne de débordement écrit dans une colonne d'espacement, et
    l'occupation invente alors une colonne qui n'existe pas — le nombre de colonnes
    se met à varier d'une page à l'autre et la chaîne se rompt.

    Les colonnes physiques qu'aucune cellule PORTEUSE de la rangée ne couvre sont
    des espacements purs : elles disparaissent."""
    groups: List[List[int]] = []
    for start, span in (frag.spans[ri] if ri < len(frag.spans) else []):
        text = frag.cells[ri][start] if (ri < len(frag.cells) and len(frag.cells[ri]) > start) else ""
        if not text:
            continue
        groups.append(list(range(start, start + span)))
    if not groups or not attach_orphans:
        return groups
    # Colonnes physiques qu'aucune cellule d'en-tête ne couvre. Une rangée de corps
    # peut être découpée plus finement que l'en-tête — typiquement quand l'analyseur
    # matérialise une gouttière à gauche du tableau. Ces colonnes sont rattachées au
    # groupe le plus proche : les abandonner perdrait leur contenu, et en faire des
    # colonnes autonomes ferait varier le schéma d'une page à l'autre.
    covered = {c for g in groups for c in g}
    for c in range(frag.n_physical_cols):
        if c in covered:
            continue
        nearest = min(groups, key=lambda g: min(abs(c - x) for x in g))
        nearest.append(c)
    for g in groups:
        g.sort()
    return groups


def _column_intervals(frag: PhysicalTableFragment, ri: int) -> List[Tuple[float, float]]:
    """Colonnes SÉMANTIQUES comme intervalles en x, lus sur la rangée `ri`.

    Un intervalle, et non un indice de colonne : l'en-tête et le corps d'un même
    tableau ne sont pas toujours découpés aux mêmes endroits, et apparier par
    indice décale alors tout le contenu d'une colonne — une phrase exacte se
    retrouve sous la mauvaise étiquette, ce qui est précisément l'échec que ce lot
    doit supprimer. La géométrie, elle, ne ment pas."""
    out: List[Tuple[float, float]] = []
    row_boxes = frag.boxes[ri] if ri < len(frag.boxes) else []
    row_text = frag.cells[ri] if ri < len(frag.cells) else []
    for start, _span in (frag.spans[ri] if ri < len(frag.spans) else []):
        if not (len(row_text) > start and row_text[start]):
            continue
        box = row_boxes[start] if len(row_boxes) > start else None
        if box is None:
            continue
        out.append((box[0], box[2]))
    return sorted(out)


def _assign_row(frag: PhysicalTableFragment, ri: int,
                intervals: List[Tuple[float, float]],
                exclude: Optional[Tuple[float, float]] = None) -> List[str]:
    """Texte de chaque colonne sémantique pour la rangée `ri`.

    Chaque cellule rejoint la colonne avec laquelle elle se RECOUVRE le plus. Une
    cellule qui ne recouvre aucune colonne (gouttière) est rattachée à la plus
    proche : l'écarter perdrait son contenu."""
    out = [[] for _ in intervals]
    if not intervals:
        return []
    for pos, text, _box in _assign_positions(frag, ri, intervals, exclude):
        out[pos].append(text)
    return [" ".join(p).strip() for p in out]


def _assign_positions(frag: PhysicalTableFragment, ri: int,
                      intervals: List[Tuple[float, float]],
                      exclude: Optional[Tuple[float, float]] = None) -> List[Tuple[int, str, tuple]]:
    """(colonne, texte, bbox) de chaque cellule porteuse de la rangée — règle de `_assign_row`."""
    out: List[Tuple[int, str, tuple]] = []
    if not intervals:
        return out
    row_boxes = frag.boxes[ri] if ri < len(frag.boxes) else []
    row_text = frag.cells[ri] if ri < len(frag.cells) else []
    for c, text in enumerate(row_text):
        if not text:
            continue
        box = row_boxes[c] if len(row_boxes) > c else None
        if box is None:
            continue
        x0, x1 = box[0], box[2]
        if exclude is not None and min(x1, exclude[1]) - max(x0, exclude[0]) > OVERLAP_TOLERANCE_PT:
            # Zone d'ancrage d'une matrice : cette cellule NOMME la ligne, elle
            # n'est pas une donnée. Le repli de proximité la ferait autrement entrer
            # dans la première colonne de données.
            continue
        best, best_overlap = None, OVERLAP_TOLERANCE_PT
        for i, (a, b) in enumerate(intervals):
            overlap = min(x1, b) - max(x0, a)
            if overlap > best_overlap:
                best, best_overlap = i, overlap
        if best is None:
            centre = (x0 + x1) / 2.0
            best = min(range(len(intervals)),
                       key=lambda i: min(abs(centre - intervals[i][0]),
                                         abs(centre - intervals[i][1])))
        out.append((best, text, box))
    return out


def _text_in_interval(frag: PhysicalTableFragment, ri: int,
                      interval: Tuple[float, float]) -> str:
    """Texte des cellules qui RECOUVRENT réellement cet intervalle.

    Sans repli « colonne la plus proche », contrairement à `_assign_row` : ici on
    interroge une zone précise, et y rapatrier une cellule voisine ferait entrer
    une valeur de données dans l'étiquette de ligne."""
    a, b = interval
    parts = []
    row_boxes = frag.boxes[ri] if ri < len(frag.boxes) else []
    row_text = frag.cells[ri] if ri < len(frag.cells) else []
    for c, text in enumerate(row_text):
        if not text:
            continue
        box = row_boxes[c] if len(row_boxes) > c else None
        if box is None:
            continue
        if min(box[2], b) - max(box[0], a) > OVERLAP_TOLERANCE_PT:
            parts.append(text)
    return " ".join(parts).strip()


def _has_cell_in_interval(frag: PhysicalTableFragment, ri: int,
                          interval: Tuple[float, float]) -> bool:
    """Une cellule EXISTE-t-elle dans cet intervalle ? (bordée, même vide)

    À distinguer d'une cellule vide : une cellule absente signifie que la rangée
    n'est pas découpée à cet endroit, donc qu'elle prolonge visuellement la
    précédente."""
    a, b = interval
    for box in (frag.boxes[ri] if ri < len(frag.boxes) else []):
        if box is None:
            continue
        if min(box[2], b) - max(box[0], a) > OVERLAP_TOLERANCE_PT:
            return True
    return False


def _cell_gaps(frag: PhysicalTableFragment, words: Dict[int, list], ri: int) -> List[float]:
    """Écart bord-haut -> premier glyphe, pour chaque cellule PORTEUSE de la rangée.

    Une cellule vide n'a pas de premier glyphe : elle ne participe pas, et ne reçoit
    surtout pas un écart fictif de zéro, qui la ferait passer pour une continuation.
    Une cellule fusionnée est mesurée UNE fois, sur sa boîte réelle."""
    out: List[float] = []
    ws = words.get(frag.page_number) or []
    row = frag.boxes[ri] if ri < len(frag.boxes) else []
    for box in row:
        if box is None:
            continue
        tops = [float(w["top"]) for w in ws
                if float(w["top"]) >= box[1] - 1 and float(w["bottom"]) <= box[3] + 1
                and float(w["x0"]) >= box[0] - 1 and float(w["x1"]) <= box[2] + 1]
        if tops:
            out.append(round(min(tops) - box[1], 2))
    return out


def row_gap(frag: PhysicalTableFragment, words: Dict[int, list], ri: int) -> Optional[float]:
    """Écart de la rangée : le MINIMUM des écarts de ses cellules porteuses.

    Le minimum, et non la médiane : une rangée peut poursuivre un paragraphe coupé
    dans UNE seule colonne pendant que les autres en ouvrent un neuf. La médiane
    serait alors tirée vers le haut et classerait toute la rangée comme nouvelle,
    perdant la continuation."""
    gaps = _cell_gaps(frag, words, ri)
    return min(gaps) if gaps else None


def document_row_gaps(frags: Sequence[PhysicalTableFragment],
                      words: Dict[int, list]) -> List[float]:
    out: List[float] = []
    for frag in frags:
        for ri in range(len(frag.boxes)):
            g = row_gap(frag, words, ri)
            if g is not None:
                out.append(g)
    return out


def calibrate_row_gap_cut(gaps: Sequence[float]) -> Optional[float]:
    """Coupe locale au document, sous laquelle une rangée poursuit un paragraphe.

    On ne modélise QUE la rangée ordinaire — toujours majoritaire dans un tableau —
    par sa médiane et sa dispersion propre, puis on déclare continuation ce qui
    commence nettement plus haut qu'elle.

    Aucune bimodalité n'est exigée : un document dont une seule ligne est coupée par
    la pagination n'a qu'un mode peuplé, et une méthode à deux classes s'y abstenait
    sur un cas pourtant net.

    Rend ``None`` — donc abstention, et repli sur les seules règles structurelles —
    quand l'échantillon est trop faible ou qu'aucune rangée ne passe sous la coupe."""
    vals = sorted(v for v in gaps if v is not None)
    if len(vals) < MIN_GAP_SAMPLE:
        return None
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    devs = sorted(abs(v - median) for v in vals)
    m = len(devs) // 2
    mad = devs[m] if len(devs) % 2 else (devs[m - 1] + devs[m]) / 2.0
    cut = median - max(GAP_CUT_FLOOR_PT, GAP_CUT_MAD_FACTOR * mad)
    if not any(v <= cut for v in vals):
        return None
    return round(cut, 3)


def _cell_starts(frag: PhysicalTableFragment, ri: int) -> set:
    """Colonnes physiques où une cellule COMMENCE réellement dans cette rangée.

    À distinguer d'une cellule vide : une cellule existe (bordée, vide) ou n'existe
    pas du tout (la rangée n'est pas découpée à cet endroit). Cette différence est
    ce qui sépare une nouvelle ligne sans identifiant d'une ligne de débordement."""
    return {s for s, _ in (frag.spans[ri] if ri < len(frag.spans) else [])}


# --------------------------------------------------------------------------- #
# En-têtes
# --------------------------------------------------------------------------- #
@dataclass
class HeaderModel:
    #: indices des rangées physiques consommées par l'en-tête.
    header_rows: List[int]
    #: colonne sémantique de DONNÉES -> intervalle en x.
    intervals: List[Tuple[float, float]]
    #: chemin par colonne sémantique de DONNÉES.
    column_paths: List[List[str]]
    hierarchical: bool
    #: intervalles nommant les LIGNES (matrice). Ce ne sont pas des colonnes de
    #: données : elles n'ont pas leur place dans le schéma.
    row_header_intervals: List[Tuple[float, float]] = field(default_factory=list)

    @property
    def row_header_interval(self) -> Optional[Tuple[float, float]]:
        """Enveloppe des colonnes d'étiquettes, ou ``None`` hors matrice."""
        if not self.row_header_intervals:
            return None
        return (min(a for a, _ in self.row_header_intervals),
                max(b for _, b in self.row_header_intervals))


def _group_text(row: List[str], group: List[int]) -> str:
    """Texte d'une colonne sémantique dans une rangée : ses colonnes physiques jointes.

    Une cellule de corps peut être découpée autrement que l'en-tête ; on rassemble
    donc tout ce que le groupe recouvre, dans l'ordre de lecture."""
    parts = [row[c] for c in group if len(row) > c and row[c]]
    return " ".join(parts).strip()


def _modal_intervals(frag: PhysicalTableFragment) -> List[Tuple[float, float]]:
    """Grille FEUILLE : la segmentation en x la plus fréquente parmi les rangées.

    C'est celle du corps, majoritaire par construction dans un tableau ordinaire. À
    fréquence égale on retient la plus FINE, qui est celle des données et non celle
    d'un niveau d'en-tête fusionné."""
    counts: Dict[Tuple[Tuple[float, float], ...], int] = {}
    for ri in range(len(frag.boxes)):
        seg = tuple(sorted((round(b[0], 1), round(b[2], 1))
                           for b in frag.boxes[ri] if b is not None))
        if seg:
            counts[seg] = counts.get(seg, 0) + 1
    if not counts:
        return []
    top = max(counts.values())
    best = max((seg for seg, n in counts.items() if n == top), key=len)
    return [(a, b) for a, b in best]


def _covers_several(box, leaves: List[Tuple[float, float]]) -> int:
    return sum(1 for (a, b) in leaves if min(box[2], b) - max(box[0], a) > 0.5)


def _column_header_depth(frag: PhysicalTableFragment, leaves: List[Tuple[float, float]]) -> int:
    """Préfixe de rangées formant la hiérarchie des COLONNES.

    Une rangée y entre si elle porte une fusion horizontale, ou si sa colonne la plus
    à gauche est vide. Les deux conditions sont nécessaires ensemble : la fusion
    seule manque la rangée d'en-tête FEUILLE, qui par construction n'en a pas ; la
    colonne gauche vide seule ferait passer pour une matrice tout tableau plat
    précédé d'une gouttière.

    Une rangée réduite à UNE cellule couvrant toute la largeur est un titre ou un
    séparateur, jamais un niveau de hiérarchie : elle est exclue."""
    if not leaves:
        return 0
    depth = 0
    for ri in range(len(frag.boxes)):
        row = [b for b in frag.boxes[ri] if b is not None]
        full_width = len(row) == 1 and _covers_several(row[0], leaves) == len(leaves)
        merged = (not full_width) and any(_covers_several(b, leaves) >= 2 for b in row)
        corner_empty = not _leaf_text(frag, ri, leaves[0])
        if merged or corner_empty:
            depth += 1
        else:
            break
    return depth if depth < len(frag.boxes) else 0


def _row_header_depth(frag: PhysicalTableFragment, leaves: List[Tuple[float, float]],
                      header_depth: int) -> int:
    """Nombre de colonnes feuilles servant d'ÉTIQUETTES de lignes.

    La hiérarchie des colonnes se matérialise par ses fusions : la première feuille
    qu'une cellule d'en-tête couvre AVEC une autre marque son bord gauche, et tout ce
    qui précède indexe les lignes. Ce test ne lit pas le coin, il mesure l'étendue
    des fusions — il vaut donc que le coin soit vide ou rempli.

    Sans aucune fusion (hiérarchie à un seul niveau), on retombe sur les feuilles de
    tête restées sans texte d'en-tête."""
    if header_depth == 0 or not leaves:
        return 0
    first_covered = None
    for ri in range(header_depth):
        for box in (frag.boxes[ri] if ri < len(frag.boxes) else []):
            if box is None or _covers_several(box, leaves) < 2:
                continue
            for idx, (a, b) in enumerate(leaves):
                if min(box[2], b) - max(box[0], a) > 0.5:
                    first_covered = idx if first_covered is None else min(first_covered, idx)
                    break
    if first_covered is not None:
        return min(first_covered, max(0, len(leaves) - 1))
    depth = 0
    for idx, leaf in enumerate(leaves):
        if any(_leaf_text(frag, ri, leaf) for ri in range(header_depth)):
            break
        depth = idx + 1
    return min(depth, max(0, len(leaves) - 1))


def _leaf_text(frag: PhysicalTableFragment, ri: int,
               leaf: Tuple[float, float]) -> str:
    a, b = leaf
    row_t = frag.cells[ri] if ri < len(frag.cells) else []
    row_b = frag.boxes[ri] if ri < len(frag.boxes) else []
    parts = [t for c, t in enumerate(row_t)
             if t and c < len(row_b) and row_b[c] is not None
             and min(row_b[c][2], b) - max(row_b[c][0], a) > 0.5]
    return " ".join(parts).strip()


def _hierarchical_header_rows(frag: PhysicalTableFragment) -> List[int]:
    """Rangées de tête d'une MATRICE : angle supérieur gauche vide, lignes nommées.

    Les deux conditions comptent. Sans la seconde, un tableau plat dont la première
    colonne physique n'est qu'un espacement — cas fréquent quand l'analyseur
    matérialise une gouttière — serait pris pour une matrice, et sa première
    colonne de données passerait pour une colonne d'étiquettes de lignes."""
    rows: List[int] = []
    for ri, row in enumerate(frag.cells):
        if (row and row[0]) or not any(c for c in row):
            break
        rows.append(ri)
    if not rows:
        return []
    # Une SEULE rangée de tête à première cellule vide ne fait pas une matrice :
    # c'est le motif ordinaire d'un tableau plat précédé d'une gouttière. Une
    # matrice empile au moins deux niveaux d'en-tête au-dessus de ses lignes nommées.
    if len(rows) < 2:
        return []
    labelled_below = any(row[0] for row in frag.cells[len(rows):] if row)
    return rows if labelled_below else []


def _looks_like_header_row(frag: PhysicalTableFragment,
                           intervals: List[Tuple[float, float]], ri: int) -> bool:
    """La rangée `ri` se distingue-t-elle STRUCTURELLEMENT du corps ?

    Signal générique : l'en-tête est textuel là où le corps est numérique. On exige
    donc au moins une colonne où la rangée candidate n'est pas numérique alors que
    la majorité du corps l'est. Aucun mot connu n'intervient."""
    if ri + 1 >= len(frag.cells):
        return False
    head = _assign_row(frag, ri, intervals)
    body = [_assign_row(frag, i, intervals) for i in range(ri + 1, len(frag.cells))]
    for pos in range(len(intervals)):
        head_val = head[pos] if pos < len(head) else ""
        col_vals = [b[pos] for b in body if pos < len(b) and b[pos]]
        if not col_vals:
            continue
        numeric_share = sum(1 for v in col_vals if _is_numericish(v)) / len(col_vals)
        if numeric_share >= 0.5 and not _is_numericish(head_val):
            return True
    return False


def _matrix_model(frag: PhysicalTableFragment, leaves: List[Tuple[float, float]],
                  h: int) -> Optional[HeaderModel]:
    """Modèle de matrice pour une profondeur d'en-tête donnée, ou ``None``."""
    if h <= 0 or h >= len(frag.boxes):
        return None
    v = _row_header_depth(frag, leaves, h)
    data_leaves = leaves[v:]
    if len(data_leaves) < 2:
        return None
    paths = [_leaf_path(frag, h, leaf) for leaf in data_leaves]
    return HeaderModel(header_rows=list(range(h)), intervals=data_leaves,
                       column_paths=paths, hierarchical=True,
                       row_header_intervals=leaves[:v])


def _leaf_path(frag: PhysicalTableFragment, h: int,
               leaf: Tuple[float, float]) -> List[str]:
    """Chemin d'une colonne feuille : les cellules d'en-tête qui la recouvrent.

    Une branche moins profonde garde sa profondeur : aucune étiquette n'est inventée
    pour égaliser les niveaux."""
    path: List[str] = []
    for ri in range(h):
        t = _leaf_text(frag, ri, leaf)
        if t and (not path or path[-1] != t):
            path.append(t)
    return path


def _build_header(frag: PhysicalTableFragment, *,
                  header_confirmed_by_repetition: bool) -> HeaderModel:
    leaves = _modal_intervals(frag)
    h = _column_header_depth(frag, leaves)
    if h > 0:
        model = _matrix_model(frag, leaves, h)
        # Deux colonnes feuilles ne peuvent pas porter le MÊME chemin : la relation
        # d'une valeur à sa colonne serait alors ambiguë. Si cela arrive, le bloc
        # d'en-tête s'est arrêté trop tôt — sa dernière rangée, celle qui distingue
        # les feuilles, a été prise pour une rangée de données. On l'annexe, et
        # seulement si les chemins deviennent uniques ; sinon la partition est
        # réellement sous-déterminée et l'on renonce à l'affirmer.
        if model is not None and not _paths_unique(model):
            model = _matrix_model(frag, leaves, h + 1)
            if model is not None and not _paths_unique(model):
                model = None
        if model is not None:
            return model

    if frag.cells:
        intervals = _column_intervals(frag, 0)
        if len(intervals) >= 2 and (header_confirmed_by_repetition
                                    or _looks_like_header_row(frag, intervals, 0)):
            paths = _paths_from_header_rows(frag, intervals, [0])
            header_rows = [0]
            # Même règle que la matrice (NATIVE-TABLE-GUARD-1, constat réel : deux en-têtes
            # identiques, la rangée suivante portant les feuilles qui les distinguent). Deux
            # colonnes au même chemin : les cellules, indexées par étiquette, s'écrasaient et une
            # valeur disparaissait en silence. On annexe la rangée suivante si elle est TEXTUELLE
            # et rend les chemins uniques ; sinon on renonce aux étiquettes (colonnes neutres)
            # plutôt que d'affirmer une relation valeur/colonne ambiguë.
            if not _unique_paths(paths):
                annexed = None
                if len(frag.cells) > 2 and not any(c and _is_numericish(c) for c in frag.cells[1]):
                    annexed = _paths_from_header_rows(frag, intervals, [0, 1])
                if annexed is not None and _unique_paths(annexed):
                    paths, header_rows = annexed, [0, 1]
                else:
                    paths = [[NEUTRAL_COLUMN.format(i + 1)] for i in range(len(intervals))]
                    header_rows = []
            return HeaderModel(header_rows=header_rows, intervals=intervals, column_paths=paths,
                               hierarchical=False)

    # Aucun en-tête textuel : étiquettes neutres, structure conservée.
    best: List[Tuple[float, float]] = []
    for ri in range(len(frag.cells)):
        iv = _column_intervals(frag, ri)
        if len(iv) > len(best):
            best = iv
    return HeaderModel(
        header_rows=[],
        intervals=best,
        column_paths=[[NEUTRAL_COLUMN.format(i + 1)] for i in range(len(best))],
        hierarchical=False,
    )


def _unique_paths(paths: List[List[str]]) -> bool:
    keys = [tuple(p) for p in paths]
    return len(set(keys)) == len(keys) and all(keys)


def _paths_unique(model: HeaderModel) -> bool:
    keys = [tuple(p) for p in model.column_paths]
    return len(set(keys)) == len(keys) and all(keys)


def _paths_from_header_rows(frag: PhysicalTableFragment,
                            intervals: List[Tuple[float, float]],
                            header_rows: List[int]) -> List[List[str]]:
    """Chemin de colonne = valeurs des rangées d'en-tête, fusions propagées.

    Une cellule fusionnée couvre plusieurs colonnes : sa valeur s'applique à toutes
    celles que sa largeur recouvre. La géométrie donne cette portée exactement, donc
    la propagation n'est pas une devinette."""
    paths: List[List[str]] = [[] for _ in intervals]
    for ri in header_rows:
        row_boxes = frag.boxes[ri] if ri < len(frag.boxes) else []
        row_text = frag.cells[ri] if ri < len(frag.cells) else []
        for c, text in enumerate(row_text):
            if not text:
                continue
            box = row_boxes[c] if len(row_boxes) > c else None
            if box is None:
                continue
            for pos, (a, b) in enumerate(intervals):
                if min(box[2], b) - max(box[0], a) > 0:
                    if not paths[pos] or paths[pos][-1] != text:
                        paths[pos].append(text)
    for pos in range(len(paths)):
        if not paths[pos]:
            paths[pos] = [NEUTRAL_COLUMN.format(pos + 1)]
    return paths


def _schema_from_paths(paths: List[List[str]]) -> List[str]:
    return [PATH_SEP.join(p) for p in paths]


# --------------------------------------------------------------------------- #
# Regroupement des fragments en tableaux logiques
# --------------------------------------------------------------------------- #
def _geometry_compatible(a: PhysicalTableFragment, b: PhysicalTableFragment) -> bool:
    """Les deux fragments occupent-ils la même gouttière et le même découpage ?

    On compare les bords EXTERNES et le nombre de colonnes SÉMANTIQUES, pas les
    bords physiques : un même tableau peut sortir en 4 colonnes physiques sur une
    page et en 6 sur la suivante, selon les cellules d'espacement que l'analyseur
    matérialise. Comparer les bords bruts ferait donc rompre la chaîne sur une
    variation qui n'a aucun sens sémantique."""
    width = max(a.bbox[2] - a.bbox[0], 1.0)
    tol = width * COLUMN_GEOMETRY_TOLERANCE
    if abs(a.bbox[0] - b.bbox[0]) > tol or abs(a.bbox[2] - b.bbox[2]) > tol:
        return False
    return _continuation_group_count(a) == _continuation_group_count(b)


def _group_count(frag: PhysicalTableFragment) -> int:
    """Nombre de colonnes sémantiques d'un fragment, d'après sa rangée de tête."""
    return len(_column_intervals(frag, 0))


def _continuation_group_count(frag: PhysicalTableFragment) -> int:
    """Idem, lu sur la rangée de référence (cf. `_reference_row`).

    Réservé à la CONTINUATION : comparaison géométrique de deux fragments, et tête d'une
    chaîne déjà prouvée par la géométrie. Un fragment isolé qui s'ouvre sur une rangée
    pleine largeur garde le rejet `single_semantic_column` (constat : sous-régions et
    texte pivoté seraient sinon structurés en colonnes neutres)."""
    return len(_column_intervals(frag, _reference_row(frag)))


def _reference_row(frag: PhysicalTableFragment) -> int:
    """Première rangée qui découpe réellement le fragment en colonnes.

    D'ordinaire la rangée 0. Mais un fragment de CONTINUATION peut s'ouvrir sur une
    rangée séparatrice (une seule cellule couvrant plusieurs colonnes de la grille
    majoritaire) : la lire comme la grille faisait voir une seule colonne et rejeter
    tout le corps (constat réel NATIVE-TABLE-GUARD-1R, `single_semantic_column`)."""
    modal = _modal_intervals(frag)
    if len(modal) < 2:
        return 0
    for ri in range(len(frag.cells)):
        if any(frag.cells[ri]) and not _is_separator_row(frag, ri, modal):
            return ri
    return 0


def _head_group_count(chain: List[PhysicalTableFragment]) -> int:
    """Colonnes de la tête d'une chaîne : rangée de référence si la chaîne est une
    continuation prouvée (plusieurs fragments), rangée 0 sinon (comportement inchangé)."""
    return _continuation_group_count(chain[0]) if len(chain) > 1 else _group_count(chain[0])


def _same_column_edges(a: PhysicalTableFragment, b: PhysicalTableFragment) -> bool:
    """Mêmes bords de colonnes de corps, au filet près (grille majoritaire)."""
    ia, ib = _modal_intervals(a), _modal_intervals(b)
    if len(ia) < 2 or len(ia) != len(ib):
        return False
    return all(abs(x0 - y0) <= RULING_TOLERANCE_PT and abs(x1 - y1) <= RULING_TOLERANCE_PT
               for (x0, x1), (y0, y1) in zip(ia, ib))


def _is_headerless(frag: PhysicalTableFragment) -> bool:
    """Le fragment ne porte AUCUNE rangée d'en-tête textuelle (corps seul)."""
    return not _build_header(frag, header_confirmed_by_repetition=False).header_rows


def _vertically_continuous(prev: PhysicalTableFragment, nxt: PhysicalTableFragment) -> bool:
    """Le tableau descend-il au bas d'une page pour reprendre en haut de la suivante ?

    C'est CE signal, et non l'égalité des en-têtes, qui autorise le chaînage. Deux
    tableaux indépendants à schéma identique s'arrêtent avant le bas de page ; un
    tableau paginé, non."""
    return (nxt.page_number == prev.page_number + 1
            and prev.reaches_page_bottom
            and nxt.top_ratio <= TOP_CONTINUITY_RATIO)


def _chain_fragments(frags: List[PhysicalTableFragment],
                     words: Optional[Dict[int, list]] = None,
                     gap_cut: Optional[float] = None) -> List[List[PhysicalTableFragment]]:
    """Chaînes de fragments appartenant au même tableau logique.

    Trois conditions sont toujours exigées : pages adjacentes, même signature
    d'en-tête normalisée, même gouttière. La quatrième — que le tableau descende au
    bas de sa page — établit qu'il a été COUPÉ plutôt que terminé ; mais un tableau
    peut être coupé par une mise en page qui ne le mène pas jusqu'en bas. Une reprise
    typographique en tête du fragment suivant est alors une preuve de coupure au
    moins aussi forte, et la remplace.

    L'ordre évite tout raisonnement circulaire : la calibration est faite sur le
    document entier AVANT le chaînage, et ne dépend d'aucune décision de chaînage."""
    chains: List[List[PhysicalTableFragment]] = []
    for frag in frags:
        placed = False
        # La chaîne candidate est la plus récente qui se termine sur la page PRÉCÉDENTE,
        # pas forcément la dernière ouverte : un habillage de page reconnu comme tableau
        # (cartouche répété en tête de chaque page) s'intercale sinon entre deux morceaux
        # du même corps et rompt la chaîne (constat réel NATIVE-TABLE-GUARD-1R).
        for chain in reversed(chains):
            prev = chain[-1]
            if prev.page_number != frag.page_number - 1 or not _geometry_compatible(prev, frag):
                continue
            if _same_header_signature(prev, frag):
                ok = (_vertically_continuous(prev, frag)
                      or _resumes_typographically(frag, words, gap_cut))
            else:
                # Continuation SANS en-tête répété (NATIVE-TABLE-RECOVERY-2) : seule la
                # géométrie peut la prouver — mêmes bords de colonnes au filet près, et
                # coupure en bas de page reprise en haut de la suivante. Un fragment qui
                # porte son propre en-tête ouvre toujours un nouveau tableau.
                ok = (_vertically_continuous(prev, frag)
                      and _same_column_edges(prev, frag)
                      and _is_headerless(frag))
            if ok:
                chain.append(frag)
                placed = True
                break
        if not placed:
            chains.append([frag])
    return chains


def _resumes_typographically(frag: PhysicalTableFragment,
                             words: Optional[Dict[int, list]],
                             gap_cut: Optional[float]) -> bool:
    """La première rangée de corps du fragment reprend-elle un paragraphe en cours ?"""
    if not words or gap_cut is None:
        return False
    intervals = _column_intervals(frag, 0)
    for ri in range(1, len(frag.boxes)):
        if _is_separator_row(frag, ri, intervals):
            continue
        g = row_gap(frag, words, ri)
        return g is not None and g <= gap_cut
    return False


def _first_row_signature(frag: PhysicalTableFragment) -> Tuple[str, ...]:
    if not frag.cells:
        return ()
    intervals = _column_intervals(frag, 0)
    return tuple(_norm_key(v) for v in _assign_row(frag, 0, intervals))


def _same_header_signature(a: PhysicalTableFragment, b: PhysicalTableFragment) -> bool:
    """Comparaison NORMALISÉE (espaces, casse, césures) — jamais octet à octet."""
    sa, sb = _first_row_signature(a), _first_row_signature(b)
    return bool(sa) and sa == sb


# --------------------------------------------------------------------------- #
# Bandeau d'en-tête réglé (NATIVE-TABLE-RECOVERY-2)
# --------------------------------------------------------------------------- #
_PURE_NUMBER = re.compile(r"^[+\-]?\d[\d\s.,]*%?$")
_HAS_LETTER = re.compile(r"[^\W\d_]")


def _segment_at(geom: PageGeometry, x: float) -> List[Tuple[float, float, float]]:
    return [seg for seg in geom.v_segments if abs(seg[0] - x) <= RULING_TOLERANCE_PT]


def _band_lines(words: Sequence[dict], box: Tuple[float, float, float, float]) -> List[str]:
    """Lignes de texte des mots CENTRÉS dans la boîte, dans l'ordre de lecture."""
    x0, top, x1, bot = box
    inside = [w for w in words
              if x0 <= (float(w["x0"]) + float(w["x1"])) / 2.0 <= x1
              and top <= (float(w["top"]) + float(w["bottom"])) / 2.0 <= bot]
    lines: List[Tuple[float, List[dict]]] = []
    for w in sorted(inside, key=lambda w: (float(w["top"]), float(w["x0"]))):
        if lines and abs(lines[-1][0] - float(w["top"])) <= 3.0:
            lines[-1][1].append(w)
        else:
            lines.append((float(w["top"]), [w]))
    return [" ".join(str(w["text"]) for w in sorted(ws, key=lambda w: float(w["x0"])))
            for _, ws in lines]


def _band_paths(words: Sequence[dict], intervals: List[Tuple[float, float]], inner: List[float],
                picks: Sequence[Tuple[float, float, float]], left: float, right: float,
                band_top: float, band_bot: float) -> Optional[List[List[str]]]:
    """Chemins de colonnes lus dans un bandeau réglé, ou ``None`` (étiquettes non démontrées).

    `picks[i]` : segment du filet intérieur `inner[i]` dans le bandeau. Un filet qui commence
    plus bas que le bandeau ne sépare que les niveaux inférieurs ; son départ marque la limite
    d'un niveau, et la cellule du niveau supérieur devient le PARENT des colonnes qu'elle couvre."""
    cuts = sorted({round(sg[1], 1) for sg in picks if sg[1] > band_top + RULING_TOLERANCE_PT})
    levels = [band_top] + cuts + [band_bot]
    paths: List[List[str]] = [[] for _ in intervals]
    for lv in range(len(levels) - 1):
        ltop, lbot = levels[lv], levels[lv + 1]
        present = [i for i, sg in enumerate(picks) if sg[1] <= ltop + RULING_TOLERANCE_PT]
        bounds = [left] + [inner[i] for i in present] + [right]
        for a, b in zip(bounds, bounds[1:]):
            lines = _band_lines(words, (a, ltop, b, lbot))
            if len(lines) > HEADER_BAND_MAX_LINES:
                return None
            text = normalize_cell(" ".join(lines))
            if text and (_is_numericish(text) or not _HAS_LETTER.search(text)
                         or any(_PURE_NUMBER.match(t) for t in text.split())):
                # Nombre isolé, ou cellule sans lettre (« - ») : rangée de données ou de
                # total, jamais une étiquette de colonne.
                return None
            for pos, (ia, ib) in enumerate(intervals):
                centre = (ia + ib) / 2.0
                if a <= centre <= b and text and (not paths[pos] or paths[pos][-1] != text):
                    paths[pos].append(text)
    if not all(paths) or not _unique_paths(paths):
        return None
    return paths


def _ruled_header_band(head: PhysicalTableFragment, intervals: List[Tuple[float, float]],
                       geometry: Dict[int, PageGeometry], words_for_page,
                       frags: Sequence[PhysicalTableFragment]) -> Optional[dict]:
    """En-tête d'un corps SANS rangée d'en-tête, lu dans un bandeau réglé de la même grille.

    Deux emplacements seulement, tous deux prouvés par la géométrie :

    - **même page** : le bandeau finit juste au-dessus du corps (≤ ``HEADER_BAND_MAX_GAP_PT``) ;
    - **page précédente** : le corps reprend en haut de page, et la grille de la page
      précédente — mêmes filets de colonnes — descend jusqu'en bas ; le bandeau est son
      sommet. C'est le cas d'un tableau paginé dont seule la première page est titrée.

    Conditions communes (sinon ``None`` : colonnes neutres, rien d'inventé) : un filet
    vertical à CHAQUE bord intérieur de colonne du corps, mêmes bords extérieurs, aucun
    autre tableau reconnu entre bandeau et corps, au plus ``HEADER_BAND_MAX_LINES`` lignes
    par niveau, chaque colonne nommée par un texte NON numérique, chemins tous distincts.
    Un niveau supérieur (filets intérieurs partiels) est conservé comme parent des
    colonnes qu'il recouvre : la hiérarchie n'est jamais aplatie."""
    if len(intervals) < 2:
        return None
    left, right = intervals[0][0], intervals[-1][1]
    inner = [(intervals[i][1] + intervals[i + 1][0]) / 2.0 for i in range(len(intervals) - 1)]
    head_top = head.bbox[1]

    def borders_cover(geom, top, bot) -> bool:
        return all(any(s[1] <= top + RULING_TOLERANCE_PT and s[2] >= bot - RULING_TOLERANCE_PT
                       for s in _segment_at(geom, x)) for x in (left, right))

    candidates = []
    geom = geometry.get(head.page_number)
    if geom is not None:
        picks = []
        for x in inner:
            segs = [sg for sg in _segment_at(geom, x)
                    if head_top - HEADER_BAND_MAX_GAP_PT <= sg[2] <= head_top + RULING_TOLERANCE_PT]
            picks.append(max(segs, key=lambda sg: sg[2]) if segs else None)
        if all(picks):
            candidates.append(("same_page", geom, picks))
    prev = geometry.get(head.page_number - 1)
    if prev is not None and head.top_ratio <= TOP_CONTINUITY_RATIO:
        picks = [min(_segment_at(prev, x), key=lambda sg: sg[1]) if _segment_at(prev, x) else None
                 for x in inner]
        if all(picks):
            candidates.append(("previous_page", prev, picks))

    for relation, g, picks in candidates:
        bottoms = [sg[2] for sg in picks]
        if max(bottoms) - min(bottoms) > RULING_TOLERANCE_PT:
            continue
        band_top, band_bot = min(sg[1] for sg in picks), min(bottoms)
        if band_bot - band_top <= 1.0 or not borders_cover(g, band_top, band_bot):
            continue
        if relation == "previous_page":
            # Le bandeau est le SOMMET d'une grille qui descend en bas de page.
            grid_bot = max(s[2] for s in _segment_at(g, left))
            if (not any(abs(s[1] - band_top) <= RULING_TOLERANCE_PT for s in _segment_at(g, left))
                    or grid_bot / g.height < MIN_BOTTOM_RATIO):
                continue
            between = (band_bot, grid_bot)
        else:
            between = (band_bot, head_top)
        # Aucun tableau reconnu ne s'intercale (un bandeau reconnu lui-même est admis).
        if any(f is not head and f.page_number == g.page_number
               and min(f.bbox[2], right) - max(f.bbox[0], left) > 0
               and f.bbox[1] >= band_bot - RULING_TOLERANCE_PT and f.bbox[1] < between[1] - RULING_TOLERANCE_PT
               for f in frags):
            continue
        paths = _band_paths(words_for_page(g.page_number) or [], intervals, inner, picks,
                            left, right, band_top, band_bot)
        if paths is None:
            continue
        return {"paths": paths,
                "provenance": {"kind": "ruled_header_band", "relation": relation,
                               "page": g.page_number,
                               "bbox": [round(left, 1), round(band_top, 1),
                                        round(right, 1), round(band_bot, 1)]},
                "band": (g.page_number, (left, band_top, right, band_bot))}
    return None


# --------------------------------------------------------------------------- #
# Lignes logiques
# --------------------------------------------------------------------------- #
def _is_separator_row(frag: PhysicalTableFragment, ri: int,
                      intervals: Optional[List[Tuple[float, float]]] = None) -> bool:
    """Rangée dont TOUT le contenu tient dans une cellule couvrant PLUSIEURS colonnes.

    Signal purement topologique : le moteur ne lit pas l'étiquette et ne cherche pas
    à savoir ce qu'elle désigne. Une rangée de données, même très éparse, répartit
    son contenu dans des colonnes distinctes ; une rangée éparse à une seule valeur
    garde une cellule d'une seule colonne et n'est donc pas un séparateur.

    Exiger une cellule unique couvrant TOUTE la largeur, comme le faisait TABLE-1,
    était trop strict : un séparateur réel est souvent redécoupé par l'analyseur en
    une cellule porteuse plus des cellules vides."""
    intervals = intervals or _column_intervals(frag, 0)
    if len(intervals) < 2:
        return False
    row_t = frag.cells[ri] if ri < len(frag.cells) else []
    row_b = frag.boxes[ri] if ri < len(frag.boxes) else []
    filled = [row_b[c] for c, t in enumerate(row_t)
              if t and c < len(row_b) and row_b[c] is not None]
    if len(filled) != 1:
        return False
    box = filled[0]
    covered = sum(1 for (a, b) in intervals if min(box[2], b) - max(box[0], a) > 0.5)
    return covered >= 2


def _row_cells(frag: PhysicalTableFragment, intervals: List[Tuple[float, float]],
               schema: List[str], ri: int,
               exclude: Optional[Tuple[float, float]] = None) -> Dict[str, str]:
    values = _assign_row(frag, ri, intervals, exclude)
    return {schema[pos]: v for pos, v in enumerate(values) if v and pos < len(schema)}


def _row_boxes(frag: PhysicalTableFragment, intervals: List[Tuple[float, float]],
               schema: List[str], ri: int,
               exclude: Optional[Tuple[float, float]] = None) -> Dict[str, Tuple[float, float, float, float]]:
    """Enveloppe des cellules physiques lues pour chaque colonne (provenance)."""
    acc: Dict[str, List[float]] = {}
    for pos, _text, box in _assign_positions(frag, ri, intervals, exclude):
        if pos >= len(schema):
            continue
        cur = acc.get(schema[pos])
        acc[schema[pos]] = (list(box) if cur is None else
                            [min(cur[0], box[0]), min(cur[1], box[1]), max(cur[2], box[2]), max(cur[3], box[3])])
    return {k: tuple(round(v, 1) for v in b) for k, b in acc.items()}


def _continuation_decision(*, is_first_body_row: bool, fragment_is_chained: bool,
                           anchor_empty: bool, anchor_cell_missing: bool,
                           row_gap_value: Optional[float], gap_cut: Optional[float],
                           open_row: Optional[LogicalRow],
                           cells: Dict[str, str]) -> bool:
    """La rangée poursuit-elle la ligne logique ouverte ?

    Trois situations STRUCTURELLES, et aucune ne se réduit à « la première cellule
    est vide » — règle interdite, parce qu'une ligne peut légitimement n'avoir aucun
    identifiant :

    A. **Débordement dans la page** : la rangée n'a PAS DE CELLULE dans la colonne
       d'ancrage. Une cellule vide existe et est bordée ; une cellule absente
       signifie que la rangée n'est pas découpée là, donc qu'elle prolonge la
       précédente.

    B. **Reprise après saut de page, ancrage vide** : fragment chaîné, première
       rangée de corps, ancrage vide, et recouvrement de colonnes avec la ligne
       ouverte.

    C. **Reprise après saut de page, ancrage PORTEUR** : même situation, mais la
       cellule d'ancrage contient la SUITE de son propre texte. Seule la typographie
       la distingue d'une ligne neuve : elle reprend sans espacement de paragraphe.
       C'est le cas que TABLE-1 ne savait pas traiter.
    """
    if not (open_row and cells):
        return False
    if anchor_cell_missing:
        return True
    if not (fragment_is_chained and is_first_body_row):
        return False
    typographic = (gap_cut is not None and row_gap_value is not None
                   and row_gap_value <= gap_cut)
    if anchor_empty:
        if gap_cut is not None and row_gap_value is not None:
            # La typographie prime sur le recouvrement de colonnes : une ligne
            # neuve dépourvue d'identifiant ouvre un paragraphe, et le
            # recouvrement seul la confondrait avec une reprise.
            return typographic
        previous_columns = {k for fr in open_row.fragments for k in fr.cells}
        return bool(previous_columns & set(cells))
    return typographic


def _logical_row_id(table_id: str, index: int) -> str:
    """Identité STRUCTURELLE : position dans le tableau logique. Jamais textuelle."""
    return f"{table_id}:r{index}"


def _table_id(source_digest: str, first_page: int, schema: List[str], occurrence: int) -> str:
    """Identité déterministe : source + première page + schéma + rang d'occurrence.

    Aucun UUID aléatoire, aucune clé de base : deux extractions des mêmes octets
    rendent le même identifiant, y compris après réindexation."""
    payload = "|".join([source_digest, str(first_page), "".join(schema), str(occurrence)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Construction d'un tableau logique
# --------------------------------------------------------------------------- #
def _build_logical_table(chain: List[PhysicalTableFragment], *, source_digest: str,
                         occurrence: int, limits: TableLimits,
                         words: Optional[Dict[int, list]] = None,
                         gap_cut: Optional[float] = None,
                         header_band: Optional[dict] = None) -> Optional[LogicalTable]:
    head = chain[0]
    confirmed = len(chain) > 1 and _same_header_signature(chain[0], chain[1])
    header = _build_header(head, header_confirmed_by_repetition=confirmed)
    if header_band is not None and not header.header_rows:
        # Corps sans en-tête : les étiquettes viennent du bandeau réglé de la même grille.
        header = HeaderModel(header_rows=[], intervals=header.intervals,
                             column_paths=[list(p) for p in header_band["paths"]], hierarchical=False)
    schema = _schema_from_paths(header.column_paths)
    intervals = header.intervals
    if len(intervals) < 2 or len(schema) != len(intervals):
        return None

    # Une grille d'UNE seule rangée sans en-tête n'est pas un tableau crédible :
    # c'est le motif typique d'un amas de traits ou d'un encadré décoratif. On ne
    # structure pas, et le texte de page reste donc intact.
    if not header.header_rows and len(head.cells) < 2:
        return None

    anchor_interval = header.row_header_interval or intervals[0]
    table_type = TABLE_TYPE_MATRIX if header.hierarchical else TABLE_TYPE_GENERIC
    table_id = _table_id(source_digest, head.page_number, schema, occurrence)
    table = LogicalTable(
        table_id=table_id,
        table_type=table_type,
        schema=schema,
        column_paths=header.column_paths,
        source_pages=sorted({f.page_number for f in chain}),
        header_source=(dict(header_band["provenance"]) if header_band is not None
                       and header.column_paths == header_band["paths"] else None),
    )

    open_row: Optional[LogicalRow] = None
    context_label: Optional[str] = None
    n_cells = 0

    for frag_index, frag in enumerate(chain):
        # Les groupes sont recalculés par fragment : un même tableau peut être
        # découpé différemment d'une page à l'autre. On ne retient le découpage
        # local que s'il donne le MÊME nombre de colonnes sémantiques.
        local = _column_intervals(frag, _reference_row(frag)) if frag_index else intervals
        frag_intervals = local if len(local) == len(intervals) else intervals
        header_rows = set(header.header_rows if frag_index == 0
                          else _repeated_header_rows(frag, frag_intervals, header))
        body_indices = [ri for ri in range(len(frag.cells)) if ri not in header_rows]
        seen_body = False

        for ri in body_indices:
            if len(table.rows) >= limits.max_rows_per_table or n_cells >= limits.max_cells_per_table:
                break
            if _is_separator_row(frag, ri, frag_intervals):
                label = next((c for c in frag.cells[ri] if c), "")
                context_label = label or context_label
                row = LogicalRow(
                    logical_row_id=_logical_row_id(table_id, len(table.rows)),
                    row_kind=ROW_KIND_SEPARATOR,
                    row_label=label or None,
                )
                box = next((b for t, b in zip(frag.cells[ri], frag.boxes[ri]) if t and b is not None), None)
                row.fragments.append(SourceFragment(
                    page_number=frag.page_number, physical_row_index=ri,
                    cells={schema[0]: label} if label else {},
                    cell_boxes={schema[0]: tuple(round(v, 1) for v in box)} if label and box else {}))
                table.rows.append(row)
                open_row = None
                seen_body = True
                continue

            cells = _row_cells(frag, frag_intervals, schema, ri,
                               header.row_header_interval)
            label_value = (_text_in_interval(frag, ri, anchor_interval)
                           if header.row_header_interval else cells.get(schema[0], ""))
            if not cells and not label_value:
                continue
            n_cells += len(cells)
            is_first_body_row = not seen_body
            seen_body = True

            if _continuation_decision(
                is_first_body_row=is_first_body_row,
                fragment_is_chained=frag_index > 0,
                anchor_empty=not label_value,
                anchor_cell_missing=not _has_cell_in_interval(frag, ri, anchor_interval),
                row_gap_value=(row_gap(frag, words, ri) if words else None),
                gap_cut=gap_cut,
                open_row=open_row,
                cells=cells,
            ):
                open_row.fragments.append(SourceFragment(
                    page_number=frag.page_number, physical_row_index=ri, cells=cells,
                    cell_boxes=_row_boxes(frag, frag_intervals, schema, ri, header.row_header_interval)))
                continue

            row = LogicalRow(
                logical_row_id=_logical_row_id(table_id, len(table.rows)),
                row_kind=ROW_KIND_DATA,
                row_label=label_value or None,
                context_label=context_label,
            )
            row.fragments.append(SourceFragment(
                page_number=frag.page_number, physical_row_index=ri, cells=cells,
                cell_boxes=_row_boxes(frag, frag_intervals, schema, ri, header.row_header_interval)))
            table.rows.append(row)
            open_row = row

    if not any(r.row_kind == ROW_KIND_DATA for r in table.rows):
        return None
    return table


def _repeated_header_rows(frag: PhysicalTableFragment,
                          intervals: List[Tuple[float, float]],
                          header: HeaderModel) -> List[int]:
    """Rangées d'en-tête RÉPÉTÉES en tête d'un fragment de continuation.

    Elles fournissent le schéma et le contexte mais ne sont JAMAIS des données."""
    if not header.header_rows:
        return []
    out: List[int] = []
    for ri in range(min(len(header.header_rows), len(frag.cells))):
        row_sig = tuple(_norm_key(v) for v in _assign_row(frag, ri, intervals))
        head_sig = tuple(_norm_key(p[min(ri, len(p) - 1)]) if p else ""
                         for p in header.column_paths)
        if row_sig == head_sig:
            out.append(ri)
    return out


# --------------------------------------------------------------------------- #
# API publique
# --------------------------------------------------------------------------- #
def extract_generic_tables(data: bytes, *, pages: Optional[Sequence[int]] = None,
                           limits: Optional[TableLimits] = None) -> TableExtractionReport:
    """Reconstruit les tableaux LOGIQUES d'un PDF à couche texte.

    Best-effort et sans exception : ``pdfplumber`` absent, PDF illisible ou
    structure douteuse rendent un rapport vide plutôt qu'une erreur — l'ingestion
    ordinaire ne doit jamais échouer à cause des tableaux."""
    report = TableExtractionReport()
    limits = limits or TableLimits()
    data = data or b""
    if not data:
        return report
    try:
        import pdfplumber
    except Exception:
        logger.info("pdfplumber indisponible : tableaux génériques ignorés (extra [tables]).")
        return report

    digest = hashlib.sha256(data).hexdigest()[:16]
    wanted = set(pages) if pages else None
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            geometry: Dict[int, PageGeometry] = {}
            frags = _collect_fragments(pdf, wanted, limits, report.outcomes, geometry)
            if not frags:
                # Aucune grille complète : seule une grille à colonnes RÉGLÉES (rangées non
                # tracées) peut encore être certifiée. Sans elle, aucun coût supplémentaire.
                grid_pages = [pn for pn, g in geometry.items() if _column_grids(g) or _segmented_column_grids(g)]
                if not grid_pages:
                    return report
                words = {pn: (pdf.pages[pn - 1].extract_words() or []) for pn in grid_pages}
                native = _NativeWords(data, pdf)
                _ruleless_grids(report, frags, geometry, lambda pn: native.words(pn) or words.get(pn),
                                digest=digest, limits=limits)
                for page_index, boxes in report.suppressed_regions.items():
                    if boxes:
                        _residual_text(report, page_index, boxes, words.get(page_index) or [],
                                       native.words(page_index))
                return report
            # Positions de glyphes lues UNE fois : elles servent à la calibration
            # typographique puis à la suppression des régions structurées.
            words = {p.page_number: (p.extract_words() or []) for p in pdf.pages}
            gap_cut = calibrate_row_gap_cut(document_row_gaps(frags, words))
            native = _NativeWords(data, pdf)
            _structure(report, frags, digest=digest, limits=limits,
                       words=words, gap_cut=gap_cut, geometry=geometry,
                       words_for_page=lambda pn: native.words(pn) or words.get(pn))
            _ruleless_grids(report, frags, geometry, lambda pn: native.words(pn) or words.get(pn),
                            digest=digest, limits=limits)
            # Texte résiduel calculé dans la MÊME passe : rouvrir le PDF pour cela
            # doublerait le coût d'analyse de mise en page, le poste le plus cher.
            for page_index, boxes in report.suppressed_regions.items():
                if not boxes:
                    continue
                _residual_text(report, page_index, boxes, words.get(page_index) or [],
                               native.words(page_index))
    except Exception as e:
        logger.warning("generic table extraction failed: %s", e.__class__.__name__)
        return TableExtractionReport()
    return report


# --------------------------------------------------------------------------- #
# Grille réglée sans filets horizontaux (NATIVE-TABLE-GROUNDING-3)
# --------------------------------------------------------------------------- #
def _column_grids(geom: PageGeometry) -> List[dict]:
    """Grilles de colonnes RÉGLÉES d'une page : au moins deux filets intérieurs aux MÊMES
    coupures verticales (mêmes segments, au filet près), encadrés par deux bords continus.

    Les coupures communes sont la signature d'une grille : un filet intérieur s'interrompt
    au même endroit sur toutes les colonnes (rangée de section pleine largeur, ou fin du
    bandeau d'en-tête). Aucun mot n'intervient."""
    by_x: Dict[float, List[Tuple[float, float]]] = {}
    for x, top, bot in geom.v_segments:
        if bot - top < 1.0:
            continue  # trait dégénéré (coin de rectangle, tic) : pas un filet
        by_x.setdefault(x, []).append((top, bot))

    def same(sa, sb) -> bool:
        # Mêmes coupures ; seul le DÉBUT du premier segment peut différer : un filet intérieur qui
        # ne sépare que le niveau inférieur d'un en-tête hiérarchique commence plus bas.
        return (len(sa) == len(sb) and abs(sa[0][1] - sb[0][1]) <= RULING_TOLERANCE_PT
                and all(abs(a0 - b0) <= RULING_TOLERANCE_PT and abs(a1 - b1) <= RULING_TOLERANCE_PT
                        for (a0, a1), (b0, b1) in zip(sa[1:], sb[1:])))

    xs = sorted(by_x)
    used, grids = set(), []
    for x in xs:
        if x in used:
            continue
        group = [y for y in xs if y not in used and same(by_x[y], by_x[x])]
        if len(group) < 2:
            continue
        segs = [(min(by_x[y][0][0] for y in group), by_x[x][0][1])] + by_x[x][1:]
        top, bot = segs[0][0], segs[-1][1]
        lefts = [y for y in xs if y < group[0] - RULING_TOLERANCE_PT
                 and any(a <= top + RULING_TOLERANCE_PT and b >= bot - RULING_TOLERANCE_PT for a, b in by_x[y])]
        rights = [y for y in xs if y > group[-1] + RULING_TOLERANCE_PT
                  and any(a <= top + RULING_TOLERANCE_PT and b >= bot - RULING_TOLERANCE_PT for a, b in by_x[y])]
        if not lefts or not rights:
            continue
        # Les bords LES PLUS PROCHES : un filet extérieur plus lointain encadrerait une autre grille.
        left, right = max(lefts), min(rights)
        if any(left + RULING_TOLERANCE_PT < y < right - RULING_TOLERANCE_PT and y not in group for y in xs
               if any(a < bot and b > top for a, b in by_x[y])):
            continue  # un filet étranger traverse la grille : découpage non démontré
        used.update(group)
        grids.append({"left": left, "right": right, "inner": group, "segments": segs, "top": top, "bottom": bot,
                      "first": {y: by_x[y][0] for y in group}})
    return grids


def _normalized_rules(geom: PageGeometry) -> List[Tuple[float, List[Tuple[float, float]]]]:
    """Filets verticaux NORMALISÉS : les bords distants d'au plus `SEGMENTED_EDGE_MERGE_PT` (les deux côtés d'un
    rectangle fin, ou un filet redessiné) forment UN filet — abscisse moyenne, segments réunis (jointure ≤ 1 pt).
    Un groupe ne s'étend jamais au-delà de cet écart depuis son premier bord : pas de chaînage."""
    by_x: Dict[float, List[Tuple[float, float]]] = {}
    for x, top, bot in geom.v_segments:
        if bot - top >= 1.0:
            by_x.setdefault(x, []).append((top, bot))
    clusters: List[List[float]] = []
    for x in sorted(by_x):
        if clusters and x - clusters[-1][0] <= SEGMENTED_EDGE_MERGE_PT:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    rules = []
    for cl in clusters:
        spans = sorted(sp for x in cl for sp in by_x[x])
        merged = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= merged[-1][1] + 1.0:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        rules.append((sum(cl) / len(cl), [(a, b) for a, b in merged]))
    return rules


def _covers(segments: Sequence[Tuple[float, float]], top: float, bot: float) -> bool:
    return any(a <= top + RULING_TOLERANCE_PT and b >= bot - RULING_TOLERANCE_PT for a, b in segments)


def _segmented_column_grids(geom: PageGeometry) -> List[dict]:
    """NATIVE-TABLE-GEOMETRY-12 — grilles à filets de colonnes SEGMENTÉS, non reconnues par `_column_grids`
    (filets intérieurs coupés à des hauteurs différentes par des rangées fusionnées, bords en rectangles fins).

    Preuves exigées, toutes géométriques (aucun mot, aucune valeur) :
    - **bandeau d'en-tête** : un segment commun (mêmes bornes, au filet près) à au moins deux filets intérieurs ;
      tout autre filet intérieur et les deux bords le couvrent entièrement ;
    - **bords** : les filets les plus proches à gauche et à droite qui couvrent le bandeau ;
    - **corps** : les intervalles sous le bandeau où TOUS les filets intérieurs ET les deux bords sont tracés ;
      chaque intervalle est un segment du corps, les ruptures communes séparent les sections ;
    - **aucun filet étranger** ne traverse la région entre les bords.
    La lecture des rangées (en-tête démontré, sections, ancrage, arrêts) reste celle de `_ruleless_grids`."""
    rules = _normalized_rules(geom)
    grids: List[dict] = []
    used = set()
    for i, (x, segs) in enumerate(rules):
        if i in used:
            continue
        band = segs[0]
        if band[1] - band[0] < 4.0:
            continue
        shared = [j for j, (_, sj) in enumerate(rules)
                  if abs(sj[0][0] - band[0]) <= RULING_TOLERANCE_PT and abs(sj[0][1] - band[1]) <= RULING_TOLERANCE_PT]
        if len(shared) < 2:
            continue
        lo, hi = rules[shared[0]][0], rules[shared[-1]][0]
        lefts = [j for j in range(len(rules)) if rules[j][0] < lo - RULING_TOLERANCE_PT and _covers(rules[j][1], *band)]
        rights = [j for j in range(len(rules)) if rules[j][0] > hi + RULING_TOLERANCE_PT and _covers(rules[j][1], *band)]
        if not lefts or not rights:
            continue
        li, ri = max(lefts), min(rights)
        left, right = rules[li][0], rules[ri][0]
        inner = [j for j in range(li + 1, ri) if _covers(rules[j][1], *band)]
        partial = [j for j in range(li + 1, ri) if j not in inner
                   and any(a < band[1] - RULING_TOLERANCE_PT and b > band[0] + RULING_TOLERANCE_PT for a, b in rules[j][1])]
        if not set(shared) <= set(inner) or partial:
            continue  # un filet entre les bords coupe le bandeau sans le couvrir : découpage non démontré
        # Corps : intersection des tracés de tous les filets intérieurs et des deux bords, sous le bandeau.
        members = [li] + inner + [ri]
        cuts = sorted({round(v, 2) for j in members for a, b in rules[j][1] for v in (a, b) if v > band[1] + RULING_TOLERANCE_PT})
        body: List[Tuple[float, float]] = []
        points = [band[1]] + cuts
        for a, b in zip(points, points[1:]):
            mid_top, mid_bot = a + 0.5, b - 0.5
            if mid_bot - mid_top < 4.0:
                continue
            if all(_covers(rules[j][1], mid_top, mid_bot) for j in members):
                if body and abs(body[-1][1] - a) <= 1.0:
                    body[-1] = (body[-1][0], b)
                else:
                    body.append((a, b))
        if not body:
            continue
        top, bottom = band[0], body[-1][1]
        if any(left + RULING_TOLERANCE_PT < rx < right - RULING_TOLERANCE_PT and k not in members
               and any(a < bottom and b > top for a, b in rsegs) for k, (rx, rsegs) in enumerate(rules)):
            continue  # filet étranger
        used.update(members)
        grids.append({"left": left, "right": right, "inner": [rules[j][0] for j in inner],
                      "segments": [band] + body, "top": top, "bottom": bottom,
                      "first": {rules[j][0]: (rules[j][1][0] if rules[j][1][0][0] <= band[0] + RULING_TOLERANCE_PT else band)
                                for j in inner},
                      "segmented": True})
    return grids


def _text_lines(words: Sequence[dict]) -> List[Tuple[float, List[dict]]]:
    """Lignes de texte par centre vertical (tolérance `LINE_TOLERANCE_PT`)."""
    lines: List[Tuple[float, List[dict]]] = []
    for w in sorted(words, key=lambda w: ((float(w["top"]) + float(w["bottom"])) / 2.0, float(w["x0"]))):
        cy = (float(w["top"]) + float(w["bottom"])) / 2.0
        if lines and abs(lines[-1][0] - cy) <= LINE_TOLERANCE_PT:
            lines[-1][1].append(w)
        else:
            lines.append((cy, [w]))
    return lines


def _column_index(word: dict, intervals: Sequence[Tuple[float, float]]) -> int:
    """Colonne du centre d'un mot (-1 hors des colonnes)."""
    cx = (float(word["x0"]) + float(word["x1"])) / 2.0
    return next((i for i, (a, b) in enumerate(intervals) if a <= cx <= b), -1)


def _ruleless_grids(report: TableExtractionReport, frags: Sequence[PhysicalTableFragment],
                    geometry: Dict[int, PageGeometry], words_for_page, *, digest: str,
                    limits: TableLimits) -> None:
    """Certifie les cellules d'une grille à colonnes RÉGLÉES dont les rangées ne sont pas tracées.

    Conditions (sinon rien n'est publié, le texte de page reste intact) :

    - colonnes : filets verticaux dessinés (`_column_grids`), région non couverte par un
      tableau déjà reconnu ;
    - en-tête : le premier segment de la grille est un bandeau d'étiquettes démontré
      (`_band_paths` : lettres, aucun nombre isolé, chemins distincts) — sans en-tête, refus ;
    - sections : les interruptions communes des filets intérieurs portent les libellés de
      groupe (contexte), jamais des données ;
    - rangées : une ligne d'ANCRAGE occupe au moins `ANCHOR_MIN_COLUMN_SHARE` des colonnes ;
      chaque autre ligne rejoint l'ancre la plus proche (égalité : refus) ; la rangée n'est
      certifiée que si CHAQUE cellule est centrée sur son ancre (`ROW_CENTERING_TOLERANCE_PT`),
      si aucun mot ne chevauche un filet et si elle porte au moins un nombre.

    Seules les rangées certifiées sont publiées et retirées du texte de page ; les autres y
    restent, avec le statut de la page (jamais levé ici)."""
    # NATIVE-TABLE-GEOMETRY-12 : un fragment REJETÉ (`single_semantic_column`) ne masque pas sa région ; la
    # certification qui suit reste entièrement indépendante et stricte.
    masking = [f for f in frags if id(f) not in report.rejected_fragment_ids]
    for page_number in sorted(geometry):
        geom = geometry[page_number]
        # Grilles historiques d'abord ; une grille segmentée n'est examinée que si aucune grille déjà CERTIFIÉE
        # ne recouvre sa région (jamais deux preuves concurrentes pour les mêmes rangées).
        certified_regions: List[Tuple[float, float, float, float]] = []
        for grid in _column_grids(geom) + _segmented_column_grids(geom):
            left, right, inner = grid["left"], grid["right"], grid["inner"]
            segmented = bool(grid.get("segmented"))
            if segmented and any(left < r and right > l and grid["top"] < b and grid["bottom"] > t
                                 for l, t, r, b in certified_regions):
                continue
            if any(f.page_number == page_number and f.bbox[0] < right and f.bbox[2] > left
                   and f.bbox[1] < grid["bottom"] and f.bbox[3] > grid["top"] for f in masking):
                continue  # région déjà traitée par l'analyseur (grille complète)
            segments = grid["segments"]
            if len(segments) < 2:
                continue
            words = [w for w in (words_for_page(page_number) or [])
                     if left <= (float(w["x0"]) + float(w["x1"])) / 2.0 <= right]
            xs = [left] + list(inner) + [right]
            intervals = list(zip(xs, xs[1:]))
            band_top, band_bot = segments[0]
            # Segment réel de chaque filet dans le bandeau : un départ plus bas = niveau inférieur.
            picks = [(x, grid["first"][x][0], grid["first"][x][1]) for x in inner]
            paths = _band_paths(words, intervals, list(inner), picks, left, right, band_top, band_bot)
            if paths is None:
                report.outcomes.append({"page": page_number, "outcome": OUTCOME_REJECTED,
                                        "reason": "ruleless_grid_header_not_proven"})
                continue
            schema = _schema_from_paths(paths)
            table = LogicalTable(
                table_id=_table_id(digest, page_number, schema, len(report.tables)),
                table_type=TABLE_TYPE_GENERIC, schema=schema, column_paths=paths,
                source_pages=[page_number],
                header_source={"kind": "ruled_header_band", "relation": "same_grid", "page": page_number,
                               "bbox": [round(left, 1), round(band_top, 1), round(right, 1), round(band_bot, 1)]},
                structure=STRUCTURE_SEGMENTED_RULED_COLUMNS if segmented else STRUCTURE_RULED_COLUMNS_ANCHOR_ROWS)
            refused, regions = 0, []
            context_label: Optional[str] = None
            body = segments[1:]
            prev_bot = band_bot
            stopped = False
            for seg_top, seg_bot in body:
                if stopped:
                    break
                # Interruption commune des filets avant ce segment : libellé de section.
                gap = [w for w in words if prev_bot < (float(w["top"]) + float(w["bottom"])) / 2.0 < seg_top]
                if segmented and gap and (len({_column_index(w, intervals) for w in gap}) > 1
                                          or any(re.search(r"\d", str(w["text"])) for w in gap)):
                    # Grille segmentée : une interruption qui porte du texte dans PLUSIEURS colonnes ou un
                    # nombre (rangée de total, nouvel en-tête, autre tableau) clôt la grille — on ne
                    # rattache jamais les rangées qui suivent aux en-têtes de celle-ci.
                    break
                if gap:
                    label = normalize_cell(" ".join(" ".join(str(w["text"]) for w in sorted(ws, key=lambda w: float(w["x0"])))
                                                    for _, ws in _text_lines(gap)))
                    if _HAS_LETTER.search(label):
                        context_label = label
                        table.rows.append(LogicalRow(logical_row_id=_logical_row_id(table.table_id, len(table.rows)),
                                                     row_kind=ROW_KIND_SEPARATOR, row_label=label))
                prev_bot = seg_bot
                inside = [w for w in words if seg_top <= (float(w["top"]) + float(w["bottom"])) / 2.0 <= seg_bot]
                if any(float(w["x0"]) < x - RULING_TOLERANCE_PT and float(w["x1"]) > x + RULING_TOLERANCE_PT
                       for w in inside for x in inner):
                    refused += 1  # une cellule chevauche un filet : colonnes non démontrées ici
                    continue
                lines = _text_lines(inside)
                if segmented:
                    # Ligne d'étiquettes sur plusieurs colonnes, sans aucun nombre, DANS un segment : nouvel
                    # en-tête possible (tableau suivant aux filets continus) — la grille s'arrête avant elle.
                    for li, (_cy, ws) in enumerate(lines):
                        if (len({_column_index(w, intervals) for w in ws}) > 1
                                and not any(re.search(r"\d", str(w["text"])) for w in ws)):
                            lines, stopped = lines[:li], True
                            break

                def column_of(w) -> int:
                    cx = (float(w["x0"]) + float(w["x1"])) / 2.0
                    return next(i for i, (a, b) in enumerate(intervals) if a <= cx <= b)

                occupied = [{column_of(w) for w in ws} for _, ws in lines]
                need = max(2, -(-len(intervals) * ANCHOR_MIN_COLUMN_SHARE // 1))
                anchors = [i for i, occ in enumerate(occupied) if len(occ) >= need]
                if not anchors:
                    continue
                members: Dict[int, List[int]] = {a: [] for a in anchors}
                ambiguous = set()
                for li, (cy, _ws) in enumerate(lines):
                    dists = sorted((abs(cy - lines[a][0]), a) for a in anchors)
                    if len(dists) > 1 and abs(dists[0][0] - dists[1][0]) < 0.5:
                        ambiguous.update({dists[0][1], dists[1][1]})
                    members[dists[0][1]].append(li)
                for a in anchors:
                    if len(table.rows) >= limits.max_rows_per_table:
                        break
                    anchor_y = lines[a][0]
                    cells: Dict[str, str] = {}
                    boxes: Dict[str, Tuple[float, float, float, float]] = {}
                    ok = a not in ambiguous
                    for col in range(len(intervals)):
                        cws = [(lines[li][0], w) for li in members[a] for w in lines[li][1] if column_of(w) == col]
                        if not cws:
                            continue
                        ys = sorted({round(cy, 1) for cy, _ in cws})
                        if abs((ys[0] + ys[-1]) / 2.0 - anchor_y) > ROW_CENTERING_TOLERANCE_PT:
                            ok = False  # cellule non centrée sur l'ancre : rangée non démontrée
                            break
                        text = normalize_cell(" ".join(
                            " ".join(str(w["text"]) for w in sorted((w for cy, w in cws if round(cy, 1) == y),
                                                                    key=lambda w: float(w["x0"]))) for y in ys))
                        cells[schema[col]] = text
                        boxes[schema[col]] = (round(min(float(w["x0"]) for _, w in cws), 1),
                                              round(min(float(w["top"]) for _, w in cws), 1),
                                              round(max(float(w["x1"]) for _, w in cws), 1),
                                              round(max(float(w["bottom"]) for _, w in cws), 1))
                    if not ok or not any(re.search(r"\d", v) for v in cells.values()):
                        refused += 1
                        continue
                    row = LogicalRow(logical_row_id=_logical_row_id(table.table_id, len(table.rows)),
                                     row_kind=ROW_KIND_DATA, row_label=cells.get(schema[0]) or None,
                                     context_label=context_label)
                    row.fragments.append(SourceFragment(page_number=page_number, physical_row_index=a,
                                                        cells=cells, cell_boxes=boxes))
                    table.rows.append(row)
                    regions.append((left, min(b[1] for b in boxes.values()), right, max(b[3] for b in boxes.values())))
            certified = sum(1 for r in table.rows if r.row_kind == ROW_KIND_DATA)
            if certified < RULELESS_MIN_ROWS:
                report.outcomes.append({"page": page_number, "outcome": OUTCOME_REJECTED,
                                        "reason": "ruleless_grid_rows_not_proven", "rows_refused": refused})
                continue
            report.tables.append(table)
            certified_regions.append((left, grid["top"], right, grid["bottom"]))
            report.outcomes.append({"page": page_number, "outcome": OUTCOME_STRUCTURED,
                                    "reason": "segmented_grid" if segmented else "ruleless_grid",
                                    "table_id": table.table_id, "rows_certified": certified, "rows_refused": refused})
            # Seules les rangées CERTIFIÉES quittent le texte de page.
            report.suppressed_regions.setdefault(page_number, []).extend(regions)


_TOKEN = re.compile(r"[^\W_]{3,}")


def _word_tokens(words: Sequence[dict]) -> set:
    return {t.casefold() for w in words for t in _TOKEN.findall(str(w.get("text") or ""))}


class _NativeWords:
    """Mots positionnés du moteur natif (PyMuPDF), lus à la demande, page par page.

    Référence de fidélité et source de repli : certaines polices (glyphes mis à
    l'échelle, chasse élargie) sont découpées lettre à lettre par ``pdfplumber``
    (« Q T E », « Id fich e ») alors que le moteur natif les lit correctement.
    Utilisés seulement si la page a le même repère dans les deux moteurs."""

    def __init__(self, data: bytes, pdf) -> None:
        self._data, self._pdf, self._doc, self._cache = data, pdf, None, {}

    def words(self, page_number: int) -> Optional[List[dict]]:
        if page_number in self._cache:
            return self._cache[page_number]
        out = None
        try:
            if self._doc is None:
                import fitz  # PyMuPDF, dépendance du moteur natif

                self._doc = fitz.open(stream=self._data, filetype="pdf")
            page = self._doc[page_number - 1]
            ref = self._pdf.pages[page_number - 1]
            if (int(page.rotation or 0) == 0 and int(getattr(ref, "rotation", 0) or 0) == 0
                    and abs(float(page.rect.width) - float(ref.width)) <= 1.0
                    and abs(float(page.rect.height) - float(ref.height)) <= 1.0
                    and abs(float(page.rect.x0)) <= 0.5 and abs(float(page.rect.y0)) <= 0.5):
                out = [{"x0": w[0], "top": w[1], "x1": w[2], "bottom": w[3], "text": w[4]}
                       for w in page.get_text("words")]
        except Exception:
            out = None
        self._cache[page_number] = out
        return out


def _residual_text(report: TableExtractionReport, page_index: int,
                   boxes: Sequence[Tuple[float, float, float, float]],
                   plumber_words: Sequence[dict], native_words: Optional[Sequence[dict]]) -> None:
    """Texte de page privé des régions structurées, depuis des mots FIDÈLES.

    Le texte résiduel REMPLACE le texte natif de la page : s'il est reconstruit depuis
    des mots mal découpés, tout le reste de la page devient introuvable (constat réel
    NATIVE-TABLE-GUARD-1R : une page entière perdue pour un cartouche structuré). On
    mesure donc la fidélité de ``pdfplumber`` aux mots du moteur natif ; en deçà de
    ``RESIDUAL_FIDELITY_MIN``, on reconstruit depuis les mots natifs. Sans référence
    native, comportement inchangé."""
    if native_words:
        ref = _word_tokens(native_words)
        recall = (len(ref & _word_tokens(plumber_words)) / len(ref)) if ref else 1.0
        if recall < RESIDUAL_FIDELITY_MIN:
            report.residual_page_text[page_index] = text_from_words(native_words, boxes)
            report.residual_engine[page_index] = "pymupdf"
            return
    report.residual_page_text[page_index] = text_from_words(plumber_words, boxes)
    report.residual_engine[page_index] = "pdfplumber"


def _structure(report: TableExtractionReport, frags: List[PhysicalTableFragment], *,
               digest: str, limits: TableLimits,
               words: Optional[Dict[int, list]] = None,
               gap_cut: Optional[float] = None,
               geometry: Optional[Dict[int, PageGeometry]] = None,
               words_for_page=None) -> None:
    occurrence = 0
    fallback = 0
    chains = _chain_fragments(frags, words, gap_cut)
    # Bandeaux d'en-tête : résolus AVANT la construction, car un bandeau que l'analyseur
    # a reconnu comme tableau autonome appartient au corps qu'il nomme — il ne doit pas
    # être publié seul (ni compté comme grille non structurée).
    bands: Dict[int, dict] = {}
    consumed: Dict[int, int] = {}
    if geometry:
        provider = words_for_page or (lambda pn: (words or {}).get(pn))
        for ci, chain in enumerate(chains):
            head = chain[0]
            if _head_group_count(chain) < 2 or not _is_headerless(head):
                continue
            band = _ruled_header_band(head, _build_header(head, header_confirmed_by_repetition=False).intervals,
                                      geometry, provider, frags)
            if band is None:
                continue
            bands[ci] = band
            bpage, (bx0, btop, bx1, bbot) = band["band"]
            for cj, other in enumerate(chains):
                if cj != ci and len(other) == 1 and other[0].page_number == bpage:
                    ox0, otop, ox1, obot = other[0].bbox
                    if (otop >= btop - RULING_TOLERANCE_PT and obot <= bbot + RULING_TOLERANCE_PT
                            and ox0 >= bx0 - RULING_TOLERANCE_PT and ox1 <= bx1 + RULING_TOLERANCE_PT):
                        consumed[cj] = ci
    built: Dict[int, LogicalTable] = {}
    for ci, chain in enumerate(chains):
        if ci in consumed:
            continue
        head = chain[0]
        if _head_group_count(chain) < 2:
            for f in chain:
                report.outcomes.append({"page": f.page_number, "outcome": OUTCOME_REJECTED,
                                        "reason": "single_semantic_column"})
                report.rejected_fragment_ids.add(id(f))
            continue
        if len(report.tables) >= limits.max_logical_tables:
            for f in chain:
                report.outcomes.append({"page": f.page_number, "outcome": OUTCOME_FALLBACK,
                                        "reason": "logical_table_limit"})
            fallback += 1
            continue

        table = _build_logical_table(chain, source_digest=digest, occurrence=occurrence,
                                     limits=limits, words=words, gap_cut=gap_cut,
                                     header_band=bands.get(ci))
        if table is None:
            for f in chain:
                report.outcomes.append({"page": f.page_number, "outcome": OUTCOME_FALLBACK,
                                        "reason": "not_structurable"})
            fallback += 1
            continue

        occurrence += 1
        report.tables.append(table)
        built[ci] = table
        for f in chain:
            report.outcomes.append({"page": f.page_number, "outcome": OUTCOME_STRUCTURED,
                                    "reason": "", "table_id": table.table_id})
            # Seule une région STRUCTURÉE devient retirable du texte de page.
            report.suppressed_regions.setdefault(f.page_number, []).append(f.bbox)

    for cj, ci in consumed.items():
        owner = built.get(ci)
        f = chains[cj][0]
        if owner is not None and owner.header_source is not None:
            # Le bandeau reconnu porte les étiquettes du corps : même tableau, même sort.
            report.outcomes.append({"page": f.page_number, "outcome": OUTCOME_STRUCTURED,
                                    "reason": "header_band", "table_id": owner.table_id})
            report.suppressed_regions.setdefault(f.page_number, []).append(f.bbox)
        else:
            report.outcomes.append({"page": f.page_number, "outcome": OUTCOME_FALLBACK,
                                    "reason": "not_structurable"})
            fallback += 1

    if fallback:
        report.warnings.append(WARNING_UNSTRUCTURED)


# --------------------------------------------------------------------------- #
# Sérialisation pour l'indexation
# --------------------------------------------------------------------------- #
def fragment_to_text(table: LogicalTable, row: LogicalRow, fragment: SourceFragment) -> str:
    """Texte indexé d'un fragment : chaque valeur porte son étiquette de colonne.

    Sans étiquette, un fragment de continuation n'est qu'un sac de phrases — c'est
    exactement ce que le lot précédent a constaté sur le texte de page aplati.

    Statuts de lecture (`cell_states`, vide pour le rail natif → texte inchangé) :
    illisible = marqueur explicite, jamais une absence ; approximatif et ambigu =
    valeur suffixée de son statut ; vide = toujours omis."""
    lines: List[str] = []
    if row.context_label:
        lines.append(f"Context: {row.context_label}")
    if row.row_label:
        lines.append(f"Row: {row.row_label}")
    states = fragment.cell_states
    for label in table.schema:
        state = states.get(label)
        if state == CELL_STATE_UNREADABLE:
            lines.append(f"{label}:\n{UNREADABLE_CELL_MARKER}")
            continue
        value = fragment.cells.get(label)
        if value:
            if state is not None:
                value = f"{value} ({state})"
            lines.append(f"{label}:\n{value}")
    return "\n\n".join(lines).strip()


def fragment_to_meta(table: LogicalTable, row: LogicalRow, fragment: SourceFragment) -> dict:
    """Métadonnées GÉNÉRIQUES : aucun champ métier, aucune valeur du corpus.

    ``cell_states`` n'apparaît que s'il est non vide (rail natif : clés inchangées) ;
    ``requires_visual_check`` passe à True pour une cellule illisible ou ambiguë,
    jamais pour une valeur seulement approximative (étiquetée dans le texte)."""
    states = fragment.cell_states
    meta = {
        "kind": "table",
        "table_type": table.table_type,
        "table_id": table.table_id,
        "logical_row_id": row.logical_row_id,
        "row_kind": row.row_kind,
        "row_label": row.row_label,
        "context_label": row.context_label,
        "column_schema": list(table.schema),
        "columns_present": sorted(fragment.cells),
        "source_page": fragment.page_number,
        "logical_row_pages": row.source_pages,
        "continuation": row.is_continued and fragment.page_number != row.source_pages[0],
        "table_pages": list(table.source_pages),
        "requires_visual_check": any(s in CELL_STATES_REQUIRING_CHECK for s in states.values()),
    }
    if states:
        meta["cell_states"] = dict(sorted(states.items()))
    if fragment.cell_boxes:
        # Provenance de chaque valeur : enveloppe de ses cellules physiques sur `source_page`.
        meta["cell_boxes"] = {k: list(v) for k, v in sorted(fragment.cell_boxes.items())}
    if table.header_source:
        meta["header_source"] = dict(table.header_source)
    if table.structure:
        meta["structure"] = table.structure
    return meta
