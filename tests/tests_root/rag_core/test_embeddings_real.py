"""Smoke-test opt-in des vrais embeddings (B5).

Ces tests valident que la *qualité sémantique* peut être mesurée avec un vrai
provider — chose impossible avec le stub. Ils sont EXCLUS de la voie standard par
le marqueur `external` et activés explicitement :

    RAG_RUN_EXTERNAL_TESTS=1 RAG_BENCH_REAL_EMBEDDINGS=1 EMBEDDINGS_PROVIDER=ollama \\
        python -m pytest -m "external and embeddings" ../tests/tests_root -q

Deux verrous, volontairement distincts : `RAG_RUN_EXTERNAL_TESTS` autorise la
famille externe, `RAG_BENCH_REAL_EMBEDDINGS` atteste qu'un provider réel est bien
configuré. Aucun des deux ne touche le réseau à la collecte.
"""
import os

import pytest

from testing.external import external

pytestmark = external(pytest.mark.embeddings)

RUN = os.getenv("RAG_BENCH_REAL_EMBEDDINGS", "").strip().lower() in ("1", "true", "yes")

skip_reason = "défini RAG_BENCH_REAL_EMBEDDINGS=1 + EMBEDDINGS_PROVIDER réel pour exécuter"


@pytest.mark.skipif(not RUN, reason=skip_reason)
def test_real_embeddings_have_expected_dimension(settings):
    from django.conf import settings as dj_settings

    from apps.llm_router.embeddings import embed_texts

    provider = os.getenv("EMBEDDINGS_PROVIDER", "stub").strip().lower()
    assert provider != "stub", "EMBEDDINGS_PROVIDER doit être un vrai provider"

    dim = int(getattr(dj_settings, "RAG_EMBEDDING_DIM", 768))
    vectors = embed_texts(["Paris est la capitale de la France."], dim=dim)

    assert len(vectors) == 1
    assert len(vectors[0]) == dim
    # un vrai embedding n'est pas tout-à-zéro
    assert any(abs(x) > 0.0 for x in vectors[0])


@pytest.mark.skipif(not RUN, reason=skip_reason)
def test_real_embeddings_semantic_similarity():
    """Proximité sémantique sans recouvrement lexical (le stub en est incapable)."""
    import math

    from apps.llm_router.embeddings import embed_texts

    a, b, c = embed_texts(
        [
            "Comment réinitialiser mon mot de passe ?",
            "Procédure de récupération des identifiants oubliés.",
            "Recette traditionnelle de la tarte aux pommes.",
        ]
    )

    def cos(u, v):
        dot = sum(x * y for x, y in zip(u, v))
        nu = math.sqrt(sum(x * x for x in u))
        nv = math.sqrt(sum(y * y for y in v))
        return dot / (nu * nv) if nu and nv else 0.0

    # a (mot de passe) plus proche de b (identifiants) que de c (tarte)
    assert cos(a, b) > cos(a, c)
