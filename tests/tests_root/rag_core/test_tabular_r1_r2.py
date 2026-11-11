"""R1 (rattachement d'un seuil numérique) et R2 (dates ISO en CSV).

Les deux réserves qui empêchaient un GO staging plein. Toutes deux produisaient des
**sur-refus** : le moteur demandait une clarification là où il pouvait répondre.

Le principe directeur de ces corrections, et donc de ces tests : élargir la couverture
UNIQUEMENT là où la colonne et le type se démontrent. Chaque fichier de test contient
donc autant de cas « doit répondre » que de cas « doit continuer à refuser » — sans les
seconds, on ne mesurerait que le relâchement.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pytest

from apps.rag_core.tabular import column_resolver as CR
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import planner as PL
from apps.rag_core.tabular import types as T


@dataclass(frozen=True)
class _Limits:
    max_filters: int = 16
    max_candidate_columns: int = 512
    column_match_min_score: float = 0.60
    column_match_min_margin: float = 0.15


LIMITS = _Limits()


def _col(name: str, pos: int, ctype: str) -> CR.ColumnCandidate:
    return CR.ColumnCandidate(uuid.uuid4(), name, T.normalize_header(name), pos, ctype)


#: Table RICHE : plusieurs colonnes numériques. C'est la configuration qui déclenchait
#: le sur-refus — avec une seule colonne numérique, il n'y a pas d'ambiguïté possible.
MULTI = [
    _col("Reference", 0, T.TYPE_IDENTIFIER),
    _col("Region", 1, T.TYPE_TEXT),
    _col("Quantite", 2, T.TYPE_INTEGER),
    _col("Prix unitaire", 3, T.TYPE_CURRENCY),
    _col("CA HT", 4, T.TYPE_CURRENCY),
    _col("CA TTC", 5, T.TYPE_CURRENCY),
    _col("Taux remise", 6, T.TYPE_PERCENTAGE),
]
MONO = [_col("Libelle", 0, T.TYPE_TEXT), _col("Montant", 1, T.TYPE_CURRENCY)]


def _filters(question: str, columns, *, target: str | None = None):
    tgt = next((c for c in columns if c.original_name == target), None)
    return PL._build_filters(question, columns, LIMITS, [], target_column=tgt)


def _named(entry, columns) -> str:
    return next(c.original_name for c in columns if str(c.column_id) == entry["column_id"])


# =========================================================================== #
# R1 — le seuil est rattaché à SA colonne
# =========================================================================== #
@pytest.mark.parametrize("question,columns,target,attendu,operateur", [
    # Le nom de la mesure précède l'opérateur — signal le plus fort.
    ("somme du CA HT superieur a 1 000", MULTI, "CA HT", "CA HT", "greater_than"),
    ("nombre de commandes dont la quantite depasse 100", MULTI, None, "Quantite", "greater_than"),
    # Le filtre porte sur une colonne DIFFÉRENTE de la mesure agrégée.
    ("total des ventes dont le prix unitaire depasse 50", MULTI, "CA HT", "Prix unitaire", "greater_than"),
    # Une seule colonne numérique : le rattachement est forcé, sans ambiguïté.
    ("somme du montant superieur a 1000", MONO, "Montant", "Montant", "greater_than"),
    # Aucune mesure nommée, mais la colonne agrégée est numérique : le seuil porte
    # sur ce qu'on somme.
    ("total des ventes superieures a 1 000", MONO, "Montant", "Montant", "greater_than"),
    # Opérateurs
    ("somme du CA HT inferieur a 500", MULTI, "CA HT", "CA HT", "less_than"),
    ("somme du CA HT entre 1000 et 5000", MULTI, "CA HT", "CA HT", "between"),
    # Robustesse d'écriture
    ("somme du CA HT superieur a -500", MULTI, "CA HT", "CA HT", "greater_than"),
    ("somme du CA HT superieur a 1 234,56", MULTI, "CA HT", "CA HT", "greater_than"),
    ("SOMME DU CA HT SUPERIEUR A 1000", MULTI, "CA HT", "CA HT", "greater_than"),
    ("somme du  ca ht   superieur   a  1000", MULTI, "CA HT", "CA HT", "greater_than"),
    ("somme du CA HT supérieur à 1000", MULTI, "CA HT", "CA HT", "greater_than"),
    # Formulations anglaises
    ("sum of CA HT greater than 1000", MULTI, "CA HT", "CA HT", "greater_than"),
    ("sum of CA HT less than 500", MULTI, "CA HT", "CA HT", "less_than"),
])
def test_le_seuil_est_rattache_a_la_bonne_colonne(question, columns, target,
                                                  attendu, operateur):
    filters = _filters(question, columns, target=target)
    assert filters, f"aucun filtre produit pour {question!r}"
    assert _named(filters[0], columns) == attendu
    assert filters[0]["operator"] == operateur


def test_colonne_monetaire_et_colonne_de_quantite_ne_se_confondent_pas():
    """Deux colonnes numériques de NATURES différentes, désignées explicitement."""
    montant = _filters("somme du CA HT superieur a 1000", MULTI, target="CA HT")
    quantite = _filters("nombre de lignes dont la quantite depasse 10", MULTI)
    assert _named(montant[0], MULTI) == "CA HT"
    assert _named(quantite[0], MULTI) == "Quantite"


def test_un_pourcentage_est_normalise_en_fraction():
    """12 % vaut 0.12 sur une colonne de pourcentage — comme Excel.

    Filtrer sur 12 ne retiendrait rien et rendrait un résultat vide présenté comme
    un fait.
    """
    filters = _filters("nombre de lignes dont le taux remise depasse 12 %", MULTI)
    assert _named(filters[0], MULTI) == "Taux remise"
    assert Decimal(filters[0]["value"]) == Decimal("0.12")


def test_les_bornes_dun_between_sont_remises_dans_lordre():
    filters = _filters("somme du CA HT entre 5000 et 1000", MULTI, target="CA HT")
    lo, hi = (Decimal(v) for v in filters[0]["values"])
    assert lo < hi


# =========================================================================== #
# R1 — ce qui doit CONTINUER à refuser
# =========================================================================== #
@pytest.mark.parametrize("question", [
    "total des lignes superieures a 1 000",
    "somme superieure a 1000",
    "nombre d enregistrements superieurs a 500",
    "total des elements superieurs a 100",
])
def test_un_seuil_non_rattachable_reste_une_clarification(question):
    """Le cas nommé par la mission. Aucun mot ne désigne une mesure : plusieurs
    colonnes numériques sont candidates, et deviner produirait un chiffre plausible
    et faux. Le code est distinct de celui d'une mesure ambiguë."""
    with pytest.raises(E.TabularClarificationRequired) as exc:
        _filters(question, MULTI)
    assert exc.value.code == E.ERR_AMBIGUOUS_FILTER_COLUMN
    assert exc.value.candidates


def test_deux_colonnes_aux_noms_proches_ne_sont_jamais_arbitrees():
    colonnes = [
        _col("Libelle", 0, T.TYPE_TEXT),
        _col("Montant net comptable", 1, T.TYPE_CURRENCY),
        _col("Montant net paye", 2, T.TYPE_CURRENCY),
    ]
    with pytest.raises(E.TabularClarificationRequired) as exc:
        _filters("somme du montant net superieur a 1000", colonnes)
    assert exc.value.code == E.ERR_AMBIGUOUS_FILTER_COLUMN
    assert len(exc.value.candidates) == 2


def test_un_seuil_sans_colonne_numerique_est_refuse():
    colonnes = [_col("Libelle", 0, T.TYPE_TEXT), _col("Statut", 1, T.TYPE_TEXT)]
    with pytest.raises(E.TabularError) as exc:
        _filters("total des lignes superieures a 1000", colonnes)
    assert exc.value.code == E.ERR_INCOMPATIBLE_FILTER_TYPE


def test_le_rattachement_est_deterministe():
    signatures = {str(_filters("somme du CA HT superieur a 1 000", MULTI, target="CA HT"))
                  for _ in range(5)}
    assert len(signatures) == 1


# =========================================================================== #
# R2 — reconnaissance ISO stricte
# =========================================================================== #
@pytest.mark.parametrize("valeur,attendu", [
    ("2026-01-15", (T.TYPE_DATE, date(2026, 1, 15))),
    ("2026-02-01 08:30:00", (T.TYPE_DATETIME, datetime(2026, 2, 1, 8, 30))),
    ("2026-02-01T08:30:00", (T.TYPE_DATETIME, datetime(2026, 2, 1, 8, 30))),
    ("2026-02-01T08:30:00Z", (T.TYPE_DATETIME, datetime(2026, 2, 1, 8, 30))),
])
def test_formats_iso_reconnus(valeur, attendu):
    assert T.parse_iso_temporal(valeur) == attendu


@pytest.mark.parametrize("valeur,attendu_utc", [
    ("2026-02-01T08:30:00+02:00", datetime(2026, 2, 1, 6, 30)),
    ("2026-02-01T08:30:00-05:00", datetime(2026, 2, 1, 13, 30)),
    ("2026-02-01T08:30:00Z", datetime(2026, 2, 1, 8, 30)),
])
def test_les_fuseaux_sont_normalises_en_utc(valeur, attendu_utc):
    """`minimum`/`maximum` ordonnent des INSTANTS.

    Sans normalisation, `08:30+02:00` et `08:30Z` se compareraient comme des chaînes
    et l'ordre serait faux de deux heures.
    """
    kind, value = T.parse_iso_temporal(valeur)
    assert kind == T.TYPE_DATETIME
    assert value == attendu_utc
    assert T.has_timezone(valeur) is True


@pytest.mark.parametrize("valeur", [
    "REF-2026-04",        # référence métier
    "Series-2025-01",     # idem
    "2026-13-01",         # mois inexistant
    "2026-02-31",         # jour inexistant dans ce mois
    "0012-AB-34",         # forme voisine, pas une date
    "2026-1-5",           # non zéro-paddé : pas de l'ISO strict
    "20260115",           # sans séparateur
    "15/01/2026",         # format local, ambigu sans locale
])
def test_les_sosies_ne_deviennent_jamais_des_dates(valeur):
    """Le risque central de R2.

    Accepter `REF-2026-04` transformerait une référence en date et produirait un tri
    silencieusement faux. La validation passe par le CALENDRIER : aucune expression
    régulière ne détecte que le 31 février n'existe pas.
    """
    assert T.parse_iso_temporal(valeur) is None


# =========================================================================== #
# R2 — politique d'inférence sur échantillon
# =========================================================================== #
@pytest.mark.parametrize("echantillon,attendu", [
    (["2026-01-15", "2026-02-20", "2026-03-25"], T.TYPE_DATE),
    (["2026-01-15T08:00:00", "2026-02-20T09:30:00", "2026-03-25T10:00:00"], T.TYPE_DATETIME),
    (["2026-01-15T08:00:00Z", "2026-02-20T09:30:00Z", "2026-03-25T10:00:00Z"], T.TYPE_DATETIME),
    # Mélange date + datetime : unifié en datetime, aucune information perdue.
    (["2026-01-15", "2026-02-20T09:30:00", "2026-03-25"], T.TYPE_DATETIME),
    # Les cellules vides ne disqualifient pas la colonne.
    (["2026-01-15", None, "2026-02-20", "", "2026-03-25"], T.TYPE_DATE),
])
def test_une_colonne_de_dates_iso_est_typee_date(echantillon, attendu):
    assert T.infer_column_type(echantillon)[0] == attendu


@pytest.mark.parametrize("echantillon,raison", [
    (["2026-01-15", "2026-13-01", "2026-03-25"], "une seule intruse suffit"),
    (["2026-13-01", "2026-02-31", "abc"], "aucune date valide"),
    (["2026-01-15", "REF-2026-04", "2026-03-25"], "mélange date et identifiant"),
    (["2026-01-15", "2026-02-20"], "échantillon sous le minimum de preuve"),
])
def test_une_colonne_non_homogene_reste_du_texte(echantillon, raison):
    """Typer en date une colonne contenant une intruse obligerait à décider quoi en
    faire — et toute réponse (ignorer, mettre à NULL) fausse silencieusement un
    `minimum`. `text` fait REFUSER les opérations temporelles, avec un code."""
    assert T.infer_column_type(echantillon)[0] == T.TYPE_TEXT, raison


def test_les_identifiants_restent_des_identifiants():
    """Non-régression : R2 ne doit pas capter ce qui revenait à l'identifiant."""
    assert T.infer_column_type(["000123", "000456", "000789"])[0] == T.TYPE_IDENTIFIER
    assert T.infer_column_type(["REF-2026-04", "REF-2026-05", "REF-2026-06"])[0] == T.TYPE_IDENTIFIER


def test_une_valeur_invalide_ne_devient_jamais_null_silencieusement():
    """Une intruse APRÈS l'échantillon (grand fichier) fait ÉCHOUER la requête.

    C'est le comportement voulu : un NULL muet retirerait une ligne d'un décompte
    ou d'un minimum sans que rien ne le signale.
    """
    with pytest.raises(T.TabularTypeError) as exc:
        T.coerce("2026-13-01", T.TYPE_DATE)
    assert exc.value.code == E.ERR_MIXED_COLUMN_TYPE


def test_la_politique_dinference_est_explicite_et_documentee():
    """Les seuils sont des constantes nommées, pas des nombres enfouis."""
    assert T.MIN_DATE_SAMPLE >= 3
    assert T.DATE_RATIO_REQUIRED == 1.0
