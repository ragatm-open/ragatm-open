"""Sécurité du pont CSV natif — le verrou se referme, et définitivement.

Le pont ouvre TEMPORAIREMENT l'accès disque de DuckDB, ce qui touche au verrou
central du moteur. Le compromis n'est acceptable que si trois propriétés tiennent, et
ce fichier existe pour les figer :

1. une seule instruction s'exécute pendant la fenêtre, dont le texte est une constante
   du module et le chemin un paramètre lié pointant vers un fichier que nous venons
   d'écrire ;
2. le verrou se referme AVANT que le plan de l'utilisateur ne s'exécute ;
3. il est IRRÉVERSIBLE — DuckDB refuse de réactiver l'accès sur une base en cours.

Sans la troisième, les deux premières ne vaudraient rien : il suffirait au plan de
rouvrir la porte.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from apps.rag_core.tabular import executor as EX


@dataclass(frozen=True)
class _Limits:
    max_query_seconds: float = 600.0
    max_memory_mb: int = 512
    max_rows_scanned: int = 5_000_000
    max_columns: int = 256
    max_result_rows: int = 1_000


COLUMNS = [("c0", "currency")]


def _rows(n: int):
    return [(Decimal(i),) for i in range(n)]


@pytest.fixture
def conn_apres_pont():
    """Connexion AYANT servi au pont, laissée OUVERTE.

    L'ouverture est essentielle : sur une connexion fermée, toute requête échoue en
    `ConnectionException` et le test passerait sans rien prouver du verrou.
    """
    conn = EX._connect(memory_limit_mb=512, defer_lock=True)
    EX._ingest_via_csv_bridge(conn, COLUMNS, _rows(EX.CSV_BRIDGE_MIN_ROWS + 1),
                              limits=_Limits())
    yield conn
    conn.close()


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_parquet('/etc/passwd')",
    "SELECT * FROM read_csv('/tmp/*.csv')",              # glob
    "SELECT * FROM read_csv('https://example.com/a.csv')",  # URL
    "SELECT * FROM read_csv('../../etc/passwd')",        # traversée
    "COPY t TO '/tmp/fuite.csv'",
    "ATTACH '/tmp/autre.db'",
    "INSTALL httpfs",
    "LOAD httpfs",
])
def test_le_verrou_est_referme_apres_le_pont(conn_apres_pont, sql):
    """Tous les vecteurs d'accès externe échouent, connexion encore ouverte."""
    with pytest.raises(Exception) as exc:
        conn_apres_pont.execute(sql)
    assert "Permission" in type(exc.value).__name__ or "Invalid" in type(exc.value).__name__, (
        f"{sql!r} n'a pas été bloqué par le verrou : {type(exc.value).__name__}")


def test_le_verrou_est_irreversible(conn_apres_pont):
    """La propriété qui rend le compromis acceptable.

    Si le plan pouvait rouvrir l'accès, refermer le verrou après l'ingestion ne
    servirait à rien. DuckDB refuse la réactivation sur une base en cours d'exécution.
    """
    with pytest.raises(Exception) as exc:
        conn_apres_pont.execute("SET enable_external_access = true")
    assert "Invalid" in type(exc.value).__name__


def test_la_table_reste_interrogeable_apres_le_verrou(conn_apres_pont):
    """Le verrou ferme l'accès disque, pas la table déjà chargée."""
    total = conn_apres_pont.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert total == EX.CSV_BRIDGE_MIN_ROWS + 1


def test_le_chemin_du_pont_nest_jamais_influence_par_lexterieur():
    """Le chemin vient d'un `TemporaryDirectory` interne, jamais d'une entrée.

    Contrôle par le SOURCE : le SQL du pont ne doit contenir aucune interpolation de
    chemin — seulement un paramètre lié.
    """
    import inspect

    source = inspect.getsource(EX._ingest_via_csv_bridge)
    assert "tempfile.TemporaryDirectory" in source
    assert "read_csv(?" in source, "le chemin doit être un PARAMÈTRE, pas une interpolation"
    assert "0o600" in source


def test_en_dessous_du_seuil_le_pont_nest_pas_utilise():
    """Sous le seuil, le chemin historique reste en place — verrou posé d'emblée."""
    conn = EX._connect(memory_limit_mb=512, defer_lock=False)
    try:
        with pytest.raises(Exception) as exc:
            conn.execute("SELECT * FROM read_csv('/etc/passwd')")
        assert "Permission" in type(exc.value).__name__
    finally:
        conn.close()
