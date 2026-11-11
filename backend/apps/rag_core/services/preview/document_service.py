# backend/apps/rag_core/services/preview/document_service.py
from __future__ import annotations

"""Aperçu documentaire structuré (DOCX) — orchestration.

Ordre imposé, et non négociable :

    permissions (par la vue)
      → format supporté
      → résolution du chunk        (document ET RagSpace vérifiés)
      → offsets validés            (jamais « réparés »)
      → lecture du cache           ← APRÈS la résolution du chunk, jamais avant
      → ré-extraction canonique    (le MÊME extracteur que l'ingestion)
      → garde d'intégrité          (version + compteurs blocs/caractères)
      → bornes de blocs vérifiées  (reconstruction == texte extrait)
      → projection des offsets     → blocs + sous-plages exactes
      → fenêtre bornée
      → réponse structurée

Ce qui rend l'ensemble sûr sans rien persister de nouveau : l'extraction DOCX est
**déterministe**, et `ExtractionResult.text` est une fonction pure des blocs. Les
offsets déjà en base (`Chunk.meta.char_start/char_end`) suffisent donc à retrouver
le passage — à condition de prouver, à chaque lecture, que le texte réextrait est
bien celui qui a été indexé. C'est le rôle de la garde d'intégrité : sans elle,
une évolution de l'extracteur déplacerait les offsets et l'aperçu surlignerait
silencieusement le mauvais passage.
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
from apps.rag_core.services.ingest.extraction.docx_extractor import (
    EXTRACTOR_VERSION,
    DocxExtractor,
)

from . import config as preview_config
from . import errors as E
from .document_blocks import (
    build_headings,
    find_covering_blocks,
    index_blocks,
    validate_offsets,
)

#: Version du contrat de réponse. Incrémentée si la FORME change.
SCHEMA_VERSION = 1

DOCX_SUFFIXES = (".docx",)

#: Ce que l'aperçu ne restitue pas — annoncé dans CHAQUE réponse.
#:
#: Ces valeurs ne sont pas décoratives : elles décrivent des contenus que le
#: pipeline **n'indexe pas** (en-têtes, pieds, notes, commentaires vivent dans des
#: parties du DOCX que l'extracteur ne lit jamais). Les afficher exposerait du
#: contenu que le RAG a écarté ; les taire laisserait croire à une complétude que
#: l'aperçu n'a pas.
OMITTED = (
    "exact_layout",
    "fonts",
    "colors",
    "floating_shapes",
    "images",
    "headers",
    "footers",
    "footnotes",
    "endnotes",
    "comments",
    "computed_pagination",
)

#: Limitations PERMANENTES du pipeline actuel — vraies pour tout document.
PERMANENT_WARNINGS = (
    "exact_layout_not_rendered",
    "computed_pagination_unavailable",
    "images_not_extracted",
    "headers_footers_not_indexed",
    "notes_not_indexed",
    "comments_not_indexed",
    "lists_flattened",
    "tables_simplified",
)


def _is_docx(document) -> bool:
    name = (getattr(document, "original_filename", "") or "").lower()
    return name.endswith(DOCX_SUFFIXES)


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


def resolve_chunk(*, document, chunk_id):
    """Charge le chunk cité et rend ses offsets. Ne devine rien.

    L'absence du chunk signifie une réindexation : les chunks sont supprimés puis
    recréés, donc un identifiant inconnu désigne une version qui n'existe plus.
    Cela doit se **dire**, pas se rattraper par un passage ressemblant.
    """
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
    return chunk, meta.get("char_start"), meta.get("char_end")


def extract_document(document, data: bytes):
    """Ré-extrait le DOCX avec **exactement** l'extracteur de l'ingestion.

    Aucune logique XML n'est recopiée, aucune normalisation supplémentaire n'est
    appliquée : toute divergence de traitement produirait un texte différent de
    l'indexé, donc des offsets qui ne désignent plus rien.
    """
    try:
        return DocxExtractor().extract(
            data,
            ExtractionContext(filename=getattr(document, "filename", "") or "document.docx"),
        )
    except (RejectedFile, ExtractionLimitExceeded) as exc:
        raise E.PreviewUnsupported(
            E.ERR_DOCUMENT_UNREADABLE, "document refusé par les gardes de sécurité"
        ) from exc
    except ExtractionError as exc:
        raise E.PreviewUnsupported(
            E.ERR_DOCUMENT_UNREADABLE, "document illisible ou endommagé"
        ) from exc
    except Exception as exc:
        raise E.PreviewUnsupported(
            E.ERR_DOCUMENT_UNREADABLE, "document illisible ou endommagé"
        ) from exc


def assert_integrity(document, result) -> None:
    """Refuse une extraction qui ne correspond plus à ce qui a été indexé.

    Deux gardes complémentaires, parce qu'aucune ne suffit seule :

    - **Version d'extracteur** — précise, mais absente des documents ingérés avant
      son introduction. On ne peut donc pas l'exiger sans rendre invisible tout
      l'existant, ce que la mission interdit explicitement.
    - **Compteurs `blocks` / `chars`** — déjà persistés par `finalize()` pour TOUS
      les documents, y compris historiques. Moins précis qu'une version, mais ils
      détectent exactement ce qui compte ici : un texte de longueur différente,
      donc des offsets qui ont bougé.

    Ensemble, elles couvrent l'existant sans migration et les nouveaux documents
    avec précision.
    """
    meta = getattr(document, "meta", None) or {}
    extraction = meta.get("extraction") if isinstance(meta, dict) else None
    if not isinstance(extraction, dict):
        # Aucun repère : on ne peut rien prouver. On préfère refuser plutôt que
        # d'afficher un surlignage dont rien ne garantit la position.
        raise E.PreviewStale(
            E.ERR_PREVIEW_UNAVAILABLE,
            "aucune métadonnée d'extraction pour ce document",
        )

    persisted_version = extraction.get("extractor_version")
    if persisted_version and persisted_version != EXTRACTOR_VERSION:
        raise E.PreviewStale(
            E.ERR_DOCUMENT_VERSION_MISMATCH,
            "ce document a été indexé par une autre version de l'extracteur",
        )

    expected_chars = extraction.get("chars")
    expected_blocks = extraction.get("blocks")
    actual_chars = len(result.text or "")
    actual_blocks = len([b for b in (result.blocks or []) if (b.text or "").strip()])

    if isinstance(expected_chars, int) and expected_chars != actual_chars:
        raise E.PreviewStale(
            E.ERR_DOCUMENT_VERSION_MISMATCH,
            "le contenu extrait ne correspond plus à celui qui a été indexé",
        )
    if isinstance(expected_blocks, int) and expected_blocks != actual_blocks:
        raise E.PreviewStale(
            E.ERR_DOCUMENT_VERSION_MISMATCH,
            "la structure extraite ne correspond plus à celle qui a été indexée",
        )


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


def build_document_preview(
    *,
    document,
    chunk_id: Any = None,
    context_before: int | None = None,
    context_after: int | None = None,
    first_block: int | None = None,
    last_block: int | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """Construit l'aperçu documentaire. Lève un `PreviewError` typé sur tout refus."""
    started = time.perf_counter()

    if not _is_docx(document):
        raise E.PreviewUnsupported(
            E.ERR_UNSUPPORTED_FORMAT, "seuls les documents .docx sont prévisualisables"
        )
    if expected_sha256 and expected_sha256 != (getattr(document, "sha256", "") or ""):
        raise E.PreviewStale(
            E.ERR_DOCUMENT_VERSION_MISMATCH,
            "le document a changé depuis la création de cette réponse",
        )

    before = _validate_context(
        context_before,
        default=preview_config.doc_context_before(),
        maximum=preview_config.doc_max_context_before(),
        label="context_before",
    )
    after = _validate_context(
        context_after,
        default=preview_config.doc_context_after(),
        maximum=preview_config.doc_max_context_after(),
        label="context_after",
    )

    # --- Chunk : autorité de localisation, résolu AVANT tout le reste --------
    char_start = char_end = None
    resolved_chunk = None
    if chunk_id:
        resolved_chunk, char_start, char_end = resolve_chunk(
            document=document, chunk_id=chunk_id
        )
    elif first_block is None:
        raise E.PreviewInvalidRequest(
            E.ERR_INVALID_BLOCK_RANGE, "indiquez un chunk_id ou une fenêtre de blocs"
        )

    # --- Ré-extraction et intégrité -----------------------------------------
    data = _read_document_bytes(document)
    result = extract_document(document, data)
    assert_integrity(document, result)

    text = result.text or ""
    indexed = index_blocks(result.blocks or [], expected_text=text)

    if len(indexed) > preview_config.doc_max_document_blocks():
        raise E.PreviewLimitExceeded(
            E.ERR_DOCUMENT_TOO_LARGE,
            "ce document dépasse les limites de l'aperçu",
        )

    # Document vide : ce n'est PAS une erreur. Une réponse d'erreur ferait croire
    # à un défaut de lecture là où le document ne porte simplement aucun texte.
    if not indexed:
        return _serialize(
            document=document, chunk=resolved_chunk,
            char_start=char_start, char_end=char_end,
            indexed=[], window=[], covering={},
            headings=[], headings_truncated=False,
            first_index=0, last_index=0,
            truncated=False, truncation_reason=None,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    # --- Projection des offsets sur les blocs -------------------------------
    covering: dict[int, Any] = {}
    source_first = source_last = None
    if resolved_chunk is not None:
        # Un chunk résolu DOIT porter des offsets exploitables. S'il n'en a pas,
        # c'est un refus typé — surtout pas un repli sur la navigation par blocs,
        # qui afficherait le début du document en laissant croire à une citation.
        char_start, char_end = validate_offsets(
            char_start, char_end, text_length=len(text)
        )
        pairs = find_covering_blocks(indexed, char_start=char_start, char_end=char_end)
        if not pairs:
            raise E.PreviewInvalidRequest(
                E.ERR_INVALID_CHUNK_OFFSETS,
                "la source citée ne recouvre aucun bloc de ce document",
            )
        covering = {b.index: h for b, h in pairs}
        source_first, source_last = pairs[0][0].index, pairs[-1][0].index
        anchor_first, anchor_last = source_first, source_last
    else:
        anchor_first = max(0, min(int(first_block), len(indexed) - 1))
        anchor_last = (
            max(anchor_first, min(int(last_block), len(indexed) - 1))
            if last_block is not None
            else anchor_first
        )

    # --- Fenêtre bornée ------------------------------------------------------
    win_first = max(0, anchor_first - before)
    win_last = min(len(indexed) - 1, anchor_last + after)

    truncated, reason = False, None
    max_blocks = preview_config.doc_max_blocks_per_response()
    if (win_last - win_first + 1) > max_blocks:
        win_last = win_first + max_blocks - 1
        truncated, reason = True, "max_blocks"

    window = indexed[win_first : win_last + 1]

    # Borne CARDINALE de caractères : elle prime sur le nombre de blocs.
    max_total = preview_config.doc_max_total_chars()
    total = 0
    kept = []
    for block in window:
        total += min(len(block.text), preview_config.doc_max_block_chars())
        if total > max_total and kept:
            truncated, reason = True, "max_total_chars"
            break
        kept.append(block)
    window = kept
    if window:
        win_last = window[-1].index

    headings, headings_truncated = build_headings(
        indexed, limit=preview_config.doc_max_headings()
    )

    return _serialize(
        document=document, chunk=resolved_chunk,
        char_start=char_start, char_end=char_end,
        indexed=indexed, window=window, covering=covering,
        headings=headings, headings_truncated=headings_truncated,
        first_index=win_first, last_index=win_last,
        source_first=source_first, source_last=source_last,
        truncated=truncated, truncation_reason=reason,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _serialize(
    *, document, chunk, char_start, char_end, indexed, window, covering,
    headings, headings_truncated, first_index, last_index,
    truncated, truncation_reason, duration_ms,
    source_first=None, source_last=None,
) -> dict:
    max_chars = preview_config.doc_max_block_chars()

    blocks = []
    for block in window:
        display = block.text
        block_truncated = False
        if len(display) > max_chars:
            display = display[:max_chars]
            block_truncated = True

        highlight = covering.get(block.index)
        payload = {
            "id": block.id,
            "index": block.index,
            "type": _block_type(block.kind),
            "text": display,
            "highlight": (
                {"start": highlight.start, "end": highlight.end} if highlight else None
            ),
            "truncated": block_truncated,
            "full_length": len(block.text),
            "warnings": [],
        }
        if payload["type"] == "heading":
            payload["level"] = block.level if isinstance(block.level, int) else 1
        elif payload["type"] == "list":
            # Le pipeline agrège tous les items consécutifs en un seul bloc, avec
            # une puce uniforme : ni la numérotation, ni le niveau, ni la frontière
            # entre deux listes distinctes ne survivent. On le DIT plutôt que de
            # laisser croire à une liste fidèle.
            payload["list_fidelity"] = "flattened"
        elif payload["type"] == "table":
            # Rendu préformaté, jamais une grille : les fusions (`gridSpan`,
            # `vMerge`) sont ignorées à l'extraction, donc les lignes n'ont pas
            # toutes le même nombre de champs. Reconstruire un tableau HTML
            # fabriquerait un alignement colonne/valeur que la donnée ne porte pas.
            payload["render_mode"] = "preformatted"
            payload["warnings"].append("table_simplified")
        blocks.append(payload)

    return {
        "schema_version": SCHEMA_VERSION,
        "document": {
            "id": str(document.id),
            "filename": getattr(document, "filename", "") or "",
            "sha256": getattr(document, "sha256", "") or "",
            "preview_kind": "document",
        },
        "source": (
            {
                "chunk_id": str(chunk.id),
                "char_start": char_start,
                "char_end": char_end,
            }
            if chunk is not None
            else None
        ),
        "navigation": {
            "headings": headings,
            "headings_truncated": headings_truncated,
        },
        "viewport": {
            "first_block": first_index,
            "last_block": last_index,
            "total_blocks": len(indexed),
            "source_first_block": source_first,
            "source_last_block": source_last,
        },
        "blocks": blocks,
        "fidelity": {
            "simplified": True,
            "extractor_version": EXTRACTOR_VERSION,
            "warnings": list(PERMANENT_WARNINGS)
            + ([] if headings else ["headings_not_detected"]),
            "omitted": list(OMITTED),
        },
        "truncation": {"truncated": truncated, "reason": truncation_reason},
    }


def _block_type(kind: str) -> str:
    """Type de bloc EXPOSÉ. Seuls quatre types existent réellement au MVP."""
    return {
        "title": "heading",
        "paragraph": "paragraph",
        "list": "list",
        "table": "table",
    }.get(kind, "unsupported")


def build_document_preview_cached(**kwargs) -> dict:
    """Variante mémoïsée. **N'appeler qu'après vérification de la permission.**

    ⚠️ Le chunk est résolu **avant** toute lecture du cache. Mettre un `chunk_id`
    en clé sans le vérifier laisserait un aperçu survivre à la réindexation qui a
    supprimé ce chunk : pendant tout le TTL, une source périmée continuerait de
    s'afficher comme valide — exactement ce que `chunk_stale` existe pour empêcher.
    """
    document = kwargs["document"]
    chunk_id = kwargs.get("chunk_id")

    char_start = char_end = None
    if chunk_id:
        _, char_start, char_end = resolve_chunk(document=document, chunk_id=chunk_id)

    if not preview_config.cache_enabled():
        return build_document_preview(**kwargs)

    raw = (
        f"{document.id}|{getattr(document, 'sha256', '')}|{chunk_id or ''}"
        f"|{char_start}|{char_end}"
        f"|{kwargs.get('first_block')}|{kwargs.get('last_block')}"
        f"|{kwargs.get('context_before')}|{kwargs.get('context_after')}"
        f"|{EXTRACTOR_VERSION}|{SCHEMA_VERSION}"
    )
    key = "preview:document:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    hit = cache.get(key)
    if hit is not None:
        return hit

    out = build_document_preview(**kwargs)
    try:
        cache.set(key, out, preview_config.cache_ttl_s())
    except Exception:  # pragma: no cover - un cache indisponible n'invalide rien
        pass
    return out
