# backend/apps/rag_core/services/preview/locator.py
from __future__ import annotations

"""Localisation d'une source tabulaire — **`Chunk.meta` fait autorité**.

Le texte du snippet n'est JAMAIS analysé pour retrouver une feuille ou une plage.
La localisation est déjà persistée à l'ingestion (`excel_render._meta`) et projetée
jusqu'au client ; ce module ne fait que la lire, la valider, et refuser toute
incohérence.

Règle d'or, héritée de `stable_source_resolver` : **jamais de choix arbitraire**.
Pas de `first()`, pas de « feuille approchante », pas de plage « voisine ». Toute
contradiction entre ce que demande l'appelant et ce que dit le chunk est un refus
typé, pas un arbitrage silencieux.
"""

from dataclasses import dataclass, field
from typing import Any

# Algèbre des références de cellules — réutilisée telle quelle plutôt que
# réimplémentée. Le module est PUR (re + unicodedata + dataclasses), déjà couvert
# par des tests, et c'est la seule implémentation du dépôt : en écrire une seconde
# ferait diverger la lecture de plages entre l'aperçu et le reste du système.
from apps.rag_core.benchmark.stable_ground_truth import (
    StableGroundTruthError,
    col_to_num,
    normalize_cell_range,
    normalize_sheet_name,
    num_to_col,
    parse_cell_range,
)

from . import errors as E

#: `kind` des chunks issus d'un classeur (cf. `excel_render._meta`).
TABULAR_KINDS = ("excel_table", "excel_narrative")


@dataclass(frozen=True)
class CellRect:
    """Rectangle 1-based, bornes incluses."""

    col_start: int
    row_start: int
    col_end: int
    row_end: int

    @property
    def a1(self) -> str:
        return (
            f"{num_to_col(self.col_start)}{self.row_start}"
            f":{num_to_col(self.col_end)}{self.row_end}"
        )

    @property
    def cell_count(self) -> int:
        return (self.row_end - self.row_start + 1) * (self.col_end - self.col_start + 1)


@dataclass(frozen=True)
class SourceLocation:
    """Localisation structurée d'une citation tabulaire.

    `sheet_index` est **1-based** — c'est la convention de `SheetInfo.index`, donc
    celle qui vit déjà dans `Chunk.meta`. Elle est la clé CANONIQUE : deux feuilles
    nommées ``"Data"`` et ``"Data "`` sont distinctes dans Excel mais collisionnent
    après normalisation (NFC + trim externe). Un index ne collisionne jamais.
    Le nom sert à l'affichage et au CONTRÔLE de cohérence.
    """

    sheet_index: int
    sheet_name: str
    #: Plages citées, ordonnées. Plusieurs quand le chunk est découpé par colonnes :
    #: la zone couverte est alors NON CONTIGUË (identifiant + fenêtre).
    ranges: tuple[CellRect, ...]
    header_range: str | None = None
    headers: tuple[str, ...] = field(default_factory=tuple)
    sheet_state: str = "visible"

    @property
    def bounding_rect(self) -> CellRect:
        """Plus petit rectangle contenant toutes les plages citées.

        Sert à CADRER la fenêtre affichée, jamais à surligner : surligner ce
        rectangle marquerait, pour un chunk découpé par colonnes, toutes les
        colonnes intermédiaires — qui ne font pas partie de la citation.
        """
        return CellRect(
            col_start=min(r.col_start for r in self.ranges),
            row_start=min(r.row_start for r in self.ranges),
            col_end=max(r.col_end for r in self.ranges),
            row_end=max(r.row_end for r in self.ranges),
        )


def parse_rect(a1: str) -> CellRect:
    """`A749:D761` → rectangle. Lève `PreviewInvalidRequest` si mal formé."""
    try:
        c1, r1, c2, r2 = parse_cell_range(a1)
    except StableGroundTruthError as exc:
        raise E.PreviewInvalidRequest(
            E.ERR_INVALID_CELL_RANGE, f"plage invalide : {a1!r}"
        ) from exc
    return CellRect(col_start=c1, row_start=r1, col_end=c2, row_end=r2)


def _ranges_from_meta(meta: dict) -> tuple[CellRect, ...]:
    """Plages RÉELLEMENT couvertes par le chunk.

    `data_ranges` prime quand il existe : un chunk découpé par colonnes porte la
    colonne d'identification reportée **plus** une fenêtre de colonnes, et
    `cell_range` n'en décrit alors que la fenêtre. Surligner `cell_range` seul
    omettrait l'identifiant — une citation incomplète présentée comme complète.
    """
    raw = meta.get("data_ranges")
    candidates: list[str] = []
    if isinstance(raw, list) and raw:
        candidates = [str(x) for x in raw if isinstance(x, str) and x.strip()]
    if not candidates:
        one = meta.get("cell_range") or meta.get("data_range")
        if isinstance(one, str) and one.strip():
            candidates = [one]

    rects: list[CellRect] = []
    seen: set[str] = set()
    for a1 in candidates:
        try:
            norm = normalize_cell_range(a1)
        except StableGroundTruthError:
            # Une plage malformée parmi d'autres n'invalide pas la citation entière ;
            # elle est écartée. Si AUCUNE ne survit, l'appelant lève plus bas.
            continue
        if norm in seen:
            continue
        seen.add(norm)
        rects.append(parse_rect(norm))
    return tuple(rects)


def location_from_chunk_meta(meta: Any) -> SourceLocation:
    """Lit la localisation d'un `Chunk.meta` tabulaire. Ne devine jamais."""
    if not isinstance(meta, dict):
        raise E.PreviewInvalidRequest(
            E.ERR_CHUNK_LOCATION_INVALID, "métadonnées de chunk absentes"
        )

    kind = meta.get("kind")
    if not (isinstance(kind, str) and kind in TABULAR_KINDS):
        raise E.PreviewUnsupported(
            E.ERR_CHUNK_NOT_TABULAR,
            "cette source ne provient pas d'un classeur",
        )

    sheet_index = meta.get("sheet_index")
    if not isinstance(sheet_index, int) or isinstance(sheet_index, bool) or sheet_index < 1:
        # 1-based : `SheetInfo.index` commence à 1. Un 0 signalerait une méta
        # écrite par un autre pipeline — on refuse plutôt que de décaler d'un cran.
        raise E.PreviewInvalidRequest(
            E.ERR_CHUNK_LOCATION_INVALID,
            "index de feuille absent ou hors convention (1-based attendu)",
        )

    sheet_name = normalize_sheet_name(str(meta.get("sheet_name") or ""))
    if not sheet_name:
        raise E.PreviewInvalidRequest(
            E.ERR_CHUNK_LOCATION_INVALID, "nom de feuille absent"
        )

    ranges = _ranges_from_meta(meta)
    if not ranges:
        raise E.PreviewInvalidRequest(
            E.ERR_CHUNK_LOCATION_INVALID, "aucune plage de cellules exploitable"
        )

    headers = tuple(
        str(h) for h in (meta.get("headers") or []) if isinstance(h, str)
    )
    header_range = meta.get("header_range")

    return SourceLocation(
        sheet_index=sheet_index,
        sheet_name=sheet_name,
        ranges=ranges,
        header_range=header_range if isinstance(header_range, str) else None,
        headers=headers,
        sheet_state=str(meta.get("sheet_state") or "visible"),
    )


def assert_no_conflict(
    location: SourceLocation,
    *,
    requested_sheet_index: int | None,
    requested_sheet_name: str | None,
) -> None:
    """Refuse toute contradiction entre la demande et ce que dit le chunk.

    Arbitrer silencieusement — en préférant l'un ou l'autre — afficherait une
    feuille que l'appelant n'a pas demandée tout en prétendant montrer la source
    citée. Le refus est la seule réponse honnête.
    """
    if (
        requested_sheet_index is not None
        and requested_sheet_index != location.sheet_index
    ):
        raise E.PreviewInvalidRequest(
            E.ERR_SHEET_REFERENCE_CONFLICT,
            "l'index de feuille demandé contredit celui de la source citée",
        )
    if requested_sheet_name:
        if normalize_sheet_name(requested_sheet_name) != location.sheet_name:
            raise E.PreviewInvalidRequest(
                E.ERR_SHEET_REFERENCE_CONFLICT,
                "le nom de feuille demandé contredit celui de la source citée",
            )


def column_letters(start: int, end: int) -> list[str]:
    return [num_to_col(i) for i in range(start, end + 1)]


__all__ = [
    "CellRect",
    "SourceLocation",
    "TABULAR_KINDS",
    "assert_no_conflict",
    "col_to_num",
    "column_letters",
    "location_from_chunk_meta",
    "num_to_col",
    "parse_rect",
]
