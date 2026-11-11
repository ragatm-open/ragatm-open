# backend/apps/rag_core/services/ingest/extraction/text_extractor.py
from __future__ import annotations

"""Extracteur texte brut (TXT). Réutilise le décodage best-effort existant."""

from .base import Block, BlockType, ExtractionContext, ExtractionResult
from . import config, mime as M


class TextExtractor:
    name = "text"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == M.PLAIN

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        from .base import decode_best_effort

        text, warn = decode_best_effort(data, declared=ctx.declared_content_type)
        limit = config.max_extracted_chars()
        res = ExtractionResult()
        if warn:
            res.add_warning(warn)
        if limit and len(text) > limit:
            text = text[:limit]
            res.partial = True
            res.add_warning("truncated_max_chars")
        res.text = text.strip()
        if res.text:
            res.blocks = [Block(type=BlockType.PARAGRAPH, text=res.text)]
        res.metadata["lines"] = res.text.count("\n") + 1 if res.text else 0
        return res.finalize(fmt="txt", mime=M.PLAIN, method="text")
