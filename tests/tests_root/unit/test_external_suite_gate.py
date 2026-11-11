"""Garde : la voie standard n'active jamais les tests à service réel.

Ce module ne vérifie pas « aucun appel réseau n'a eu lieu » — une telle
introspection serait fragile et coûteuse (la suite parle à PostgreSQL en
permanence). Il verrouille les trois conditions de CONFIGURATION dont découle
l'absence d'appel, qui sont, elles, vérifiables directement :

1. sans `RAG_RUN_EXTERNAL_TESTS=1`, la famille externe est désactivée ;
2. le marqueur `external` est déclaré dans la config pytest unique ;
3. la voie standard du Makefile l'exclut explicitement.

Motif : les sondes de disponibilité (`_gateway_reachable`, `_ollama_has_nomic`)
vivaient dans des `skipif` de niveau module, donc évaluées à la COLLECTE. La
suite « standard » émettait deux requêtes réseau avant son premier test, et
exécutait pour de bon les tests concernés dès que la Gateway ou Ollama
répondaient sur la machine — 14,8 s de benchmark sémantique réel, mesurées. Son
résultat dépendait donc du poste, ce que le forçage de `conftest.py` cherche
précisément à empêcher.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from testing.external import RUN_EXTERNAL_TESTS

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_la_famille_externe_est_desactivee_par_defaut():
    """La suite standard tourne sans `RAG_RUN_EXTERNAL_TESTS` : rien n'est activé.

    Si ce test échoue, c'est que l'environnement courant autorise les tests
    externes — l'exécution en cours n'est donc pas la voie standard.
    """
    assert RUN_EXTERNAL_TESTS is False, (
        "RAG_RUN_EXTERNAL_TESTS est posé : cette exécution peut atteindre des "
        "services réels et n'est pas la voie standard."
    )


def test_le_marqueur_external_est_declare():
    """Un marqueur non déclaré serait silencieusement inopérant sous -m."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    markers = config["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("external:") for m in markers), markers


def test_la_voie_standard_du_makefile_exclut_external():
    """L'exclusion doit vivre dans la commande, pas seulement dans les intentions.

    Le dépôt a déjà payé ce défaut : `test_tabular_volume.py` se déclarait « exclu
    de la suite par défaut » dans son en-tête alors qu'aucun `-m` ne l'appliquait.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "PYTEST_STANDARD_MARKERS := not tabular_volume and not external" in makefile, (
        "la sélection standard n'exclut plus `external` (ou a été renommée)"
    )
