# backend/apps/rag_core/services/preview/service.py
from __future__ import annotations

"""Orchestration de l'aperçu tabulaire.

Ordre imposé, et non négociable :

    permissions (par l'appelant)
      → format supporté
      → résolution du chunk          (localisation = Chunk.meta, jamais le snippet)
      → cohérence de la demande      (contradiction = refus, jamais arbitrage)
      → lecture des octets           (chemin issu du FileField, jamais d'une entrée)
      → inventaire des feuilles      (politique d'INGESTION, pas wb.sheetnames)
      → fenêtre bornée
      → lecture normalisée           (excel_normalize rejoué à l'identique)
      → réponse structurée

La permission est vérifiée par la vue **avant** d'entrer ici, et donc avant toute
ouverture de fichier et avant toute lecture de cache — un cache consulté avant la
permission servirait à un utilisateur une réponse qu'il n'a pas le droit de calculer.
"""

import hashlib
from dataclasses import asdict
from typing import Any

from django.core.cache import cache

from apps.rag_core.services.ingest.extraction import config as ingest_config

from . import config as preview_config
from . import errors as E
from . import reader as R
from .locator import (
    CellRect,
    SourceLocation,
    assert_no_conflict,
    column_letters,
    location_from_chunk_meta,
    parse_rect,
)

#: Version du contrat de réponse. Incrémentée si la FORME change.
SCHEMA_VERSION = 1

#: Version de la normalisation de cellule. Entre dans la clé de cache : une
#: évolution de `excel_normalize` doit invalider les aperçus mis en cache, sinon
#: l'aperçu servirait une chaîne que l'ingestion ne produit plus.
NORMALIZATION_VERSION = 1

XLSX_SUFFIXES = (".xlsx",)

#: Ce que l'aperçu ne restitue pas. Annoncé dans CHAQUE réponse (invariant de
#: transparence) : un aperçu simplifié qui ne le dit pas laisse croire à une
#: fidélité visuelle qu'il n'a pas.
OMITTED_FEATURES = (
    "styles",
    "colors",
    "column_widths",
    "row_heights",
    "images",
    "charts",
    "pivot_tables",
    "conditional_formatting",
    "comments",
    "frozen_panes",
    "filters",
    "merged_cells",
    "print_layout",
)


def _is_xlsx(document) -> bool:
    name = (getattr(document, "original_filename", "") or "").lower()
    return name.endswith(XLSX_SUFFIXES)


def _read_document_bytes(document) -> bytes:
    """Octets du fichier source. Le chemin vient du `FileField`, jamais d'une entrée.

    Aucun `open(path)` sur une valeur issue de la base : ce serait un vecteur de
    traversée de chemin / lecture de fichier arbitraire (même doctrine que
    `DocumentDownloadView`).
    """
    storage_file = getattr(document, "storage_file", None)
    if not storage_file or not getattr(storage_file, "name", ""):
        raise E.PreviewNotFound(E.ERR_NO_FILE, "fichier source indisponible")
    try:
        with storage_file.open("rb") as fh:
            return fh.read()
    except E.PreviewError:
        raise
    except Exception as exc:
        raise E.PreviewNotFound(E.ERR_NO_FILE, "fichier source illisible") from exc


def resolve_chunk_location(*, document, chunk_id) -> SourceLocation:
    """Charge le chunk cité et en lit la localisation.

    Les trois vérifications qui suivent ne sont pas redondantes :
    l'appartenance au document empêche de projeter la citation d'un document sur
    un autre ; l'appartenance au RagSpace ferme le même trou au niveau du
    périmètre (`Chunk.rag_space` est dupliqué exprès pour cela) ; l'absence du
    chunk signifie une réindexation, et doit se dire — pas se rattraper.
    """
    from apps.rag_core.models.chunk import Chunk

    chunk = Chunk.objects.filter(id=chunk_id).only(
        "id", "document_id", "rag_space_id", "meta"
    ).first()
    if chunk is None:
        # Les chunks sont SUPPRIMÉS puis recréés à chaque réindexation : un id
        # inconnu signifie que la réponse cite une version qui n'existe plus.
        raise E.PreviewStale(
            E.ERR_CHUNK_STALE,
            "la source citée n'existe plus : le document a été réindexé",
        )

    if str(chunk.document_id) != str(document.id):
        raise E.PreviewNotFound(
            E.ERR_CHUNK_DOCUMENT_MISMATCH,
            "cette source n'appartient pas à ce document",
        )
    if str(chunk.rag_space_id) != str(document.rag_space_id):
        raise E.PreviewNotFound(
            E.ERR_CHUNK_DOCUMENT_MISMATCH,
            "cette source n'appartient pas à ce document",
        )

    return location_from_chunk_meta(chunk.meta)


def _uncached_formula_cells(document, *, sheet_name: str | None = None) -> frozenset[str]:
    """Cellules porteuses d'une formule SANS valeur en cache, pour cette feuille.

    Vient de `Document.meta.excel.uncached_formula_cells`, persisté à l'ingestion
    par un scan XML (`excel_workbook`). Les références y sont qualifiées par la
    feuille (``"Budget!C12"``) et la liste est **plafonnée** à l'ingestion : un
    classeur qui en compte davantage n'en verra qu'une partie signalée. C'est une
    perte de signalement, jamais une valeur fausse.

    Reconstruire l'information ici exigerait une seconde ouverture du classeur en
    `data_only=False` — soit doubler le coût de lecture d'un chemin d'affichage
    cliqué en boucle, pour une information déjà en base.
    """
    meta = getattr(document, "meta", None) or {}
    excel = meta.get("excel") if isinstance(meta, dict) else None
    refs = (excel or {}).get("uncached_formula_cells") if isinstance(excel, dict) else None
    if not isinstance(refs, list):
        return frozenset()

    out: set[str] = set()
    for raw in refs:
        if not isinstance(raw, str) or "!" not in raw:
            continue
        sheet, _, cell = raw.rpartition("!")
        if sheet_name is not None and sheet != sheet_name:
            continue
        if cell:
            out.add(cell.upper())
    return frozenset(out)


def _validate_context(value: int | None, *, default: int, maximum: int, label: str) -> int:
    if value is None:
        return default
    if value < 0:
        raise E.PreviewInvalidRequest(
            E.ERR_CONTEXT_LIMIT_EXCEEDED, f"{label} ne peut pas être négatif"
        )
    if value > maximum:
        raise E.PreviewInvalidRequest(
            E.ERR_CONTEXT_LIMIT_EXCEEDED,
            f"{label} dépasse la limite autorisée ({maximum})",
        )
    return value


def _cache_key(*, document, sheet_index: int, display: CellRect,
               highlight: tuple[CellRect, ...]) -> str:
    """Clé de cache.

    Elle inclut le **sha256 du fichier** : sans lui, un document remplacé servirait
    l'aperçu de l'ancienne version. Elle inclut la **version de normalisation** :
    sans elle, une évolution du rendu de cellule laisserait circuler des chaînes
    que l'ingestion ne produit plus. Elle n'inclut PAS l'utilisateur : la réponse
    ne dépend que du fichier, et la permission est vérifiée avant toute lecture du
    cache — deux utilisateurs autorisés doivent obtenir la même chose.
    """
    marks = "|".join(r.a1 for r in highlight)
    raw = (
        f"{document.id}|{getattr(document, 'sha256', '')}|{sheet_index}"
        f"|{display.a1}|{marks}|{SCHEMA_VERSION}|{NORMALIZATION_VERSION}"
        f"|{preview_config.max_cell_chars()}|{ingest_config.excel_max_cell_chars()}"
    )
    return "preview:tabular:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_preview(
    *,
    document,
    chunk_id: Any = None,
    sheet_index: int | None = None,
    sheet_name: str | None = None,
    cell_range: str | None = None,
    context_rows: int | None = None,
    context_columns: int | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """Construit l'aperçu. Lève un `PreviewError` typé sur tout refus."""
    if not _is_xlsx(document):
        raise E.PreviewUnsupported(
            E.ERR_UNSUPPORTED_FORMAT,
            "seuls les classeurs .xlsx sont prévisualisables",
        )

    if expected_sha256 and expected_sha256 != (getattr(document, "sha256", "") or ""):
        raise E.PreviewStale(
            E.ERR_DOCUMENT_VERSION_MISMATCH,
            "le document a changé depuis la création de cette réponse",
        )

    ctx_rows = _validate_context(
        context_rows,
        default=preview_config.default_context_rows(),
        maximum=preview_config.max_context_rows(),
        label="context_rows",
    )
    ctx_cols = _validate_context(
        context_columns,
        default=preview_config.default_context_columns(),
        maximum=preview_config.max_context_columns(),
        label="context_columns",
    )

    # --- Localisation : le chunk fait autorité -----------------------------
    location: SourceLocation | None = None
    if chunk_id:
        location = resolve_chunk_location(document=document, chunk_id=chunk_id)
        assert_no_conflict(
            location,
            requested_sheet_index=sheet_index,
            requested_sheet_name=sheet_name,
        )

    if location is not None:
        target_sheet_index = location.sheet_index
        highlight = location.ranges
        focus = location.bounding_rect
    else:
        # Navigation libre (changement d'onglet) : aucune citation à surligner.
        if sheet_index is None:
            raise E.PreviewInvalidRequest(
                E.ERR_CHUNK_LOCATION_INVALID,
                "indiquez un chunk_id ou un sheet_index",
            )
        target_sheet_index = sheet_index
        highlight = ()
        focus = parse_rect(cell_range) if cell_range else CellRect(1, 1, 1, 1)

    # Une plage explicite reste permise avec un chunk, mais elle ne peut que
    # RECADRER : elle ne remplace jamais le surlignage de la source citée.
    if cell_range and location is not None:
        focus = parse_rect(cell_range)

    # --- Lecture ------------------------------------------------------------
    data = _read_document_bytes(document)
    wb = R.load_workbook_bounded(data)
    try:
        sheets = R.list_sheets(wb)
        target = R.resolve_sheet(sheets, target_sheet_index)

        # Cohérence nom/feuille réelle : le chunk a été écrit sur une version du
        # fichier ; si le nom ne correspond plus, la position ne veut plus rien dire.
        if location is not None:
            from apps.rag_core.benchmark.stable_ground_truth import normalize_sheet_name

            if normalize_sheet_name(target.name) != location.sheet_name:
                raise E.PreviewStale(
                    E.ERR_DOCUMENT_VERSION_MISMATCH,
                    "la feuille citée ne correspond plus à ce classeur",
                )

        display = R.compute_display_rect(
            focus, context_rows=ctx_rows, context_columns=ctx_cols
        )
        display, truncated, truncation_reason = R.clamp_display_rect(display)

        header_row = None
        if location is not None and location.header_range:
            try:
                header_row = parse_rect(location.header_range).row_start
            except E.PreviewError:
                header_row = None

        result = R.read_window(
            wb,
            sheet_name=target.name,
            display=display,
            highlight=highlight,
            header_row=header_row,
            uncached_formula_cells=_uncached_formula_cells(
                document, sheet_name=target.name
            ),
        )
    finally:
        try:
            wb.close()
        except Exception:  # pragma: no cover - fermeture best-effort
            pass

    # Une fenêtre vide alors qu'on visait des cellules précises signifie que la
    # plage est hors de la feuille — un refus explicite, pas une grille vide.
    if not result.rows and (highlight or cell_range):
        raise E.PreviewInvalidRequest(
            E.ERR_RANGE_OUT_OF_BOUNDS,
            "la plage demandée est hors des dimensions de la feuille",
        )

    return _serialize(
        document=document,
        sheets=sheets,
        target=target,
        location=location,
        display=display,
        result=result,
        highlight=highlight,
        truncated=truncated,
        truncation_reason=truncation_reason,
    )


def _serialize(
    *, document, sheets, target, location, display, result, highlight,
    truncated: bool, truncation_reason: str | None,
) -> dict:
    letters = column_letters(display.col_start, display.col_end)
    header = list(result.header)
    if not header and location is not None and location.headers:
        # Repli : les en-têtes persistés dans le chunk. Ce sont EXACTEMENT ceux
        # qui ont servi à indexer — préférables à une reconstruction approximative.
        header = list(location.headers)

    columns = [
        {
            "index": display.col_start + i,
            "letter": letter,
            "header": header[i] if i < len(header) else "",
        }
        for i, letter in enumerate(letters)
    ]

    cited_sheet = location.sheet_index if location is not None else None

    warnings: list[str] = []
    doc_meta = getattr(document, "meta", None) or {}
    excel_meta = doc_meta.get("excel") if isinstance(doc_meta, dict) else None
    if isinstance(excel_meta, dict):
        if excel_meta.get("requires_recalculation"):
            warnings.append("workbook_requires_recalculation")
        if excel_meta.get("merged_ranges"):
            warnings.append("merged_cells_not_rendered")
        if excel_meta.get("external_links"):
            warnings.append("external_links_not_followed")

    return {
        "schema_version": SCHEMA_VERSION,
        "document": {
            "id": str(document.id),
            "filename": getattr(document, "filename", "") or "",
            "sha256": getattr(document, "sha256", "") or "",
        },
        "workbook": {
            "sheet_count": sum(1 for s in sheets if s.previewable),
            "sheets": [
                {
                    "index": s.index,
                    "name": s.name,
                    "state": s.state,
                    "has_citation": s.index == cited_sheet,
                }
                for s in sheets
                if s.previewable
            ],
        },
        "viewport": {
            "sheet_index": target.index,
            "sheet_name": target.name,
            "sheet_state": target.state,
            "requested_ranges": [r.a1 for r in highlight],
            "display_range": display.a1,
            "row_start": display.row_start,
            "row_end": display.row_end,
            "column_start": letters[0] if letters else "A",
            "column_end": letters[-1] if letters else "A",
        },
        "columns": columns,
        "rows": [
            {
                "row_number": row.row_number,
                "cells": [asdict(c) for c in row.cells],
            }
            for row in result.rows
        ],
        "fidelity": {
            "simplified": True,
            "omitted": list(OMITTED_FEATURES),
            "warnings": warnings,
        },
        "truncation": {
            "truncated": truncated,
            "reason": truncation_reason,
        },
    }


def build_preview_cached(**kwargs) -> dict:
    """Variante mémoïsée. **N'appeler qu'après vérification de la permission.**

    Le cache ne peut jamais servir à un utilisateur une réponse qu'il ne pourrait
    pas recalculer lui-même : la permission est contrôlée en amont, dans la vue, et
    la clé ne dépend que du fichier et de la fenêtre.

    ⚠️ **Le chunk est résolu AVANT toute lecture du cache.** Un `chunk_id` mis en
    clé sans être vérifié laisserait un aperçu survivre à la réindexation qui a
    supprimé le chunk : pendant toute la durée du TTL, une source périmée
    continuerait de s'afficher comme si elle était valide — exactement ce que le
    refus `chunk_stale` existe pour empêcher. La résolution est une requête sur
    clé primaire ; elle ne pèse rien face à la lecture du classeur, qui est le
    coût que le cache sert réellement à éviter.
    """
    document = kwargs["document"]
    chunk_id = kwargs.get("chunk_id")

    location = None
    if chunk_id:
        location = resolve_chunk_location(document=document, chunk_id=chunk_id)
        assert_no_conflict(
            location,
            requested_sheet_index=kwargs.get("sheet_index"),
            requested_sheet_name=kwargs.get("sheet_name"),
        )

    if not preview_config.cache_enabled():
        return build_preview(**kwargs)

    # La clé porte la localisation RÉSOLUE plutôt que l'identifiant du chunk :
    # deux chunks différents pointant la même zone partagent alors leur aperçu,
    # et un chunk disparu n'atteint jamais cette ligne.
    if location is not None:
        loc_key = f"{location.sheet_index}|{'+'.join(r.a1 for r in location.ranges)}"
    else:
        loc_key = f"{kwargs.get('sheet_index')}|{kwargs.get('cell_range') or ''}"

    key = (
        "preview:tabular:"
        + hashlib.sha256(
            f"{document.id}|{getattr(document, 'sha256', '')}|{loc_key}"
            f"|{kwargs.get('context_rows')}|{kwargs.get('context_columns')}"
            f"|{SCHEMA_VERSION}|{NORMALIZATION_VERSION}".encode("utf-8")
        ).hexdigest()
    )

    hit = cache.get(key)
    if hit is not None:
        return hit

    out = build_preview(**kwargs)
    try:
        cache.set(key, out, preview_config.cache_ttl_s())
    except Exception:  # pragma: no cover - un cache indisponible n'invalide rien
        pass
    return out
