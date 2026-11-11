# backend/apps/rag_core/services/ingest/extraction/excel_columns.py
from __future__ import annotations

"""
Fenêtres de COLONNES pour les tableaux larges. Module PUR.

Le défaut que ceci corrige n'est pas esthétique, il est fatal au retrieval.

Une feuille de capitalisation à 44 colonnes est découpée en 50 chunks de lignes. Chaque
chunk répétait les 44 intitulés — « Series A-1 », « Inputted Cash Investment »,
« Voting Thresholds / … ». Résultat : les 50 chunks devenaient **lexicalement quasi
identiques**. Une recherche sur « Cash Investment » les matchait tous, avec un `ts_rank`
**rigoureusement égal** (0,263827 pour les 51), et l'unique chunk qui portait la réponse
sortait 52e — au tirage.

Aucun classement ne peut départager une égalité parfaite. Ni RRF, ni la diversité, ni un
reranker : le problème n'était pas l'ordre, c'était que les chunks ne se distinguaient
plus. Il fallait cesser de les rendre identiques.

On découpe donc aussi **horizontalement**, en s'appuyant sur ce que le classeur dit
déjà de lui-même :

1. les **bandeaux de groupe** reconstruits par P1.1 (`Premoney Capitalization / …`) —
   les colonnes d'un même groupe restent ensemble ;
2. les **colonnes vides** entre les groupes, que l'auteur a posées comme séparateurs ;
3. à défaut, un sous-découpage par taille — borné, jamais silencieux.

La **colonne d'identification** (celle qui nomme les lignes) est reportée dans chaque
fenêtre : sans elle, « 3450656 » ne dit pas de qui il parle.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .excel_headers import HEADER_SEPARATOR

#: Intitulés qui désignent typiquement la colonne nommant les LIGNES.
_IDENTITY_HINTS = (
    "security", "securities", "series", "name", "nom", "libelle", "libellé",
    "category", "categorie", "catégorie", "type", "projet", "project", "item",
    "description", "designation", "désignation", "poste", "rubrique", "holder",
    "shareholder", "purchaser", "investor", "actionnaire", "capitalization",
)

#: Proportion de cellules TEXTUELLES au-delà de laquelle une colonne nomme des lignes.
_IDENTITY_TEXT_RATIO = 0.6


@dataclass(frozen=True)
class ColumnWindow:
    """Une fenêtre de colonnes CONTIGUË, plus les colonnes d'identification reportées."""

    #: Index (0-based, relatifs au bloc) des colonnes de la fenêtre — contigus.
    columns: tuple
    #: Index des colonnes d'identification reportées (hors fenêtre).
    identity: tuple = ()
    #: Bandeau de groupe commun, quand il existe (« Postmoney Capitalization »).
    group: Optional[str] = None
    index: int = 1
    #: La fenêtre est-elle un MORCEAU d'un groupe trop large ?
    split_from_group: bool = False

    @property
    def rendered(self) -> tuple:
        """Colonnes réellement rendues, dans l'ordre : identification puis fenêtre."""
        return tuple(self.identity) + tuple(self.columns)


def column_group(header: str) -> Optional[str]:
    """Bandeau parent d'un en-tête hiérarchique, ou ``None``.

    Réutilise la reconstruction de P1.1 — on ne bâtit pas une seconde hiérarchie qui
    pourrait diverger de celle qui est affichée et citée.
    """
    if not header or HEADER_SEPARATOR not in header:
        return None
    return header.split(HEADER_SEPARATOR, 1)[0].strip() or None


def _is_blank_column(rows: Sequence[Sequence[str]], i: int) -> bool:
    return all(not (r[i] if i < len(r) else "") for r in rows)


def _looks_numeric(v: str) -> bool:
    s = (v or "").strip().rstrip("%").replace(" ", "").replace(",", ".")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def detect_identity_columns(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> tuple:
    """Colonne(s) nommant les lignes. **Conservateur : dans le doute, rien.**

    Une identification inventée serait pire qu'aucune : elle attribuerait des chiffres à
    la mauvaise entité. On n'accepte donc que la PREMIÈRE colonne, et seulement si elle
    se comporte comme un libellé — majoritairement textuelle, non vide.
    """
    if not headers or not rows:
        return ()

    values = [(r[0] if r else "") for r in rows]
    filled = [v for v in values if v]
    if not filled:
        return ()

    textual = sum(1 for v in filled if not _looks_numeric(v))
    if textual / len(filled) >= _IDENTITY_TEXT_RATIO:
        return (0,)

    # Repli : un intitulé explicite peut racheter une colonne au contenu ambigu.
    head = (headers[0] or "").lower()
    if any(h in head for h in _IDENTITY_HINTS):
        return (0,)
    return ()


def build_column_windows(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    max_columns: int,
    identity: Sequence[int] = (),
) -> tuple:
    """Découpe les colonnes en fenêtres. Renvoie ``(windows, warnings)``.

    Les frontières viennent, dans l'ordre : d'une colonne **vide** (l'auteur l'a posée
    là), d'un **changement de bandeau** (P1.1), puis seulement d'une borne de taille.
    """
    warnings: List[str] = []
    n = len(headers)
    identity = tuple(identity)

    runs: List[List[int]] = []
    current: List[int] = []
    current_group: object = object()      # sentinelle : rien ne lui est égal

    for i in range(n):
        if i in identity:
            continue
        if _is_blank_column(rows, i):
            # Une colonne vide entre deux groupes est une FRONTIÈRE voulue par l'auteur.
            # Elle n'est pas rendue : elle ne porterait qu'un « colN: » sans valeur.
            if current:
                runs.append(current)
                current = []
            current_group = object()
            continue

        group = column_group(headers[i])
        if current and group != current_group:
            runs.append(current)
            current = []
        current_group = group
        current.append(i)

    if current:
        runs.append(current)

    windows: List[ColumnWindow] = []
    for run in runs:
        group = column_group(headers[run[0]])
        # Un groupe homogène ne l'est que si TOUTES ses colonnes le partagent.
        if group is not None and any(column_group(headers[i]) != group for i in run):
            group = None
        split = len(run) > max_columns
        if split and "excel_wide_table_group_split" not in warnings:
            warnings.append("excel_wide_table_group_split")
        for start in range(0, len(run), max_columns):
            chunk_cols = run[start:start + max_columns]
            windows.append(ColumnWindow(
                columns=tuple(chunk_cols),
                identity=identity,
                group=group,
                index=len(windows) + 1,
                split_from_group=split,
            ))

    if not identity and windows:
        warnings.append("excel_wide_table_identity_column_missing")

    return windows, warnings
