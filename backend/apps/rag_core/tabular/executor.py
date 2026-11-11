"""Exécution DuckDB — connexion éphémère, en mémoire, verrouillée.

Le principe de confinement de ce module tient en une phrase : **DuckDB ne voit jamais
un fichier.** Les lignes lui sont fournies depuis Python, par insertion paramétrée dans
une table temporaire. Il n'y a donc aucun chemin à valider, aucun `read_csv`, aucun
`read_parquet`, aucun `ATTACH` utile — la surface d'attaque « lire un fichier arbitraire »
n'existe pas, elle est absente par construction plutôt que filtrée.

S'y ajoutent des verrous explicites, parce qu'une propriété non testée n'est pas une
propriété :

- connexion **`:memory:`**, créée puis fermée par requête, jamais partagée ;
- `enable_external_access = false` — coupe `read_csv`, `read_parquet`, `COPY … TO`,
  `ATTACH` vers un fichier, et l'installation d'extensions ;
- `autoinstall_known_extensions` / `autoload_known_extensions` à `false` ;
- `memory_limit` et `threads` bornés par la configuration ;
- une seule instruction par requête (le compilateur n'en produit jamais deux) ;
- délai maximal appliqué côté Python, avec interruption de la connexion.

Aucun texte SQL ne vient d'ailleurs que de `plan_compiler`. Ce module n'accepte pas de
chaîne SQL de l'extérieur : sa signature prend un `CompiledQuery`.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from apps.rag_core.tabular import deadline as DL
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import plan_compiler as PC

logger = logging.getLogger(__name__)

#: L'ingestion est découpée en lots, et le délai est vérifié ENTRE les lots.
#:
#: `conn.interrupt()` ne peut pas arrêter un `executemany` : chaque INSERT unitaire
#: est court, la boucle est côté Python, et l'interruption tombe donc systématiquement
#: entre deux instructions sans rien annuler. Mesuré : 34 s d'ingestion sous un
#: plafond de 0,5 s, rendues avec un statut `ok`. La seule borne fiable est un
#: contrôle explicite d'échéance — d'où ce découpage.
#:
#: Plafond de taille d'un lot. Au-delà, la boucle Python coûterait plus que le gain.
INGEST_CHUNK_ROWS = 10_000

#: Premier lot, volontairement PETIT : il sert de sonde de débit. Le contrôle
#: d'échéance n'a lieu qu'ENTRE les lots, donc le dépassement maximal possible est la
#: durée d'UN lot. Avec un lot fixe de 10 000 lignes et un débit mesuré de ~480
#: lignes/s, ce dépassement atteignait 21 s — soit 57× un budget de 0,1 s. Un plafond
#: qu'on dépasse de 57× n'est pas un plafond.
INGEST_PROBE_ROWS = 500

#: Part du budget qu'un lot doit viser. Le dépassement résiduel est donc de l'ordre de
#: 10 % du budget au lieu d'une constante absolue : la borne suit ce qu'on lui demande.
INGEST_CHUNK_BUDGET_RATIO = 0.10

#: Types de colonnes du catalogue -> type DuckDB. `DECIMAL(38, 10)` couvre les montants
#: comptables sans jamais passer par un flottant. Les identifiants restent du TEXTE :
#: c'est ce qui préserve « 001245 ».
_DUCKDB_TYPES = {
    "text": "VARCHAR",
    "identifier": "VARCHAR",
    "boolean": "BOOLEAN",
    "integer": "BIGINT",
    "decimal": "DECIMAL(38, 10)",
    "percentage": "DECIMAL(38, 10)",
    "currency": "DECIMAL(38, 10)",
    "date": "DATE",
    "datetime": "TIMESTAMP",
}


@dataclass(frozen=True)
class ExecutionOutcome:
    """Résultat brut d'exécution, avant mise en forme."""

    rows: tuple[tuple[Any, ...], ...]
    scanned_row_count: int
    returns_rows: bool


class _Timeout:
    """Interrompt la connexion DuckDB au-delà du temps RESTANT de l'échéance.

    Le minuteur est armé sur `deadline.remaining_seconds()`, jamais sur le budget
    initial : une requête qui a déjà consommé huit secondes sur dix ne doit pas se
    voir accorder dix secondes de plus pour son SELECT.

    DuckDB n'expose pas de `statement_timeout` ; `interrupt()` est le mécanisme prévu.
    Le minuteur est armé avant l'exécution et désarmé dans un `finally` — une requête
    rapide ne paie rien.
    """

    def __init__(self, conn, seconds: float):
        self._conn = conn
        self._seconds = max(0.05, float(seconds))
        self._timer: threading.Timer | None = None
        self.fired = False

    def __enter__(self):
        def _fire():
            self.fired = True
            try:
                self._conn.interrupt()
            except Exception:  # pragma: no cover - la connexion peut déjà être close
                logger.debug("tabular: interrupt sur connexion déjà fermée")

        self._timer = threading.Timer(self._seconds, _fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *exc):
        if self._timer is not None:
            self._timer.cancel()
        return False


#: Seuil au-delà duquel le pont CSV natif est préféré à `executemany`. En dessous, le
#: coût fixe du pont (fichier temporaire, ouverture) dépasse son gain.
CSV_BRIDGE_MIN_ROWS = 5_000


def _write_canonical_csv(columns, rows, path, *, deadline=None) -> None:
    """Écrit les lignes DÉJÀ normalisées dans un CSV canonique.

    Point essentiel pour l'exactitude : les valeurs ont traversé `types.coerce` avant
    d'arriver ici. Toute la sémantique du moteur — séparateurs français, pourcentages
    en fraction, devises, zéros initiaux, dates ISO, refus des valeurs invalides — a
    donc déjà été appliquée en Python. Ce fichier ne contient que des formes
    canoniques, que DuckDB relit par un cast exact.
    """
    import csv as _csv

    with open(path, "w", newline="", encoding="utf-8") as flux:
        writer = _csv.writer(flux, delimiter=",", quotechar='"',
                             quoting=_csv.QUOTE_MINIMAL, lineterminator="\n")
        for index, row in enumerate(rows):
            writer.writerow(
                "" if v is None
                else (format(v, "f") if isinstance(v, Decimal) else v)
                for v in row)
            # L'écriture reste une boucle Python : sans contrôle d'échéance ici, un
            # très gros fichier repasserait hors budget — exactement le défaut que le
            # découpage de `_ingest` avait corrigé sur l'autre chemin.
            if deadline is not None and index % 20_000 == 19_999:
                deadline.raise_if_expired(phase="ingestion")


def _ingest_via_csv_bridge(conn, columns, rows, *, limits, deadline=None) -> None:
    """Charge les lignes via le lecteur CSV natif de DuckDB, puis REFERME le verrou.

    Mesuré : 142 000 lignes/s contre 1 193 pour `executemany`, soit 119×. À un million
    de lignes, cela ramène le transfert de ~14 minutes à une dizaine de secondes.

    ---- Analyse de sécurité, car ce chemin ouvre temporairement l'accès disque ----

    `read_csv` exige `enable_external_access`, qui est le verrou central du moteur. Le
    compromis n'est acceptable que parce que DuckDB permet de le refermer, et de le
    refermer DÉFINITIVEMENT :

    1. la connexion est créée avec l'accès actif (défaut de DuckDB) ;
    2. une SEULE instruction s'exécute dans cet état : le `CREATE TABLE` ci-dessous,
       dont le texte est une constante de ce module et dont le chemin est un
       PARAMÈTRE lié, jamais une interpolation ;
    3. le chemin désigne un fichier temporaire que NOUS venons d'écrire, en mode 0600,
       dans un répertoire privé — il ne provient ni du plan, ni de la question, ni du
       stockage utilisateur, et ne peut donc être ni un glob, ni une URL, ni un lien
       symbolique choisi par un tiers ;
    4. les types de colonnes sont donnés explicitement (`columns={...}`) : aucune
       auto-détection, donc aucune surprise de typage ;
    5. `SET enable_external_access = false` referme aussitôt. DuckDB refuse ensuite de
       le réactiver sur une base en cours d'exécution — vérifié : l'opération lève
       `InvalidInputException`. Le verrou est donc à sens unique.

    Le plan de l'utilisateur ne s'exécute qu'APRÈS l'étape 5. Il rencontre exactement
    les mêmes interdictions qu'auparavant : `read_csv`, `read_parquet`, `COPY TO`,
    `ATTACH`, `INSTALL` et `LOAD` échouent tous en `PermissionException` (testé).
    """
    import tempfile

    # Le pont partage l'échéance de la requête. Sans elle, il deviendrait un
    # contournement du plafond : plus rapide, mais non borné.
    deadline = DL.ensure(deadline, limits)

    marks = ", ".join(f"'{alias}': '{_DUCKDB_TYPES[ctype]}'" for alias, ctype in columns)

    with tempfile.TemporaryDirectory(prefix="ragatm_tab_") as repertoire:
        chemin = os.path.join(repertoire, "canonique.csv")
        _write_canonical_csv(columns, rows, chemin, deadline=deadline)
        os.chmod(chemin, 0o600)
        deadline.raise_if_expired(phase="canonical_csv_write")

        # UNIQUE instruction exécutée avec l'accès disque actif. Texte constant,
        # chemin en paramètre lié, types explicites.
        conn.execute(
            f"CREATE TEMP TABLE {PC.TABLE_NAME} AS "
            f"SELECT * FROM read_csv(?, header=false, columns={{{marks}}}, "
            f"nullstr='', quote='\"', escape='\"')",
            [chemin])

    # Verrou refermé AVANT toute exécution du plan utilisateur. Irréversible.
    conn.execute("SET enable_external_access = false")


def _connect(*, memory_limit_mb: int, defer_lock: bool = False):
    """Connexion en mémoire, verrouillée. Chaque verrou a sa raison d'être.

    `defer_lock=True` DIFFÈRE la fermeture de l'accès disque : le pont CSV natif en a
    besoin le temps d'une instruction, et la referme lui-même juste après. Le verrou
    n'est jamais omis, seulement déplacé de quelques millisecondes — et il tombe
    toujours AVANT que le plan de l'utilisateur ne s'exécute.
    """
    import duckdb

    conn = duckdb.connect(":memory:")
    # `enable_external_access=false` est LE verrou : il coupe d'un coup l'accès au
    # système de fichiers, au réseau, à `ATTACH` et à l'installation d'extensions.
    # Les autres réglages sont une défense en profondeur, pas des doublons inutiles.
    verrous = (
        () if defer_lock else ("SET enable_external_access = false",)
    ) + (
        "SET autoinstall_known_extensions = false",
        "SET autoload_known_extensions = false",
        f"SET memory_limit = '{int(memory_limit_mb)}MB'",
        "SET threads = 1",
        # La barre de progression de DuckDB écrit sur stdout : elle pollue les sorties
        # de test et les journaux, et n'a aucun destinataire côté serveur.
        "SET enable_progress_bar = false",
    )
    for statement in verrous:
        conn.execute(statement)
    return conn


def _ddl_create_table(columns: list[tuple[str, str]]) -> str:
    """`CREATE TEMP TABLE` — alias générés, types issus d'une table fermée.

    Les deux moitiés du DDL sont sous contrôle du moteur : les alias viennent de
    `column_alias` (dérivés d'entiers) et les types de `_DUCKDB_TYPES` (liste fermée).
    Aucun nom d'origine ne participe.
    """
    parts = []
    for alias, ctype in columns:
        sql_type = _DUCKDB_TYPES.get(ctype)
        if sql_type is None:
            raise E.TabularError(
                E.ERR_INCOMPATIBLE_COLUMN_TYPE, f"type de colonne inconnu : {ctype!r}")
        parts.append(f"{alias} {sql_type}")
    return f"CREATE TEMP TABLE {PC.TABLE_NAME} ({', '.join(parts)})"


def execute(query: PC.CompiledQuery, *, columns: list[tuple[str, str]],
            rows: list[tuple[Any, ...]], limits,
            deadline: "DL.Deadline | None" = None) -> ExecutionOutcome:
    """Exécute une requête compilée sur des lignes fournies EN MÉMOIRE.

    `columns` : `(alias, type_catalogue)` dans l'ordre des colonnes de `rows`.
    `rows`    : lignes déjà normalisées et typées par le chargeur.

    Rien d'autre n'entre : ni chemin, ni nom de fichier, ni SQL externe.
    """
    if len(rows) > limits.max_rows_scanned:
        raise E.TabularLimitExceeded(
            E.ERR_TOO_MANY_ROWS,
            f"{len(rows)} lignes à parcourir, plafond {limits.max_rows_scanned}",
            scanned=len(rows), limit=limits.max_rows_scanned)
    if len(columns) > limits.max_columns:
        raise E.TabularLimitExceeded(
            E.ERR_TOO_MANY_COLUMNS,
            f"{len(columns)} colonnes, plafond {limits.max_columns}")

    # Au-delà du seuil, le pont CSV natif l'emporte largement (119× mesuré). En
    # dessous, son coût fixe — fichier temporaire, ouverture — dépasse son gain.
    deadline = DL.ensure(deadline, limits)
    deadline.raise_if_expired(phase="execute_start")
    via_pont = len(rows) >= CSV_BRIDGE_MIN_ROWS
    conn = _connect(memory_limit_mb=limits.max_memory_mb, defer_lock=via_pont)
    try:
        # Le délai et la traduction des erreurs couvrent TOUTE l'opération, pas
        # seulement le SELECT. C'est le correctif de qualification staging : le
        # chargement des lignes (`executemany`) était hors des deux gardes, si bien
        # qu'une requête d'une minute sortait `ok` sous un plafond de 0,1 s, et qu'un
        # dépassement mémoire pendant l'insertion remontait en `OutOfMemoryException`
        # brute — un 500, au lieu du `limit_exceeded` contractuel. Sur un million de
        # lignes, l'insertion est de loin l'étape la plus longue : la laisser hors du
        # budget revenait à ne pas avoir de budget.
        with _Timeout(conn, deadline.remaining_seconds()) as guard:
            try:
                if via_pont:
                    # Crée la table ET la remplit, puis referme le verrou.
                    _ingest_via_csv_bridge(conn, columns, rows, limits=limits,
                                           deadline=deadline)
                else:
                    conn.execute(_ddl_create_table(columns))
                    _ingest(conn, columns, rows, limits=limits, deadline=deadline)
                deadline.mark("duckdb_import")
                deadline.raise_if_expired(phase="before_query")
                cursor = conn.execute(query.sql, list(query.params))
                fetched = cursor.fetchall()
            except E.TabularError:
                raise
            except Exception as exc:
                if guard.fired:
                    raise E.TabularLimitExceeded(
                        E.ERR_QUERY_TIMEOUT,
                        "exécution DuckDB interrompue : budget épuisé",
                        phase="duckdb_query") from exc
                if _is_memory_error(exc):
                    raise E.TabularLimitExceeded(
                        E.ERR_MEMORY_EXCEEDED,
                        f"mémoire dépassée (plafond {limits.max_memory_mb} Mo)") from exc
                # Toute autre erreur du moteur remonte AVEC sa cause. Elle ne doit
                # jamais devenir un résultat vide : c'est le mode d'échec que la
                # spécification classe NO-GO.
                raise E.TabularError(
                    E.ERR_INVALID_PLAN_SCHEMA,
                    f"échec d'exécution du moteur : {exc}") from exc

        if query.returns_rows and len(fetched) > limits.max_result_rows:
            raise E.TabularLimitExceeded(
                E.ERR_TOO_MANY_RESULT_ROWS,
                f"{len(fetched)} lignes en résultat, plafond {limits.max_result_rows}")

        return ExecutionOutcome(
            rows=tuple(tuple(r) for r in fetched),
            scanned_row_count=len(rows),
            returns_rows=query.returns_rows,
        )
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - fermeture best-effort
            logger.debug("tabular: fermeture de connexion DuckDB en échec")


def _is_memory_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "out of memory" in text or "memory limit" in text


def count_selected(query_where_params, *, columns, rows, where_sql, limits,
                   deadline: "DL.Deadline | None" = None) -> int:
    """Compte les lignes retenues par les filtres — base de `selected_row_count`.

    Recompté par le moteur sur les mêmes filtres, jamais estimé et jamais dérivé du
    résultat : une somme ne dit pas combien de lignes l'ont produite.
    """
    # Le décompte réingère la MÊME table : sans le pont, une requête filtrée payait
    # deux fois un chargement lent, et le gain de `execute` était annulé.
    deadline = DL.ensure(deadline, limits)
    deadline.raise_if_expired(phase="count_selected_start")
    via_pont = len(rows) >= CSV_BRIDGE_MIN_ROWS
    conn = _connect(memory_limit_mb=limits.max_memory_mb, defer_lock=via_pont)
    try:
        # Même budget global que `execute` : l'insertion est dans la garde.
        with _Timeout(conn, deadline.remaining_seconds()) as guard:
            try:
                if via_pont:
                    _ingest_via_csv_bridge(conn, columns, rows, limits=limits)
                else:
                    conn.execute(_ddl_create_table(columns))
                    _ingest(conn, columns, rows, limits=limits, deadline=deadline)
                sql = f"SELECT COUNT(*) FROM {PC.TABLE_NAME}{where_sql}"
                value = conn.execute(sql, list(query_where_params)).fetchone()[0]
            except Exception as exc:
                if guard.fired:
                    raise E.TabularLimitExceeded(
                        E.ERR_QUERY_TIMEOUT,
                        "décompte DuckDB interrompu : budget épuisé",
                        phase="count_selected") from exc
                if _is_memory_error(exc):
                    raise E.TabularLimitExceeded(
                        E.ERR_MEMORY_EXCEEDED,
                        f"mémoire dépassée (plafond {limits.max_memory_mb} Mo)") from exc
                raise
        return int(value)
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            logger.debug("tabular: fermeture de connexion DuckDB en échec")


def to_decimal(value: Any) -> Decimal | None:
    """Convertit un scalaire DuckDB en `Decimal` sans jamais passer par `float`."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # `repr` donne la représentation courte qui reconstruit le même double.
        return Decimal(repr(value))
    return Decimal(str(value))


def _ingest(conn, columns, rows, *, limits, deadline=None) -> None:
    """Charge les lignes par lots, en vérifiant l'échéance ENTRE chaque lot.

    C'est ici que s'applique réellement le plafond de temps sur les gros fichiers : à
    un million de lignes, le chargement domine très largement le SELECT. Une garde qui
    ne couvrirait que la requête laisserait donc l'essentiel du travail hors budget.

    La taille des lots est **adaptative**, et ce n'est pas un raffinement. Le contrôle
    n'ayant lieu qu'entre deux lots, le dépassement maximal est la durée d'un lot : un
    lot fixe de 10 000 lignes représentait 21 s au débit observé, soit 57× un budget de
    0,1 s. On mesure donc le débit sur un petit lot de sonde, puis on dimensionne les
    suivants pour qu'ils visent une fraction du budget.
    """
    if not rows:
        return
    marks = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {PC.TABLE_NAME} VALUES ({marks})"
    deadline = DL.ensure(deadline, limits)
    cible_par_lot = max(0.02, deadline.budget_seconds * INGEST_CHUNK_BUDGET_RATIO)

    total = len(rows)
    position = 0
    taille = min(INGEST_PROBE_ROWS, total)

    while position < total:
        lot = rows[position:position + taille]
        depart = time.monotonic()
        conn.executemany(sql, lot)
        duree = time.monotonic() - depart
        position += len(lot)

        deadline.raise_if_expired(phase="duckdb_ingest")

        # Recalibrage : débit observé -> taille visant `cible_par_lot`.
        if duree > 0:
            debit = len(lot) / duree
            taille = int(debit * cible_par_lot)
        taille = max(INGEST_PROBE_ROWS, min(INGEST_CHUNK_ROWS, taille))
