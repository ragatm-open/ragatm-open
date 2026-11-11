# backend/apps/rag_core/services/ingest/extraction/html_extractor.py
from __future__ import annotations

"""
Extracteur HTML SÉCURISÉ (stdlib `html.parser`).

- tout HTML est traité comme hostile : scripts/styles/iframes et contenu actif
  sont supprimés AVEC leur contenu ;
- aucune ressource externe n'est chargée, aucun lien n'est suivi ;
- extraction : titre de page, H1/H2/H3..., paragraphes, listes, tableaux ;
- HTML malformé : html.parser est tolérant, on ne plante jamais ;
- la sortie est du texte neutralisé (jamais réinterprété comme HTML).
"""

from typing import List

from . import config, mime as M
from .base import (
    Block,
    BlockType,
    ExtractionContext,
    ExtractionResult,
    decode_best_effort,
    join_blocks_text,
)
from .htmltext import parse_html_document


class HtmlExtractor:
    name = "html"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == M.HTML

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        html, warn = decode_best_effort(data, declared=ctx.declared_content_type)
        res = ExtractionResult()
        if warn:
            res.add_warning(warn)

        limit = config.html_max_chars()
        if limit and len(html) > limit:
            html = html[:limit]
            res.partial = True
            res.add_warning("truncated_max_chars")

        title, blocks, removed_active, link_count = parse_html_document(html)

        out_blocks: List[Block] = []
        if title and title.strip():
            out_blocks.append(Block(type=BlockType.TITLE, text=title.strip(), level=1))
            res.metadata["html_title"] = title.strip()[:512]
        out_blocks.extend(blocks)

        n_sections = sum(1 for b in out_blocks if b.type == BlockType.TITLE)

        res.blocks = out_blocks
        res.text = join_blocks_text(out_blocks)
        res.metadata["sections"] = n_sections
        res.metadata["links"] = link_count
        if removed_active:
            res.add_warning("active_content_removed")
        # rappel de la garantie sécurité (pas d'I/O réseau pendant l'extraction)
        res.add_warning("html_treated_as_untrusted")
        return res.finalize(fmt="html", mime=M.HTML, method="html")
