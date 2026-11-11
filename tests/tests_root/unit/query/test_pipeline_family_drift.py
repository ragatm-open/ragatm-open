"""Une famille de pipeline qui DÉRIVE ne doit pas produire un silence.

La garde existante refuse une famille ABSENTE de la réponse d'embeddings. Elle ne dit
rien du cas plus insidieux : une famille bien présente, mais différente de celle sous
laquelle le corpus a été encodé. La Gateway peut en effet changer les poids d'un modèle
sans changer son nom — le `pipeline_id` porte alors un autre digest.

Le filtre SQL fait exactement ce qu'on lui demande : il ne trouve rien. Et un vectoriel
vide est indistinguable d'un espace qui n'a pas la réponse. Le système répondait donc
« je ne sais pas » sur un corpus qui contient la réponse, sans le moindre signal.

Signalé le 23/08/2026 par l'audit AIGW-WORKLOAD-ISOLATION de la Gateway.
"""
import pytest

from apps.llm_router.embedding_pipeline import TASK_QUERY
from apps.llm_router.embeddings import embed_texts, embed_texts_with_identity
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.repositories.chunk_repository import (
    pipeline_families_present,
    search_chunks_vector,
)
from apps.rag_core.services.query.candidate_retriever import retrieve_candidates
from apps.rag_core.services.query.contracts import (
    PreparedQuery,
    QueryExecutionContext,
    QueryRequest,
    RetrievalPolicy,
)
from apps.rag_core.services.query.errors import RetrievalInvariantError
from apps.rag_core.services.query.trace import RetrievalTraceBuilder
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

DIM = 768
QUESTION = "obligations contractuelles"
TEXTE = "obligations et delais du contrat"


@pytest.fixture(autouse=True)
def _stub(settings):
    settings.EMBEDDINGS_PROVIDER = "stub"


@pytest.fixture(autouse=True)
def _no_corpus(monkeypatch):
    import apps.corpus.services.retrieval as cr
    monkeypatch.setattr(cr, "allowed_corpus_ids_for_workspace", lambda ws: [])


@pytest.fixture
def espace(db):
    ws = Workspace.objects.create(name="DRIFT-WS")
    return RagSpace.objects.create(
        workspace=ws, name="DRIFT-GOV",
        embedding_provider="ai_gateway", embedding_model="fr-en-retrieval",
    )


def _famille_courante(space) -> str:
    """La famille que le chemin de requête produit — jamais un littéral, qui se
    périmerait en silence dès que la Gateway change de format d'identité."""
    return embed_texts_with_identity(
        ["sonde"], dim=DIM, provider=space.embedding_provider,
        model=space.embedding_model, task=TASK_QUERY,
    ).family


def _document(space, *, famille, reindex=False, texte=TEXTE):
    doc = Document.objects.create(
        rag_space=space, filename=f"{famille[:16]}-{reindex}.pdf",
        status=DocumentStatus.READY,
        embedding_provider=space.embedding_provider,
        embedding_model=space.embedding_model,
        reindex_required=reindex,
    )
    Chunk.objects.create(
        rag_space=space, document=doc, chunk_index=0, text=texte, page_number=1, meta={},
        embedding=embed_texts([texte], dim=DIM, provider=space.embedding_provider,
                              model=space.embedding_model)[0],
        pipeline_family=famille,
    )
    return doc


def _ctx(space):
    req = QueryRequest.from_legacy_args(
        rag_space_id=space.id, question=QUESTION, rag_space=space
    )
    ctx = QueryExecutionContext.create(
        req, started_at=0.0, trace=RetrievalTraceBuilder(entrypoint="internal", request_id="rid")
    )
    ctx.prepared = PreparedQuery.from_resolved(
        original_question=QUESTION, queries_for_search=[QUESTION],
        primary_retrieval_query=QUESTION, inventory=False, explanatory=False,
        news=False, has_temporal_intent=False, comparative=False,
    )
    ctx.policy = RetrievalPolicy(
        fts_required=True, hybrid_enabled=True, vector_available=True,
        temporal_enabled=False, rerank_enabled=False, diversity_enabled=False,
        min_sources=1, min_text_rank=0.0, min_score=0.0, top_k=4, vector_top_k=20,
        candidate_k=4, lexical_weight=1.0,
        rerank_trigger_min_candidates=1, rerank_top_k=4,
    )
    return ctx


def _lancer(space):
    return retrieve_candidates(
        _ctx(space), fts_search=lambda **k: [], vector_search=search_chunks_vector
    )


# --- Inventaire des familles ------------------------------------------------
def test_l_inventaire_voit_la_famille_etrangere(espace):
    _document(espace, famille="legacy:ai_gateway:fr-en-retrieval:768:AUTRE-DIGEST")
    inventaire = pipeline_families_present(
        rag_space_id=espace.id,
        embedding_provider=espace.embedding_provider,
        embedding_model=espace.embedding_model,
    )
    assert inventaire == {"legacy:ai_gateway:fr-en-retrieval:768:AUTRE-DIGEST": 1}


def test_l_inventaire_applique_les_memes_conditions_d_eligibilite(espace):
    """Un document à réindexer n'est pas comparable : il ne doit pas peser dans le
    diagnostic, sans quoi une dérive serait annoncée là où il n'y a qu'un document
    en attente."""
    _document(espace, famille="famille-obsolete", reindex=True)
    assert pipeline_families_present(
        rag_space_id=espace.id,
        embedding_provider=espace.embedding_provider,
        embedding_model=espace.embedding_model,
    ) == {}


# --- La garde ---------------------------------------------------------------
def test_une_famille_etrangere_fait_echouer_la_recherche(espace):
    _document(espace, famille="legacy:ai_gateway:fr-en-retrieval:768:AUTRE-DIGEST")
    with pytest.raises(RetrievalInvariantError) as exc:
        _lancer(espace)
    message = str(exc.value)
    assert "famille de pipeline" in message
    assert "AUTRE-DIGEST" in message, "le message doit nommer ce qui a été trouvé"


def test_la_bonne_famille_ne_declenche_rien(espace):
    """Contrôle : la garde ne doit pas se déclencher quand tout va bien."""
    _document(espace, famille=_famille_courante(espace))
    merged = _lancer(espace)
    assert merged, "le fragment attendu doit remonter"


def test_un_espace_vide_reste_vide_sans_crier_a_la_derive(espace):
    """Zéro source n'est pas zéro source POUR CETTE RAISON. Un espace sans document
    comparable doit rendre une réponse vide, pas une erreur d'invariant."""
    assert _lancer(espace) == []


def test_un_espace_non_gouverne_est_inchange(db):
    """Le revers exigé à chaque lot : réparer les espaces gouvernés ne doit rien
    changer aux autres. Ici la famille du chunk est étrangère ET la recherche vide —
    la combinaison exacte qui déclenche la garde en mode gouverné."""
    ws = Workspace.objects.create(name="DRIFT-WS-LEGACY")
    space = RagSpace.objects.create(
        workspace=ws, name="DRIFT-LEGACY",
        embedding_provider="ollama", embedding_model="nomic-embed-text",
    )
    _document(space, famille="famille-sans-rapport")
    _lancer(space)  # ne lève pas
