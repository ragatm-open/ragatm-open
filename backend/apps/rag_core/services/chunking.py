from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class ChunkSpec:
    chunk_index: int
    text: str
    page_number: Optional[int]
    char_start: int
    char_end: int
    source: str
    # Métadonnées additionnelles (entités métier, type de page…) fusionnées dans
    # Chunk.meta. Le dict est mutable (frozen interdit la réassignation, pas la
    # mutation) : les consommateurs font `spec.meta.update(...)`.
    meta: dict = field(default_factory=dict)


def _iter_slices(total_len: int, chunk_size: int, overlap: int) -> Iterable[tuple[int, int]]:
    """
    Slices déterministes, ordonnés, sans trous.
    chunk_size > 0, 0 <= overlap < chunk_size.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk_size")

    start = 0
    while start < total_len:
        end = min(start + chunk_size, total_len)
        yield start, end
        if end >= total_len:
            break
        start = end - overlap


def chunk_text_deterministic(
    text: str,
    *,
    source: str,
    chunk_size: int,
    overlap: int,
    page_number: Optional[int] = None,
    base_char_offset: int = 0,
    start_chunk_index: int = 0,
) -> list[ChunkSpec]:
    """
    Chunker simple mais propre :
    - taille cible configurable (chars)
    - overlap configurable
    - chunk_index strictement ordonné
    - aucun chunk vide
    - char_start/char_end déterministes (sur le texte normalisé CRLF->LF)
    """
    if text is None:
        raise ValueError("text must not be None")

    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    n = len(raw)
    if n == 0:
        return []

    out: list[ChunkSpec] = []
    idx = start_chunk_index

    for s, e in _iter_slices(n, chunk_size, overlap):
        piece = raw[s:e].strip()
        if not piece:
            continue

        out.append(
            ChunkSpec(
                chunk_index=idx,
                text=piece,
                page_number=page_number,
                char_start=base_char_offset + s,
                char_end=base_char_offset + e,
                source=source,
            )
        )
        idx += 1

    return out
