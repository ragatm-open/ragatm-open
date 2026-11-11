# backend/apps/rag_core/services/ingest/extraction/markdown_extractor.py
from __future__ import annotations

"""
Extracteur Markdown (stdlib pur).

Préserve titres (niveaux), listes, tableaux et blocs de code (blocs distincts).
Le front matter YAML est extrait en métadonnées (jamais injecté dans le contenu RAG).
Le HTML inline est neutralisé. Les liens externes ne sont jamais suivis.
"""

import re
from typing import List, Tuple

from . import config, mime as M
from .base import (
    Block,
    BlockType,
    ExtractionContext,
    ExtractionResult,
    decode_best_effort,
    join_blocks_text,
)
from .htmltext import neutralize_inline_html

_ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+\S")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)(.*)$")


def _split_front_matter(text: str) -> Tuple[str, str]:
    """Sépare un éventuel front matter YAML (--- ... ---) en tête.
    Renvoie (front_matter, corps)."""
    if not text.startswith("---"):
        return "", text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, min(len(lines), 200)):
        if lines[i].strip() in ("---", "..."):
            fm = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            return fm, body
    return "", text


class MarkdownExtractor:
    name = "markdown"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == M.MARKDOWN

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        raw, warn = decode_best_effort(data, declared=ctx.declared_content_type)
        res = ExtractionResult()
        if warn:
            res.add_warning(warn)

        limit = config.markdown_max_chars()
        if limit and len(raw) > limit:
            raw = raw[:limit]
            res.partial = True
            res.add_warning("truncated_max_chars")

        front_matter, body = _split_front_matter(raw)
        if front_matter.strip():
            res.metadata["front_matter"] = front_matter.strip()[:4000]
            res.add_warning("front_matter_extracted")

        blocks: List[Block] = []
        n_sections = 0
        html_removed = False

        lines = body.splitlines()
        i = 0
        para: List[str] = []
        list_buf: List[str] = []
        table_buf: List[str] = []

        def flush_para():
            nonlocal html_removed
            if para:
                txt = " ".join(s.strip() for s in para if s.strip()).strip()
                para.clear()
                if txt:
                    txt2, removed = neutralize_inline_html(txt)
                    html_removed = html_removed or removed
                    if txt2.strip():
                        blocks.append(Block(type=BlockType.PARAGRAPH, text=txt2.strip()))

        def flush_list():
            if list_buf:
                txt = "\n".join(list_buf)
                list_buf.clear()
                blocks.append(Block(type=BlockType.LIST, text=txt.strip()))

        def flush_table():
            if table_buf:
                txt = "\n".join(table_buf)
                table_buf.clear()
                blocks.append(Block(type=BlockType.TABLE, text=txt.strip()))

        def flush_all():
            flush_para()
            flush_list()
            flush_table()

        while i < len(lines):
            line = lines[i]

            # bloc de code clôturé : préservé tel quel, NON parsé, bloc distinct
            fence = _FENCE_RE.match(line)
            if fence:
                flush_all()
                marker = fence.group(1)[0] * 3
                lang = (fence.group(2) or "").strip()
                code_lines: List[str] = []
                i += 1
                while i < len(lines) and not lines[i].lstrip().startswith(marker):
                    code_lines.append(lines[i])
                    i += 1
                i += 1  # saute la fence de fermeture
                code = "\n".join(code_lines)
                blocks.append(
                    Block(type=BlockType.CODE, text=code, meta={"lang": lang} if lang else {})
                )
                continue

            stripped = line.strip()

            if not stripped:
                flush_all()
                i += 1
                continue

            heading = _ATX_RE.match(line)
            if heading:
                flush_all()
                level = len(heading.group(1))
                title_txt, removed = neutralize_inline_html(heading.group(2).strip())
                html_removed = html_removed or removed
                if title_txt.strip():
                    blocks.append(Block(type=BlockType.TITLE, text=title_txt.strip(), level=level))
                    n_sections += 1
                i += 1
                continue

            if _TABLE_ROW_RE.match(line):
                flush_para()
                flush_list()
                table_buf.append(stripped)
                i += 1
                continue
            else:
                flush_table()

            if _LIST_RE.match(line):
                flush_para()
                txt2, removed = neutralize_inline_html(stripped)
                html_removed = html_removed or removed
                list_buf.append(txt2)
                i += 1
                continue
            else:
                flush_list()

            para.append(line)
            i += 1

        flush_all()

        if html_removed:
            res.add_warning("html_inline_neutralized")

        res.blocks = blocks
        res.text = join_blocks_text(blocks)
        res.metadata["sections"] = n_sections
        res.metadata["lines"] = body.count("\n") + 1 if body else 0
        return res.finalize(fmt="markdown", mime=M.MARKDOWN, method="markdown")
