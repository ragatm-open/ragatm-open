"""Typage et normalisation des cellules — production.

Écrit pour la production, **indépendamment** de `benchmark/tabular_types.py`. Ce n'est
pas une duplication par négligence : si le moteur et sa vérité terrain partageaient le
même normaliseur, un défaut de conversion serait invisible au benchmark, qui le
reproduirait des deux côtés. L'indépendance est ce qui rend la mesure valable.

Trois règles gouvernent le module :

1. **Un identifiant n'est jamais un nombre.** `001245` reste `"001245"`. C'est la règle
   qui décide seule du sort des références, codes postaux, SIRET et numéros de dossier.
2. **Jamais de `float` pour une valeur métier.** `Decimal` de bout en bout ; un `float`
   ne sert qu'à traverser une bibliothèque qui l'impose, et repasse par `str()`.
3. **Une valeur invalide n'est jamais convertie en zéro.** Elle est écartée avec un
   avertissement, ou elle fait échouer la colonne. Zéro est une valeur, pas un aveu
   d'ignorance.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.rag_core.tabular import errors as E

# --------------------------------------------------------------------------- #
# Types du catalogue
# --------------------------------------------------------------------------- #
TYPE_TEXT = "text"
TYPE_IDENTIFIER = "identifier"
TYPE_INTEGER = "integer"
TYPE_DECIMAL = "decimal"
TYPE_BOOLEAN = "boolean"
TYPE_DATE = "date"
TYPE_DATETIME = "datetime"
TYPE_PERCENTAGE = "percentage"
TYPE_CURRENCY = "currency"

ALL_TYPES = frozenset({
    TYPE_TEXT, TYPE_IDENTIFIER, TYPE_INTEGER, TYPE_DECIMAL, TYPE_BOOLEAN,
    TYPE_DATE, TYPE_DATETIME, TYPE_PERCENTAGE, TYPE_CURRENCY,
})

#: Types sur lesquels une somme ou une moyenne a un sens.
NUMERIC_TYPES = frozenset({TYPE_INTEGER, TYPE_DECIMAL, TYPE_PERCENTAGE, TYPE_CURRENCY})
#: Types ordonnables (min, max, tri, comparaisons).
ORDERED_TYPES = NUMERIC_TYPES | {TYPE_DATE, TYPE_DATETIME}
#: Types purement textuels.
TEXT_TYPES = frozenset({TYPE_TEXT, TYPE_IDENTIFIER})

#: Symboles monétaires reconnus -> code ISO. L'unité est CONSERVÉE : un montant sans
#: devise est un nombre sans signification métier.
CURRENCY_SYMBOLS = {
    "€": "EUR", "EUR": "EUR",
    "$": "USD", "USD": "USD",
    "£": "GBP", "GBP": "GBP",
    "¥": "JPY", "JPY": "JPY",
    "CHF": "CHF",
}

_ERROR_VALUES = frozenset({
    "#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NULL!", "#NUM!", "#N/A", "#GETTING_DATA",
})

# --------------------------------------------------------------------------- #
# Dates ISO en TEXTE (R2)
# --------------------------------------------------------------------------- #
# Dans un CSV, tout est du texte : une colonne de dates ISO arrivait donc en `text`,
# ce qui interdisait `minimum`, `maximum`, un tri chronologique et un `between` de
# période. Le moteur refusait — un sur-refus, pas un résultat faux, mais un défaut.
#
# La reconnaissance est STRICTE, et c'est le point délicat. Beaucoup d'identifiants
# métier ressemblent à des dates : `REF-2026-04`, `Series-2025-01`. Les accepter
# transformerait une référence en date et produirait un tri silencieusement faux.
# D'où : ancrage complet de la chaîne, et validation par le CALENDRIER (`date()`
# refuse `2026-13-01` et `2026-02-31`, qu'aucune expression régulière ne détecte).
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d{1,6})?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})?$")

#: Nombre minimal de valeurs non vides pour OSER typer une colonne en date. Sous ce
#: seuil, il n'y a pas de preuve : deux cellules qui ressemblent à des dates peuvent
#: être un accident. On reste en texte.
MIN_DATE_SAMPLE = 3

#: Proportion EXIGÉE de valeurs non vides valides. 1.0, délibérément : une seule
#: valeur non conforme dans l'échantillon signifie que la colonne n'est pas
#: homogène. La typer en date obligerait ensuite à décider quoi faire de
#: l'intruse — et toute réponse à cette question (ignorer, mettre à NULL) fausse
#: silencieusement un `minimum` ou un décompte. On préfère `text`, qui fait REFUSER
#: les opérations temporelles avec un code explicite.
#:
#: Une valeur invalide APRÈS l'échantillon (grand fichier) n'est pas ignorée non
#: plus : `coerce` lève, la requête échoue avec un code — jamais un NULL muet.
DATE_RATIO_REQUIRED = 1.0


def parse_iso_temporal(value: Any):
    """Reconnaît une date ou un datetime ISO STRICT. `None` si ce n'en est pas un.

    Renvoie `(type, valeur)`. Les datetimes porteurs d'un fuseau sont normalisés en
    **UTC** : `minimum`/`maximum` ordonnent alors des instants comparables. Sans
    normalisation, `2026-01-01T00:00+02:00` et `2026-01-01T00:00Z` se compareraient
    comme des chaînes et l'ordre serait faux d'une heure.
    """
    if isinstance(value, datetime):
        return TYPE_DATETIME, value
    if isinstance(value, date):
        return TYPE_DATE, value
    if not isinstance(value, str):
        return None

    s = value.strip()
    if not s:
        return None

    m = _ISO_DATE_RE.match(s)
    if m:
        try:
            return TYPE_DATE, date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            # `2026-13-01`, `2026-02-31` : la forme est bonne, la date n'existe pas.
            return None

    m = _ISO_DATETIME_RE.match(s)
    if not m:
        return None
    try:
        base = datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
    except ValueError:
        return None

    tz = m.group("tz")
    if tz:
        if tz == "Z":
            offset = timedelta(0)
        else:
            sign = 1 if tz[0] == "+" else -1
            offset = sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))
        # Normalisation UTC : on RETIRE le décalage pour obtenir l'instant.
        base = base - offset
    return TYPE_DATETIME, base


def has_timezone(value: Any) -> bool:
    """Vrai si la valeur textuelle portait un fuseau explicite (`Z` ou `±HH:MM`)."""
    if not isinstance(value, str):
        return False
    m = _ISO_DATETIME_RE.match(value.strip())
    return bool(m and m.group("tz"))


def infer_temporal_type(sample: list) -> tuple[str, bool] | None:
    """`(type, fuseaux_presents)` si l'échantillon est temporel, sinon `None`.

    Politique explicite, testable, et volontairement stricte — cf. `MIN_DATE_SAMPLE`
    et `DATE_RATIO_REQUIRED`.
    """
    non_empty = [v for v in sample if not is_empty(v)]
    if len(non_empty) < MIN_DATE_SAMPLE:
        return None

    kinds: set[str] = set()
    tz_seen = False
    valid = 0
    for v in non_empty:
        parsed = parse_iso_temporal(v)
        if parsed is None:
            # Une seule intruse suffit à disqualifier la colonne (cf. constante).
            return None
        kinds.add(parsed[0])
        tz_seen = tz_seen or has_timezone(v)
        valid += 1

    if valid / len(non_empty) < DATE_RATIO_REQUIRED:
        return None
    # Un mélange date + datetime est unifié en datetime : c'est le type qui porte
    # l'information la plus fine, aucune valeur n'est perdue.
    return (TYPE_DATETIME if TYPE_DATETIME in kinds else TYPE_DATE), tz_seen


#: Une valeur qui « ressemble à un nombre » mais dont le format est ambigu. `1,234`
#: vaut 1.234 ou 1234 selon la locale : deviner serait un faux résultat silencieux.
_AMBIGUOUS_SINGLE_SEPARATOR = re.compile(r"^-?\d{1,3}[.,]\d{3}$")


class TabularTypeError(E.TabularError):
    """Conversion impossible. Porte un code stable."""


def normalize_header(header: str) -> str:
    """Nom canonique d'une colonne : minuscules, sans accent, séparateurs unifiés."""
    s = (header or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def is_empty(value: Any) -> bool:
    """Vide = None ou chaîne blanche. `0` et `False` NE SONT PAS vides."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def is_excel_error(value: Any) -> bool:
    return isinstance(value, str) and value.strip().upper() in _ERROR_VALUES


def looks_like_identifier(value: Any) -> bool:
    """Un identifiant : chiffres avec zéro initial significatif, ou code alphanumérique.

    C'est la garde qui empêche `001245` de devenir `1245`. Le zéro initial n'est pas
    une décoration : il fait partie de la valeur, et le perdre casse silencieusement
    toute jointure ou recherche ultérieure.
    """
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    if s.isdigit() and len(s) > 1 and s[0] == "0":
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-/]{1,63}", s)
                and re.search(r"[A-Za-z]", s) and re.search(r"\d", s))


def parse_decimal(value: Any, *, allow_ambiguous: bool = False) -> Decimal:
    """Convertit en `Decimal`. Lève plutôt que de deviner.

    Gère les deux conventions (`1 234,56` et `1,234.56`) en retenant le **dernier**
    séparateur comme séparateur décimal. Un nombre à séparateur unique suivi de trois
    chiffres (`1,234`) est ambigu : refusé, sauf demande explicite.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TabularTypeError(E.ERR_MIXED_COLUMN_TYPE, "un booléen n'est pas un nombre")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if not isinstance(value, str):
        raise TabularTypeError(
            E.ERR_MIXED_COLUMN_TYPE, f"valeur non numérique : {type(value).__name__}")

    s = value.strip()
    if not s:
        raise TabularTypeError(E.ERR_MIXED_COLUMN_TYPE, "cellule vide")
    if is_excel_error(s):
        raise TabularTypeError(E.ERR_EXCEL_ERROR_VALUE, f"erreur Excel : {s}")

    negative = False
    # Convention comptable : (1 234,56) désigne un montant négatif.
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.endswith("-"):
        negative, s = True, s[:-1].strip()

    for sym in CURRENCY_SYMBOLS:
        s = s.replace(sym, "")
    percent = s.rstrip().endswith("%")
    s = s.rstrip().rstrip("%").strip()
    s = s.replace(" ", "").replace(" ", "").replace(" ", "")

    if not allow_ambiguous and _AMBIGUOUS_SINGLE_SEPARATOR.fullmatch(s):
        raise TabularTypeError(
            E.ERR_MIXED_COLUMN_TYPE,
            f"format numérique ambigu : {value!r} vaut 1.234 ou 1234 selon la locale")

    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else \
            s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")

    if s.startswith("-"):
        negative, s = True, s[1:]

    try:
        out = Decimal(s)
    except InvalidOperation as exc:
        raise TabularTypeError(
            E.ERR_MIXED_COLUMN_TYPE, f"valeur non numérique : {value!r}") from exc

    if percent:
        # Excel stocke 12,5 % comme 0.125. On aligne : l'affichage n'est pas la valeur.
        out = out / Decimal(100)
    return -out if negative else out


def detect_currency(value: Any, number_format: str = "") -> str:
    """Code ISO de la devise, depuis la valeur ou le format de nombre. `""` si aucune."""
    haystack = f"{value if isinstance(value, str) else ''} {number_format or ''}"
    for sym, iso in CURRENCY_SYMBOLS.items():
        if sym in haystack:
            return iso
    return ""


def parse_date(value: Any) -> date:
    """Convertit en `date`. Un format ambigu (`03/04/2025`) est refusé sans locale."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TabularTypeError(E.ERR_MIXED_COLUMN_TYPE, "valeur non convertible en date")
    s = value.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError as exc:
            # `2026-13-01` a la bonne FORME mais n'existe pas. Sans ce garde, un
            # ValueError nu remontait jusqu'à la vue et sortait en 500 au lieu d'un
            # refus typé.
            raise TabularTypeError(
                E.ERR_MIXED_COLUMN_TYPE, f"date inexistante : {s!r}") from exc
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if d > 12 and mo <= 12:
            try:
                return date(int(m.group(3)), mo, d)
            except ValueError as exc:
                raise TabularTypeError(
                    E.ERR_MIXED_COLUMN_TYPE, f"date inexistante : {s!r}") from exc
        raise TabularTypeError(
            E.ERR_MIXED_COLUMN_TYPE,
            f"date ambiguë : {s!r} (jour/mois indiscernables sans locale explicite)")
    raise TabularTypeError(E.ERR_MIXED_COLUMN_TYPE, f"date non reconnue : {s!r}")


def parse_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "vrai", "1", "oui", "yes"):
        return True
    if s in ("false", "faux", "0", "non", "no"):
        return False
    raise TabularTypeError(E.ERR_MIXED_COLUMN_TYPE, f"booléen non reconnu : {value!r}")


def coerce(value: Any, column_type: str):
    """Convertit une cellule vers le type déclaré de sa colonne.

    Renvoie `None` pour une cellule vide (le NULL SQL). **Lève** pour une valeur non
    convertible : le moteur ne transforme jamais une valeur invalide en zéro, en chaîne
    vide, ou en date par défaut.
    """
    if is_empty(value):
        return None
    if is_excel_error(value):
        raise TabularTypeError(
            E.ERR_EXCEL_ERROR_VALUE, f"erreur Excel dans une cellule : {value}")

    if column_type in (TYPE_TEXT, TYPE_IDENTIFIER):
        return str(value).strip()
    if column_type == TYPE_BOOLEAN:
        return parse_boolean(value)
    if column_type == TYPE_INTEGER:
        d = parse_decimal(value)
        if d != d.to_integral_value():
            raise TabularTypeError(
                E.ERR_MIXED_COLUMN_TYPE, f"valeur décimale dans une colonne entière : {value!r}")
        return int(d)
    if column_type in (TYPE_DECIMAL, TYPE_PERCENTAGE, TYPE_CURRENCY):
        return parse_decimal(value)
    if column_type in (TYPE_DATE, TYPE_DATETIME):
        parsed = parse_iso_temporal(value)
        if parsed is None:
            # Ni un NULL, ni un zéro : une valeur non conforme dans une colonne
            # temporelle est une anomalie de données. La taire fausserait un
            # `minimum` ou un décompte sans que rien ne le signale.
            try:
                fallback = parse_date(value)          # formats non-ISO explicites
            except TabularTypeError:
                raise TabularTypeError(
                    E.ERR_MIXED_COLUMN_TYPE,
                    f"valeur non conforme dans une colonne temporelle : {value!r}"
                ) from None
            parsed = (TYPE_DATE, fallback)
        kind, out = parsed
        if column_type == TYPE_DATE:
            return out.date() if isinstance(out, datetime) else out
        return out if isinstance(out, datetime) else datetime.combine(
            out, datetime.min.time())
    raise TabularTypeError(E.ERR_INCOMPATIBLE_COLUMN_TYPE, f"type inconnu : {column_type!r}")


def infer_column_type(values: list[Any], *, number_format: str = "") -> tuple[str, str]:
    """Déduit `(type, devise)` d'un échantillon de cellules non vides.

    Un mélange entier/décimal est unifié en `decimal`. Tout autre mélange rend la
    colonne inexploitable : `text`, ce qui interdira d'y sommer quoi que ce soit —
    plutôt qu'une conversion partielle qui laisserait tomber des lignes en silence.
    """
    sample = [v for v in values if not is_empty(v)]
    if not sample:
        return TYPE_TEXT, ""

    currency = ""
    for v in sample:
        currency = detect_currency(v, number_format)
        if currency:
            break

    if all(isinstance(v, bool) for v in sample):
        return TYPE_BOOLEAN, ""
    if all(isinstance(v, datetime) for v in sample):
        return TYPE_DATETIME, ""
    if all(isinstance(v, (date, datetime)) for v in sample):
        return TYPE_DATE, ""

    # Le temporel se teste AVANT l'identifiant : `2026-01-15` satisfait la forme
    # d'un identifiant alphanumérique, et serait donc capté par la mauvaise règle.
    # C'est exactement le défaut R2 — une colonne de dates CSV typée `text`.
    temporal = infer_temporal_type(sample)
    if temporal is not None:
        return temporal[0], ""

    # L'identifiant se teste AVANT le numérique : `001245` est numériquement
    # convertible, et c'est précisément le piège.
    if all(looks_like_identifier(v) for v in sample):
        return TYPE_IDENTIFIER, ""

    kinds = set()
    for v in sample:
        try:
            d = parse_decimal(v)
        except TabularTypeError:
            kinds.add("text")
            break
        kinds.add("integer" if d == d.to_integral_value() else "decimal")

    if kinds == {"integer"}:
        return (TYPE_CURRENCY if currency else TYPE_INTEGER), currency
    if kinds and kinds <= {"integer", "decimal"}:
        if currency:
            return TYPE_CURRENCY, currency
        if "%" in (number_format or ""):
            return TYPE_PERCENTAGE, ""
        return TYPE_DECIMAL, ""
    return TYPE_TEXT, ""
