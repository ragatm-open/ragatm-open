# backend/apps/actions/normalize.py
"""Normalisation déterministe des saisies utilisateur — **autorité backend**.

Le frontend peut avoir une implémentation miroir pour la pré-détection UX, mais la
vérité est ici. Des fixtures partagées (``tests/actions/fixtures_normalize.py``)
permettent de vérifier la cohérence front/back sans prétendre partager le code.

Couvre : casse, espaces, accents, apostrophes/guillemets, ponctuation légère,
variantes Unicode (NFKC).
"""
from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
# Apostrophes/guillemets typographiques -> ASCII simple.
_APOSTROPHES = {
    "’": "'",  # ’
    "‘": "'",  # ‘
    "ʼ": "'",  # ʼ
    "´": "'",  # ´
    "`": "'",  # `
}
_QUOTES = {
    "«": '"',  # «
    "»": '"',  # »
    "“": '"',  # “
    "”": '"',  # ”
    "„": '"',  # „
}
# Ponctuation « légère » collée qu'on sépare pour la tokenisation grossière.
_TRANS = {ord(k): v for k, v in {**_APOSTROPHES, **_QUOTES}.items()}


def strip_accents(s: str) -> str:
    """Retire les diacritiques (é -> e). Ne touche pas aux autres caractères."""
    nfd = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")


def normalize(text: str) -> str:
    """Forme canonique pour la détection déterministe.

    - NFKC (unifie les variantes de compatibilité) ;
    - apostrophes/guillemets -> ASCII ;
    - minuscule ;
    - accents retirés ;
    - espaces compressés ; trim.

    Idempotente : ``normalize(normalize(x)) == normalize(x)``.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.translate(_TRANS)
    t = t.lower()
    t = strip_accents(t)
    t = _WS_RE.sub(" ", t).strip()
    return t
