"""Porte d'entrée UNIQUE des tests dépendant d'un service réel.

Un test « externe » est un test qui a besoin d'autre chose que du dépôt et d'une
base Postgres : l'AI Gateway, un serveur Ollama, un provider
d'embeddings réel, un corpus non versionné. La voie standard ne doit jamais en
exécuter un — ni son corps, ni sa *sonde de disponibilité*.

Deux verrous, et le second est le point important :

1. le marqueur ``external``, qui les retire de la sélection standard
   (``-m "not tabular_volume and not external"``) ;
2. l'activation explicite ``RAG_RUN_EXTERNAL_TESTS=1``, lue ici **sans aucun
   accès réseau** — un simple `os.getenv`.

La sonde réseau elle-même (« la Gateway répond-elle ? ») ne doit être appelée
qu'à l'EXÉCUTION du test, via :func:`skip_unless_reachable`. Un
``pytest.mark.skipif(not _sonde(), ...)`` écrit au niveau module s'évalue à
l'IMPORT, donc pendant la *collecte* : la suite standard émettait ainsi deux
requêtes réseau (Gateway `:8100` timeout 5 s, Ollama `:11434` timeout 3 s) avant
d'avoir lancé le moindre test, et exécutait pour de bon les tests concernés dès
que ces services répondaient sur la machine. Le résultat de la suite dépendait
donc du poste — exactement ce que le forçage du conftest cherche à empêcher.
"""
from __future__ import annotations

import os
from typing import Callable

import pytest

#: Activation explicite. Aucune valeur par défaut « pratique » : un test externe
#: ne doit jamais partir tout seul.
RUN_EXTERNAL_TESTS = os.getenv("RAG_RUN_EXTERNAL_TESTS", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

SKIP_REASON = (
    "test externe (service réel requis) : poser RAG_RUN_EXTERNAL_TESTS=1, "
    "ou lancer `make test-backend-external`"
)

#: Marqueur de saut, évalué à l'import mais SANS réseau (lecture d'environnement).
requires_external = pytest.mark.skipif(not RUN_EXTERNAL_TESTS, reason=SKIP_REASON)


def external(*extra_marks):
    """Renvoie le `pytestmark` d'un module externe.

    Usage::

        from testing.external import external
        pytestmark = external(pytest.mark.integration)

    Le marqueur de NATURE (`integration`, `embeddings`) est conservé : il dit ce
    que le test exerce, là où `external` dit à quelle fréquence il tourne.
    """
    return [pytest.mark.external, requires_external, *extra_marks]


def skip_unless_reachable(probe: Callable[[], bool], service: str) -> None:
    """Saute le test si le service est injoignable — sonde appelée MAINTENANT.

    À appeler depuis le corps du test ou depuis une fixture, jamais au niveau
    module : c'est ce qui garantit qu'aucune requête ne part pendant la collecte.
    """
    if not probe():
        pytest.skip(f"{service} injoignable")
