"""Types et normalisation déterministes pour le benchmark Excel `tabular_aggregation`.

Une vérité terrain d'agrégation est un **nombre calculé**. Elle n'est défendable que si
la conversion « cellule Excel → valeur canonique » est totalement déterministe et si
**toute ambiguïté est refusée** plutôt que tranchée arbitrairement.

Décisions structurantes
-----------------------

1. **`Decimal`, jamais `float`.** Une somme monétaire calculée en binaire flottant n'est
   pas reproductible (`0.1 + 0.2 != 0.3`). Les valeurs natives `float` d'openpyxl sont
   converties via ``Decimal(str(v))`` : ``str`` donne le plus court littéral qui
   *round-trip*, donc ``12345.67`` et non ``12345.670000000000072759576141834259``.
2. **Les types natifs priment.** Excel stocke les nombres et les dates de façon typée ;
   openpyxl les rend en `int` / `float` / `datetime`. L'analyse de chaîne n'est qu'un
   **repli** pour les cellules réellement textuelles — c'est là que vit l'ambiguïté.
3. **L'ambiguïté est une erreur, pas une heuristique.** ``03/04/2025`` sans locale
   explicite, ``1,234`` sans séparateur concurrent : refusés (`ambiguous_date`,
   `ambiguous_number_format`). Le benchmark préfère perdre un cas que fabriquer une
   vérité terrain fausse.
4. **Les pourcentages sont des fractions.** Excel stocke 12,5 % comme ``0.125`` + un
   format d'affichage. La valeur canonique est la **fraction** ; l'échelle d'affichage
   n'appartient pas à la vérité terrain.

Aucun LLM, aucun retrieval, aucun accès base. Module pur et testable isolément.
"""
from __future__ import annotations

import datetime as _dt
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# --- Types de cellule canoniques -------------------------------------------
TYPE_STRING = "string"
TYPE_INTEGER = "integer"
TYPE_DECIMAL = "decimal"
TYPE_PERCENTAGE = "percentage"
TYPE_DATE = "date"
TYPE_DATETIME = "datetime"
TYPE_BOOLEAN = "boolean"
TYPE_EMPTY = "empty"

CELL_TYPES = frozenset({
    TYPE_STRING, TYPE_INTEGER, TYPE_DECIMAL, TYPE_PERCENTAGE,
    TYPE_DATE, TYPE_DATETIME, TYPE_BOOLEAN, TYPE_EMPTY,
})

#: Types sur lesquels une agrégation arithmétique (somme, moyenne…) a un sens.
NUMERIC_TYPES = frozenset({TYPE_INTEGER, TYPE_DECIMAL, TYPE_PERCENTAGE})
#: Types ordonnables (min/max/comparaisons).
ORDERED_TYPES = NUMERIC_TYPES | {TYPE_DATE, TYPE_DATETIME}

# --- Codes d'erreur STABLES ------------------------------------------------
ERR_UNSUPPORTED_CELL_TYPE = "unsupported_cell_type"
ERR_AMBIGUOUS_DATE = "ambiguous_date"
ERR_AMBIGUOUS_NUMBER_FORMAT = "ambiguous_number_format"
ERR_EXCEL_ERROR_VALUE = "excel_error_value"
ERR_FORMULA_RESULT_UNAVAILABLE = "formula_result_unavailable"
ERR_INVALID_DATE = "invalid_date"
ERR_UNSUPPORTED_LOCALE = "unsupported_locale"

#: Valeurs d'erreur Excel : jamais silencieusement traitées comme du texte.
EXCEL_ERROR_VALUES = frozenset({
    "#DIV/0!", "#N/A", "#VALUE!", "#REF!", "#NAME?", "#NULL!", "#NUM!", "#SPILL!", "#CALC!",
})

#: Locales de date explicitement supportées. `None` = refus de toute date ambiguë.
LOCALE_FR = "fr"   # jour/mois/année
LOCALE_ISO = "iso"  # année-mois-jour
LOCALE_US = "us"   # mois/jour/année
_LOCALES = frozenset({LOCALE_FR, LOCALE_ISO, LOCALE_US})

#: Espaces (dont insécables fine et normale) et symboles monétaires courants.
_SPACES = "     "
_CURRENCY = "€$£¥₽¤"
_TRUE_TOKENS = frozenset({"true", "vrai", "oui", "yes", "1", "x"})
_FALSE_TOKENS = frozenset({"false", "faux", "non", "no", "0"})

_RE_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RE_ISO_DATETIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$")
_RE_SLASH_DATE = re.compile(r"^(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{2,4})$")


class TabularTypeError(ValueError):
    """Erreur de typage/normalisation. Porte un ``code`` stable et exposable."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------- #
# Nombres
# --------------------------------------------------------------------------- #
def normalize_decimal(value: Any, *, scale: int | None = None,
                      rounding: str = ROUND_HALF_UP) -> Decimal:
    """Convertit une valeur en ``Decimal`` canonique.

    ``scale`` quantifie le résultat (2 -> centimes) avec ``ROUND_HALF_UP`` par défaut :
    la convention comptable usuelle, et surtout une convention **explicite** — le
    ``ROUND_HALF_EVEN`` par défaut de Python surprendrait sur des montants.

    Un ``float`` passe par ``str`` pour éviter d'hériter du bruit binaire.
    """
    if isinstance(value, bool):
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, "booléen non convertible en décimal")
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, float):
        dec = Decimal(str(value))
    elif isinstance(value, str):
        dec = parse_number(value)
    else:
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, f"type {type(value).__name__}")
    if not dec.is_finite():
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, "valeur non finie")
    if scale is not None:
        dec = dec.quantize(Decimal(1).scaleb(-scale), rounding=rounding)
    return dec


def parse_canonical_decimal(raw: str) -> Decimal:
    """Analyse un décimal **canonique** : point décimal, aucun séparateur de milliers.

    Distinct de :func:`parse_number`, qui affronte du texte de provenance inconnue et
    refuse ``1,234`` comme ambigu. Ici la valeur est *écrite par nous* — tolérance d'un
    cas, borne d'un filtre, résultat attendu d'un dataset : sa forme est imposée, donc
    ``0.001`` n'a rien d'ambigu.
    """
    s = str(raw).strip()
    if not re.fullmatch(r"[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?", s):
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE,
                               f"{raw!r} n'est pas un décimal canonique")
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, f"{raw!r}") from e


def parse_number(raw: str) -> Decimal:
    """Analyse un nombre **textuel**, en refusant les formats ambigus.

    Gère : espaces insécables, symboles monétaires, signe en préfixe/suffixe,
    parenthèses comptables ``(1 234,56)`` = négatif, pourcentages ``12,5 %``.

    Séparateur décimal : si ``.`` et ``,`` cohabitent, **le dernier est le décimal**
    (``1.234,56`` -> 1234.56 ; ``1,234.56`` -> 1234.56). Si un seul séparateur est
    présent et suivi d'exactement 3 chiffres, le format est **ambigu** (``1,234`` vaut
    1.234 ou 1234 selon la locale) -> refus.
    """
    if not isinstance(raw, str):
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, f"type {type(raw).__name__}")
    s = raw.strip()
    if s.upper() in EXCEL_ERROR_VALUES:
        raise TabularTypeError(ERR_EXCEL_ERROR_VALUE, s)
    if not s:
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, "chaîne vide")

    percent = "%" in s
    s = s.replace("%", "")
    for ch in _SPACES + _CURRENCY:
        s = s.replace(ch, "")

    negative = False
    if s.startswith("(") and s.endswith(")"):       # convention comptable
        negative, s = True, s[1:-1]
    if s.endswith("-"):                              # signe suffixe (exports SAP/AS400)
        negative, s = True, s[:-1]
    if s.startswith("-"):
        negative, s = True, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if not s:
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, "aucun chiffre")

    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        dec_sep = "." if s.rfind(".") > s.rfind(",") else ","
        thou_sep = "," if dec_sep == "." else "."
        s = s.replace(thou_sep, "").replace(dec_sep, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        head, _, tail = s.rpartition(sep)
        if s.count(sep) > 1:
            # Séparateurs répétés -> milliers sans ambiguïté possible (1.234.567).
            s = s.replace(sep, "")
        elif len(tail) == 3 and head and head.isdigit():
            raise TabularTypeError(
                ERR_AMBIGUOUS_NUMBER_FORMAT,
                f"{raw!r} : '{sep}' suivi de 3 chiffres — décimal ou milliers ?")
        else:
            s = f"{head or '0'}.{tail}"   # ',5' -> 0.5 : le zéro implicite compte
    if not re.fullmatch(r"\d*\.?\d+", s):
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, f"{raw!r} n'est pas un nombre")

    try:
        dec = Decimal(s)
    except InvalidOperation as e:
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, f"{raw!r}") from e
    if negative:
        dec = -dec
    if percent:
        dec = dec / Decimal(100)
    return dec


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def normalize_date(value: Any, *, locale: str | None = None) -> str:
    """Normalise une date en ``YYYY-MM-DD`` (ou ``YYYY-MM-DDTHH:MM:SS`` si heure).

    Les objets ``date``/``datetime`` natifs (ce que rend openpyxl sur une cellule
    formatée date) sont convertis directement. Une **chaîne** ``jj/mm/aaaa`` n'est
    acceptée que si elle est non ambiguë (jour > 12) ou si ``locale`` est explicite.
    """
    if locale is not None and locale not in _LOCALES:
        raise TabularTypeError(ERR_UNSUPPORTED_LOCALE, str(locale))
    if isinstance(value, _dt.datetime):
        if (value.hour, value.minute, value.second) == (0, 0, 0):
            return value.date().isoformat()
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, _dt.date):
        return value.isoformat()
    if not isinstance(value, str):
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, f"type {type(value).__name__}")

    s = value.strip()
    if s.upper() in EXCEL_ERROR_VALUES:
        raise TabularTypeError(ERR_EXCEL_ERROR_VALUE, s)
    if m := _RE_ISO_DATETIME.match(s):
        y, mo, d, h, mi, sec = m.groups()
        return _build_date(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0), raw=s)
    if m := _RE_ISO_DATE.match(s):
        y, mo, d = (int(x) for x in m.groups())
        return _build_date(y, mo, d, raw=s)
    if m := _RE_SLASH_DATE.match(s):
        return _slash_date(m, locale=locale, raw=s)
    raise TabularTypeError(ERR_INVALID_DATE, f"{value!r} : format de date non reconnu")


def _slash_date(m: re.Match, *, locale: str | None, raw: str) -> str:
    a, b, c = (int(x) for x in m.groups())
    if len(m.group(1)) == 4:                       # 2025-04-03 déjà couvert, ici 2025/04/03
        return _build_date(a, b, c, raw=raw)
    year = c if c > 99 else 2000 + c if c < 70 else 1900 + c
    if locale == LOCALE_US:
        return _build_date(year, a, b, raw=raw)
    if locale in (LOCALE_FR, LOCALE_ISO):
        return _build_date(year, b, a, raw=raw)
    # Sans locale : seul un jour > 12 lève l'ambiguïté jour/mois.
    if a > 12 and b <= 12:
        return _build_date(year, b, a, raw=raw)
    if b > 12 and a <= 12:
        return _build_date(year, a, b, raw=raw)
    raise TabularTypeError(
        ERR_AMBIGUOUS_DATE,
        f"{raw!r} : jour et mois tous deux <= 12 — locale explicite requise")


def _build_date(y: int, mo: int, d: int, h: int = 0, mi: int = 0, sec: int = 0, *,
                raw: str) -> str:
    try:
        if (h, mi, sec) == (0, 0, 0):
            return _dt.date(y, mo, d).isoformat()
        return _dt.datetime(y, mo, d, h, mi, sec).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError as e:
        raise TabularTypeError(ERR_INVALID_DATE, f"{raw!r} : {e}") from e


# --------------------------------------------------------------------------- #
# Typage et normalisation d'une cellule
# --------------------------------------------------------------------------- #
def is_empty(value: Any) -> bool:
    """Cellule vide : ``None`` ou chaîne blanche. ``0`` et ``False`` NE SONT PAS vides."""
    return value is None or (isinstance(value, str) and not value.strip())


def infer_cell_type(value: Any, *, number_format: str = "") -> str:
    """Type canonique d'une valeur native openpyxl.

    ``number_format`` sert uniquement à distinguer un pourcentage (``0.125`` affiché
    ``12,5 %``) d'un décimal ordinaire : l'information n'est pas dans la valeur.
    """
    if is_empty(value):
        return TYPE_EMPTY
    if isinstance(value, str) and value.strip().upper() in EXCEL_ERROR_VALUES:
        raise TabularTypeError(ERR_EXCEL_ERROR_VALUE, value.strip())
    if isinstance(value, bool):
        return TYPE_BOOLEAN
    if isinstance(value, _dt.datetime):
        return TYPE_DATE if (value.hour, value.minute, value.second) == (0, 0, 0) else TYPE_DATETIME
    if isinstance(value, _dt.date):
        return TYPE_DATE
    if isinstance(value, int):
        return TYPE_PERCENTAGE if "%" in number_format else TYPE_INTEGER
    if isinstance(value, (float, Decimal)):
        return TYPE_PERCENTAGE if "%" in number_format else TYPE_DECIMAL
    if isinstance(value, str):
        return TYPE_STRING
    raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, f"type {type(value).__name__}")


def normalize_cell(value: Any, *, cell_type: str, locale: str | None = None) -> Any:
    """Valeur canonique d'une cellule selon son type déclaré.

    Retours : ``None`` (vide), ``bool``, ``Decimal`` (numérique/pourcentage),
    ``str`` ISO (date/datetime) ou ``str`` normalisée (texte).
    """
    if cell_type not in CELL_TYPES:
        raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, cell_type)
    if cell_type == TYPE_EMPTY or is_empty(value):
        return None
    if cell_type == TYPE_BOOLEAN:
        return _normalize_boolean(value)
    if cell_type in NUMERIC_TYPES:
        return normalize_decimal(value)
    if cell_type in (TYPE_DATE, TYPE_DATETIME):
        return normalize_date(value, locale=locale)
    return normalize_text(value)


def _normalize_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        tok = value.strip().casefold()
        if tok in _TRUE_TOKENS:
            return True
        if tok in _FALSE_TOKENS:
            return False
    raise TabularTypeError(ERR_UNSUPPORTED_CELL_TYPE, f"{value!r} n'est pas un booléen")


def normalize_text(value: Any) -> str:
    """Texte canonique : espaces (dont insécables) réduits, extrémités coupées.

    La casse et les accents sont **conservés** — ce sont des données métier, pas des
    clés de recherche. Le repli de casse ne sert qu'aux comparaisons (`fold_for_match`).
    """
    s = str(value)
    for ch in _SPACES[1:]:
        s = s.replace(ch, " ")
    return re.sub(r"\s+", " ", s).strip()


def fold_for_match(value: Any) -> str:
    """Forme repliée pour comparer deux libellés (casse et espaces neutralisés)."""
    return normalize_text(value).casefold()


def coerce_for_aggregation(value: Any, *, column_type: str,
                           locale: str | None = None) -> Any:
    """Valeur exploitable par une agrégation, ou ``None`` si la cellule est vide.

    Lève plutôt que de deviner : une cellule non numérique dans une colonne numérique
    est une **anomalie de vérité terrain**, pas une valeur à ignorer en silence. La
    décision d'ignorer ou de rejeter appartient à la politique du cas, pas au parseur.
    """
    if is_empty(value):
        return None
    if column_type in NUMERIC_TYPES:
        return normalize_decimal(value)
    if column_type in (TYPE_DATE, TYPE_DATETIME):
        return normalize_date(value, locale=locale)
    if column_type == TYPE_BOOLEAN:
        return _normalize_boolean(value)
    return normalize_text(value)
