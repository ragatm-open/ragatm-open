"""Corpus VOLUMÉTRIQUE déterministe pour la qualification du moteur tabulaire.

Ce module ne mesure rien : il **fabrique les fichiers** sur lesquels la mesure porte.
Trois exigences le gouvernent, et elles ne sont pas négociables si l'on veut que les
chiffres publiés veuillent dire quelque chose :

1. **Déterminisme total.** Graine fixe, aucune valeur dérivée de l'horloge, aucun UUID
   tiré au hasard. Deux générations à un mois d'intervalle produisent des octets
   identiques — donc le même `sha256`, le même catalogue, le même `plan_hash`. Sans
   cela, une régression de performance serait indiscernable d'un corpus qui a changé.
2. **Salissure réaliste et CONTRÔLÉE.** NULL, négatifs, doublons, minorité de dates
   invalides, délimiteur et guillemets à l'intérieur d'un champ, accents. Un corpus
   propre mesurerait un moteur qui n'existe pas.
3. **Fichiers hors dépôt.** Un CSV d'un million de lignes pèse ~100 Mo : il vit dans un
   répertoire temporaire, et seuls sa taille et son empreinte sont publiés.

Point de politique important, volontairement exploité ici : `types.infer_column_type`
n'accepte une colonne temporelle que si **100 %** des valeurs non vides échantillonnées
(200 premières lignes) sont des dates ISO valides (`DATE_RATIO_REQUIRED = 1.0`). Une
minorité de dates invalides fait donc basculer la colonne en `text`, ce qui interdit
`between`, `minimum` et le tri chronologique. C'est le comportement voulu — mais il rend
les opérations de date impossibles à mesurer sur un fichier sale. D'où **deux variantes**
de chaque taille : `clean` (dates toutes valides, mesurable) et `dirty` (minorité
invalide, sert à démontrer le refus).
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from random import Random

#: Graine unique du corpus. Toute valeur pseudo-aléatoire du module en descend.
SEED = 20260718

#: Délimiteur des CSV générés. `;` est choisi pour que le champ texte puisse contenir
#: une virgule ET le délimiteur lui-même (échappé par `csv.writer`) : c'est exactement
#: le cas que `catalog._sniff_delimiter` doit continuer à trancher correctement.
DELIMITER = ";"

HEADERS = (
    "Date", "Identifiant", "Catégorie", "Région", "Quantité",
    "Prix unitaire", "Montant HT", "Montant TTC", "Statut", "Texte court",
)

#: Modalités. Accentuées à dessein : le moteur normalise les en-têtes mais PAS les
#: valeurs, et l'égalité textuelle compilée est `lower(trim(...))`.
REGIONS = ("Ouest", "Est", "Nord", "Sud", "Île-de-France", "Centre", "Occitanie")
#: Modalité rare, injectée une ligne sur 997 : c'est elle qui rend mesurable un filtre
#: SÉLECTIF (~0,1 %) par opposition à un filtre non sélectif (~20 %).
REGION_RARE = "Corse"
STATUTS = ("Gagné", "Perdu", "En cours", "Validé", "Annulé")
CATEGORIES = (
    "Matériel", "Logiciel", "Prestation", "Maintenance", "Formation", "Licence",
    "Hébergement", "Réseau", "Sécurité", "Conseil", "Déplacement",
)

#: Périodes couvertes par la colonne date (3 ans) — assez large pour qu'un `between`
#: de période retienne une fraction non triviale des lignes.
DATE_ORIGIN = date(2023, 1, 1)
DATE_SPAN_DAYS = 1095

#: Valeurs de date volontairement non conformes, injectées dans la variante `dirty`.
#: La première tombe à l'index 7, donc DANS l'échantillon de 200 lignes : c'est ce qui
#: garantit que la colonne bascule en `text` de façon reproductible. Une intruse
#: apparaissant seulement après la 200ᵉ ligne produirait un tout autre comportement
#: (typage `date` puis échec `mixed_column_type` au chargement) — cf. `DIRTY_LATE_INDEX`.
INVALID_DATES = ("2026-13-45", "31/02/2025", "n/a", "2025-02-30", "aucune")

#: Périodicités des anomalies. Toutes premières entre elles pour que les anomalies ne
#: se superposent pas systématiquement sur les mêmes lignes.
# Chaque anomalie porte un DÉCALAGE distinct (`== k`, pas `== 0`).
#
# Sans ces décalages, la ligne 0 satisfaisait TOUTES les conditions à la fois : elle
# était simultanément l'unique ligne « Corse » d'un petit corpus, une ligne à montant
# nul et une ligne à quantité nulle. Le filtre sélectif ne retenait donc qu'une ligne
# sans montant, et la somme sortait en `empty_selection` — un défaut du corpus, pris
# à tort pour un défaut du moteur.
NULL_MONTANT_EVERY, NULL_MONTANT_OFFSET = 137, 11
NULL_DATE_EVERY, NULL_DATE_OFFSET = 211, 23
NULL_TEXTE_EVERY, NULL_TEXTE_OFFSET = 89, 7
NULL_QUANTITE_EVERY, NULL_QUANTITE_OFFSET = 173, 31
NEGATIVE_MONTANT_EVERY, NEGATIVE_MONTANT_OFFSET = 313, 41
#: La modalité rare tombe sur `== 1` : elle est ainsi présente dès ~1 000 lignes ET
#: ne coïncide avec aucune anomalie.
RARE_REGION_EVERY, RARE_REGION_OFFSET = 499, 1
DUPLICATE_EVERY, DUPLICATE_OFFSET = 500, 53
INVALID_DATE_EVERY, INVALID_DATE_OFFSET = 331, 67
INVALID_DATE_OFFSET = 7


@dataclass(frozen=True)
class VolumeFile:
    """Fichier généré, décrit par ce qui permet de le citer dans un rapport."""

    name: str
    path: Path
    kind: str                      # "csv" | "xlsx"
    row_count: int                 # lignes de données réellement écrites
    column_count: int
    size_bytes: int
    sha256: str
    variant: str = ""              # "clean" | "dirty" | ""
    note: str = ""

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "path": str(self.path), "kind": self.kind,
            "row_count": self.row_count, "column_count": self.column_count,
            "size_bytes": self.size_bytes, "size_mb": round(self.size_mb, 2),
            "sha256": self.sha256, "variant": self.variant, "note": self.note,
        }


def corpus_dir() -> Path:
    """Répertoire des fichiers générés. JAMAIS dans le dépôt.

    Surchargable par `TABULAR_VOLUME_DIR` pour pouvoir viser un disque plus rapide ou
    plus grand que `/tmp` sans toucher au code.
    """
    raw = os.environ.get("TABULAR_VOLUME_DIR")
    base = Path(raw) if raw else Path(os.environ.get("TMPDIR", "/tmp")) / "ragatm_tabular_volume"
    base.mkdir(parents=True, exist_ok=True)
    return base


# --------------------------------------------------------------------------- #
# Génération des lignes
# --------------------------------------------------------------------------- #
def _texte(i: int) -> str:
    """Texte court contenant le DÉLIMITEUR, des guillemets et des accents.

    Trois pièges en une seule cellule : `csv.writer` doit la citer, `csv.Sniffer` doit
    continuer à voir `;` comme séparateur, et le décodage doit rester en UTF-8.
    """
    return f'Réf. "{i % 997:03d}"; lot n°{i % 13} — évalué, à contrôler'


def _row(i: int, rng: Random, *, dirty: bool) -> list[str]:
    """Une ligne de données, entièrement déterminée par `i` et l'état de `rng`.

    Les modalités sont dérivées de `i` (et non de `rng`) pour que la SÉLECTIVITÉ des
    filtres soit calculable exactement : « une ligne sur 997 » est une garantie, pas une
    espérance statistique.
    """
    # -- date ---------------------------------------------------------------
    if i % NULL_DATE_EVERY == NULL_DATE_OFFSET:
        d = ""
    elif dirty and i % INVALID_DATE_EVERY == INVALID_DATE_OFFSET:
        d = INVALID_DATES[(i // INVALID_DATE_EVERY) % len(INVALID_DATES)]
    else:
        d = (DATE_ORIGIN + timedelta(days=i % DATE_SPAN_DAYS)).isoformat()

    # -- identifiant : zéro initial TOUJOURS présent (i < 10^7) --------------
    identifiant = f"{i % 10_000_000:08d}"

    region = REGION_RARE if i % RARE_REGION_EVERY == RARE_REGION_OFFSET else REGIONS[i % len(REGIONS)]
    categorie = CATEGORIES[i % len(CATEGORIES)]
    statut = STATUTS[i % len(STATUTS)]

    # -- quantité : entière, parfois négative, parfois nulle -----------------
    quantite = "" if i % NULL_QUANTITE_EVERY == NULL_QUANTITE_OFFSET else str((i % 250) - 25)

    # -- montants : deux décimales EXACTEMENT ------------------------------
    # Deux décimales et non trois : `types._AMBIGUOUS_SINGLE_SEPARATOR` refuse
    # `12.345` (1.234 ou 1234 selon la locale) et ferait basculer la colonne en `text`.
    prix = rng.randrange(50, 250_00) / 100.0
    montant = rng.randrange(1_00, 5_000_00) / 100.0
    if i % NEGATIVE_MONTANT_EVERY == 5:
        montant = -montant
    if i % NULL_MONTANT_EVERY == NULL_MONTANT_OFFSET:
        montant_ht = montant_ttc = ""
    else:
        montant_ht = f"{montant:.2f}"
        montant_ttc = f"{montant * 1.2:.2f}"

    texte = "" if i % NULL_TEXTE_EVERY == NULL_TEXTE_OFFSET else _texte(i)

    return [d, identifiant, categorie, region, quantite,
            f"{prix:.2f}", montant_ht, montant_ttc, statut, texte]


def csv_bytes(row_count: int, *, dirty: bool) -> bytes:
    """Octets exacts d'un CSV de `row_count` lignes de données.

    Les doublons sont des répétitions VERBATIM de la ligne précédente : un doublon
    « presque identique » ne testerait pas la même chose (un `count_distinct` doit
    voir deux lignes rigoureusement égales).
    """
    rng = Random(SEED)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=DELIMITER, lineterminator="\n")
    writer.writerow(HEADERS)

    previous: list[str] | None = None
    written = 0
    i = 0
    while written < row_count:
        if previous is not None and i % DUPLICATE_EVERY == DUPLICATE_EVERY - 1:
            row = list(previous)
        else:
            row = _row(i, rng, dirty=dirty)
        writer.writerow(row)
        previous = row
        written += 1
        i += 1
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- #
# Écriture + empreinte
# --------------------------------------------------------------------------- #
def _write(path: Path, data: bytes) -> tuple[int, str]:
    path.write_bytes(data)
    return len(data), hashlib.sha256(data).hexdigest()


def generate_csv(row_count: int, *, dirty: bool, directory: Path | None = None,
                 force: bool = False) -> VolumeFile:
    """Écrit un CSV volumétrique et renvoie sa description.

    Le fichier est réutilisé s'il existe déjà avec la bonne taille : régénérer un
    million de lignes à chaque exécution coûterait plus cher que la mesure elle-même,
    et le déterminisme garantit que le fichier présent est bien celui attendu.
    """
    directory = directory or corpus_dir()
    variant = "dirty" if dirty else "clean"
    name = f"volume_{row_count}_{variant}.csv"
    path = directory / name

    if force or not path.exists():
        size, digest = _write(path, csv_bytes(row_count, dirty=dirty))
    else:
        data = path.read_bytes()
        size, digest = len(data), hashlib.sha256(data).hexdigest()

    return VolumeFile(
        name=name, path=path, kind="csv", row_count=row_count,
        column_count=len(HEADERS), size_bytes=size, sha256=digest, variant=variant,
        note=("minorité de dates invalides -> colonne Date typée `text`"
              if dirty else "dates toutes valides -> colonne Date typée `date`"),
    )


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #
def _typed_cell(value: str, header: str):
    """Convertit une cellule CSV en valeur Python typée pour openpyxl.

    Un XLSX qui ne contiendrait que du texte ne testerait pas le même chemin : le
    catalogue infère alors les types depuis les objets Python (`int`, `float`, `date`),
    pas depuis des chaînes. On écrit donc de vrais types.
    """
    if value == "":
        return None
    if header == "Date":
        try:
            y, m, d = value.split("-")
            return date(int(y), int(m), int(d))
        except ValueError:
            return value          # date invalide : reste une chaîne (variante dirty)
    if header == "Identifiant":
        return value              # zéro initial : DOIT rester du texte
    if header == "Quantité":
        return int(value)
    if header in ("Prix unitaire", "Montant HT", "Montant TTC"):
        return float(value)
    return value


def _write_workbook(path: Path, sheets: dict[str, list[list]]) -> None:
    """Écrit un classeur en mode `write_only` (indispensable au-delà de ~50 k lignes)."""
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    wb.save(path)


def _rows_typed(row_count: int, *, dirty: bool) -> list[list]:
    rng = Random(SEED)
    out: list[list] = [list(HEADERS)]
    previous: list[str] | None = None
    for i in range(row_count):
        if previous is not None and i % DUPLICATE_EVERY == DUPLICATE_EVERY - 1:
            raw = list(previous)
        else:
            raw = _row(i, rng, dirty=dirty)
        previous = raw
        out.append([_typed_cell(v, h) for v, h in zip(raw, HEADERS)])
    return out


def _describe(path: Path, *, name: str, kind: str, row_count: int,
              column_count: int, note: str = "", variant: str = "") -> VolumeFile:
    data = path.read_bytes()
    return VolumeFile(name=name, path=path, kind=kind, row_count=row_count,
                      column_count=column_count, size_bytes=len(data),
                      sha256=hashlib.sha256(data).hexdigest(), note=note,
                      variant=variant)


def generate_xlsx_simple(row_count: int, *, directory: Path | None = None,
                         force: bool = False) -> VolumeFile:
    """Classeur mono-feuille, colonnes typées, mêmes anomalies que le CSV `clean`."""
    directory = directory or corpus_dir()
    name = f"volume_{row_count}.xlsx"
    path = directory / name
    if force or not path.exists():
        _write_workbook(path, {"Ventes": _rows_typed(row_count, dirty=False)})
    return _describe(path, name=name, kind="xlsx", row_count=row_count,
                     column_count=len(HEADERS), variant="clean",
                     note="mono-feuille, types natifs Excel")


def generate_xlsx_multisheet(rows_per_sheet: int = 20_000,
                             directory: Path | None = None,
                             force: bool = False) -> VolumeFile:
    """Trois feuilles exploitables : éprouve `table_selector` à volume."""
    directory = directory or corpus_dir()
    name = "volume_multisheet.xlsx"
    path = directory / name
    if force or not path.exists():
        base = _rows_typed(rows_per_sheet, dirty=False)
        _write_workbook(path, {
            "Ventes 2023": base,
            "Ventes 2024": base,
            "Ventes 2025": base,
        })
    return _describe(path, name=name, kind="xlsx", row_count=rows_per_sheet * 3,
                     column_count=len(HEADERS), variant="clean",
                     note=f"3 feuilles de {rows_per_sheet} lignes")


#: Motif d'une cellule openpyxl porteuse de formule, dans le XML de feuille. openpyxl
#: n'écrit JAMAIS de valeur en cache (`<v>`) : il n'évalue pas les formules. Pour
#: fabriquer le cas « formule AVEC valeur en cache » — le cas normal d'un classeur
#: enregistré par Excel — il faut injecter le `<v>` soi-même dans le XML.
#: openpyxl écrit en réalité `<c r="H2"><f>G2*1.2</f><v></v></c>` — un conteneur de
#: valeur VIDE. C'est exactement l'état « formule jamais recalculée ». On remplace ce
#: `<v></v>` (ou son absence) par une valeur réelle.
_FORMULA_CELL = re.compile(rb"(<c\b[^>]*>)(<f>[^<]*</f>)(?:<v></v>)?")


def _inject_cached_values(path: Path, value: bytes = b"1234.56") -> int:
    """Ajoute une valeur en cache à chaque cellule de formule. Renvoie le compte.

    Réécrit l'archive ZIP en place. Sans cela, il est IMPOSSIBLE de produire avec
    openpyxl seul un classeur « formules calculées », donc impossible de vérifier que
    le moteur distingue « formule avec cache » de « formule sans cache ».
    """
    injected = 0
    with zipfile.ZipFile(path) as zin:
        entries = [(item, zin.read(item.filename)) for item in zin.infolist()]

    out: list[tuple[zipfile.ZipInfo, bytes]] = []
    for item, payload in entries:
        if item.filename.startswith("xl/worksheets/") and item.filename.endswith(".xml"):
            payload, count = _FORMULA_CELL.subn(
                rb"\1\2<v>" + value + rb"</v>", payload)
            injected += count
        out.append((item, payload))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item, payload in out:
            zout.writestr(item.filename, payload)
    return injected


def _formula_sheet(row_count: int, *, formula_rows: int) -> list[list]:
    """Feuille dont les `formula_rows` premières lignes portent une formule en `Montant TTC`."""
    rows = _rows_typed(row_count, dirty=False)
    ttc = HEADERS.index("Montant TTC")
    for r in range(1, min(formula_rows, row_count) + 1):
        excel_row = r + 1                       # +1 : ligne 1 = en-têtes
        rows[r][ttc] = f"=G{excel_row}*1.2"     # G = Montant HT (7ᵉ colonne)
    return rows


def generate_xlsx_formulas_cached(row_count: int = 5_000,
                                  directory: Path | None = None,
                                  force: bool = False) -> VolumeFile:
    """Classeur à formules AVEC valeurs en cache (cas d'un fichier enregistré par Excel)."""
    directory = directory or corpus_dir()
    name = "volume_formules_avec_cache.xlsx"
    path = directory / name
    injected = 0
    if force or not path.exists():
        _write_workbook(path, {"Ventes": _formula_sheet(row_count, formula_rows=50)})
        injected = _inject_cached_values(path)
    return _describe(path, name=name, kind="xlsx", row_count=row_count,
                     column_count=len(HEADERS), variant="clean",
                     note=f"50 formules avec valeur en cache (injectées : {injected})")


def generate_xlsx_formulas_uncached(row_count: int = 5_000,
                                    directory: Path | None = None,
                                    force: bool = False) -> VolumeFile:
    """Classeur à quelques formules SANS valeur en cache (jamais recalculé)."""
    directory = directory or corpus_dir()
    name = "volume_formules_sans_cache.xlsx"
    path = directory / name
    if force or not path.exists():
        _write_workbook(path, {"Ventes": _formula_sheet(row_count, formula_rows=5)})
    return _describe(path, name=name, kind="xlsx", row_count=row_count,
                     column_count=len(HEADERS), variant="clean",
                     note="5 formules sans valeur en cache")


def generate_xlsx_mixed_types(row_count: int = 20_000,
                              directory: Path | None = None,
                              force: bool = False) -> VolumeFile:
    """Colonne `Montant HT` mêlant nombres et chaînes — inexploitable pour une somme.

    L'intruse est placée dans l'échantillon (200 premières lignes) : l'inférence doit
    voir le mélange et retomber en `text`, ce qui fera REFUSER la somme. Le mode
    d'échec inacceptable serait une somme partielle rendue avec le statut `ok`.
    """
    directory = directory or corpus_dir()
    name = "volume_types_mixtes.xlsx"
    path = directory / name
    if force or not path.exists():
        rows = _rows_typed(row_count, dirty=False)
        ht = HEADERS.index("Montant HT")
        for r in (3, 17, 42, 130):
            if r < len(rows):
                rows[r][ht] = "non communiqué"
        _write_workbook(path, {"Ventes": rows})
    return _describe(path, name=name, kind="xlsx", row_count=row_count,
                     column_count=len(HEADERS), variant="clean",
                     note="4 valeurs textuelles dans une colonne numérique")


# --------------------------------------------------------------------------- #
# Assemblage
# --------------------------------------------------------------------------- #
#: Tailles de la qualification volumétrique complète.
FULL_SIZES = (100_000, 500_000, 1_000_000)
#: Taille de la variante rapide (CI, tests unitaires).
FAST_SIZE = 20_000


def build_csv_corpus(sizes=FULL_SIZES, *, directory: Path | None = None,
                     force: bool = False) -> dict[str, VolumeFile]:
    """Les deux variantes (`clean`, `dirty`) pour chaque taille demandée."""
    directory = directory or corpus_dir()
    out: dict[str, VolumeFile] = {}
    for size in sizes:
        for dirty in (False, True):
            f = generate_csv(size, dirty=dirty, directory=directory, force=force)
            out[f.name] = f
    return out


def build_xlsx_corpus(*, directory: Path | None = None, force: bool = False,
                      simple_rows: int = 100_000) -> dict[str, VolumeFile]:
    directory = directory or corpus_dir()
    files = [
        generate_xlsx_simple(simple_rows, directory=directory, force=force),
        generate_xlsx_multisheet(directory=directory, force=force),
        generate_xlsx_formulas_cached(directory=directory, force=force),
        generate_xlsx_formulas_uncached(directory=directory, force=force),
        generate_xlsx_mixed_types(directory=directory, force=force),
    ]
    return {f.name: f for f in files}
