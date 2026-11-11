# backend/apps/rag_core/services/ingest/extraction/eml_signature.py
"""
Signature structurelle d'un courriel MIME `.eml`.

Détecteur du hook de signatures structurées, évalué après les
signatures fortes (exécutable, image, OLE2, PDF, ZIP) et avant les replis texte.

- l'extension doit être `.eml` — un `.txt` contenant un en-tête de courriel reste du
  texte : on ne reclasse jamais un fichier sur son seul contenu textuel ;
- ET le bloc d'en-têtes (jusqu'à la première ligne vide) doit être fait EXCLUSIVEMENT de
  champs RFC 5322 (`Nom: valeur`) ou de lignes de continuation, et porter au moins deux
  champs de courriel connus. Une boîte mbox (`From ` sans deux-points) n'est pas un `.eml` ;
- le Content-Type client n'est jamais une preuve ;
- l'analyse MIME elle-même vit dans `eml_email`, pas ici.

Avant ce lot, un `.eml` était indexé comme TEXTE BRUT : en-têtes de transport, blocs
base64 des pièces jointes et HTML compris. Il est désormais classé, puis extrait par un
parseur MIME — ou refusé nommément si la porte de format est fermée.
"""

from __future__ import annotations

import re
from typing import Optional

from .base import ExtractionContext

EML_MIME = "message/rfc822"

_HEAD_BYTES = 16384
_FIELD = re.compile(rb"^([!-9;-~]+):")  # nom de champ RFC 5322 : imprimables sauf « : »
_KNOWN = {
    b"from", b"to", b"cc", b"date", b"subject", b"message-id", b"mime-version", b"received",
    b"return-path", b"reply-to", b"content-type", b"in-reply-to", b"references", b"sender",
    b"delivered-to",
}
# Au moins un champ d'IDENTITÉ de message : un fichier de configuration `Clé: valeur`
# qui citerait `Content-Type` et `Date` ne doit pas devenir un courriel.
_IDENTITY = {b"from", b"message-id", b"received", b"return-path", b"subject"}


def has_eml_signature(data: bytes) -> bool:
    head = (data or b"")[:_HEAD_BYTES]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    lines = head.replace(b"\r\n", b"\n").split(b"\n")
    fields = set()
    seen_header = False
    for line in lines:
        if not line.strip():
            break  # fin du bloc d'en-têtes
        if line[:1] in (b" ", b"\t"):
            if not seen_header:
                return False  # continuation sans champ préalable
            continue
        m = _FIELD.match(line)
        if not m:
            return False  # ligne qui n'est ni un champ ni une continuation : pas un en-tête
        seen_header = True
        fields.add(m.group(1).lower())
    known = fields & _KNOWN
    return len(known) >= 2 and bool(known & _IDENTITY)


def detect_eml_signature(data: bytes, ctx: ExtractionContext) -> Optional[str]:
    """Détecteur pour `mime.register_structured_detector` : `message/rfc822` ou None."""
    if not ctx.lower_name.endswith(".eml"):
        return None
    return EML_MIME if has_eml_signature(data) else None
