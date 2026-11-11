"""Non-régression : pas de défaut fantôme de retrieval dans `domain/policies.py`.

`DEFAULT_TOP_K=6` et `DEFAULT_VECTOR_TOP_K=40` ont vécu dans ce module en
contredisant `settings.base` (8 et 8). Ils n'étaient ni des champs de `RagPolicy`,
ni importés par personne : du code mort qui faisait croire, à la lecture, à une
divergence de réglage — et qui aurait piégé quiconque aurait construit une
`RagPolicy` partielle.

La valeur effective de `top_k`/`vector_top_k` vient de `settings`, lue par
l'orchestrateur. Ce test empêche la réintroduction d'une seconde source.
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from apps.rag_core.domain import policies


GHOST_CONSTANTS = ("DEFAULT_TOP_K", "DEFAULT_VECTOR_TOP_K")


@pytest.mark.parametrize("name", GHOST_CONSTANTS)
def test_ghost_default_is_not_reintroduced(name: str):
    assert not hasattr(policies, name), (
        f"{name} est un défaut fantôme : la valeur effective vient de settings, "
        "lue par services/query/orchestrator.py. Ne pas le redéclarer ici."
    )


@pytest.mark.parametrize("field", ("top_k", "vector_top_k"))
def test_ragpolicy_has_no_retrieval_size_field(field: str):
    """`RagPolicy` ne porte pas ces tailles : un champ y créerait un défaut caché."""
    noms = {f.name for f in dataclasses.fields(policies.RagPolicy)}
    assert field not in noms


def test_orchestrator_reads_the_sizes_from_settings():
    """Verrou de la source de vérité : `settings.RAG_TOP_K`, pas une constante locale."""
    from apps.rag_core.services.query import orchestrator

    source = inspect.getsource(orchestrator)
    assert 'getattr(settings, "RAG_TOP_K"' in source
    assert 'getattr(settings, "RAG_VECTOR_TOP_K"' in source


def test_remaining_field_defaults_agree_with_settings():
    """Les défauts de champs CONSERVÉS ne contredisent pas les settings.

    Ils ne pilotent rien — l'orchestrateur passe tout explicitement — mais un écart
    piégerait une construction partielle, exactement ce qui s'est produit avec les
    deux constantes supprimées.

    `DEFAULT_RERANK_TOP_K` est volontairement EXCLU : il vaut 5 quand
    `settings.RAG_RERANK_TOP_K` vaut 4, divergence réelle mais hors périmètre de ce
    lot (elle exige de toucher `test_min_text_rank_semantics`, qui construit une
    `RagPolicy` partielle). Consignée comme dette ; ne pas l'assertir ici tant
    qu'elle n'est pas tranchée, sous peine d'un test dépendant du `.env` local.
    """
    from django.conf import settings

    assert policies.DEFAULT_MIN_SCORE == pytest.approx(float(settings.RAG_MIN_SCORE))
    assert policies.DEFAULT_MIN_SOURCES == int(settings.RAG_MIN_SOURCES)
    assert policies.DEFAULT_RERANK_ENABLED == bool(settings.RAG_RERANK_ENABLED)
