# backend/apps/rag_core/services/ingest/extraction/excel_normalize.py
from __future__ import annotations

"""
Normalisation d'une cellule Excel en texte indexable. Module PUR (aucun Django).

Principe directeur : **fidélité d'abord, jamais d'invention**.

- On n'invente aucune valeur : une cellule vide reste vide, une formule sans cache
  reste vide (JAMAIS `0`), une erreur Excel reste l'erreur.
- On n'applique aucune locale implicite : `1234.56` s'indexe `1234.56`, pas
  `1 234,56` (le FTS et le LLM travaillent mieux sur du brut, et l'ambiguïté
  virgule/point est un piège classique).
- La SEULE exception assumée est le **pourcentage** : Excel stocke `0.1` et affiche
  `10 %`. Indexer `0.1` ferait répondre « 0,1 » à « quel est le taux ? ». On applique
  donc le `number_format` déclaré par le document — ce n'est pas une conversion
  arbitraire, c'est honorer la sémantique d'affichage que le classeur porte lui-même.

Dates et heures — ce que le spike E1 a mesuré :

| `number_format` | valeur openpyxl | rendu |
|---|---|---|
| `[h]:mm:ss` (VRAIE durée) | `timedelta` | `60:00:00` ✅ |
| `h:mm:ss` + valeur ≥ 1 jour | `datetime(1900,1,2,12:00)` | `12:00:00` (+ warning) |
| date | `datetime` | `2026-07-13` |
| date+heure | `datetime` | `2026-07-13T14:30:00` |

Le second cas est le piège : openpyxl place la valeur sur l'époque 1900 et l'on
obtiendrait `1900-01-02T12:00:00`, une **fausse date**. Excel, lui, n'affiche que
`12:00:00` pour ce format. On rend donc la partie horaire — ce qui est à la fois
fidèle à l'affichage ET débarrassé de la fausse date.
"""

import re
from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple

# Préfixes de formula-injection (mêmes règles que l'extracteur CSV : une cellule
# TEXTE commençant par `=`/`+`/`-`/`@` est neutralisée pour toute restitution).
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_FORMULA_CONTROL = ("\t", "\r")

# Caractères de contrôle interdits (hors \t \n \r, retirés séparément).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Un `number_format` peut contenir du texte littéral entre guillemets (ex. `"€"`)
# ou échappé (`\%`). On retire ces littéraux avant de chercher les jetons.
_NF_LITERAL = re.compile(r'"[^"]*"|\\.')
# Jetons de durée : entre crochets -> `[h]`, `[mm]`, `[ss]`.
_NF_ELAPSED = re.compile(r"\[(h+|m+|s+)\]", re.IGNORECASE)
# Jetons de date (hors crochets).
_NF_DATE_TOKEN = re.compile(r"[ymd]", re.IGNORECASE)
_NF_TIME_TOKEN = re.compile(r"[hs]|AM/PM|A/P", re.IGNORECASE)

# ⚠️ Piège d'Excel : `m` signifie MOIS **ou** MINUTES selon le contexte. Dans
# `h:mm:ss`, les `mm` sont des minutes ; dans `yyyy-mm-dd`, c'est un mois. La règle
# Excel : `m` vaut minutes s'il suit immédiatement une heure (`h`) ou précède
# immédiatement des secondes (`s`). On retire ces `m`-minutes AVANT de chercher un
# jeton de date, sinon tout format horaire passerait pour un format de date.
_NF_MINUTES_AFTER_H = re.compile(r"(h+\s*[:.]?\s*)(m+)", re.IGNORECASE)
_NF_MINUTES_BEFORE_S = re.compile(r"(m+)(\s*[:.]?\s*s+)", re.IGNORECASE)


def _strip_minutes(body: str) -> str:
    body = _NF_MINUTES_AFTER_H.sub(r"\1", body)
    return _NF_MINUTES_BEFORE_S.sub(r"\2", body)


class CellFlags:
    """Drapeaux levés par la normalisation d'UNE cellule (agrégés par l'extracteur)."""

    TRUNCATED = "truncated"
    ERROR_CELL = "error_cell"
    NEUTRALIZED = "neutralized"
    DURATION_UNSUPPORTED = "duration_unsupported"


def _nf_body(number_format: str) -> str:
    """Section utile d'un `number_format`, littéraux retirés.

    Un format Excel peut porter jusqu'à 4 sections (positif;négatif;zéro;texte).
    On ne regarde que la première : c'est elle qui décrit le type.
    """
    nf = (number_format or "").split(";")[0]
    return _NF_LITERAL.sub("", nf)


def is_percent_format(number_format: str) -> bool:
    return "%" in _nf_body(number_format)


def is_elapsed_format(number_format: str) -> bool:
    """`[h]:mm:ss` — une DURÉE, pas une heure du jour."""
    return bool(_NF_ELAPSED.search(_nf_body(number_format)))


def is_time_only_format(number_format: str) -> bool:
    """Format horaire SANS composante de date (`h:mm:ss`, `hh:mm`).

    Les `m` de minutes sont retirés d'abord : sans cela, `h:mm:ss` contiendrait un
    « jeton de date » (`m`) et serait pris pour une date.
    """
    body = _strip_minutes(_NF_ELAPSED.sub("", _nf_body(number_format)))
    return bool(_NF_TIME_TOKEN.search(body)) and not _NF_DATE_TOKEN.search(body)


def percent_decimals(number_format: str) -> int:
    """Nombre de décimales déclarées : `0%` -> 0, `0.00%` -> 2."""
    body = _nf_body(number_format)
    m = re.search(r"\.(0+)\s*%", body)
    return len(m.group(1)) if m else 0


def currency_symbol(number_format: str) -> Optional[str]:
    """Devise DÉTECTÉE (méta seulement — le nombre est indexé brut)."""
    nf = number_format or ""
    for sym in ("€", "$", "£", "¥", "CHF", "USD", "EUR"):
        if sym in nf:
            return sym
    return None


def is_dangerous_text(value: str) -> bool:
    if not value:
        return False
    if value.lstrip()[:1] in _FORMULA_PREFIXES:
        return True
    return value[:1] in _FORMULA_CONTROL


def _clean_text(s: str) -> str:
    """Retire les caractères de contrôle et normalise les blancs (jamais d'exception)."""
    s = _CONTROL_CHARS.sub("", s)
    return " ".join(s.split())


def _format_timedelta(td: timedelta) -> str:
    """`timedelta` -> `HH:MM:SS`, heures pouvant dépasser 24 (`60:00:00`)."""
    total = int(round(td.total_seconds()))
    sign = "-" if total < 0 else ""
    total = abs(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def _round_to_second(dt):
    """Les heures Excel sont des flottants : `14:30` s'écrit `0.604166666…`.

    Sans arrondi, on indexerait `14:29:59` pour un `14:30` affiché (mesuré en E1).
    """
    if dt.microsecond >= 500_000:
        dt = dt + timedelta(seconds=1)
    return dt.replace(microsecond=0)


def _format_number(v) -> str:
    """Nombre -> texte, sans locale, sans séparateur de milliers.

    `bool` est traité en amont (en Python, `True` est un `int`).
    """
    if isinstance(v, int):
        return str(v)                      # entiers exacts, y compris > 2^53
    f = float(v)
    if f.is_integer() and abs(f) < 1e16:
        return str(int(f))                 # 250000.0 -> "250000"
    return repr(f)                         # repr : plus court aller-retour fidèle


def render_cell(
    value,
    *,
    number_format: str = "",
    data_type: str = "",
    max_chars: int = 2000,
) -> Tuple[str, list]:
    """Normalise UNE cellule. Renvoie ``(texte, drapeaux)``.

    ``data_type`` est celui d'openpyxl : ``e`` (erreur), ``s`` (texte), ``b``
    (booléen), ``n`` (nombre), ``d`` (date).
    """
    flags: list = []

    # 1) ERREUR Excel — la vérité du document. Jamais None, jamais 0.
    if data_type == "e":
        flags.append(CellFlags.ERROR_CELL)
        return str(value), flags

    # 2) Vide (cellule absente OU formule sans valeur en cache — l'extracteur
    #    distingue les deux via le scan XML, pas ici).
    if value is None:
        return "", flags

    # 3) Booléen AVANT nombre (bool est un int en Python).
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), flags

    # 4) Durée VRAIE (`[h]:mm:ss`) : openpyxl rend un timedelta. Fidèle.
    if isinstance(value, timedelta):
        return _format_timedelta(value), flags

    # 5) Dates / heures.
    if isinstance(value, datetime):
        if is_time_only_format(number_format):
            # Format horaire pur. Si la valeur porte AUSSI des jours (> 1 jour depuis
            # l'époque 1900), openpyxl produit une DATE artificielle : on ne l'indexe
            # PAS (ce serait une fausse date). Excel n'affiche de toute façon que
            # l'heure pour ce format — on est donc fidèle à ce que voit l'utilisateur,
            # mais on signale que la composante « jours » n'est pas représentable.
            if (value.year, value.month, value.day) != (1900, 1, 1):
                flags.append(CellFlags.DURATION_UNSUPPORTED)
            return _round_to_second(value).time().isoformat(timespec="seconds"), flags
        dt = _round_to_second(value)
        if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
            return dt.date().isoformat(), flags
        return dt.isoformat(sep="T", timespec="seconds"), flags

    if isinstance(value, date):
        return value.isoformat(), flags

    if isinstance(value, time):
        return _round_to_second(
            datetime(1900, 1, 1, value.hour, value.minute, value.second, value.microsecond)
        ).time().isoformat(timespec="seconds"), flags

    # 6) Nombres.
    if isinstance(value, (int, float)):
        if is_percent_format(number_format):
            # 0.1 + `0%` -> « 10% ». On honore le format déclaré par le document.
            pct = value * 100
            d = percent_decimals(number_format)
            return (f"{pct:.{d}f}%" if d else f"{_format_number(round(pct, 10))}%"), flags
        return _format_number(value), flags

    # 7) Texte — AUCUNE coercition ("00123" reste "00123", "10%" reste du texte).
    text = _clean_text(str(value))
    if is_dangerous_text(text):
        text = "'" + text
        flags.append(CellFlags.NEUTRALIZED)
    if max_chars and len(text) > max_chars:
        text = text[: max(0, max_chars - 1)] + "…"
        flags.append(CellFlags.TRUNCATED)
    return text, flags
