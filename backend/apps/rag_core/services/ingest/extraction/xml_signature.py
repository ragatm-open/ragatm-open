# backend/apps/rag_core/services/ingest/extraction/xml_signature.py
"""
Signature structurelle d'un document XML générique.

Détecteur enregistré dans le hook de signatures structurées : il est
évalué APRÈS les signatures fortes (exécutable, image, OLE2, PDF, famille ZIP) et AVANT
les replis génériques. Politique :

- l'extension doit être `.xml` — un `.html`, `.svg`, `.xsd`, `.xsl`, `.md`, `.txt`… n'est
  JAMAIS reclassé XML, même s'il est techniquement du XML bien formé ;
- ET le contenu doit porter une signature structurelle, après BOM / blancs /
  commentaires : déclaration `<?xml`, instruction de traitement, `<!DOCTYPE` non-HTML
  (refusé plus loin par `unsafe_xml`, mais classé XML pour que le refus soit le bon),
  ou une balise racine bien formée dont le nom local n'est pas `html` ;
- le Content-Type client n'est jamais une preuve ;
- la validation SÉCURISÉE (defusedxml) vit dans l'extracteur, pas ici.

Un `.xml` dont le contenu est du HTML (`<!DOCTYPE html`, racine `<html>`) reste HTML :
la détection HTML existante s'applique ensuite (choix documenté, §78 du lot).
"""

from __future__ import annotations

import re
from typing import Optional

from .base import ExtractionContext

XML_MIME = "application/xml"

_HEAD_BYTES = 8192
_UTF8_BOM = b"\xef\xbb\xbf"
_WS = b" \t\r\n"
_NAME = rb"[A-Za-z_\x80-\xff][A-Za-z0-9_.\-\x80-\xff]*"
_START_TAG = re.compile(rb"<(" + _NAME + rb"(?::" + _NAME + rb")?)")


def _head_as_utf8(data: bytes) -> bytes:
    head = data[:_HEAD_BYTES]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        # UTF-16 (BOM) : on ramène l'en-tête en UTF-8 pour l'analyse de signature ;
        # le parseur, lui, lit les octets d'origine.
        try:
            return head.decode("utf-16", errors="ignore").encode("utf-8", errors="ignore")
        except Exception:
            return b""
    if head.startswith(_UTF8_BOM):
        head = head[len(_UTF8_BOM):]
    return head


def _skip_leading(head: bytes) -> bytes:
    """Retire blancs et commentaires de tête. Un commentaire non terminé dans
    l'en-tête vaut absence de signature (fermé)."""
    while True:
        head = head.lstrip(_WS)
        if head.startswith(b"<!--"):
            end = head.find(b"-->", 4)
            if end < 0:
                return b""
            head = head[end + 3:]
            continue
        return head


def has_xml_signature(data: bytes) -> bool:
    """Le contenu ressemble-t-il structurellement à un document XML (non HTML) ?"""
    head = _skip_leading(_head_as_utf8(data or b""))
    if not head.startswith(b"<"):
        return False
    if head[:9].upper() == b"<!DOCTYPE":
        rest = head[9:].lstrip(_WS)
        return not rest[:4].lower().startswith(b"html")
    if head.startswith(b"<?"):
        # `<?xml …?>` ou toute instruction de traitement en tête : document XML.
        return True
    if head.startswith(b"<!"):
        # `<![CDATA[` ou autre déclaration en tête : pas un document.
        return False
    m = _START_TAG.match(head)
    if not m:
        return False
    local = m.group(1).rsplit(b":", 1)[-1].lower()
    return local != b"html"


def detect_xml_signature(data: bytes, ctx: ExtractionContext) -> Optional[str]:
    """Détecteur pour `mime.register_structured_detector` : `application/xml` ou None."""
    if not ctx.lower_name.endswith(".xml"):
        return None
    return XML_MIME if has_xml_signature(data) else None
