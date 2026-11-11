"""Le plafond de temps et le plafond mémoire couvrent l'INGESTION, pas seulement le SELECT.

Défaut trouvé pendant la qualification volumétrique, dans du code livré au lot 1 :
`_Timeout` n'enveloppait que `conn.execute(query.sql)`. Le chargement des lignes
(`executemany`) était donc HORS budget — et à un million de lignes, le chargement est
de très loin l'étape la plus longue. Mesuré : 34 s d'ingestion sous un plafond de
0,5 s, rendues avec un statut `ok`.

Deuxième surprise : déplacer l'ingestion dans la garde ne suffisait pas.
`conn.interrupt()` ne peut pas arrêter un `executemany` — chaque INSERT unitaire est
court, la boucle est côté Python, l'interruption tombe entre deux instructions sans
rien annuler. La seule borne fiable est un contrôle explicite d'échéance entre lots.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest

from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import executor as EX
from apps.rag_core.tabular import plan_compiler as PC
from apps.rag_core.tabular import plan_schema as PS


@dataclass(frozen=True)
class _Limits:
    max_rows_scanned: int = 2_000_000
    max_columns: int = 256
    max_filters: int = 16
    max_result_rows: int = 1_000
    max_groups: int = 1_000
    max_query_seconds: float = 60.0
    max_memory_mb: int = 512


DOC, SHEET, TABLE, COLUMN = (uuid.uuid4() for _ in range(4))
COLUMNS = [("c0", "currency")]


def _query():
    plan = PS.parse_plan({
        "schema_version": "1.0", "operation": "sum",
        "source": {"document_id": str(DOC), "sheet_id": str(SHEET),
                   "table_id": str(TABLE)},
        "target": {"column_id": str(COLUMN)}})
    return PC.compile_plan(plan, positions={COLUMN: 0}, max_result_rows=1_000)


def _rows(n: int):
    return [(Decimal(i) / 100,) for i in range(n)]


def test_le_delai_borne_le_chargement_pas_seulement_la_requete():
    """Le défaut d'origine : 34 s sous un plafond de 0,5 s, rendues `ok`."""
    started = time.perf_counter()
    with pytest.raises(E.TabularLimitExceeded) as exc:
        EX.execute(_query(), columns=COLUMNS, rows=_rows(400_000),
                   limits=_Limits(max_query_seconds=0.5))
    elapsed = time.perf_counter() - started

    assert exc.value.code == E.ERR_QUERY_TIMEOUT
    # La borne est vérifiée entre LOTS : le dépassement se mesure en fractions de
    # seconde, pas en dizaines. Sans le découpage, ce test durait 34 s et passait.
    assert elapsed < 10.0, f"le plafond n'a pas borné le chargement ({elapsed:.1f}s)"


def test_le_pont_csv_natif_respecte_aussi_lecheance():
    """Le chemin rapide ne doit pas être un contournement du budget.

    Le pont CSV est 119× plus rapide, donc une même volumétrie n'y déclenche plus le
    délai — c'est le but. Mais l'écriture du fichier canonique reste une boucle
    Python : sans contrôle d'échéance, un très gros fichier repasserait hors budget.
    """
    from apps.rag_core.tabular import executor as _EX

    assert _EX.CSV_BRIDGE_MIN_ROWS > 0
    started = time.perf_counter()
    with pytest.raises(E.TabularLimitExceeded) as exc:
        EX.execute(_query(), columns=COLUMNS, rows=_rows(2_000_000),
                   limits=_Limits(max_query_seconds=0.5))
    assert exc.value.code == E.ERR_QUERY_TIMEOUT
    assert time.perf_counter() - started < 15.0


def test_un_depassement_memoire_pendant_le_chargement_est_typé():
    """Sans le correctif : `duckdb.OutOfMemoryException` nue → 500.

    Le délai est volontairement généreux pour ISOLER la mémoire — sinon l'échéance
    se déclenche d'abord et masque ce qu'on veut mesurer.
    """
    with pytest.raises(E.TabularLimitExceeded) as exc:
        EX.execute(_query(), columns=COLUMNS, rows=_rows(400_000),
                   limits=_Limits(max_memory_mb=1, max_query_seconds=600.0))
    assert exc.value.code == E.ERR_MEMORY_EXCEEDED


def test_aucun_resultat_partiel_nest_rendu_apres_une_limite():
    """Les deux gardes LÈVENT : aucune valeur ne remonte, donc rien de partiel."""
    with pytest.raises(E.TabularLimitExceeded):
        EX.execute(_query(), columns=COLUMNS, rows=_rows(400_000),
                   limits=_Limits(max_query_seconds=0.5))


def test_la_requete_suivante_reste_executable_apres_un_depassement():
    """Une connexion mal nettoyée rendrait le service inutilisable après un incident."""
    with pytest.raises(E.TabularLimitExceeded):
        EX.execute(_query(), columns=COLUMNS, rows=_rows(400_000),
                   limits=_Limits(max_query_seconds=0.5))

    outcome = EX.execute(_query(), columns=COLUMNS, rows=_rows(100),
                         limits=_Limits())
    assert outcome.rows[0][0] == sum((Decimal(i) / 100 for i in range(100)),
                                     Decimal(0))


def test_le_decoupage_dingestion_est_une_constante_nommee():
    """La granularité du contrôle d'échéance doit être lisible, pas enfouie."""
    assert EX.INGEST_CHUNK_ROWS >= 1_000
