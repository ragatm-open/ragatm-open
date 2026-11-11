# backend/apps/rag_core/services/preview/presentation_blocks.py
from __future__ import annotations

"""Unités, bases d'offset et blocs d'une présentation — reconstruction exacte.

Deux pièges gouvernent tout ce module, et aucun n'est théorique.

**Piège 1 — le texte des offsets n'existe pas.** `_build_slide_specs` avance son
curseur de ``len(unit.text.strip()) + 1`` par unité, tandis que l'extracteur
assemble ``res.text`` avec ``"\\n\\n"``. Les offsets persistés vivent donc dans un
texte **virtuel** à séparateur d'UN caractère, qui n'est matérialisé nulle part.
Appliquer ``res.text[char_start:char_end]`` produit un décalage de +1 caractère
par unité déjà traitée — imperceptible sur la première diapositive, franc au
milieu d'un deck. `res.text` est donc **proscrit** de ce chemin, et un test le
verrouille.

**Piège 2 — l'ensemble des unités dépend de la configuration.** Les notes
produisent des unités intercalées entre les diapositives. Activer ou désactiver
`DOCUMENT_PPTX_INCLUDE_NOTES` décale toutes les bases suivantes (mesuré : +897
caractères sur quatre diapositives). Rejouer les bases avec un autre réglage
qu'à l'ingestion surligne au mauvais endroit **sans lever**. D'où la garde
`notes_configuration_mismatch`, en amont.
"""

from dataclasses import dataclass, field

from . import errors as E

#: Séparateur d'unités du texte VIRTUEL des offsets. Ce n'est PAS celui de
#: `res.text` ("\n\n") — voir le piège 1.
UNIT_SEPARATOR_LEN = 1

#: Séparateur des parties À L'INTÉRIEUR d'une unité (`render_slide`/`render_notes`).
BLOCK_SEPARATOR = "\n\n"

#: Préfixes posés par le rendu. Ils font partie du texte INDEXÉ : les retirer
#: décalerait les offsets et rendrait le surlignage faux.
_MARKER_PREFIX = "[Diapositive "
_TITLE_PREFIX = "Titre : "
_TABLE_PREFIX = "Tableau :"
_IMAGE_PREFIX = "[Image : "

KIND_SLIDE = "pptx_slide"
KIND_NOTES = "pptx_notes"


@dataclass(frozen=True)
class IndexedUnit:
    """Une unité de chunking (diapositive ou notes) et ses bornes virtuelles."""

    kind: str
    slide_number: int
    slide_title: str | None
    text: str
    start: int
    end: int
    has_images: bool | None = None
    requires_visual_check: bool | None = None


@dataclass(frozen=True)
class SlideBlock:
    """Une partie du texte d'une unité, avec ses bornes LOCALES à l'unité."""

    index: int
    type: str
    text: str
    start: int
    end: int
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def block_id(self, slide_number: int, kind: str) -> str:
        """Identifiant de SESSION, dérivé du rang.

        Stable pour un couple (contenu du fichier, version d'extracteur) donné,
        et pour lui seul. Ne jamais le persister ni le conserver côté client
        d'une ouverture à l'autre : après réingestion, le même libellé pourrait
        désigner un autre bloc.
        """
        prefix = "n" if kind == KIND_NOTES else "b"
        return f"s{slide_number}-{prefix}{self.index}"


@dataclass(frozen=True)
class BlockHighlight:
    start: int
    end: int


def index_units(units) -> list[IndexedUnit]:
    """Rejoue EXACTEMENT le curseur de `_build_slide_specs`.

    `units` est `ExtractionResult.slides` — la liste de dicts ``{text, meta}``
    produite par l'extracteur, notes comprises si elles ont été incluses.

    La règle ``base += len(text.strip()) + 1`` est recopiée d'`indexing.py` et
    ne doit pas en diverger : c'est elle qui définit ce que `char_start` désigne.
    """
    out: list[IndexedUnit] = []
    base = 0
    for unit in units or []:
        text = (unit.get("text") or "").strip()
        if not text:
            # `_build_slide_specs` saute les unités vides SANS avancer le curseur.
            continue
        meta = unit.get("meta") or {}
        out.append(
            IndexedUnit(
                kind=str(meta.get("kind") or ""),
                slide_number=int(meta.get("slide_number") or 0),
                slide_title=meta.get("slide_title"),
                text=text,
                start=base,
                end=base + len(text),
                has_images=meta.get("has_images"),
                requires_visual_check=meta.get("requires_visual_check"),
            )
        )
        base += len(text) + UNIT_SEPARATOR_LEN
    return out


def find_unit(units: list[IndexedUnit], *, kind: str, slide_number: int) -> IndexedUnit:
    """L'unité correspondant au chunk cité. Ne renvoie jamais « la plus proche »."""
    for unit in units:
        if unit.slide_number == slide_number and unit.kind == kind:
            return unit
    raise E.PreviewNotFound(
        E.ERR_SLIDE_NOT_FOUND,
        "la diapositive citée n'existe plus dans cette présentation",
    )


def split_blocks(unit: IndexedUnit) -> list[SlideBlock]:
    """Découpe le texte d'une unité en blocs, sur le séparateur du rendu.

    `render_slide` et `render_notes` assemblent leurs parties par ``"\\n\\n"`` et
    aucune partie n'en contient elle-même — vérifié sur le corpus de référence.
    Le découpage est donc **exact et réversible**, et `index_blocks` le contrôle.
    """
    blocks: list[SlideBlock] = []
    cursor = 0
    for i, raw in enumerate(unit.text.split(BLOCK_SEPARATOR)):
        kind, warnings = _classify(raw, unit_kind=unit.kind)
        blocks.append(
            SlideBlock(
                index=i,
                type=kind,
                text=raw,
                start=cursor,
                end=cursor + len(raw),
                warnings=warnings,
            )
        )
        cursor += len(raw) + len(BLOCK_SEPARATOR)

    rebuilt = BLOCK_SEPARATOR.join(b.text for b in blocks)
    if rebuilt != unit.text:
        # La règle d'assemblage du rendu a changé : les bornes ne désignent plus
        # ce qu'on croit. On refuse plutôt que de surligner de travers.
        raise E.PreviewUnsupported(
            E.ERR_PREVIEW_UNAVAILABLE,
            "la structure extraite ne correspond plus au texte indexé",
        )
    return blocks


def _classify(text: str, *, unit_kind: str) -> tuple[str, tuple[str, ...]]:
    """Type d'un bloc, déduit des préfixes que le rendu pose lui-même.

    Aucune heuristique de mise en forme : on ne reconnaît que ce que
    `pptx_render` a explicitement écrit.
    """
    if unit_kind == KIND_NOTES:
        # Une unité de notes est homogène : tout y est du commentaire d'orateur.
        return "speaker_notes", ()

    if text.startswith(_MARKER_PREFIX):
        # Marqueur de provenance injecté dans le texte INDEXÉ. Il est citable,
        # donc il doit rester visible et occuper ses caractères.
        return "paragraph", ("provenance_marker",)
    if text.startswith(_TITLE_PREFIX):
        return "title", ()
    if text.startswith(_TABLE_PREFIX):
        # Préformaté, jamais une grille : python-pptx RÉPÈTE la valeur d'une
        # cellule fusionnée sur toute son étendue, et la matrice de cellules
        # n'est nulle part persistée. Re-parser notre propre Markdown pour en
        # refaire un tableau serait un aller-retour fragile.
        return "table", ("table_simplified", "merged_cells_may_repeat")
    if text.startswith(_IMAGE_PREFIX):
        # Texte alternatif RÉELLEMENT indexé — ce n'est pas un placeholder
        # inventé : la chaîne existe dans le texte qui a produit le chunk.
        return "paragraph", ("alt_text",)
    if text.startswith("- ") or "\n- " in text:
        return "list", ("list_numbering_lost",)
    return "paragraph", ()


def validate_offsets(char_start, char_end, *, unit: IndexedUnit) -> tuple[int, int]:
    """Contrôle les offsets d'un chunk contre l'unité résolue. Ne répare jamais."""
    if not isinstance(char_start, int) or not isinstance(char_end, int):
        raise E.PreviewInvalidRequest(
            E.ERR_INVALID_CHUNK_OFFSETS, "offsets de chunk absents ou non entiers"
        )
    if isinstance(char_start, bool) or isinstance(char_end, bool):
        raise E.PreviewInvalidRequest(
            E.ERR_INVALID_CHUNK_OFFSETS, "offsets de chunk non entiers"
        )
    if char_start < 0 or char_end <= char_start:
        raise E.PreviewInvalidRequest(
            E.ERR_INVALID_CHUNK_OFFSETS, "bornes de chunk incohérentes"
        )
    if char_start < unit.start or char_end > unit.end:
        # Le chunk déborde de son unité : soit la présentation a changé, soit les
        # bases ont été rejouées avec une autre configuration (notes).
        raise E.PreviewStale(
            E.ERR_DOCUMENT_VERSION_MISMATCH,
            "la source citée ne correspond plus à cette diapositive",
        )
    return char_start, char_end


def project_highlight(
    blocks: list[SlideBlock], *, unit: IndexedUnit, char_start: int, char_end: int
) -> dict[int, BlockHighlight]:
    """Projette la plage citée sur les blocs, en coordonnées LOCALES au bloc.

    Les chunks sont découpés au caractère, sans égard pour les frontières de
    blocs : le premier et le dernier bloc d'une citation ne sont couverts que
    **partiellement**. Renvoyer un simple drapeau étendrait la citation à du
    texte qu'elle ne porte pas.
    """
    local_start = char_start - unit.start
    local_end = char_end - unit.start

    out: dict[int, BlockHighlight] = {}
    for block in blocks:
        if block.end <= local_start or block.start >= local_end:
            continue
        out[block.index] = BlockHighlight(
            start=max(local_start, block.start) - block.start,
            end=min(local_end, block.end) - block.start,
        )
    return out


__all__ = [
    "BLOCK_SEPARATOR",
    "BlockHighlight",
    "IndexedUnit",
    "KIND_NOTES",
    "KIND_SLIDE",
    "SlideBlock",
    "UNIT_SEPARATOR_LEN",
    "find_unit",
    "index_units",
    "project_highlight",
    "split_blocks",
    "validate_offsets",
]
