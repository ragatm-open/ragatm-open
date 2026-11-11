"""Concurrence sur le chemin CSV rapide — isolation d'abord, débit ensuite.

La baseline historique était de ~0,1 requête/s sans aucune montée en charge, mesurée
AVANT le pont CSV natif. Elle n'est plus représentative : le pont passe l'essentiel du
travail dans le C++ de DuckDB, qui relâche le GIL.

Le contrôle décisif de ce fichier n'est pas le débit mais l'**isolation**. Chaque
document porte une sentinelle distincte ; une réponse portant la sentinelle d'un autre
document prouverait une fuite d'état entre requêtes. Un débit flatteur obtenu au prix
d'un résultat croisé serait un échec, pas un progrès.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest

from apps.rag_core.tabular import catalog as C
from apps.rag_core.tabular import deadline as DL
from apps.rag_core.tabular import executor as EX
from apps.rag_core.tabular import plan_compiler as PC
from apps.rag_core.tabular import plan_schema as PS

pytestmark = pytest.mark.tabular_concurrency

#: Assez de lignes pour passer par le pont (`CSV_BRIDGE_MIN_ROWS`), assez peu pour que
#: la suite reste rapide.
ROWS = 6_000


@dataclass(frozen=True)
class _Limits:
    max_rows_scanned: int = 5_000_000
    max_columns: int = 256
    max_filters: int = 16
    max_result_rows: int = 1_000
    max_groups: int = 1_000
    max_query_seconds: float = 120.0
    max_memory_mb: int = 512


def _corpus(sentinelle: str) -> bytes:
    lignes = ["Reference;Region;Montant"]
    lignes += [f"{i:08d};{sentinelle};{i}.50" for i in range(ROWS)]
    return "\n".join(lignes).encode("utf-8")


def _bundle(sentinelle: str):
    data = _corpus(sentinelle)
    cat = C.build_csv_catalog(data, document_id=uuid.uuid4(),
                              content_sha256="0" * 64, filename=f"{sentinelle}.csv")
    table = cat.tables[0]
    rows = C.load_rows(data, table, is_csv=True, max_rows=10 ** 9)
    cols = [(PC.column_alias(c.position), c.inferred_type) for c in table.columns]
    return cat, table, rows, cols


def _somme_filtree(bundle, sentinelle: str):
    cat, table, rows, cols = bundle
    montant = next(c for c in table.columns if c.original_name == "Montant")
    region = next(c for c in table.columns if c.original_name == "Region")
    plan = PS.parse_plan({
        "schema_version": "1.0", "operation": "sum",
        "source": {"document_id": str(cat.document_id),
                   "sheet_id": str(table.sheet_id),
                   "table_id": str(table.table_id)},
        "target": {"column_id": str(montant.column_id)},
        "filters": [{"column_id": str(region.column_id),
                     "operator": "equals", "value": sentinelle}]})
    query = PC.compile_plan(
        plan, positions=table.positions(), max_result_rows=1_000,
        column_types={c.column_id: c.inferred_type for c in table.columns})
    outcome = EX.execute(query, columns=cols, rows=rows, limits=_Limits(),
                         deadline=DL.Deadline.from_limits(_Limits()))
    return outcome.rows[0][0], plan.plan_hash()


ATTENDU = sum(Decimal(f"{i}.50") for i in range(ROWS))


@pytest.fixture(scope="module")
def bundles():
    return {"SENTA": _bundle("SENTA"), "SENTB": _bundle("SENTB")}


@pytest.mark.parametrize("workers", [2, 5])
def test_aucune_fuite_entre_documents_concurrents(bundles, workers):
    """Deux documents, deux sentinelles : aucun résultat ne doit traverser.

    C'est le contrôle qui compte. Une fuite d'état se manifeste par une valeur ou un
    `plan_hash` appartenant à l'autre document, bien avant de produire une exception.
    """
    resultats: list[tuple[str, Decimal, str]] = []
    erreurs: list[str] = []
    verrou = threading.Lock()

    def tache(i: int) -> None:
        sentinelle = "SENTA" if i % 2 == 0 else "SENTB"
        try:
            valeur, plan_hash = _somme_filtree(bundles[sentinelle], sentinelle)
            with verrou:
                resultats.append((sentinelle, valeur, plan_hash))
        except Exception as exc:                      # noqa: BLE001 - remonté tel quel
            with verrou:
                erreurs.append(f"{type(exc).__name__}: {exc}")

    fils = [threading.Thread(target=tache, args=(i,)) for i in range(workers)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()

    assert not erreurs, erreurs
    assert len(resultats) == workers

    # Chaque requête rend la somme de SON document.
    for _sentinelle, valeur, _ in resultats:
        assert valeur == ATTENDU

    # Un `plan_hash` par document, et deux documents distincts en produisent deux.
    par_sentinelle: dict[str, set[str]] = {}
    for sentinelle, _, plan_hash in resultats:
        par_sentinelle.setdefault(sentinelle, set()).add(plan_hash)
    for sentinelle, hashes in par_sentinelle.items():
        assert len(hashes) == 1, f"{sentinelle} : plan instable sous concurrence"
    if len(par_sentinelle) > 1:
        tous = [h for s in par_sentinelle.values() for h in s]
        assert len(set(tous)) == len(par_sentinelle), (
            "deux documents partagent un plan_hash : la preuve n'est pas rattachée "
            "à son document")


def test_le_resultat_concurrent_est_identique_au_sequentiel(bundles):
    """Le déterminisme ne doit pas dépendre du parallélisme."""
    sequentiel, hash_seq = _somme_filtree(bundles["SENTA"], "SENTA")

    obtenus: list[tuple[Decimal, str]] = []
    verrou = threading.Lock()

    def tache() -> None:
        valeur, plan_hash = _somme_filtree(bundles["SENTA"], "SENTA")
        with verrou:
            obtenus.append((valeur, plan_hash))

    fils = [threading.Thread(target=tache) for _ in range(5)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()

    assert all(v == sequentiel for v, _ in obtenus)
    assert all(h == hash_seq for _, h in obtenus)


def test_aucun_etat_mutable_partage_entre_executions():
    """Chaque exécution ouvre SA connexion et SON fichier temporaire.

    Une connexion ou un répertoire partagés seraient le vecteur d'une fuite : c'est
    donc une propriété de conception, vérifiée sur le source.
    """
    import inspect

    source = inspect.getsource(EX)
    assert "duckdb.connect(\":memory:\")" in source
    # Le fichier canonique vit dans un TemporaryDirectory créé par appel.
    assert "tempfile.TemporaryDirectory" in inspect.getsource(EX._ingest_via_csv_bridge)
    # Aucune connexion au niveau module.
    assert "\n_CONN" not in source and "\nCONNECTION" not in source
