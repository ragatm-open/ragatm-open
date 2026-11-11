# backend/apps/rag_core/services/ingest/extraction/ifc_signature.py
"""
Signature d'une maquette IFC — fichier physique STEP (ISO 10303-21).

Détecteur enregistré dans le hook de signatures structurées : après
les signatures fortes, avant les replis. Politique : extension **`.ifc`** ET en-tête
`ISO-10303-21;` (après BOM et blancs). Le `FILE_SCHEMA` n'est PAS exigé pour le routage :
un `.ifc` STEP dont l'en-tête est invalide ou porte un schéma non IFC doit atteindre
l'extracteur, qui répond `ifc_malformed` / `ifc_unsupported_schema` — jamais un repli
silencieux en texte brut. Un `.step` / `.stp` générique n'est jamais IFC (extension).
`.ifczip` (ZIP) et `.ifcxml` (XML) sont hors périmètre. Le Content-Type client
(`application/x-step`, `model/step`, `octet-stream`, `text/plain`) n'est jamais décisif.
"""

from __future__ import annotations

import re
from typing import Optional

from .base import ExtractionContext

#: MIME canonique interne. Aucun type IANA n'existe pour IFC ; `application/x-step` est
#: la valeur observée en pratique. Dans RagATM elle signifie « IFC »
#: puisqu'un STEP générique n'est jamais classé.
IFC_MIME = "application/x-step"

_HEAD_BYTES = 4096
_UTF8_BOM = b"\xef\xbb\xbf"
_STEP_HEADER = re.compile(rb"^\s*ISO-10303-21\s*;")
_FILE_SCHEMA = re.compile(rb"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", re.IGNORECASE)


def has_step_header(data: bytes) -> bool:
    head = (data or b"")[:_HEAD_BYTES]
    if head.startswith(_UTF8_BOM):
        head = head[len(_UTF8_BOM):]
    return _STEP_HEADER.match(head) is not None


def declared_schema(data: bytes) -> Optional[str]:
    """Schéma déclaré dans l'en-tête (diagnostic seulement — l'extracteur fait foi)."""
    m = _FILE_SCHEMA.search((data or b"")[:_HEAD_BYTES * 4])
    if not m:
        return None
    try:
        return m.group(1).decode("ascii", errors="replace").strip() or None
    except Exception:
        return None


def detect_ifc_signature(data: bytes, ctx: ExtractionContext) -> Optional[str]:
    """Détecteur pour `mime.register_structured_detector` : `application/x-step` ou None."""
    if not ctx.lower_name.endswith(".ifc"):
        return None
    return IFC_MIME if has_step_header(data or b"") else None
