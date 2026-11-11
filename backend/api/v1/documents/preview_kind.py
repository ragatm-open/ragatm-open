# backend/api/v1/documents/preview_kind.py
from __future__ import annotations

"""Type de VISUALISEUR à employer pour un document — décision CANONIQUE, côté backend.

Pourquoi ici, et pas dans le frontend
-------------------------------------
Le viewer `/doc/<id>` chargeait tout document dans une `<iframe>` en annonçant
`Accept: application/pdf,*/*`. Pour un PDF le navigateur rend nativement ; pour un
classeur `.xlsx` il ne sait rien rendre, et la zone reste **blanche** — un écran vide
sans explication, indiscernable d'un bug de chargement.

Le correctif exige de choisir un visualiseur AVANT de télécharger quoi que ce soit.
Ce choix est posé ici plutôt que dans le frontend pour deux raisons :

1. **Une seule source de décision.** Dupliquer la table des types dans le frontend
   ferait diverger les deux au premier format ajouté. Le frontend garde un repli
   défensif (`previewKind.ts`) pour un backend plus ancien, mais il ne DÉCIDE pas.
2. **Le backend connaît le type réel.** `Document.content_type` est posé à
   l'ingestion à partir d'une détection **par contenu** (`extraction/mime.py`), pas
   d'après l'extension déclarée par le client. C'est la seule valeur digne de
   confiance ; l'extension ne sert qu'en repli quand le champ est vide (documents
   ingérés avant que le champ ne soit renseigné).

Ce module ne dit PAS si un aperçu réussira — seulement quel visualiseur tenter. Un
`spreadsheet` dont la lecture échoue retombe sur l'état explicite du visualiseur
« non pris en charge », jamais sur une zone blanche.
"""

import os

from apps.rag_core.services.ingest.extraction import mime as M

#: Le document est rendu par le lecteur PDF natif du navigateur (comportement historique).
PREVIEW_PDF = "pdf"
#: Le document est rendu par le visualiseur tabulaire HTML (lot XLSX).
PREVIEW_SPREADSHEET = "spreadsheet"
#: Le document est rendu par le visualiseur documentaire structuré (lot DOCX) :
#: titres, paragraphes, listes aplaties, tableaux préformatés.
PREVIEW_DOCUMENT = "document"
#: Le document est rendu par le visualiseur de présentation (lot PPTX) :
#: navigation par diapositive, blocs textuels, tableaux préformatés.
PREVIEW_PRESENTATION = "presentation"
#: Le document est rendu en texte brut échappé.
PREVIEW_TEXT = "text"
#: Aucun aperçu intégré : état explicite + téléchargement de l'original.
PREVIEW_UNSUPPORTED = "unsupported"

PREVIEW_KINDS = (
    PREVIEW_PDF,
    PREVIEW_SPREADSHEET,
    PREVIEW_DOCUMENT,
    PREVIEW_PRESENTATION,
    PREVIEW_TEXT,
    PREVIEW_UNSUPPORTED,
)

# Types MIME → visualiseur. Seuls les types dont le rendu est MAÎTRISÉ figurent ici ;
# tout le reste tombe volontairement en `unsupported`, qui est un état honnête.
_BY_MIME = {
    M.PDF: PREVIEW_PDF,
    M.XLSX: PREVIEW_SPREADSHEET,
    M.DOCX: PREVIEW_DOCUMENT,
    M.PPTX: PREVIEW_PRESENTATION,
    M.PLAIN: PREVIEW_TEXT,
    M.MARKDOWN: PREVIEW_TEXT,
}

# Repli par extension, utilisé UNIQUEMENT si `content_type` est vide ou inconnu.
# Ne jamais s'en servir pour CONTREDIRE un `content_type` connu : la doctrine du
# dépôt est « ne jamais faire confiance à l'extension » (cf. `extraction/mime.py`).
_BY_EXTENSION = {
    ".pdf": PREVIEW_PDF,
    ".xlsx": PREVIEW_SPREADSHEET,
    ".docx": PREVIEW_DOCUMENT,
    ".pptx": PREVIEW_PRESENTATION,
    ".txt": PREVIEW_TEXT,
    ".md": PREVIEW_TEXT,
    ".markdown": PREVIEW_TEXT,
}

#: Genres gouvernés par un drapeau : fermé, le document retombe sur l'état explicite
#: (nom, format, téléchargement) — jamais sur une zone blanche.
_FLAGGED_KINDS = {
    PREVIEW_DOCUMENT: "DOCUMENT_DOCX_PREVIEW_ENABLED",
    PREVIEW_PRESENTATION: "DOCUMENT_PPTX_PREVIEW_ENABLED",
}


def _flag_open(name: str) -> bool:
    from django.conf import settings

    v = getattr(settings, name, True)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")

# `.csv` est délibérément ABSENT des deux tables. Le format est bien ingéré, mais ses
# chunks ne portent AUCUNE localisation structurée (ni feuille, ni plage, ni ligne —
# le rail CSV passe par le chunker texte générique). Le visualiseur tabulaire ne
# saurait donc pas où surligner : l'annoncer comme pris en charge serait un mensonge.
# Il tombe en `unsupported`, avec téléchargement de l'original.


def _gate(kind: str) -> str:
    """Referme un genre dont le drapeau est fermé, vers l'état explicite."""
    flag = _FLAGGED_KINDS.get(kind)
    if flag and not _flag_open(flag):
        return PREVIEW_UNSUPPORTED
    return kind


def resolve_preview_kind(*, content_type: str = "", filename: str = "") -> str:
    """Renvoie le visualiseur à employer. Ne lève jamais.

    `content_type` prime toujours (détecté par CONTENU à l'ingestion) ; l'extension
    n'est consultée que s'il est absent ou non reconnu.
    """
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    kind = _BY_MIME.get(ctype)
    if kind:
        return _gate(kind)

    # `content_type` renseigné mais inconnu de la table : c'est un format que l'on
    # sait ne pas savoir rendre. On ne va PAS chercher un avis contraire dans
    # l'extension — ce serait exactement la confiance interdite.
    if ctype and ctype != M.OCTET:
        return PREVIEW_UNSUPPORTED

    ext = os.path.splitext((filename or "").strip())[1].lower()
    return _gate(_BY_EXTENSION.get(ext, PREVIEW_UNSUPPORTED))
