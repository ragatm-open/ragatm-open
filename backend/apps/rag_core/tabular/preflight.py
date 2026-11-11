"""Préflight volumétrique — refuser AVANT de lire, pas après avoir tout chargé.

Le défaut corrigé ici est opérationnel, pas fonctionnel : un fichier d'un million de
lignes était lu, normalisé et partiellement chargé pendant plusieurs minutes avant que
`TABULAR_MAX_ROWS_SCANNED` ou le délai ne finisse par le refuser. Le refus était juste ;
son coût ne l'était pas.

Deux principes gouvernent ce module.

**Ne jamais lire au-delà de ce qu'il faut pour décider.** Le scan s'arrête à
`max_rows + 1` : une ligne de plus que la limite suffit à prouver le dépassement, et
rien au-delà n'apporte d'information. Un fichier d'un million de lignes sous une limite
de cent mille est donc refusé après cent mille et une lignes lues, pas un million.

**Ne jamais faire confiance à une dimension déclarée.** Un classeur peut annoncer
`A1:XFD1048576` alors qu'il contient trente lignes — c'est même le cas le plus courant.
La dimension sert de signal pour refuser vite quand elle dépasse *déjà* la limite ; elle
ne sert jamais à accepter, ni à établir un compte. Dans le doute, on scanne, borné.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass

from apps.rag_core.tabular import errors as E

logger = logging.getLogger(__name__)

#: Bornes de lecture du XML de feuille pendant le scan. Le scan s'arrête de toute façon
#: au dépassement ; ces bornes protègent le cas pathologique d'une feuille unique
#: gigantesque dont les premières lignes suffiraient à décider.
_SCAN_CHUNK = 1 << 20  # 1 Mio

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass(frozen=True)
class PreflightVerdict:
    """Ce que le préflight a établi, et à quel coût."""

    #: Nombre de lignes de données réellement observées (borné).
    observed_rows: int
    #: Vrai si le scan s'est arrêté parce que la borne était franchie.
    exceeded: bool
    #: Colonnes observées, quand le format permet de les connaître tôt.
    observed_columns: int = 0
    #: Comment la décision a été prise — exposé dans le journal, pas à l'utilisateur.
    method: str = ""

    @property
    def rows_are_exact(self) -> bool:
        """Le compte est exact seulement si le scan est allé jusqu'au bout."""
        return not self.exceeded


def _refuse_rows(limit: int) -> None:
    raise E.TabularLimitExceeded(
        E.ERR_INPUT_ROW_LIMIT,
        f"le document dépasse la limite de {limit} lignes exploitables",
        limit=limit)


def _refuse_columns(observed: int, limit: int) -> None:
    raise E.TabularLimitExceeded(
        E.ERR_INPUT_COLUMN_LIMIT,
        f"le document déclare {observed} colonnes, au-delà de la limite de {limit}",
        limit=limit)


def _refuse_size(observed_mb: float, limit_mb: float, code: str) -> None:
    raise E.TabularLimitExceeded(
        code,
        f"le document pèse {observed_mb:.1f} Mo, au-delà de la limite de {limit_mb} Mo",
        limit=limit_mb)


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def check_csv(data: bytes, *, delimiter: str, limits,
              deadline=None) -> PreflightVerdict:
    """Compte les lignes CSV, en s'arrêtant une ligne après la limite.

    Le comptage passe par `csv.reader`, jamais par un décompte de `\\n`. Une cellule
    peut contenir un saut de ligne entre guillemets — compter les octets `\\n`
    surestimerait alors le nombre de lignes et refuserait un document parfaitement
    valide. C'est le genre d'erreur qui ne se voit qu'en production, sur le fichier
    d'un client.
    """
    max_rows = int(limits.max_rows_scanned)
    max_cols = int(limits.max_columns)

    taille_mb = len(data) / (1024 * 1024)
    plafond_mb = getattr(limits, "max_input_file_mb", 0)
    if plafond_mb and taille_mb > plafond_mb:
        _refuse_size(taille_mb, plafond_mb, E.ERR_INPUT_SIZE_LIMIT)

    text = data.decode("utf-8", errors="replace")
    if text.startswith("﻿"):
        text = text[1:]

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    lignes = 0
    colonnes = 0
    try:
        for index, row in enumerate(reader):
            if index == 0:                      # en-tête : ne compte pas comme donnée
                colonnes = len(row)
                if colonnes > max_cols:
                    _refuse_columns(colonnes, max_cols)
                continue
            if not any(str(c).strip() for c in row):
                continue
            lignes += 1
            if lignes > max_rows:
                # Une ligne de plus que la limite : la preuve est faite, on s'arrête.
                return PreflightVerdict(observed_rows=lignes, exceeded=True,
                                        observed_columns=colonnes,
                                        method="scan_borne")
            if deadline is not None and lignes % 10_000 == 0:
                deadline.raise_if_expired(phase="preflight")
    except csv.Error as exc:
        raise E.TabularError(
            E.ERR_UNSUPPORTED_FILE_FORMAT,
            f"CSV illisible pendant le préflight : {exc}") from exc

    return PreflightVerdict(observed_rows=lignes, exceeded=False,
                            observed_columns=colonnes, method="scan_complet")


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #
def check_xlsx(data: bytes, *, limits, deadline=None) -> PreflightVerdict:
    """Refuse un classeur hors limite sans jamais l'ouvrir avec openpyxl.

    Deux étages. D'abord la **dimension déclarée**, lue dans le XML sans décompresser
    la feuille entière : si elle dépasse déjà la limite, le refus est immédiat et
    quasi gratuit. Ensuite, et seulement si la dimension est absente, incohérente ou
    sous la limite, un **scan borné** des balises `<row>`.

    La dimension ne sert qu'à REFUSER. Un classeur qui déclare `A1:A5` mais contient
    un million de lignes existe ; l'accepter sur sa déclaration serait exactement le
    défaut que `reset_dimensions()` corrige ailleurs dans ce dépôt.
    """
    max_rows = int(limits.max_rows_scanned)
    max_cols = int(limits.max_columns)

    plafond_mb = getattr(limits, "max_uncompressed_mb", 0)

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise E.TabularError(
            E.ERR_UNSUPPORTED_FILE_FORMAT, "classeur illisible") from exc

    with archive:
        if plafond_mb:
            decompresse = sum(i.file_size for i in archive.infolist())
            taille_mb = decompresse / (1024 * 1024)
            if taille_mb > plafond_mb:
                _refuse_size(taille_mb, plafond_mb, E.ERR_UNCOMPRESSED_SIZE_LIMIT)

        feuilles = [n for n in archive.namelist()
                    if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
        if not feuilles:
            return PreflightVerdict(0, False, method="aucune_feuille")

        pire_lignes = 0
        pire_colonnes = 0
        for nom in feuilles:
            declaree = _declared_dimension(archive, nom)
            if declaree is not None:
                lignes_dec, colonnes_dec = declaree
                if colonnes_dec > max_cols:
                    _refuse_columns(colonnes_dec, max_cols)
                if lignes_dec > max_rows + 1:
                    # La déclaration suffit à refuser : aucune décompression de la
                    # feuille n'est nécessaire.
                    return PreflightVerdict(
                        observed_rows=lignes_dec, exceeded=True,
                        observed_columns=colonnes_dec,
                        method="dimension_declaree")

            lignes = _scan_rows(archive, nom, max_rows=max_rows, deadline=deadline)
            pire_lignes = max(pire_lignes, lignes)
            pire_colonnes = max(pire_colonnes, (declaree or (0, 0))[1])
            if lignes > max_rows:
                return PreflightVerdict(observed_rows=lignes, exceeded=True,
                                        observed_columns=pire_colonnes,
                                        method="scan_borne")

        return PreflightVerdict(observed_rows=pire_lignes, exceeded=False,
                                observed_columns=pire_colonnes,
                                method="scan_complet")


def _declared_dimension(archive: zipfile.ZipFile, nom: str) -> tuple[int, int] | None:
    """`(lignes, colonnes)` déclarées, sans décompresser toute la feuille."""
    import re

    try:
        with archive.open(nom) as flux:
            tete = flux.read(4096)
    except (KeyError, OSError):
        return None

    m = re.search(rb'<dimension\s+ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"', tete)
    if not m:
        return None
    try:
        c1, r1, c2, r2 = (m.group(1).decode(), int(m.group(2)),
                          m.group(3).decode(), int(m.group(4)))
    except (ValueError, UnicodeDecodeError):
        return None
    return max(0, r2 - r1), _col_index(c2) - _col_index(c1) + 1


def _col_index(lettres: str) -> int:
    n = 0
    for c in lettres:
        n = n * 26 + (ord(c) - 64)
    return n


def _scan_rows(archive: zipfile.ZipFile, nom: str, *, max_rows: int,
               deadline=None) -> int:
    """Compte les balises `<row` en flux, et s'arrête dès la limite franchie.

    On ne parse pas le XML : on cherche un motif. Un parseur construirait un arbre
    pour une feuille qu'on s'apprête peut-être à refuser.
    """
    import re

    motif = re.compile(rb"<row[ >]")
    total = 0
    reste = b""
    try:
        with archive.open(nom) as flux:
            while True:
                bloc = flux.read(_SCAN_CHUNK)
                if not bloc:
                    break
                fenetre = reste + bloc
                total += len(motif.findall(fenetre))
                if total > max_rows + 1:
                    return total
                if deadline is not None:
                    deadline.raise_if_expired(phase="preflight")
                # On conserve la fin du bloc : une balise peut être coupée en deux.
                reste = fenetre[-8:]
    except (KeyError, OSError) as exc:
        logger.debug("préflight xlsx: feuille %s illisible (%s)", nom, exc)
        return 0
    # La ligne d'en-tête n'est pas une ligne de données.
    return max(0, total - 1)


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #
def check(data: bytes, *, is_csv: bool, delimiter: str = ";", limits,
          deadline=None) -> PreflightVerdict:
    """Refuse tôt, ou laisse passer. Lève `TabularLimitExceeded` en cas de dépassement."""
    verdict = (check_csv(data, delimiter=delimiter, limits=limits, deadline=deadline)
               if is_csv
               else check_xlsx(data, limits=limits, deadline=deadline))

    if verdict.exceeded:
        logger.info("tabular.preflight refus method=%s rows_observed=%d limit=%d",
                    verdict.method, verdict.observed_rows, limits.max_rows_scanned)
        _refuse_rows(int(limits.max_rows_scanned))

    logger.debug("tabular.preflight ok method=%s rows=%d cols=%d",
                 verdict.method, verdict.observed_rows, verdict.observed_columns)
    return verdict
