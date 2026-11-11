# backend/apps/rag_core/services/ingest/extraction/pptx_render.py
from __future__ import annotations

"""
Rendu textuel déterministe d'une diapositive PowerPoint.

Module PUR : aucune dépendance à Django ni à python-pptx. Il ne manipule que des
structures simples produites par `pptx_extractor` (dataclasses ci-dessous), ce qui le
rend testable sans fabriquer un `.pptx`, exactement comme `excel_render` l'est sans
openpyxl.

Deux règles gouvernent tout ce module :

1. **Déterminisme.** Deux extractions du même fichier produisent le même texte, octet
   pour octet. L'ordre des formes dans le XML d'un `.pptx` est un ordre de *dessin*
   (z-order), pas un ordre de *lecture* : il dépend de l'ordre dans lequel l'auteur a
   cliqué. On le remplace par un ordre explicite (cf. `sort_key`), jamais par une
   heuristique probabiliste et jamais par un LLM.

2. **Aucune invention.** Une diapositive sans texte produit une diapositive sans texte,
   pas une description fabriquée. Le texte alternatif est rendu comme tel (« [Image : … ] »)
   pour qu'un lecteur — humain ou modèle — ne le confonde jamais avec le contenu publié.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Marge de tolérance verticale, en EMU (English Metric Units, l'unité de python-pptx).
# 1 cm = 360 000 EMU. Deux formes dont le sommet diffère de moins de ~0,5 cm sont
# considérées sur la MÊME ligne visuelle et départagées par leur abscisse : sans cela,
# deux colonnes côte à côte désalignées de 2 pixels s'entrelaceraient dans le texte.
SAME_ROW_TOLERANCE_EMU = 180_000


@dataclass
class TextRun:
    """Un paragraphe de texte porté par une forme."""

    text: str
    level: int = 0          # niveau d'indentation de puce (0 = premier niveau)
    is_bullet: bool = False


@dataclass
class ShapeText:
    """Contenu textuel normalisé d'une forme, indépendant de python-pptx."""

    kind: str                       # "title" | "subtitle" | "body" | "table" | "chart" | "alt_text"
    runs: List[TextRun] = field(default_factory=list)
    table: Optional[List[List[str]]] = None   # lignes x cellules, si kind == "table"
    alt_text: str = ""                        # si kind == "alt_text" ; légende si kind == "chart"
    top: int = 0                              # position visuelle (EMU)
    left: int = 0
    order: int = 0                            # rang d'apparition dans le XML (départage stable)


def sort_key(shape: ShapeText) -> Tuple[int, int, int, int]:
    """Ordre de lecture déterministe d'une diapositive.

    Trois niveaux, dans cet ordre :

    1. **Placeholders structurants d'abord** : le titre précède toujours le corps, quelle
       que soit sa position à l'écran (un titre peut être placé en bas d'une diapositive).
       C'est l'ordre sémantique, celui qu'attend un lecteur.
    2. **Puis l'ordre visuel approximatif** : `top` croissant, quantifié par bandes de
       `SAME_ROW_TOLERANCE_EMU` afin que deux formes visuellement alignées ne soient pas
       départagées par un écart d'un pixel, puis `left` croissant.
    3. **Enfin l'ordre d'apparition XML** : départage TOTAL. Deux formes exactement
       superposées gardent un ordre stable entre deux exécutions — c'est ce qui rend le
       tri déterministe plutôt que « seulement » reproductible.
    """
    rank = {"title": 0, "subtitle": 1}.get(shape.kind, 2)
    band = shape.top // SAME_ROW_TOLERANCE_EMU if shape.top else 0
    return (rank, band, shape.left, shape.order)


def render_bullets(runs: List[TextRun]) -> List[str]:
    """Rend des paragraphes en préservant puces et niveaux d'indentation.

    Un paragraphe non-puce est rendu tel quel ; une puce devient `- texte`, indentée de
    deux espaces par niveau. Les paragraphes vides sont écartés (une zone de texte
    PowerPoint en contient presque toujours, ce sont des retours de mise en page, pas
    du contenu).
    """
    out: List[str] = []
    for run in runs:
        text = (run.text or "").strip()
        if not text:
            continue
        if run.is_bullet:
            indent = "  " * max(0, min(run.level, 8))
            out.append(f"{indent}- {text}")
        else:
            out.append(text)
    return out


def _escape_cell(value: str) -> str:
    """Neutralise ce qui casserait la grille Markdown.

    Le pipe est échappé (sinon il ouvre une colonne fantôme) et les retours ligne
    internes deviennent des espaces (une cellule Markdown tient sur une ligne). On ne
    tronque pas : la borne de caractères est globale et gérée par l'extracteur.
    """
    return (value or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def render_table(rows: List[List[str]]) -> str:
    """Convertit un tableau en Markdown déterministe.

    La première ligne sert d'en-tête — c'est la convention PowerPoint (`firstRow`), et
    à défaut le choix le moins surprenant. Les lignes sont normalisées à la largeur
    maximale observée : une cellule fusionnée laisse des trous dans le XML, et une grille
    Markdown irrégulière ne se rend pas. Les cellules vides restent vides — jamais
    remplies par une valeur devinée.
    """
    if not rows:
        return ""

    width = max((len(r) for r in rows), default=0)
    if width == 0:
        return ""

    def norm(row: List[str]) -> List[str]:
        cells = [_escape_cell(c) for c in row]
        cells.extend([""] * (width - len(cells)))
        return cells[:width]

    header = norm(rows[0])
    # Un en-tête entièrement vide ne rend pas une grille lisible : on numérote les
    # colonnes plutôt que d'émettre `| | | |`, qui ne veut rien dire pour un lecteur.
    if not any(header):
        header = [f"Colonne {i + 1}" for i in range(width)]
        body_rows = rows
    else:
        body_rows = rows[1:]

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    for row in body_rows:
        lines.append("| " + " | ".join(norm(row)) + " |")
    return "\n".join(lines)


def render_slide(
    *,
    slide_number: int,
    title: Optional[str],
    shapes: List[ShapeText],
) -> str:
    """Assemble le texte d'une diapositive, dans l'ordre de lecture.

    Le titre est rendu UNE fois, en tête, préfixé « Titre : ». Il n'est pas répété dans
    le corps : la forme qui le porte est exclue par l'appelant.

    Le préfixe « [Diapositive N] » porte la provenance DANS le texte, et pas seulement
    en métadonnée — c'est ce qui la fait survivre à l'embedding et la rend citable par le
    modèle. Même principe que `Feuille : …` pour un classeur.
    """
    parts: List[str] = [f"[Diapositive {slide_number}]"]
    if title:
        parts.append(f"Titre : {title.strip()}")

    for shape in sorted(shapes, key=sort_key):
        if shape.kind == "table":
            table_md = render_table(shape.table or [])
            if table_md:
                parts.append("Tableau :\n" + table_md)
        elif shape.kind == "chart":
            table_md = render_table(shape.table or [])
            if table_md:
                parts.append(f"{shape.alt_text}\n{table_md}")
        elif shape.kind == "alt_text":
            alt = (shape.alt_text or "").strip()
            if alt:
                parts.append(f"[Image : {alt}]")
        else:
            lines = render_bullets(shape.runs)
            if lines:
                parts.append("\n".join(lines))

    return "\n\n".join(p for p in parts if p.strip())


def render_notes(*, slide_number: int, runs: List[TextRun]) -> str:
    """Rend les notes du présentateur d'une diapositive.

    Le libellé est explicite : une note ne doit jamais pouvoir être lue comme le contenu
    de la diapositive, ni dans le texte indexé, ni dans une citation.
    """
    lines = render_bullets(runs)
    if not lines:
        return ""
    return f"[Diapositive {slide_number} — notes du présentateur]\n\n" + "\n".join(lines)
