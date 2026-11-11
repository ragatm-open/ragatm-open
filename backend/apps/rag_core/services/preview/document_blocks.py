# backend/apps/rag_core/services/preview/document_blocks.py
from __future__ import annotations

"""Bornes des blocs DOCX dans le texte extrait, et projection d'un chunk dessus.

Le pipeline DOCX ne persiste **aucune** structure : `ExtractionResult.blocks` est
calculé à l'ingestion puis aplati par `join_blocks_text` en un seul `text`, sur
lequel le chunker découpe **aveuglément au caractère**. Ce qui survit en base est
`Chunk.meta.char_start` / `char_end` — des offsets dans ce texte.

Ce module rétablit le lien perdu, sans rien persister :

    ré-extraction (déterministe)  →  blocs
                                  →  bornes de chaque bloc dans `text`
                                  →  intersection avec [char_start, char_end)
                                  →  blocs cités + sous-plages EXACTES

La propriété qui rend l'opération sûre est que `join_blocks_text` est une fonction
pure et simple — `"\\n\\n".join(blocs non vides strippés)`. On ne la ré-implémente
pas « à peu près » : on la rejoue, puis on **vérifie** que la reconstruction est
identique au texte de l'extracteur. Une divergence ne peut donc pas être livrée
en silence, elle lève.
"""

from dataclasses import dataclass

from . import errors as E

#: Séparateur posé par `join_blocks_text` entre deux blocs non vides.
BLOCK_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class IndexedBlock:
    """Un bloc et ses bornes dans le texte extrait. Bornes en demi-ouvert."""

    #: Rang du bloc parmi les blocs RETENUS (les blocs vides sont écartés par
    #: `join_blocks_text` : leur rang d'origine ne correspondrait à rien dans le texte).
    index: int
    kind: str
    text: str
    start: int
    end: int
    level: int | None = None

    @property
    def id(self) -> str:
        """Identifiant de SESSION, dérivé du rang.

        Il n'est stable que pour un couple (contenu du fichier, version
        d'extracteur) donné. Il ne doit être ni persisté, ni conservé côté client
        d'une ouverture à l'autre : après réingestion, le même libellé pourrait
        désigner un autre bloc.
        """
        return f"b-{self.index:05d}"


@dataclass(frozen=True)
class BlockHighlight:
    """Sous-plage citée à l'intérieur d'un bloc, en coordonnées LOCALES au bloc."""

    start: int
    end: int


def index_blocks(blocks, *, expected_text: str) -> list[IndexedBlock]:
    """Calcule les bornes de chaque bloc, puis VÉRIFIE la reconstruction.

    `expected_text` est le `ExtractionResult.text` produit par le même extracteur.
    Si la reconstruction en diffère, c'est que la règle de jointure a changé — et
    tous les offsets persistés désignent alors autre chose que ce que l'on croit.
    On refuse plutôt que de surligner au mauvais endroit.
    """
    out: list[IndexedBlock] = []
    cursor = 0
    for block in blocks:
        text = (getattr(block, "text", "") or "").strip()
        if not text:
            # Écarté par `join_blocks_text` : ce bloc n'occupe aucun caractère.
            continue
        kind = getattr(getattr(block, "type", None), "value", None) or str(
            getattr(block, "type", "")
        )
        out.append(
            IndexedBlock(
                index=len(out),
                kind=kind,
                text=text,
                start=cursor,
                end=cursor + len(text),
                level=getattr(block, "level", None),
            )
        )
        cursor += len(text) + len(BLOCK_SEPARATOR)

    rebuilt = BLOCK_SEPARATOR.join(b.text for b in out).strip()
    if rebuilt != (expected_text or "").strip():
        raise E.PreviewUnsupported(
            E.ERR_PREVIEW_UNAVAILABLE,
            "la structure extraite ne correspond plus au texte indexé",
        )
    return out


def find_covering_blocks(
    indexed: list[IndexedBlock], *, char_start: int, char_end: int
) -> list[tuple[IndexedBlock, BlockHighlight]]:
    """Blocs recouvrant `[char_start, char_end)`, avec leur sous-plage citée.

    Les chunks sont découpés au caractère, sans égard pour les frontières de blocs :
    le premier et le dernier bloc d'une citation ne sont donc couverts que
    **partiellement**. Surligner ces blocs en entier étendrait la citation à du
    texte qu'elle ne porte pas — c'est pourquoi on renvoie des bornes locales
    plutôt qu'un simple drapeau.
    """
    out: list[tuple[IndexedBlock, BlockHighlight]] = []
    for block in indexed:
        if block.end <= char_start or block.start >= char_end:
            continue
        out.append(
            (
                block,
                BlockHighlight(
                    start=max(char_start, block.start) - block.start,
                    end=min(char_end, block.end) - block.start,
                ),
            )
        )
    return out


def validate_offsets(char_start, char_end, *, text_length: int) -> tuple[int, int]:
    """Contrôle les offsets d'un chunk. Ne les « répare » jamais.

    Un offset hors bornes signale que le texte réextrait n'est pas celui qui a
    servi à l'indexation. Le corriger silencieusement afficherait une zone
    plausible mais fausse.
    """
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
    if char_end > text_length:
        # Le chunker borne `char_end` à la longueur du texte : un dépassement
        # signifie que le document réextrait est PLUS COURT qu'à l'ingestion.
        raise E.PreviewStale(
            E.ERR_DOCUMENT_VERSION_MISMATCH,
            "le document a changé depuis l'indexation de cette source",
        )
    return char_start, char_end


def build_headings(indexed: list[IndexedBlock], *, limit: int) -> tuple[list[dict], bool]:
    """Sommaire, à partir des seuls blocs que l'extracteur a classés en titre.

    Aucune heuristique n'est ajoutée ici. La détection de titres du pipeline
    repose sur le préfixe du `styleId` (`heading` / `titre` / `title`) : elle
    manque les titres portés par `outlineLvl` et accepte des styles maison qui
    n'en sont pas. Améliorer cette détection changerait le texte extrait, donc
    les offsets de tous les chunks existants — c'est le lot 2, pas celui-ci.
    Le viewer se contente donc de dire ce qu'il sait, et de le dire honnêtement.
    """
    heads: list[dict] = []
    for block in indexed:
        if block.kind != "title":
            continue
        if len(heads) >= limit:
            return heads, True
        heads.append(
            {
                "id": f"h-{block.index:05d}",
                "block_index": block.index,
                # Niveau borné à l'affichage côté client ; la donnée reste brute.
                "level": block.level if isinstance(block.level, int) else 1,
                "text": block.text,
            }
        )
    return heads, False
