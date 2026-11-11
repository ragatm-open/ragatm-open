# backend/apps/rag_core/services/ingest/extraction/xml_security.py
from __future__ import annotations

"""
Durcissement XML partagé pour les documents Office (OOXML).

Menace couverte : l'expansion d'entités XML (« billion laughs », blow-up quadratique).
``xml.etree.ElementTree`` (expat) N'EST PAS immunisé : il ne résout pas les entités
EXTERNES (pas de XXE) mais il expand bien les entités INTERNES. Une bombe à entités
fait quelques kilo-octets une fois décompressée — la garde zip-bomb (qui raisonne sur
la taille décompressée déclarée) ne la voit donc PAS passer : elle n'explose qu'à
l'intérieur du parseur.

Politique : un document Office produit par une suite bureautique ne contient JAMAIS
de déclaration ``<!DOCTYPE`` ni ``<!ENTITY``. On refuse donc ces déclarations AVANT
tout parsing, par une simple recherche de motif sur les octets — aucun parseur n'est
mobilisé, donc aucune bombe ne peut détoner.

Aucune dépendance (pas de `defusedxml`) : la garde est une pré-condition, pas un
parseur alternatif.
"""

import re

from .base import RejectedFile

# `<!` suivi (éventuellement après des blancs / retours à la ligne) de DOCTYPE ou ENTITY.
# Insensible à la casse. Ne matche PAS les commentaires `<!--` ni le mot « DOCTYPE »
# apparaissant dans du texte ordinaire (il faut la syntaxe de déclaration `<!`).
_UNSAFE_XML_DECL = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)

UNSAFE_XML_CODE = "unsafe_xml"
UNSAFE_XML_MESSAGE = (
    "Le document contient une structure XML non autorisée (déclaration DOCTYPE/ENTITY) "
    "et a été refusé pour raisons de sécurité."
)


def is_unsafe_xml(xml_bytes: bytes) -> bool:
    """True si les octets contiennent une déclaration DOCTYPE/ENTITY. Ne lève jamais."""
    if not xml_bytes:
        return False
    return _UNSAFE_XML_DECL.search(xml_bytes) is not None


def reject_unsafe_xml(xml_bytes: bytes, *, part_name: str) -> None:
    """Refuse la partie XML si elle déclare un DOCTYPE ou une ENTITY.

    À appeler IMPÉRATIVEMENT avant tout ``ET.fromstring``/``ET.parse`` sur des octets
    venant d'un fichier uploadé.

    Le message utilisateur est générique : il ne divulgue NI contenu XML, NI chemin
    système, NI trace. Le nom de la partie (ex. ``word/document.xml``) — qui est une
    constante du format, pas une donnée utilisateur — est conservé dans ``e.meta``
    pour le diagnostic.
    """
    if is_unsafe_xml(xml_bytes):
        raise RejectedFile(
            UNSAFE_XML_MESSAGE,
            code=UNSAFE_XML_CODE,
            meta={"unsafe_xml_part": str(part_name or "")},
        )


#: Profondeur d'imbrication au-delà de laquelle un XML Office est tenu pour hostile.
#: Un document réel reste très en dessous : corps > tableau > ligne > cellule >
#: paragraphe > run > texte plafonne autour de 10-15, et les tableaux imbriqués
#: n'ajoutent que quelques niveaux. 200 laisse une marge confortable tout en
#: fermant la porte à un XML conçu pour épuiser la pile du parseur.
MAX_XML_DEPTH = 200

DEEP_XML_CODE = "xml_too_deep"
DEEP_XML_MESSAGE = (
    "Le document contient une structure XML anormalement imbriquée "
    "et a été refusé pour raisons de sécurité."
)


def reject_deeply_nested_xml(
    xml_bytes: bytes, *, part_name: str, max_depth: int = MAX_XML_DEPTH
) -> None:
    """Refuse un XML dont l'imbrication dépasse `max_depth`.

    La garde anti zip-bomb ne voit pas ce cas : un XML de quelques kilo-octets peut
    porter des dizaines de milliers de niveaux et faire exploser la pile d'expat
    pendant la construction de l'arbre — donc **après** les gardes de taille.

    On utilise `XMLPullParser` plutôt qu'un balayage d'octets : un `>` non échappé
    est légal dans une valeur d'attribut, et un scan naïf compterait faux. Les
    éléments sont libérés au fil de l'eau (`clear()`), l'empreinte reste constante,
    et l'analyse s'arrête au **premier** dépassement plutôt qu'à la fin du document.

    Un XML malformé n'est pas rejeté ici : ce n'est pas le rôle de cette garde, et
    l'appelant lèvera son erreur de parsing habituelle.
    """
    from xml.etree import ElementTree as ET

    parser = ET.XMLPullParser(events=("start", "end"))
    depth = 0
    try:
        parser.feed(xml_bytes)
        for event, elem in parser.read_events():
            if event == "start":
                depth += 1
                if depth > max_depth:
                    raise RejectedFile(
                        DEEP_XML_MESSAGE,
                        code=DEEP_XML_CODE,
                        meta={"deep_xml_part": str(part_name or "")},
                    )
            else:
                depth -= 1
                elem.clear()          # empreinte constante, quel que soit le volume
    except RejectedFile:
        raise
    except ET.ParseError:
        # Malformé : laissé à l'extracteur, qui a son propre code d'erreur.
        return
