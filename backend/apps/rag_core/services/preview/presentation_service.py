# backend/apps/rag_core/services/preview/presentation_service.py
from __future__ import annotations

"""Aperçu de présentation (PPTX) — orchestration.

Ordre imposé, et non négociable :

    permissions (par la vue)
      → format supporté
      → résolution du chunk        (document ET RagSpace vérifiés)
      → versions d'extracteur/chunking
      → garde notes_included       ← AVANT toute lecture de fichier
      → lecture du cache
      → ré-extraction canonique    (le MÊME extracteur que l'ingestion)
      → rejeu des bases virtuelles (+1 par unité, JAMAIS res.text)
      → résolution de la diapositive
      → projection des offsets     → blocs + sous-plages exactes
      → fenêtre bornée
      → réponse structurée

Rien de structurel n'est persisté : ce qui survit en base, ce sont
`slide_number` et deux offsets. La reconstruction est sûre parce que
l'extraction est déterministe et que la règle de curseur du chunking est
rejouée à l'identique — à condition que l'ensemble des unités soit le même
qu'à l'ingestion, ce que la garde `notes_included` garantit.
"""

import hashlib
import time
from typing import Any

from django.core.cache import cache

from apps.rag_core.services.ingest.extraction import config as ingest_config
from apps.rag_core.services.ingest.extraction.base import (
    ExtractionContext,
    ExtractionError,
    ExtractionLimitExceeded,
    RejectedFile,
)
from apps.rag_core.services.ingest.extraction.pptx_extractor import (
    CHUNKING_VERSION,
    EXTRACTOR_VERSION,
    PptxExtractor,
)

from . import config as preview_config
from . import errors as E
from .presentation_blocks import (
    KIND_NOTES,
    KIND_SLIDE,
    find_unit,
    index_units,
    project_highlight,
    split_blocks,
    validate_offsets,
)

#: Version du contrat de réponse. Incrémentée si la FORME change.
SCHEMA_VERSION = 1

PPTX_SUFFIXES = (".pptx",)

#: Ce que l'aperçu ne restitue pas. Annoncé dans CHAQUE réponse.
OMITTED = (
    "exact_layout",
    "positions",
    "colors",
    "fonts",
    "themes",
    "images",
    "charts",
    "smartart",
    "animations",
    "transitions",
    "videos",
    "ole_objects",
)

#: Limitations PERMANENTES du pipeline actuel — vraies pour toute présentation.
#:
#: `charts_not_extracted` et `smartart_not_extracted` ne sont pas décoratifs :
#: un `GraphicFrame` ne porte ni `text_frame` ni table, donc son texte est
#: entièrement perdu à l'extraction — et **sans warning**. Une diapositive
#: composée d'un seul SmartArt paraîtrait vide. Le dire est le seul moyen
#: honnête de ne pas laisser croire que la diapositive ne contenait rien.
PERMANENT_WARNINGS = (
    "layout_not_rendered",
    "images_not_rendered",
    "charts_not_extracted",
    "smartart_not_extracted",
    "hidden_slides_not_detected",
    "list_numbering_lost",
    "tables_simplified",
    "animations_not_rendered",
    "transitions_not_rendered",
)


def _is_pptx(document) -> bool:
    name = (getattr(document, "original_filename", "") or "").lower()
    return name.endswith(PPTX_SUFFIXES)


def _read_document_bytes(document) -> bytes:
    """Octets du fichier source. Le chemin vient du `FileField`, jamais d'une entrée."""
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


def _pptx_meta(document) -> dict:
    meta = getattr(document, "meta", None) or {}
    extraction = meta.get("extraction") if isinstance(meta, dict) else None
    if not isinstance(extraction, dict):
        raise E.PreviewStale(
            E.ERR_PREVIEW_UNAVAILABLE,
            "aucune métadonnée d'extraction pour cette présentation",
        )
    return extraction


def resolve_chunk(*, document, chunk_id):
    """Charge le chunk cité et rend sa localisation. Ne devine rien."""
    from apps.rag_core.models.chunk import Chunk

    chunk = Chunk.objects.filter(id=chunk_id).only(
        "id", "document_id", "rag_space_id", "meta", "text"
    ).first()
    if chunk is None:
        raise E.PreviewStale(
            E.ERR_CHUNK_STALE,
            "la source citée n'existe plus : le document a été réindexé",
        )
    if str(chunk.document_id) != str(document.id):
        raise E.PreviewNotFound(
            E.ERR_CHUNK_DOCUMENT_MISMATCH, "cette source n'appartient pas à ce document"
        )
    if str(chunk.rag_space_id) != str(document.rag_space_id):
        raise E.PreviewNotFound(
            E.ERR_CHUNK_DOCUMENT_MISMATCH, "cette source n'appartient pas à ce document"
        )

    meta = chunk.meta if isinstance(chunk.meta, dict) else {}
    kind = meta.get("kind")
    if kind not in (KIND_SLIDE, KIND_NOTES):
        raise E.PreviewUnsupported(
            E.ERR_SLIDE_NOT_PREVIEWABLE,
            "cette source ne provient pas d'une présentation",
        )

    slide_number = meta.get("slide_number")
    if (
        not isinstance(slide_number, int)
        or isinstance(slide_number, bool)
        or slide_number < 1
    ):
        # La convention du dépôt est 1-based (`enumerate(prs.slides, start=1)`).
        raise E.PreviewInvalidRequest(
            E.ERR_INVALID_CHUNK_OFFSETS,
            "numéro de diapositive absent ou hors convention (1-based attendu)",
        )

    return chunk, kind, slide_number, meta.get("char_start"), meta.get("char_end")


def assert_versions(document) -> dict:
    """Refuse une extraction produite par une autre version du pipeline.

    Les chunks ne portent que des OFFSETS : toute évolution changeant le texte
    ou la règle de découpe les déplace silencieusement. Contrairement au DOCX,
    le PPTX **versionne déjà** son extracteur et son chunking — on s'en sert.
    """
    extraction = _pptx_meta(document)

    for key, current in (
        ("extractor_version", EXTRACTOR_VERSION),
        ("chunking_version", CHUNKING_VERSION),
    ):
        persisted = extraction.get(key)
        if persisted is not None and str(persisted) != str(current):
            raise E.PreviewStale(
                E.ERR_DOCUMENT_VERSION_MISMATCH,
                "cette présentation a été indexée par une autre version du pipeline",
            )
    return extraction


def assert_notes_configuration(document) -> bool:
    """Refuse si le réglage des notes diffère de celui de l'ingestion.

    Les notes produisent des unités **intercalées** entre les diapositives. Les
    inclure ou non change donc l'ensemble des unités, et par conséquent toutes
    les bases d'offset suivantes — mesuré : +897 caractères sur quatre
    diapositives. Rejouer les bases avec l'autre réglage surlignerait au mauvais
    endroit **sans lever**.

    Aucune compensation n'est tentée : on ne peut pas deviner la longueur de
    notes qui n'ont pas été extraites. Le refus est la seule réponse honnête.
    """
    extraction = _pptx_meta(document)
    pptx = extraction.get("pptx") if isinstance(extraction.get("pptx"), dict) else {}
    persisted = pptx.get("notes_included")
    current = ingest_config.pptx_include_notes()

    if persisted is not None and bool(persisted) != bool(current):
        raise E.PreviewStale(
            E.ERR_NOTES_CONFIGURATION_MISMATCH,
            "le réglage des notes du présentateur a changé depuis l'indexation "
            "de cette source",
        )
    return bool(current)


def extract_presentation(document, data: bytes):
    """Ré-extrait avec **exactement** l'extracteur de l'ingestion."""
    try:
        return PptxExtractor().extract(
            data,
            ExtractionContext(
                filename=getattr(document, "filename", "") or "presentation.pptx"
            ),
        )
    except RejectedFile as exc:
        code = getattr(exc, "code", "") or ""
        if "encrypted" in code:
            raise E.PreviewUnsupported(
                E.ERR_ENCRYPTED_PRESENTATION, "présentation protégée par chiffrement"
            ) from exc
        raise E.PreviewUnsupported(
            E.ERR_PRESENTATION_UNREADABLE,
            "présentation refusée par les gardes de sécurité",
        ) from exc
    except ExtractionLimitExceeded as exc:
        raise E.PreviewLimitExceeded(
            E.ERR_PRESENTATION_TOO_LARGE,
            "cette présentation dépasse les limites de l'aperçu",
        ) from exc
    except ExtractionError as exc:
        raise E.PreviewUnsupported(
            E.ERR_PRESENTATION_UNREADABLE, "présentation illisible ou endommagée"
        ) from exc
    except Exception as exc:
        raise E.PreviewUnsupported(
            E.ERR_PRESENTATION_UNREADABLE, "présentation illisible ou endommagée"
        ) from exc


def _validate_context(value: int | None, *, default: int, maximum: int, label: str) -> int:
    if value is None:
        return default
    if value < 0:
        raise E.PreviewInvalidRequest(
            E.ERR_CONTEXT_LIMIT_EXCEEDED, f"{label} ne peut pas être négatif"
        )
    if value > maximum:
        raise E.PreviewInvalidRequest(
            E.ERR_CONTEXT_LIMIT_EXCEEDED, f"{label} dépasse la limite ({maximum})"
        )
    return value


def build_presentation_preview(
    *,
    document,
    chunk_id: Any = None,
    slide_number: int | None = None,
    context_before: int | None = None,
    context_after: int | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """Construit l'aperçu. Lève un `PreviewError` typé sur tout refus."""
    started = time.perf_counter()

    if not _is_pptx(document):
        raise E.PreviewUnsupported(
            E.ERR_UNSUPPORTED_FORMAT, "seules les présentations .pptx sont prévisualisables"
        )
    if expected_sha256 and expected_sha256 != (getattr(document, "sha256", "") or ""):
        raise E.PreviewStale(
            E.ERR_DOCUMENT_VERSION_MISMATCH,
            "la présentation a changé depuis la création de cette réponse",
        )

    before = _validate_context(
        context_before,
        default=preview_config.pptx_context_before(),
        maximum=preview_config.pptx_max_context_before(),
        label="context_before",
    )
    after = _validate_context(
        context_after,
        default=preview_config.pptx_context_after(),
        maximum=preview_config.pptx_max_context_after(),
        label="context_after",
    )

    # --- Chunk : autorité de localisation, résolu AVANT tout le reste --------
    chunk = source_kind = source_slide = char_start = char_end = None
    if chunk_id:
        chunk, source_kind, source_slide, char_start, char_end = resolve_chunk(
            document=document, chunk_id=chunk_id
        )
        if slide_number is not None and int(slide_number) != source_slide:
            raise E.PreviewInvalidRequest(
                E.ERR_INVALID_SLIDE_RANGE,
                "la diapositive demandée contredit celle de la source citée",
            )
    elif slide_number is None:
        raise E.PreviewInvalidRequest(
            E.ERR_INVALID_SLIDE_RANGE, "indiquez un chunk_id ou un slide_number"
        )
    elif int(slide_number) < 1:
        raise E.PreviewInvalidRequest(
            E.ERR_INVALID_SLIDE_RANGE, "le numéro de diapositive est 1-based"
        )

    # --- Gardes de version et de configuration ------------------------------
    assert_versions(document)
    notes_included = assert_notes_configuration(document)

    # --- Ré-extraction et rejeu des bases -----------------------------------
    data = _read_document_bytes(document)
    result = extract_presentation(document, data)
    units = index_units(result.slides)
    if not units:
        raise E.PreviewUnsupported(
            E.ERR_PRESENTATION_UNREADABLE,
            "cette présentation ne contient aucun contenu indexable",
        )

    active_slide = source_slide if source_slide is not None else int(slide_number)

    highlights: dict[int, Any] = {}
    if chunk is not None:
        unit = find_unit(units, kind=source_kind, slide_number=source_slide)
        char_start, char_end = validate_offsets(char_start, char_end, unit=unit)
        blocks = split_blocks(unit)
        highlights = project_highlight(
            blocks, unit=unit, char_start=char_start, char_end=char_end
        )
        if not highlights:
            raise E.PreviewInvalidRequest(
                E.ERR_INVALID_CHUNK_OFFSETS,
                "la source citée ne recouvre aucun bloc de cette diapositive",
            )

    # --- Fenêtre bornée de diapositives -------------------------------------
    numbers = sorted({u.slide_number for u in units})
    if active_slide not in numbers:
        raise E.PreviewNotFound(
            E.ERR_SLIDE_NOT_FOUND, "cette diapositive n'existe pas dans la présentation"
        )

    pos = numbers.index(active_slide)
    lo = max(0, pos - before)
    hi = min(len(numbers) - 1, pos + after)
    window_numbers = numbers[lo : hi + 1]

    truncated, reason = False, None
    max_slides = preview_config.pptx_max_slides_per_response()
    if len(window_numbers) > max_slides:
        window_numbers = window_numbers[:max_slides]
        truncated, reason = True, "max_slides"

    return _serialize(
        document=document, result=result, units=units,
        chunk=chunk, source_kind=source_kind, source_slide=source_slide,
        char_start=char_start, char_end=char_end, highlights=highlights,
        notes_included=notes_included,
        window_numbers=window_numbers, numbers=numbers, active_slide=active_slide,
        truncated=truncated, truncation_reason=reason,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _serialize(
    *, document, result, units, chunk, source_kind, source_slide,
    char_start, char_end, highlights, notes_included,
    window_numbers, numbers, active_slide, truncated, truncation_reason, duration_ms,
) -> dict:
    max_chars = preview_config.pptx_max_block_chars()
    max_blocks = preview_config.pptx_max_blocks_per_slide()
    max_total = preview_config.pptx_max_total_chars()

    by_number: dict[int, list] = {}
    for unit in units:
        by_number.setdefault(unit.slide_number, []).append(unit)

    # --- Inventaire (borné) --------------------------------------------------
    inventory = []
    for number in numbers[: preview_config.pptx_max_inventory()]:
        slide_units = by_number[number]
        main = next((u for u in slide_units if u.kind == KIND_SLIDE), slide_units[0])
        entry = {
            "number": number,
            "title": main.slide_title,
            "has_citation": number == source_slide,
        }
        # `has_images` est absent des unités de notes : l'absence signifie
        # « inconnu », pas « faux ». On ne la convertit pas en booléen.
        if main.has_images is not None:
            entry["has_images"] = bool(main.has_images)
        if main.requires_visual_check is not None:
            entry["requires_visual_check"] = bool(main.requires_visual_check)
        inventory.append(entry)

    # --- Diapositives de la fenêtre -----------------------------------------
    slides_out = []
    total_chars = 0
    for number in window_numbers:
        slide_units = by_number[number]
        main = next((u for u in slide_units if u.kind == KIND_SLIDE), None)
        notes_unit = next((u for u in slide_units if u.kind == KIND_NOTES), None)

        blocks_out, warnings = [], []
        if main is not None:
            marks = highlights if (source_slide == number and source_kind == KIND_SLIDE) else {}
            blocks_out, used = _render_blocks(
                main, marks, max_chars=max_chars, max_blocks=max_blocks,
                budget=max_total - total_chars,
            )
            total_chars += used
            if not blocks_out:
                warnings.append("slide_has_no_indexable_text")

        notes_out = []
        if notes_unit is not None:
            marks = highlights if (source_slide == number and source_kind == KIND_NOTES) else {}
            notes_out, used = _render_blocks(
                notes_unit, marks, max_chars=max_chars, max_blocks=max_blocks,
                budget=max_total - total_chars,
            )
            total_chars += used

        entry = {
            "number": number,
            "title": (main or notes_unit).slide_title,
            "blocks": blocks_out,
            "speaker_notes": notes_out,
            "warnings": warnings,
        }
        ref = main or notes_unit
        if ref.has_images is not None:
            entry["has_images"] = bool(ref.has_images)
        if ref.requires_visual_check is not None:
            entry["requires_visual_check"] = bool(ref.requires_visual_check)
        slides_out.append(entry)

        if total_chars >= max_total:
            truncated, truncation_reason = True, "max_total_chars"
            break

    # --- Avertissements conditionnels ---------------------------------------
    warnings = list(PERMANENT_WARNINGS)
    if any(s.get("has_images") or s.get("requires_visual_check") for s in slides_out):
        warnings.append("visual_content_present")
    if notes_included:
        warnings.append("speaker_notes_included")

    pptx_md = _pptx_meta(document).get("pptx") or {}

    return {
        "schema_version": SCHEMA_VERSION,
        "document": {
            "id": str(document.id),
            "filename": getattr(document, "filename", "") or "",
            "sha256": getattr(document, "sha256", "") or "",
            "preview_kind": "presentation",
        },
        "source": (
            {
                "chunk_id": str(chunk.id),
                "kind": source_kind,
                "slide_number": source_slide,
                "char_start": char_start,
                "char_end": char_end,
                "notes_included": notes_included,
            }
            if chunk is not None
            else None
        ),
        "versions": {
            "extractor_version": EXTRACTOR_VERSION,
            "chunking_version": CHUNKING_VERSION,
        },
        "presentation": {
            "slide_count": pptx_md.get("slide_count", len(numbers)),
            "indexed_slide_count": len(numbers),
            "slides": inventory,
            "inventory_truncated": len(numbers) > preview_config.pptx_max_inventory(),
        },
        "viewport": {
            "active_slide": active_slide,
            "source_slide": source_slide,
            "first_slide": window_numbers[0] if window_numbers else active_slide,
            "last_slide": window_numbers[-1] if window_numbers else active_slide,
        },
        "slides": slides_out,
        "fidelity": {
            "simplified": True,
            "extractor_version": EXTRACTOR_VERSION,
            "chunking_version": CHUNKING_VERSION,
            "warnings": warnings,
            "omitted": list(OMITTED),
        },
        "truncation": {"truncated": truncated, "reason": truncation_reason},
    }


def _render_blocks(unit, marks, *, max_chars: int, max_blocks: int, budget: int):
    """Sérialise les blocs d'une unité. Renvoie `(blocs, caractères consommés)`."""
    out, used = [], 0
    for block in split_blocks(unit)[:max_blocks]:
        display = block.text
        block_truncated = False
        if len(display) > max_chars:
            display = display[:max_chars]
            block_truncated = True

        hl = marks.get(block.index)
        payload = {
            "id": block.block_id(unit.slide_number, unit.kind),
            "index": block.index,
            "type": block.type,
            "text": display,
            "highlight": ({"start": hl.start, "end": hl.end} if hl else None),
            "truncated": block_truncated,
            "full_length": len(block.text),
            "warnings": list(block.warnings),
        }
        if block.type == "table":
            payload["render_mode"] = "preformatted"
        elif block.type == "list":
            payload["list_fidelity"] = "flattened"
        out.append(payload)

        used += len(display)
        if used >= budget:
            break
    return out, used


def build_presentation_preview_cached(**kwargs) -> dict:
    """Variante mémoïsée. **N'appeler qu'après vérification de la permission.**

    ⚠️ Le chunk est résolu, et les gardes de version et de notes évaluées,
    **avant** toute lecture du cache. Sans cela un aperçu survivrait à la
    réindexation qui a supprimé son chunk, ou à un changement de réglage des
    notes — pendant tout le TTL, une source périmée s'afficherait comme valide.
    """
    document = kwargs["document"]
    chunk_id = kwargs.get("chunk_id")

    # Le FORMAT d'abord : un PDF n'a pas un problème de version, il n'est
    # simplement pas une présentation. Le dire dans cet ordre évite un
    # `preview_unavailable` trompeur là où `unsupported_format` est la vérité.
    if not _is_pptx(document):
        raise E.PreviewUnsupported(
            E.ERR_UNSUPPORTED_FORMAT,
            "seules les présentations .pptx sont prévisualisables",
        )

    kind = slide = char_start = char_end = None
    if chunk_id:
        _, kind, slide, char_start, char_end = resolve_chunk(
            document=document, chunk_id=chunk_id
        )
    assert_versions(document)
    notes_included = assert_notes_configuration(document)

    if not preview_config.cache_enabled():
        return build_presentation_preview(**kwargs)

    raw = (
        f"{document.id}|{getattr(document, 'sha256', '')}|{chunk_id or ''}"
        f"|{kind}|{slide}|{char_start}|{char_end}"
        f"|{kwargs.get('slide_number')}"
        f"|{kwargs.get('context_before')}|{kwargs.get('context_after')}"
        f"|{notes_included}|{EXTRACTOR_VERSION}|{CHUNKING_VERSION}|{SCHEMA_VERSION}"
    )
    key = "preview:presentation:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    hit = cache.get(key)
    if hit is not None:
        return hit

    out = build_presentation_preview(**kwargs)
    try:
        cache.set(key, out, preview_config.cache_ttl_s())
    except Exception:  # pragma: no cover - un cache indisponible n'invalide rien
        pass
    return out
