"""
Signature d'un dessin AutoCAD DWG — format binaire propriétaire.

Détecteur enregistré dans le hook de signatures structurées : après
les signatures fortes (ELF/PE, images, PDF, ZIP/OOXML, OLE2), avant les replis. Politique :
extension **`.dwg`** ET en-tête AutoCAD plausible — code de version `ACxxxx` CONNU, suivi
des cinq octets nuls de l'en-tête DWG, sur un fichier d'au moins `MIN_DWG_BYTES` octets.
Un texte qui commence par « AC1032 » n'est donc jamais un DWG, ni un `.dxf`, ni un binaire
quelconque renommé. Le Content-Type client (`application/acad`, `image/vnd.dwg`,
`application/x-dwg`, `drawing/dwg`…) n'est JAMAIS décisif : `image/vnd.dwg` ne doit pas
envoyer un dessin dans le pipeline image/OCR.

Versions : seules les familles VÉRIFIÉES sur le corpus de test LibreDWG sont supportées
(une par famille : R2000 → R2018). Une version reconnue mais non supportée est refusée
par un code nommé (`dwg_unsupported_version`), jamais traitée en texte brut.
"""

from __future__ import annotations

from typing import Optional

from .base import ExtractionContext

#: MIME canonique interne. Aucun type IANA n'existe pour DWG ; `application/acad` est la
#: valeur historique d'Autodesk, retenue ici comme valeur unique et déterministe.
#: `image/vnd.dwg` (autre valeur observée côté clients) n'est jamais produite : elle
#: ferait croire à une image.
DWG_MIME = "application/acad"

#: Taille minimale plausible : l'en-tête DWG seul occupe plus de 100 octets et aucun
#: dessin réel n'est plus petit ; borne basse volontairement prudente.
MIN_DWG_BYTES = 256

#: Versions DWG SUPPORTÉES — une par famille, toutes vérifiées sur le corpus officiel
#: LibreDWG (example_2000/2004/2007/2010/2013/2018) lors de la qualification du lot.
SUPPORTED_VERSIONS: frozenset[str] = frozenset({
    "AC1015",  # R2000
    "AC1018",  # R2004
    "AC1021",  # R2007
    "AC1024",  # R2010
    "AC1027",  # R2013
    "AC1032",  # R2018
})

#: Versions RECONNUES (l'en-tête est un DWG) mais hors périmètre : refus nommé.
#: Source : `src/common.c` de GNU LibreDWG 0.14 (table des versions).
RELEASE_BY_VERSION: dict[str, str] = {
    "AC1006": "R10", "AC1009": "R11/R12", "AC1012": "R13", "AC1014": "R14",
    "AC1015": "R2000", "AC1016": "R2000i", "AC1017": "R2002", "AC1018": "R2004",
    "AC1021": "R2007", "AC1024": "R2010", "AC1027": "R2013", "AC1032": "R2018",
}


def dwg_version(data: bytes) -> Optional[str]:
    """Code de version de l'en-tête (`AC1032`…) si les octets sont un DWG plausible."""
    head = data or b""
    if len(head) < MIN_DWG_BYTES:
        return None
    if head[:2] != b"AC" or not head[2:6].isdigit():
        return None
    # Les cinq octets qui suivent le code de version sont nuls dans tout DWG réel
    # (l'octet suivant porte la version de maintenance).
    if head[6:11] != b"\x00" * 5:
        return None
    version = head[:6].decode("ascii")
    return version if version in RELEASE_BY_VERSION else None


def is_supported_version(version: Optional[str]) -> bool:
    return bool(version) and version in SUPPORTED_VERSIONS


def release_label(version: Optional[str]) -> Optional[str]:
    return RELEASE_BY_VERSION.get(version or "")


def detect_dwg_signature(data: bytes, ctx: ExtractionContext) -> Optional[str]:
    """Détecteur pour `mime.register_structured_detector` : `application/acad` ou None."""
    if not ctx.lower_name.endswith(".dwg"):
        return None
    return DWG_MIME if dwg_version(data or b"") else None
