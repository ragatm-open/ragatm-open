# backend/apps/rag_core/services/ingest/extraction/dxf_signature.py
"""
Signature structurelle d'un dessin DXF.

Détecteur enregistré dans le hook de signatures structurées : après
les signatures fortes (exécutable, image, OLE2, PDF, famille ZIP), avant les replis.
Politique : extension `.dxf` ET signature de contenu —

- DXF binaire : en-tête « AutoCAD Binary DXF\\r\\n\\x1a\\x00 » ;
- DXF ASCII : paires code/valeur en tête — `0` / `SECTION` puis `2` / `<HEADER|CLASSES|
  TABLES|BLOCKS|ENTITIES|OBJECTS|…>` (commentaires `999` tolérés, BOM et blancs de tête
  ignorés). Le seul mot « SECTION » dans un texte n'est jamais une preuve.

Le Content-Type client (`image/vnd.dxf`, `application/dxf`, `octet-stream`…) n'est jamais
décisif : `image/vnd.dxf` ne route donc jamais un dessin vers le pipeline image/OCR. La
validation par le parseur (ezdxf) vit dans l'extracteur.
"""

from __future__ import annotations

from typing import Optional

from .base import ExtractionContext

DXF_MIME = "application/dxf"
BINARY_MAGIC = b"AutoCAD Binary DXF\r\n\x1a\x00"

_HEAD_BYTES = 8192
_UTF8_BOM = b"\xef\xbb\xbf"
_SECTION_NAMES = frozenset({
    b"HEADER", b"CLASSES", b"TABLES", b"BLOCKS", b"ENTITIES", b"OBJECTS",
    b"THUMBNAILIMAGE", b"ACDSDATA",
})


def is_binary_dxf(data: bytes) -> bool:
    return bool(data) and data.startswith(BINARY_MAGIC)


def has_ascii_dxf_signature(data: bytes) -> bool:
    head = (data or b"")[:_HEAD_BYTES]
    if head.startswith(_UTF8_BOM):
        head = head[len(_UTF8_BOM):]
    lines = [ln.strip() for ln in head.splitlines()]
    i, n = 0, len(lines)
    while i < n and lines[i] == b"":
        i += 1
    while i + 1 < n:
        code, value = lines[i], lines[i + 1]
        if code == b"999":              # commentaire : paire ignorée
            i += 2
            continue
        if code != b"0" or value.upper() != b"SECTION":
            return False
        return i + 3 < n and lines[i + 2] == b"2" and lines[i + 3].upper() in _SECTION_NAMES
    return False


def has_dxf_signature(data: bytes) -> bool:
    return is_binary_dxf(data) or has_ascii_dxf_signature(data)


def detect_dxf_signature(data: bytes, ctx: ExtractionContext) -> Optional[str]:
    """Détecteur pour `mime.register_structured_detector` : `application/dxf` ou None."""
    if not ctx.lower_name.endswith(".dxf"):
        return None
    return DXF_MIME if has_dxf_signature(data or b"") else None
