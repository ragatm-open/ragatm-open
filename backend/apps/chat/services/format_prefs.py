"""Préférences de format explicitement demandées par l'utilisateur.

Détection déterministe et bornée : aucun appel LLM supplémentaire.
Le seul objectif est de savoir si l'utilisateur a explicitement demandé un
tableau, afin d'adapter la consigne de format envoyée au modèle.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["wants_table_format", "escape_table_cell"]


def _normalize(text: str) -> str:
    """Minuscules sans accents, pour que 'Tableau' et 'tableau' se valent."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return stripped.lower()


# FR : "tableau" est un signal fort et non ambigu dans ce contexte.
# EN : le mot "table" seul est ambigu (« table des matières », « table ronde ») ;
#      on n'accepte que des tournures qui désignent explicitement un format.
_TABLE_PATTERNS = (
    re.compile(r"\btableaux?\b"),
    re.compile(r"\bas an? tables?\b"),
    re.compile(r"\bin an? tables?\b"),
    re.compile(r"\btables? format\b"),
    re.compile(r"\btabular\b"),
)


def wants_table_format(question: str) -> bool:
    """Vrai si l'utilisateur demande explicitement un rendu tabulaire.

    Volontairement conservateur : on préfère un faux négatif (réponse au format
    historique) à un faux positif (tableau imposé alors qu'il n'est pas voulu).
    """
    if not question:
        return False
    normalized = _normalize(question)
    return any(pattern.search(normalized) for pattern in _TABLE_PATTERNS)


def escape_table_cell(value: str) -> str:
    """Échappe les pipes d'une valeur destinée à une cellule Markdown.

    Sans cela, un '|' présent dans un nom de fichier ou un extrait ajoute une
    colonne fantôme et décale toute la ligne.
    """
    if not value:
        return ""
    return str(value).replace("|", r"\|")
