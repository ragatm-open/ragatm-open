"""L'espace doit être résolu AVANT le DTO, sinon les gardes vectorielles sont inertes.

`QueryExecutionContext` capture `request.rag_space`. Tant que la résolution de repli
avait lieu *après* la construction du DTO, un appelant ne passant que `rag_space_id`
faisait voyager `rag_space=None` jusqu'au `candidate_retriever`, qui en déduisait
`embedding_provider=None`, `embedding_model=None`, `pipeline_family=None`.

Conséquence mesurée sur un espace GOUVERNÉ dont tous les documents étaient marqués
`reindex_required` : la recherche vectorielle rendait 24 chunks d'un autre pipeline
au lieu de 0. Le chat n'était pas touché (il passe l'objet), mais tout l'outillage de
benchmark et de holdout mesurait autre chose que la production.
"""
import pytest

from apps.rag_core.models import RagSpace
from apps.rag_core.services.query.orchestrator import query_rag
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def governed_space(db):
    ws = Workspace.objects.create(name="ORS-WS")
    return RagSpace.objects.create(
        workspace=ws, name="ORS-GOV",
        embedding_provider="ai_gateway", embedding_model="fr-en-retrieval",
    )


def _capture_vector_kwargs(monkeypatch):
    """Intercepte la recherche vectorielle réellement exécutée."""
    seen = {}
    import apps.rag_core.services.query.orchestrator as orch

    def _spy(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(orch, "search_chunks_vector", _spy)
    return seen


def test_le_filtre_vectoriel_est_arme_sans_objet_espace(governed_space, monkeypatch, settings):
    """`query_rag(rag_space_id=...)` seul doit armer les mêmes gardes que le chat."""
    settings.EMBEDDINGS_PROVIDER = "stub"  # pas d'appel réseau
    seen = _capture_vector_kwargs(monkeypatch)

    query_rag(rag_space_id=str(governed_space.id), question="une question quelconque")

    assert seen, "la recherche vectorielle n'a pas été appelée"
    assert seen.get("embedding_provider") == "ai_gateway"
    assert seen.get("embedding_model") == "fr-en-retrieval"


def test_l_objet_espace_explicite_donne_le_meme_resultat(governed_space, monkeypatch, settings):
    """Passer l'objet ou l'identifiant ne doit rien changer — c'était l'écart."""
    settings.EMBEDDINGS_PROVIDER = "stub"
    seen_id = _capture_vector_kwargs(monkeypatch)
    query_rag(rag_space_id=str(governed_space.id), question="une question quelconque")
    by_id = dict(seen_id)

    seen_obj = _capture_vector_kwargs(monkeypatch)
    query_rag(rag_space_id=str(governed_space.id), question="une question quelconque",
              rag_space=governed_space)
    by_obj = dict(seen_obj)

    for k in ("embedding_provider", "embedding_model"):
        assert by_id.get(k) == by_obj.get(k), f"divergence sur {k}"


# ---------------------------------------------------------------------------
# §36 — la mesure doit s'exécuter dans le MÊME contexte que la production
# ---------------------------------------------------------------------------

def test_le_filtre_de_famille_gouvernee_est_arme_sans_objet_espace(
    governed_space, monkeypatch, settings
):
    """Sur un espace gouverné, la famille de pipeline doit être posée dans la requête.

    C'est la propriété qui donne sa valeur au statut `qualified` du catalogue : un
    benchmark qui mesure un leg vectoriel NON filtré mesure autre chose que la
    production, et sa conclusion ne vaut rien.
    """
    settings.EMBEDDINGS_PROVIDER = "stub"
    seen = _capture_vector_kwargs(monkeypatch)

    query_rag(rag_space_id=str(governed_space.id), question="une question quelconque")

    assert seen.get("embedding_provider") == "ai_gateway"
    assert seen.get("embedding_model") == "fr-en-retrieval"
    # Le stub produit une identité legacy : ce qui compte ici est qu'une famille soit
    # TRANSMISE, pas laquelle. Une famille absente signifierait un filtre désarmé.
    assert seen.get("pipeline_family"), "aucune famille transmise : le filtre est désarmé"


def test_tous_les_appelants_query_rag_du_depot_beneficient_de_la_resolution():
    """Aucun appelant ne peut contourner la résolution — elle est DANS `query_rag`.

    Le test lit le dépôt plutôt que d'énumérer une liste : une liste se périme, et
    c'est précisément un appelant oublié qui a produit le défaut. Six appels ne
    passent que `rag_space_id` (benchmarks, holdouts, sweeps) ; ils sont corrects
    parce que la résolution précède désormais la construction du DTO.
    """
    import inspect

    from apps.rag_core.services.query import orchestrator

    src = inspect.getsource(orchestrator.query_rag)
    resolution = src.index("RagSpace.objects.select_related")
    dto = src.index("QueryRequest.from_legacy_args")
    assert resolution < dto, (
        "la résolution de l'espace doit précéder la construction du DTO : "
        "QueryExecutionContext capture request.rag_space, et un DTO construit trop tôt "
        "fige rag_space=None pour tous les appelants qui ne passent que l'identifiant"
    )
