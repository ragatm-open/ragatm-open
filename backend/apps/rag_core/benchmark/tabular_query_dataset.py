"""Corpus synthétique et dataset de cas pour le benchmark du moteur tabulaire.

Ce module **fabrique la vérité terrain**, jamais le moteur. C'est la contrainte
fondatrice du benchmark et elle mérite d'être énoncée sans détour :

    ce fichier n'importe RIEN de `apps.rag_core.tabular` ni de
    `apps.rag_core.benchmark.tabular_reference_executor`.

La raison est qu'une vérité terrain calculée par le composant évalué — ou par un
exécuteur de référence qui partagerait ses conversions — ne mesure plus rien : un
défaut de normalisation présent des deux côtés serait reproduit à l'identique et
resterait invisible. Ici, les lignes sont construites en Python natif (``Decimal``,
``int``, ``date``, ``str``) et les valeurs attendues sont calculées par des sommes,
des tris et des comptages Python écrits dans ce fichier. Le seul point commun avec le
moteur est le **fichier source** : c'est exactement ce qu'on veut comparer.

Deux propriétés sont exigées du corpus :

1. **Reproductibilité stricte.** Graine fixe, aucun appel à l'horloge, aucune valeur
   dérivée d'un UUID. Les octets produits doivent être identiques d'une exécution à
   l'autre et d'une machine à l'autre — d'où la renormalisation de l'archive ZIP des
   classeurs (`_deterministic_xlsx`), openpyxl y inscrivant sinon l'heure courante.
2. **Représentativité des pièges.** Zéros initiaux, devise, valeurs nulles, montants
   négatifs, colonnes quasi homonymes, formule sans valeur en cache, feuille d'archive.
   Un corpus « propre » mesurerait le cas facile et rien d'autre.

Le dataset porte autant de **cas négatifs** que de cas positifs : un refus explicite
attendu (`clarification_required`, `unsupported`, `error`) est un résultat correct au
même titre qu'un nombre juste. C'est la propriété que le moteur revendique ; c'est donc
celle qu'il faut mesurer.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

#: Graine unique du corpus. Toute valeur « aléatoire » du dataset en descend, et rien
#: d'autre : ni `time`, ni `uuid4`, ni `random` global.
SEED = 20260718

#: Horodatage figé inscrit dans les métadonnées des classeurs. Une date réelle rendrait
#: les octets dépendants du moment de génération, donc le corpus non reproductible.
FROZEN_TIMESTAMP = datetime(2026, 1, 1, 0, 0, 0)

#: Date de référence des colonnes de dates. Fixe, pour la même raison.
EPOCH = date(2026, 1, 1)

#: Statuts possibles des cas attendus, alignés sur `errors.TabularError.status`.
STATUS_OK = "ok"
STATUS_CLARIFICATION = "clarification_required"
STATUS_UNSUPPORTED = "unsupported"
STATUS_ERROR = "error"

#: Catégories du dataset. Fermée : un cas hors liste est une erreur de rédaction.
CATEGORIES = (
    "document_selection",
    "sheet_selection",
    "table_selection",
    "column_resolution",
    "filtering",
    "aggregation",
    "ranking",
    "dates",
    "formulas",
    "provenance",
    "ambiguity",
    "type_errors",
    "security",
    "determinism",
)

#: Noms de fichiers du corpus. Ils apparaissent dans les questions : la sélection de
#: document se fait par citation explicite, jamais par devinette.
DOC_VENTES = "ventes_2026.csv"
DOC_ANOMALIES = "anomalies_2026.xlsx"
DOC_COMPTA = "comptabilite_2026.xlsx"
DOC_FORMULES = "budget_formules.xlsx"
DOC_MULTI = "classeur_multi.xlsx"


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CorpusDocument:
    """Un document du corpus, avec ses octets et ses lignes en Python natif.

    `rows` est la vérité terrain brute : des dictionnaires de valeurs Python, jamais
    passés par le typage du moteur. C'est sur eux que sont calculées les valeurs
    attendues du dataset.
    """

    filename: str
    data: bytes
    is_csv: bool
    sheet_name: str
    header_range: str
    data_range: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]

    @property
    def content_sha256(self) -> str:
        """Identité **canonique** du document : le hachage de ses octets.

        Le nom de fichier n'en est pas une. Deux documents peuvent le partager, un même
        document peut en changer. Résoudre par le nom, c'est accepter d'exécuter un
        dataset pourtant figé contre le mauvais classeur au premier doublon ou renommage.
        """
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Corpus:
    """Le corpus complet, écrit sur disque et disponible en mémoire."""

    directory: Path
    documents: dict[str, CorpusDocument] = field(default_factory=dict)
    #: Documents secondaires (autres feuilles d'un même classeur), indexés
    #: `fichier::feuille`. Utile pour les attentes de sélection de feuille.
    sheets: dict[str, CorpusDocument] = field(default_factory=dict)

    def doc(self, filename: str) -> CorpusDocument:
        return self.documents[filename]

    def by_content_sha256(self, sha: str) -> list[CorpusDocument]:
        """Tous les documents portant ce hachage — **la pluralité n'est pas tranchée ici**.

        Renvoyer une liste plutôt qu'un document oblige l'appelant à décider quoi faire
        de 0 ou de plusieurs correspondances, au lieu d'en choisir une silencieusement.
        """
        target = (sha or "").lower()
        return [d for d in self.documents.values() if d.content_sha256 == target]


# --------------------------------------------------------------------------- #
# Outils déterministes
# --------------------------------------------------------------------------- #
def _column_letter(index0: int) -> str:
    """`0 -> A`, `26 -> AA`. Réimplémenté ici : la vérité terrain ne dépend d'aucun
    utilitaire du moteur, y compris pour quelque chose d'aussi anodin qu'une lettre."""
    out, i = "", index0 + 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def _ranges(n_columns: int, n_rows: int, *, header_row: int = 1) -> tuple[str, str]:
    """`(header_range, data_range)` d'une table posée en A1, comme le fait le moteur."""
    last = _column_letter(n_columns - 1)
    return (f"A{header_row}:{last}{header_row}",
            f"A{header_row + 1}:{last}{header_row + n_rows}")


def _fmt_eur(amount: Decimal) -> str:
    """Montant à la française : `€ 1 234,50`, espace insécable étroit inclus.

    Le format est délibérément « sale » : c'est celui des exports réels, et c'est lui
    qui met à l'épreuve la normalisation numérique du moteur.
    """
    sign = "-" if amount < 0 else ""
    entier, _, cents = f"{abs(amount):.2f}".partition(".")
    groups = []
    while entier:
        groups.insert(0, entier[-3:])
        entier = entier[:-3]
    return f"{sign}€ {' '.join(groups)},{cents}"


def _deterministic_xlsx(sheets: list[tuple[str, list[list[Any]]]]) -> bytes:
    """Classeur XLSX aux octets stables d'une exécution à l'autre.

    Trois sources de variation devaient être neutralisées :

    * la date de création du document, qu'openpyxl prend sur l'horloge — réglable via
      `wb.properties` ;
    * la date de **modification**, qu'openpyxl réécrit à l'enregistrement
      (`writer/excel.py` : `properties.modified = datetime.now(...)`). Elle ignore donc
      ce qu'on a posé sur le classeur, et doit être corrigée après coup dans le XML ;
    * les horodatages des entrées du ZIP, qu'`ZipFile.writestr` prend sur l'horloge
      système quand on lui passe un simple nom d'entrée.

    Aucune des deux dernières ne se règle depuis l'API openpyxl : l'archive est donc
    reconstruite ici, entrées triées et datées d'une valeur figée. Sans cela, « même
    graine → mêmes octets » serait faux, et le test de reproductibilité mentirait —
    ce qui est pire qu'un test absent.
    """
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, grid in sheets:
        ws = wb.create_sheet(title=name)
        for row in grid:
            ws.append(row)
    wb.properties.creator = "ragatm-benchmark"
    wb.properties.lastModifiedBy = "ragatm-benchmark"
    wb.properties.created = FROZEN_TIMESTAMP
    wb.properties.modified = FROZEN_TIMESTAMP

    raw = io.BytesIO()
    wb.save(raw)
    wb.close()

    source = zipfile.ZipFile(io.BytesIO(raw.getvalue()), "r")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dest:
        for name in sorted(source.namelist()):
            content = source.read(name)
            if name == "docProps/core.xml":
                content = re.sub(
                    rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                    rb"\g<1>" + FROZEN_TIMESTAMP.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
                    + rb"\g<2>", content)
            info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            dest.writestr(info, content)
    source.close()
    return out.getvalue()


# --------------------------------------------------------------------------- #
# Table 1 — ventes (CSV) : zéros initiaux, devise, dates, catégories
# --------------------------------------------------------------------------- #
REGIONS = ("Ouest", "Est", "Nord", "Sud")
STATUTS = ("gagne", "perdu", "en cours")

VENTES_COLUMNS = ("Reference", "Region", "Zone", "Statut", "Quantite",
                  "CA HT", "Date vente")
VENTES_ROW_COUNT = 40


def build_ventes_rows() -> tuple[dict[str, Any], ...]:
    """Lignes de ventes, tirées d'un `random.Random(SEED)` local.

    `random.Random` instancié explicitement, jamais le générateur global : un autre
    module qui appellerait `random.seed()` ne doit pas pouvoir décaler ce corpus.

    Les montants sont **distincts deux à deux** : un ex æquo ferait légitimement échouer
    `argmax` (le moteur refuse de trancher), ce qui mesurerait le corpus et non le
    moteur. Le cas d'ex æquo est traité ailleurs, exprès.
    """
    rng = random.Random(SEED)
    montants: list[Decimal] = []
    seen: set[Decimal] = set()
    while len(montants) < VENTES_ROW_COUNT:
        candidate = (Decimal(rng.randrange(20_000, 900_000)) / Decimal(100)).quantize(
            Decimal("0.01"))
        if candidate in seen:
            continue
        seen.add(candidate)
        montants.append(candidate)

    rows: list[dict[str, Any]] = []
    for i in range(VENTES_ROW_COUNT):
        region = REGIONS[rng.randrange(len(REGIONS))]
        rows.append({
            # Zéro initial significatif : `000007` n'est pas 7. C'est la règle qui
            # décide seule du sort des références, SIRET et codes postaux.
            "Reference": f"{i + 1:06d}",
            "Region": region,
            # Doublon volontairement en MINUSCULES : le planner normalise la question,
            # donc une valeur de filtre issue d'une question est toujours minuscule.
            # `Zone` permet de mesurer le filtrage sans que la casse ne masque tout.
            "Zone": region.lower(),
            "Statut": STATUTS[rng.randrange(len(STATUTS))],
            "Quantite": rng.randrange(1, 60),
            "CA HT": montants[i],
            "Date vente": EPOCH + timedelta(days=i * 3),
        })
    return tuple(rows)


def _ventes_csv_bytes(rows: tuple[dict[str, Any], ...]) -> bytes:
    """CSV point-virgule (export français) : la virgule vit déjà dans les montants."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(VENTES_COLUMNS)
    for r in rows:
        writer.writerow([
            r["Reference"], r["Region"], r["Zone"], r["Statut"],
            str(r["Quantite"]), _fmt_eur(r["CA HT"]), r["Date vente"].isoformat(),
        ])
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- #
# Table 2 — anomalies (XLSX) : valeurs nulles et montants négatifs
# --------------------------------------------------------------------------- #
ANOMALIES_COLUMNS = ("Reference", "Libelle", "Solde", "Date operation")

#: Écrites en dur, pas tirées : ce sont des **cas de bord choisis**. Un tirage
#: produirait « des nulls quelque part » ; on veut des nulls à des endroits précis,
#: dont un qui décale la moyenne si le dénominateur est le nombre de lignes.
#:
#: `Date operation` est une vraie date de classeur (openpyxl la restitue en `datetime`),
#: contrairement aux dates ISO du CSV qui restent du texte. Le contraste est délibéré :
#: il permet de mesurer séparément ce que le moteur sait faire d'une date typée et ce
#: qu'il fait d'une date qui n'a jamais été typée.
ANOMALIES_ROWS: tuple[dict[str, Any], ...] = (
    {"Reference": "A00001", "Libelle": "avoir client", "Solde": Decimal("-125.50"),
     "Date operation": date(2026, 1, 5)},
    {"Reference": "A00002", "Libelle": "facture", "Solde": Decimal("340.25"),
     "Date operation": date(2026, 1, 19)},
    {"Reference": "A00003", "Libelle": "regularisation", "Solde": None,
     "Date operation": date(2026, 2, 2)},
    {"Reference": "A00004", "Libelle": "avoir fournisseur", "Solde": Decimal("-980.00"),
     "Date operation": date(2026, 2, 16)},
    {"Reference": "A00005", "Libelle": "facture", "Solde": Decimal("1200.75"),
     "Date operation": date(2026, 3, 2)},
    {"Reference": "A00006", "Libelle": "provision", "Solde": None,
     "Date operation": date(2026, 3, 16)},
    {"Reference": "A00007", "Libelle": "facture", "Solde": Decimal("0.00"),
     "Date operation": date(2026, 3, 30)},
    {"Reference": "A00008", "Libelle": "avoir client", "Solde": Decimal("-45.10"),
     "Date operation": date(2026, 4, 13)},
)


def _anomalies_grid() -> list[list[Any]]:
    grid: list[list[Any]] = [list(ANOMALIES_COLUMNS)]
    for r in ANOMALIES_ROWS:
        solde = r["Solde"]
        # `float(str(...))` : openpyxl n'écrit pas de Decimal. La valeur reste exacte
        # au retour parce que `repr` d'un float court reconstruit la même décimale.
        grid.append([r["Reference"], r["Libelle"],
                     None if solde is None else float(str(solde)),
                     datetime.combine(r["Date operation"], datetime.min.time())])
    return grid


# --------------------------------------------------------------------------- #
# Table 3 — comptabilité (XLSX) : colonnes quasi homonymes
# --------------------------------------------------------------------------- #
COMPTA_COLUMNS = ("Reference", "Montant brut", "Montant net", "Montant paye",
                  "Montant comptable")

#: Le piège central du moteur : quatre colonnes que seul un qualificatif distingue.
#: Une réponse numériquement exacte sur la mauvaise colonne est indétectable pour
#: l'utilisateur — c'est le mode d'échec que le résolveur existe pour empêcher.
COMPTA_ROWS: tuple[dict[str, Any], ...] = (
    {"Reference": "C00001", "Montant brut": Decimal("1000.00"),
     "Montant net": Decimal("833.33"), "Montant paye": Decimal("800.00"),
     "Montant comptable": Decimal("820.00")},
    {"Reference": "C00002", "Montant brut": Decimal("2400.00"),
     "Montant net": Decimal("2000.00"), "Montant paye": Decimal("1500.00"),
     "Montant comptable": Decimal("1950.00")},
    {"Reference": "C00003", "Montant brut": Decimal("600.00"),
     "Montant net": Decimal("500.00"), "Montant paye": Decimal("500.00"),
     "Montant comptable": Decimal("495.00")},
    {"Reference": "C00004", "Montant brut": Decimal("120.00"),
     "Montant net": Decimal("100.00"), "Montant paye": Decimal("0.00"),
     "Montant comptable": Decimal("100.00")},
)


def _compta_grid() -> list[list[Any]]:
    grid: list[list[Any]] = [list(COMPTA_COLUMNS)]
    for r in COMPTA_ROWS:
        grid.append([r["Reference"]] + [float(str(r[c])) for c in COMPTA_COLUMNS[1:]])
    return grid


# --------------------------------------------------------------------------- #
# Table 4 — formules sans valeur en cache (XLSX)
# --------------------------------------------------------------------------- #
FORMULES_COLUMNS = ("Reference", "Base", "Total calcule")

FORMULES_ROWS: tuple[dict[str, Any], ...] = (
    {"Reference": "F00001", "Base": Decimal("100.00")},
    {"Reference": "F00002", "Base": Decimal("250.00")},
    {"Reference": "F00003", "Base": Decimal("75.50")},
    {"Reference": "F00004", "Base": Decimal("410.00")},
)


def _formules_grid() -> list[list[Any]]:
    """Colonne de formules écrites SANS valeur en cache.

    openpyxl n'évalue rien : une cellule à laquelle on affecte `"=B2*2"` est stockée
    comme formule et son cache reste vide. Relue en `data_only=True`, elle vaut `None`
    — donc indiscernable d'une cellule vide. C'est précisément le scénario où une somme
    serait silencieusement fausse, et où le moteur doit refuser plutôt que répondre.
    """
    grid: list[list[Any]] = [list(FORMULES_COLUMNS)]
    for i, r in enumerate(FORMULES_ROWS, start=2):
        grid.append([r["Reference"], float(str(r["Base"])), f"=B{i}*2"])
    return grid


# --------------------------------------------------------------------------- #
# Table 5 — classeur multi-feuilles : sélection de feuille et d'archive
# --------------------------------------------------------------------------- #
MULTI_COLUMNS = ("Reference", "Region", "CA HT")

MULTI_VENTES: tuple[dict[str, Any], ...] = (
    {"Reference": "M00001", "Region": "Ouest", "CA HT": Decimal("1500.00")},
    {"Reference": "M00002", "Region": "Est", "CA HT": Decimal("2500.00")},
    {"Reference": "M00003", "Region": "Nord", "CA HT": Decimal("3000.00")},
)
MULTI_VENTES_BIS: tuple[dict[str, Any], ...] = (
    {"Reference": "N00001", "Region": "Ouest", "CA HT": Decimal("10.00")},
    {"Reference": "N00002", "Region": "Est", "CA HT": Decimal("20.00")},
    {"Reference": "N00003", "Region": "Sud", "CA HT": Decimal("30.00")},
)
MULTI_ARCHIVE: tuple[dict[str, Any], ...] = (
    {"Reference": "H00001", "Region": "Ouest", "CA HT": Decimal("999999.00")},
    {"Reference": "H00002", "Region": "Est", "CA HT": Decimal("888888.00")},
)


def _multi_grid(rows: tuple[dict[str, Any], ...]) -> list[list[Any]]:
    grid: list[list[Any]] = [list(MULTI_COLUMNS)]
    for r in rows:
        grid.append([r["Reference"], r["Region"], float(str(r["CA HT"]))])
    return grid


# --------------------------------------------------------------------------- #
# Construction du corpus
# --------------------------------------------------------------------------- #
def build_corpus(directory: str | Path) -> Corpus:
    """Écrit le corpus dans `directory` et le renvoie, octets et lignes compris."""
    dest = Path(directory)
    dest.mkdir(parents=True, exist_ok=True)
    corpus = Corpus(directory=dest)

    def register(doc: CorpusDocument, *, primary: bool = True) -> None:
        (dest / doc.filename).write_bytes(doc.data)
        if primary:
            corpus.documents[doc.filename] = doc
        corpus.sheets[f"{doc.filename}::{doc.sheet_name}"] = doc

    # -- ventes (CSV) ------------------------------------------------------
    ventes = build_ventes_rows()
    header, data_range = _ranges(len(VENTES_COLUMNS), len(ventes))
    register(CorpusDocument(
        filename=DOC_VENTES, data=_ventes_csv_bytes(ventes), is_csv=True,
        # Pour un CSV, le moteur nomme la « feuille » d'après le fichier.
        sheet_name=DOC_VENTES, header_range=header, data_range=data_range,
        columns=VENTES_COLUMNS, rows=ventes))

    # -- anomalies (XLSX) --------------------------------------------------
    header, data_range = _ranges(len(ANOMALIES_COLUMNS), len(ANOMALIES_ROWS))
    register(CorpusDocument(
        filename=DOC_ANOMALIES,
        data=_deterministic_xlsx([("Anomalies", _anomalies_grid())]), is_csv=False,
        sheet_name="Anomalies", header_range=header, data_range=data_range,
        columns=ANOMALIES_COLUMNS, rows=ANOMALIES_ROWS))

    # -- comptabilité (XLSX) -----------------------------------------------
    header, data_range = _ranges(len(COMPTA_COLUMNS), len(COMPTA_ROWS))
    register(CorpusDocument(
        filename=DOC_COMPTA,
        data=_deterministic_xlsx([("Comptabilite", _compta_grid())]), is_csv=False,
        sheet_name="Comptabilite", header_range=header, data_range=data_range,
        columns=COMPTA_COLUMNS, rows=COMPTA_ROWS))

    # -- formules (XLSX) ---------------------------------------------------
    header, data_range = _ranges(len(FORMULES_COLUMNS), len(FORMULES_ROWS))
    register(CorpusDocument(
        filename=DOC_FORMULES,
        data=_deterministic_xlsx([("Formules", _formules_grid())]), is_csv=False,
        sheet_name="Formules", header_range=header, data_range=data_range,
        columns=FORMULES_COLUMNS, rows=FORMULES_ROWS))

    # -- classeur multi-feuilles (XLSX) ------------------------------------
    multi_data = _deterministic_xlsx([
        ("Ventes", _multi_grid(MULTI_VENTES)),
        ("Ventes bis", _multi_grid(MULTI_VENTES_BIS)),
        ("Archive", _multi_grid(MULTI_ARCHIVE)),
    ])
    for sheet_name, rows in (("Ventes", MULTI_VENTES),
                             ("Ventes bis", MULTI_VENTES_BIS),
                             ("Archive", MULTI_ARCHIVE)):
        header, data_range = _ranges(len(MULTI_COLUMNS), len(rows))
        register(CorpusDocument(
            filename=DOC_MULTI, data=multi_data, is_csv=False,
            sheet_name=sheet_name, header_range=header, data_range=data_range,
            columns=MULTI_COLUMNS, rows=rows),
            primary=(sheet_name == "Ventes"))

    return corpus


# --------------------------------------------------------------------------- #
# Vérité terrain — Python nu, aucune dépendance au moteur
# --------------------------------------------------------------------------- #
Predicate = Callable[[dict[str, Any]], bool]


def _kept(rows, where: Predicate | None) -> list[dict[str, Any]]:
    return [r for r in rows if where is None or where(r)]


def gt_sum(rows, column: str, where: Predicate | None = None) -> Decimal:
    """Somme des valeurs NON NULLES. Un `None` n'est pas un zéro : il est absent."""
    return sum((r[column] for r in _kept(rows, where) if r[column] is not None),
               Decimal(0))


def gt_count(rows, where: Predicate | None = None) -> int:
    return len(_kept(rows, where))


def gt_count_distinct(rows, column: str, where: Predicate | None = None) -> int:
    return len({r[column] for r in _kept(rows, where) if r[column] is not None})


def gt_average(rows, column: str, where: Predicate | None = None) -> Decimal:
    """Moyenne sur les valeurs non nulles.

    Le dénominateur est le nombre de valeurs **agrégées**, pas le nombre de lignes.
    Diviser par le nombre de lignes serait une autre grandeur, et une réponse fausse.
    """
    values = [r[column] for r in _kept(rows, where) if r[column] is not None]
    if not values:
        raise ValueError("moyenne sur une sélection vide")
    return sum(values, Decimal(0)) / Decimal(len(values))


def gt_min(rows, column: str, where: Predicate | None = None):
    values = [r[column] for r in _kept(rows, where) if r[column] is not None]
    return min(values)


def gt_max(rows, column: str, where: Predicate | None = None):
    values = [r[column] for r in _kept(rows, where) if r[column] is not None]
    return max(values)


def gt_argmax(rows, column: str, identity: str, where: Predicate | None = None) -> str:
    kept = [r for r in _kept(rows, where) if r[column] is not None]
    return max(kept, key=lambda r: r[column])[identity]


def gt_argmin(rows, column: str, identity: str, where: Predicate | None = None) -> str:
    kept = [r for r in _kept(rows, where) if r[column] is not None]
    return min(kept, key=lambda r: r[column])[identity]


def gt_top_n(rows, column: str, identity: str, n: int,
             where: Predicate | None = None) -> list[str]:
    kept = [r for r in _kept(rows, where) if r[column] is not None]
    kept.sort(key=lambda r: r[column], reverse=True)
    return [r[identity] for r in kept[:n]]


def gt_bottom_n(rows, column: str, identity: str, n: int,
                where: Predicate | None = None) -> list[str]:
    kept = [r for r in _kept(rows, where) if r[column] is not None]
    kept.sort(key=lambda r: r[column])
    return [r[identity] for r in kept[:n]]


# --------------------------------------------------------------------------- #
# Cas
# --------------------------------------------------------------------------- #
def _case(*, id: str, question: str, document: str, category: str,
          expected_sheet: str = "", expected_table: str = "",
          expected_columns: tuple[str, ...] = (),
          expected_operation: str = "",
          expected_filters: tuple[dict[str, Any], ...] = (),
          expected_value: Any = None, tolerance: str = "0",
          expected_status: str = STATUS_OK, expected_code: str = "",
          note: str = "") -> dict[str, Any]:
    """Enregistre un cas. Tous les champs sont explicites : rien n'est déduit.

    `expected_value` est sérialisé en chaîne canonique — un `Decimal` ne survit pas à
    JSON, et un `float` réintroduirait exactement l'imprécision que le moteur évite.
    """
    if category not in CATEGORIES:
        raise ValueError(f"catégorie inconnue : {category!r}")
    if expected_status not in (STATUS_OK, STATUS_CLARIFICATION,
                               STATUS_UNSUPPORTED, STATUS_ERROR):
        raise ValueError(f"statut attendu inconnu : {expected_status!r}")
    return {
        "id": id,
        "question": question,
        "document": document,
        "expected_sheet": expected_sheet,
        "expected_table": expected_table,
        "expected_columns": list(expected_columns),
        "expected_operation": expected_operation,
        "expected_filters": [dict(f) for f in expected_filters],
        "expected_value": _canonical(expected_value),
        "tolerance": tolerance,
        "category": category,
        "expected_status": expected_status,
        "expected_code": expected_code,
        "note": note,
    }


def _canonical(value: Any) -> Any:
    """Forme canonique JSON d'une valeur attendue. Jamais de `float`."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return str(value)


def build_cases(corpus: Corpus) -> list[dict[str, Any]]:
    """Le dataset complet. Les valeurs attendues sont calculées ICI, en Python nu."""
    ventes = corpus.doc(DOC_VENTES)
    v = ventes.rows
    anomalies = corpus.doc(DOC_ANOMALIES)
    compta = corpus.doc(DOC_COMPTA)
    multi_ventes = corpus.sheets[f"{DOC_MULTI}::Ventes"]

    cases: list[dict[str, Any]] = []
    add = cases.append

    # ------------------------------------------------------------------ #
    # aggregation — le cœur : un nombre exact ou rien
    # ------------------------------------------------------------------ #
    add(_case(
        id="agg-sum-global", category="aggregation",
        question=f"Quelle est la somme du CA HT dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="sum", expected_value=gt_sum(v, "CA HT")))

    add(_case(
        id="agg-count-rows", category="aggregation",
        question=f"Combien de lignes contient {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range,
        expected_operation="count_rows", expected_value=gt_count(v)))

    add(_case(
        id="agg-count-distinct-region", category="aggregation",
        question=f"Combien de valeurs distinctes de Region dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("Region",),
        expected_operation="count_distinct",
        expected_value=gt_count_distinct(v, "Region")))

    add(_case(
        id="agg-average-ca", category="aggregation",
        question=f"Quelle est la moyenne du CA HT dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="average", expected_value=gt_average(v, "CA HT"),
        # La division exacte peut être périodique : on borne au centime, pas au bit.
        tolerance="0.0000000001"))

    add(_case(
        id="agg-max-ca", category="aggregation",
        question=f"Le maximum du CA HT dans {DOC_VENTES}",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="maximum", expected_value=gt_max(v, "CA HT")))

    add(_case(
        id="agg-min-ca", category="aggregation",
        question=f"Le minimum du CA HT dans {DOC_VENTES}",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="minimum", expected_value=gt_min(v, "CA HT")))

    add(_case(
        id="agg-sum-quantite", category="aggregation",
        question=f"Quelle est la somme de la Quantite dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("Quantite",),
        expected_operation="sum", expected_value=gt_sum(v, "Quantite")))

    # Valeurs nulles et négatives : la somme ne doit ni ignorer les négatifs ni
    # convertir les nulls en zéro (ce qui reviendrait au même total, mais pour de
    # mauvaises raisons — d'où le cas `moyenne` qui, lui, les distingue).
    add(_case(
        id="agg-sum-solde-negatifs", category="aggregation",
        question=f"Quelle est la somme du Solde dans {DOC_ANOMALIES} ?",
        document=DOC_ANOMALIES, expected_sheet="Anomalies",
        expected_table=anomalies.data_range, expected_columns=("Solde",),
        expected_operation="sum",
        expected_value=gt_sum(anomalies.rows, "Solde")))

    add(_case(
        id="agg-average-solde-nulls", category="aggregation",
        question=f"Quelle est la moyenne du Solde dans {DOC_ANOMALIES} ?",
        document=DOC_ANOMALIES, expected_sheet="Anomalies",
        expected_table=anomalies.data_range, expected_columns=("Solde",),
        expected_operation="average",
        expected_value=gt_average(anomalies.rows, "Solde"),
        tolerance="0.0000000001",
        note="dénominateur = 6 valeurs non nulles, pas 8 lignes"))

    # ------------------------------------------------------------------ #
    # filtering
    # ------------------------------------------------------------------ #
    # Le fichier est cité EN TÊTE des questions filtrantes : le motif de filtre du
    # planner s'arrête au premier point, et un nom de fichier placé après la valeur
    # ferait entrer « .csv » dans le filtre. Le cas mesurerait alors la ponctuation.

    def ouest(r):
        return r["Zone"] == "ouest"
    add(_case(
        id="flt-sum-zone-ouest", category="filtering",
        question=f"Dans {DOC_VENTES}, quelle est la somme du CA HT ou la Zone vaut ouest ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT", "Zone"),
        expected_operation="sum",
        expected_filters=({"column": "Zone", "operator": "equals", "value": "ouest"},),
        expected_value=gt_sum(v, "CA HT", ouest)))

    add(_case(
        id="flt-count-zone-est", category="filtering",
        question=f"Dans {DOC_VENTES}, combien de lignes ou la Zone vaut est ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("Zone",),
        expected_operation="count_rows",
        expected_filters=({"column": "Zone", "operator": "equals", "value": "est"},),
        expected_value=gt_count(v, lambda r: r["Zone"] == "est")))

    add(_case(
        id="flt-sum-statut-gagne", category="filtering",
        question=f"Dans {DOC_VENTES}, quelle est la somme du CA HT dont le Statut vaut gagne ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT", "Statut"),
        expected_operation="sum",
        expected_filters=({"column": "Statut", "operator": "equals", "value": "gagne"},),
        expected_value=gt_sum(v, "CA HT", lambda r: r["Statut"] == "gagne")))

    # Filtre dont la valeur est CAPITALISÉE dans le fichier. La question est normalisée
    # en minuscules par le planner : le filtre porte donc « ouest » face à « Ouest ».
    # La bonne réponse reste la somme de la région Ouest — si le moteur rend autre
    # chose, c'est une différence de casse qu'il faut voir, pas masquer.
    add(_case(
        id="flt-sum-region-casse", category="filtering",
        question=f"Dans {DOC_VENTES}, quelle est la somme du CA HT de la region Ouest ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT", "Region"),
        expected_operation="sum",
        expected_filters=({"column": "Region", "operator": "equals", "value": "Ouest"},),
        expected_value=gt_sum(v, "CA HT", lambda r: r["Region"] == "Ouest"),
        note="sensibilité à la casse des valeurs de filtre"))

    # Seuil numérique sur une table à PLUSIEURS colonnes numériques. Le seuil doit se
    # rattacher au CA HT ; l'abandonner rendrait une somme non filtrée présentée comme
    # filtrée — une réponse fausse à provenance rassurante.
    seuil = Decimal("5000.00")
    add(_case(
        id="flt-sum-superieur-seuil", category="filtering",
        question=(f"Quelle est la somme du CA HT superieur a 5000 dans {DOC_VENTES} ?"),
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="sum",
        expected_filters=({"column": "CA HT", "operator": "greater_than",
                           "value": "5000"},),
        expected_value=gt_sum(v, "CA HT", lambda r: r["CA HT"] > seuil)))

    # ------------------------------------------------------------------ #
    # ranking
    # ------------------------------------------------------------------ #
    add(_case(
        id="rnk-top-3", category="ranking",
        question=f"Les 3 premiers CA HT dans {DOC_VENTES}",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="top_n",
        expected_value=gt_top_n(v, "CA HT", "Reference", 3)))

    add(_case(
        id="rnk-bottom-3", category="ranking",
        question=f"Les 3 plus faibles CA HT dans {DOC_VENTES}",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="bottom_n",
        expected_value=gt_bottom_n(v, "CA HT", "Reference", 3)))

    add(_case(
        id="rnk-argmax", category="ranking",
        question=f"Quelle reference a le CA HT le plus eleve dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="argmax",
        expected_value=[gt_argmax(v, "CA HT", "Reference")]))

    add(_case(
        id="rnk-argmin", category="ranking",
        question=f"Quelle reference a le CA HT minimum dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="argmin",
        expected_value=[gt_argmin(v, "CA HT", "Reference")]))

    # « le plus faible » désigne un MINIMUM. Le mot « plus » est le piège : une règle
    # qui s'y fie seule répond au maximum, avec une provenance parfaitement cohérente.
    add(_case(
        id="rnk-argmin-plus-faible", category="ranking",
        question=f"Quelle reference a le CA HT le plus faible dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="argmin",
        expected_value=[gt_argmin(v, "CA HT", "Reference")],
        note="« plus faible » = argmin malgré le mot « plus »"))

    # ------------------------------------------------------------------ #
    # dates
    # ------------------------------------------------------------------ #
    jour = v[3]["Date vente"]
    add(_case(
        id="dat-count-date-precise", category="dates",
        question=(f"Dans {DOC_VENTES}, combien de lignes ou la Date vente "
                  f"vaut {jour.isoformat()} ?"),
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("Date vente",),
        expected_operation="count_rows",
        expected_filters=({"column": "Date vente", "operator": "equals",
                           "value": jour.isoformat()},),
        expected_value=gt_count(v, lambda r: r["Date vente"] == jour)))

    # Date d'un CLASSEUR : openpyxl la restitue typée, le moteur peut donc l'ordonner.
    add(_case(
        id="dat-max-date-xlsx", category="dates",
        question=f"Le maximum de la Date operation dans {DOC_ANOMALIES}",
        document=DOC_ANOMALIES, expected_sheet="Anomalies",
        expected_table=anomalies.data_range, expected_columns=("Date operation",),
        expected_operation="maximum",
        expected_value=datetime.combine(gt_max(anomalies.rows, "Date operation"),
                                        datetime.min.time())))

    # Date ambiguë : `03/04/2026` est le 3 avril ou le 4 mars selon la locale. Deviner
    # produirait un filtre plausible et faux ; le refus est la bonne réponse.
    add(_case(
        id="dat-ambigue-refus", category="dates",
        question=(f"Dans {DOC_ANOMALIES}, combien de lignes ou la Date operation "
                  f"vaut 03/04/2026 ?"),
        document=DOC_ANOMALIES, expected_sheet="Anomalies",
        expected_table=anomalies.data_range,
        expected_operation="count_rows",
        expected_status=STATUS_ERROR, expected_code="incompatible_filter_type"))

    # Même donnée, autre support : dans un CSV une date ISO n'est qu'une chaîne. Un
    # `maximum` sur une colonne texte n'a pas de sens ; le cas existe pour montrer si
    # le moteur le dit ou s'il ordonne lexicalement en silence.
    add(_case(
        id="dat-max-date-csv", category="dates",
        question=f"Le maximum de la Date vente dans {DOC_VENTES}",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("Date vente",),
        expected_operation="maximum", expected_value=gt_max(v, "Date vente"),
        note="une date ISO d'un CSV n'est pas typée en date par l'inférence"))

    # ------------------------------------------------------------------ #
    # column_resolution
    # ------------------------------------------------------------------ #
    add(_case(
        id="col-montant-net-explicite", category="column_resolution",
        question=f"Quelle est la somme du Montant net dans {DOC_COMPTA} ?",
        document=DOC_COMPTA, expected_sheet="Comptabilite",
        expected_table=compta.data_range, expected_columns=("Montant net",),
        expected_operation="sum",
        expected_value=gt_sum(compta.rows, "Montant net")))

    add(_case(
        id="col-montant-brut-explicite", category="column_resolution",
        question=f"Quelle est la somme du Montant brut dans {DOC_COMPTA} ?",
        document=DOC_COMPTA, expected_sheet="Comptabilite",
        expected_table=compta.data_range, expected_columns=("Montant brut",),
        expected_operation="sum",
        expected_value=gt_sum(compta.rows, "Montant brut")))

    # Alias métier : « CA » désigne le chiffre d'affaires. L'alias est le seul niveau
    # de résolution qui repose sur du sens plutôt que sur de la forme.
    add(_case(
        id="col-alias-ca", category="column_resolution",
        question=f"Quelle est la somme du CA HT dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="sum", expected_value=gt_sum(v, "CA HT")))

    # ------------------------------------------------------------------ #
    # ambiguity — le refus EST la bonne réponse
    # ------------------------------------------------------------------ #
    add(_case(
        id="amb-montant-nu", category="ambiguity",
        question=f"Quelle est la somme du Montant dans {DOC_COMPTA} ?",
        document=DOC_COMPTA, expected_sheet="Comptabilite",
        expected_table=compta.data_range,
        # Pas d'`expected_operation` : la bonne issue survient AVANT qu'un plan existe.
        # En déclarer une reviendrait à exiger du moteur qu'il construise le plan qu'on
        # lui demande justement de ne pas construire.
        expected_status=STATUS_CLARIFICATION, expected_code="ambiguous_column",
        note="quatre colonnes « Montant … » : aucune ne domine"))

    add(_case(
        id="amb-colonne-inexistante", category="ambiguity",
        question=f"Quelle est la somme de la Marge nette dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range,
        expected_status=STATUS_CLARIFICATION, expected_code="column_not_found"))

    # ------------------------------------------------------------------ #
    # document_selection
    # ------------------------------------------------------------------ #
    add(_case(
        id="doc-nomme-explicitement", category="document_selection",
        question=f"Quelle est la somme du Montant brut dans {DOC_COMPTA} ?",
        document=DOC_COMPTA, expected_sheet="Comptabilite",
        expected_table=compta.data_range, expected_columns=("Montant brut",),
        expected_operation="sum",
        expected_value=gt_sum(compta.rows, "Montant brut")))

    # Aucun document cité alors que le corpus en contient cinq : répondre exigerait
    # de choisir, donc de deviner sur quelles données porte le calcul.
    add(_case(
        id="doc-non-cite-ambigu", category="document_selection",
        question="Quelle est la somme du CA HT ?",
        document="", expected_status=STATUS_CLARIFICATION,
        expected_code="ambiguous_document"))

    add(_case(
        id="doc-inexistant", category="document_selection",
        question="Quelle est la somme du CA HT dans previsionnel_2030.csv ?",
        document="", expected_status=STATUS_CLARIFICATION,
        expected_code="ambiguous_document",
        note="fichier cité absent du corpus : aucun document ne peut être élu"))

    # ------------------------------------------------------------------ #
    # sheet_selection / table_selection
    # ------------------------------------------------------------------ #
    add(_case(
        id="sht-feuille-nommee", category="sheet_selection",
        question=f"La somme du CA HT sur la feuille Ventes dans {DOC_MULTI}",
        document=DOC_MULTI, expected_sheet="Ventes",
        expected_table=multi_ventes.data_range, expected_columns=("CA HT",),
        expected_operation="sum",
        expected_value=gt_sum(MULTI_VENTES, "CA HT")))

    add(_case(
        id="tbl-aucune-feuille-nommee", category="table_selection",
        question=f"La somme du CA HT dans {DOC_MULTI}",
        document=DOC_MULTI, expected_status=STATUS_CLARIFICATION,
        expected_code="ambiguous_table",
        note="« Ventes » et « Ventes bis » sont indiscernables sans précision"))

    add(_case(
        id="tbl-archive-jamais-implicite", category="table_selection",
        question=f"La somme du CA HT sur la feuille Archive dans {DOC_MULTI}",
        document=DOC_MULTI, expected_status=STATUS_CLARIFICATION,
        expected_code="ambiguous_table",
        note="une feuille d'archive n'est jamais retenue sans confirmation"))

    # ------------------------------------------------------------------ #
    # formulas
    # ------------------------------------------------------------------ #
    add(_case(
        id="for-formule-sans-cache", category="formulas",
        question=f"Quelle est la somme de la Base dans {DOC_FORMULES} ?",
        document=DOC_FORMULES, expected_sheet="Formules",
        expected_status=STATUS_ERROR, expected_code="formula_result_unavailable",
        note="une formule sans valeur en cache rend tout total non vérifiable"))

    add(_case(
        id="for-comptage-tolere", category="formulas",
        question=f"Combien de lignes contient {DOC_FORMULES} ?",
        document=DOC_FORMULES, expected_sheet="Formules",
        expected_table=corpus.doc(DOC_FORMULES).data_range,
        expected_operation="count_rows",
        expected_value=gt_count(FORMULES_ROWS),
        note="compter des lignes n'exige aucune valeur de formule"))

    # ------------------------------------------------------------------ #
    # provenance
    # ------------------------------------------------------------------ #
    add(_case(
        id="prv-plage-et-filtre", category="provenance",
        question=f"Dans {DOC_VENTES}, quelle est la somme du CA HT ou la Zone vaut nord ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT", "Zone"),
        expected_operation="sum",
        expected_filters=({"column": "Zone", "operator": "equals", "value": "nord"},),
        expected_value=gt_sum(v, "CA HT", lambda r: r["Zone"] == "nord"),
        note="la preuve doit citer la plage, la colonne mesurée et le filtre appliqué"))

    add(_case(
        id="prv-sans-filtre", category="provenance",
        question=f"Quelle est la somme de la Quantite dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("Quantite",),
        expected_operation="sum", expected_value=gt_sum(v, "Quantite")))

    # ------------------------------------------------------------------ #
    # type_errors
    # ------------------------------------------------------------------ #
    add(_case(
        id="typ-somme-sur-texte", category="type_errors",
        question=f"Quelle est la somme du Statut dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_status=STATUS_CLARIFICATION, expected_code="column_not_found",
        note="aucune colonne numérique ne correspond : sommer du texte n'a pas de sens"))

    add(_case(
        id="typ-mediane-hors-perimetre", category="type_errors",
        question=f"Quelle est la mediane du CA HT dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_status=STATUS_UNSUPPORTED,
        # Mis à jour lors de la qualification staging : le planner distingue désormais
        # « hors périmètre » (opération reconnue, non servie) de « non compris ». Le
        # STATUT était déjà bon ; c'est le code qui a gagné en précision. L'attendu
        # suit le moteur — il n'a pas été relâché pour faire passer une métrique.
        expected_code="unsupported_operation",
        note="la médiane est hors périmètre : la nommer vaut mieux que l'approximer"))

    add(_case(
        id="typ-formulation-non-couverte", category="type_errors",
        question=f"Peux-tu me parler un peu de {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_status=STATUS_UNSUPPORTED,
        expected_code="planner_could_not_build_plan"))

    # ------------------------------------------------------------------ #
    # security — une valeur reste une valeur
    # ------------------------------------------------------------------ #
    # Les charges sont écrites SANS point : le motif de filtre du planner s'arrête au
    # premier point, et une charge tronquée n'atteindrait pas le compilateur — le cas
    # ne testerait alors plus rien. Elles doivent devenir des valeurs de filtre réelles
    # pour que la propriété « une valeur reste une valeur » soit vérifiée.
    add(_case(
        id="sec-injection-valeur-filtre", category="security",
        question=(f"Dans {DOC_VENTES}, quelle est la somme du CA HT ou la Zone vaut "
                  f"ouest'; DROP TABLE t; --"),
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_status=STATUS_ERROR, expected_code="empty_selection",
        note="la charge est un paramètre : aucune ligne, et la table reste intacte"))

    add(_case(
        id="sec-lecture-fichier", category="security",
        question=(f"Dans {DOC_VENTES}, quelle est la somme du CA HT ou la Zone vaut "
                  f"/etc/passwd"),
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_status=STATUS_ERROR, expected_code="empty_selection",
        note="aucun chemin n'atteint jamais le moteur d'exécution"))

    add(_case(
        id="sec-attach-externe", category="security",
        question=(f"Dans {DOC_VENTES}, quelle est la somme du CA HT ou la Zone vaut "
                  f"x'; ATTACH '/tmp/xdb'; --"),
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_status=STATUS_ERROR, expected_code="empty_selection"))

    add(_case(
        id="sec-nom-de-colonne-hostile", category="security",
        question=(f"Dans {DOC_VENTES}, quelle est la somme du CA HT ou le Statut vaut "
                  f"' OR 1=1 --"),
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_status=STATUS_ERROR, expected_code="empty_selection",
        note="une tautologie SQL passée en paramètre ne sélectionne rien"))

    # ------------------------------------------------------------------ #
    # determinism — mêmes octets, même question, même réponse
    # ------------------------------------------------------------------ #
    add(_case(
        id="det-somme-repetee", category="determinism",
        question=f"Quelle est la somme du CA HT dans {DOC_VENTES} ?",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="sum", expected_value=gt_sum(v, "CA HT")))

    add(_case(
        id="det-classement-repete", category="determinism",
        question=f"Les 5 premiers CA HT dans {DOC_VENTES}",
        document=DOC_VENTES, expected_sheet=ventes.sheet_name,
        expected_table=ventes.data_range, expected_columns=("CA HT",),
        expected_operation="top_n",
        expected_value=gt_top_n(v, "CA HT", "Reference", 5)))

    add(_case(
        id="det-refus-repete", category="determinism",
        question=f"Quelle est la somme du Montant dans {DOC_COMPTA} ?",
        document=DOC_COMPTA, expected_sheet="Comptabilite",
        expected_status=STATUS_CLARIFICATION, expected_code="ambiguous_column",
        note="un refus doit être aussi stable qu'un calcul"))

    _check_unique_ids(cases)
    stamp_content_sha256(cases, corpus)
    return cases


def stamp_content_sha256(cases: list[dict[str, Any]], corpus: Corpus) -> None:
    """Ajoute à chaque cas le hachage canonique de son document.

    Le cas conserve `document` (nom de fichier) pour le DIAGNOSTIC, mais c'est
    `content_sha256` qui identifie le classeur à exécuter. Un cas sans hachage serait
    résolu par son nom — précisément ce que ce lot supprime.
    """
    for case in cases:
        doc = corpus.documents.get(case["document"])
        case["content_sha256"] = doc.content_sha256 if doc else ""
    validate_document_identity(cases)


class DocumentIdentityError(ValueError):
    """Cas dont l'identité documentaire est incohérente. Porte un ``code`` stable."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


ERR_DOCUMENT_WITHOUT_SHA = "document_without_content_sha256"
ERR_SHA_WITHOUT_DOCUMENT = "content_sha256_without_document"


def validate_document_identity(cases: list[dict[str, Any]]) -> None:
    """Invariant du schéma : le type de cas détermine la voie de résolution.

    Deux objectifs distincts cohabitent dans ce dataset, et ils ne doivent pas se
    contaminer :

    * un cas qui **nomme** son document impose au runner *quel* classeur exécuter — il
      doit donc porter un ``content_sha256`` ;
    * un cas de sélection documentaire laisse au **moteur** le soin de choisir depuis la
      question — lui donner un hachage court-circuiterait précisément ce qu'il mesure.

    D'où l'invariant, vérifié avant toute exécution :

        document renseigné  =>  content_sha256 OBLIGATOIRE
        document vide       =>  content_sha256 INTERDIT

    Le refuser à la construction plutôt qu'à l'exécution garantit qu'aucun cas
    incohérent n'atteint le runner, où le choix de voie deviendrait ambigu.
    """
    for case in cases:
        document = (case.get("document") or "").strip()
        sha = (case.get("content_sha256") or "").strip()
        if document and not sha:
            raise DocumentIdentityError(
                ERR_DOCUMENT_WITHOUT_SHA,
                f"cas {case.get('id', '?')} nomme {document!r} sans content_sha256 : "
                "il serait résolu par son nom")
        if not document and sha:
            raise DocumentIdentityError(
                ERR_SHA_WITHOUT_DOCUMENT,
                f"cas {case.get('id', '?')} porte un content_sha256 sans document : "
                "la sélection par le moteur serait court-circuitée")


def _check_unique_ids(cases: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for c in cases:
        if c["id"] in seen:
            raise ValueError(f"identifiant de cas dupliqué : {c['id']}")
        seen.add(c["id"])


# --------------------------------------------------------------------------- #
# Sérialisation
# --------------------------------------------------------------------------- #
def to_jsonl(cases: list[dict[str, Any]]) -> str:
    """JSONL trié par identifiant : deux générations donnent le même texte."""
    lines = [json.dumps(c, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
             for c in sorted(cases, key=lambda c: c["id"])]
    return "\n".join(lines) + "\n"


def load_jsonl(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def build_dataset(directory: str | Path) -> tuple[Corpus, list[dict[str, Any]]]:
    """Écrit corpus + `tabular_query_cases.jsonl` dans `directory`."""
    corpus = build_corpus(directory)
    cases = build_cases(corpus)
    (Path(directory) / "tabular_query_cases.jsonl").write_text(
        to_jsonl(cases), encoding="utf-8")
    return corpus, cases
