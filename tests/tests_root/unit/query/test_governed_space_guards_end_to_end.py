"""Les gardes d'un espace gouverné doivent EXCLURE, pas seulement être « armées ».

`test_orchestrator_space_resolution` prouve que `query_rag(rag_space_id=…)` transmet
bien `embedding_provider`, `embedding_model` et `pipeline_family` à la recherche
vectorielle. C'est nécessaire — ce n'est pas suffisant : un paramètre transmis mais
mal interprété en aval laisserait passer exactement les mêmes chunks, et le test
resterait vert.

Ces tests-ci ferment la boucle. Ils construisent trois documents dans un espace
gouverné — famille correcte, famille étrangère, document à réindexer — et vérifient
sur le RÉSULTAT quels fragments survivent au chemin complet, depuis le seul
identifiant d'espace.

Ils vérifient aussi le revers, que le lot d'alignement de code exige explicitement :
un espace NON gouverné ne doit rien changer de son comportement. Une correction qui
répare les espaces gouvernés en modifiant silencieusement les autres serait une
régression déguisée en correctif.
"""
import pytest

from apps.llm_router.embedding_pipeline import TASK_QUERY
from apps.llm_router.embeddings import embed_texts, embed_texts_with_identity
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.repositories.chunk_repository import search_chunks_vector
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

DIM = 768


def _famille(provider: str, model: str) -> str:
    """Famille que le chemin de requête produira — obtenue de la MÊME fonction que la
    production, jamais réécrite en dur : un littéral se périmerait en silence."""
    return embed_texts_with_identity(
        ["sonde"], dim=DIM, provider=provider, model=model, task=TASK_QUERY
    ).family


def _document(space, *, texte, famille, reindex=False):
    doc = Document.objects.create(
        rag_space=space, filename=f"{famille[:12]}-{reindex}.pdf", status=DocumentStatus.READY,
        embedding_provider=space.embedding_provider,
        embedding_model=space.embedding_model,
        reindex_required=reindex,
    )
    Chunk.objects.create(
        rag_space=space, document=doc, chunk_index=0, text=texte, page_number=1, meta={},
        embedding=embed_texts([texte], dim=DIM,
                              provider=space.embedding_provider,
                              model=space.embedding_model)[0],
        pipeline_family=famille,
    )
    return doc


@pytest.fixture
def espace_gouverne(db):
    ws = Workspace.objects.create(name="GG-WS")
    return RagSpace.objects.create(
        workspace=ws, name="GG-GOV",
        embedding_provider="ai_gateway", embedding_model="fr-en-retrieval",
    )


def _recherche(space, question="obligations contractuelles"):
    """Le chemin exact du `candidate_retriever` : mêmes gardes, mêmes paramètres."""
    lot = embed_texts_with_identity(
        [question], dim=DIM, provider=space.embedding_provider,
        model=space.embedding_model, task=TASK_QUERY,
    )
    return search_chunks_vector(
        rag_space_id=space.id, query_embedding=lot.vectors[0], top_k=50,
        embedding_provider=space.embedding_provider,
        embedding_model=space.embedding_model,
        pipeline_family=lot.family,
    )


def test_espace_gouverne_ne_retient_que_la_bonne_famille(espace_gouverne, settings):
    settings.EMBEDDINGS_PROVIDER = "stub"
    bonne = _famille("ai_gateway", "fr-en-retrieval")

    attendu = _document(espace_gouverne, texte="obligations et delais du contrat", famille=bonne)
    etranger = _document(espace_gouverne, texte="obligations et delais du contrat",
                         famille="legacy:stub:AUTRE-MODELE:768:legacy-v1")
    a_reindexer = _document(espace_gouverne, texte="obligations et delais du contrat",
                            famille=bonne, reindex=True)

    trouves = {h["source"]["document_id"] for h in _recherche(espace_gouverne)}

    assert str(attendu.id) in trouves, "le document conforme a été perdu"
    assert str(etranger.id) not in trouves, (
        "un fragment d'une AUTRE famille de pipeline a survécu : la question a été "
        "comparée à des vecteurs incomparables"
    )
    assert str(a_reindexer.id) not in trouves, (
        "un document marqué reindex_required a survécu : son vecteur ne correspond "
        "plus au modèle courant de l'espace"
    )


def test_sans_les_gardes_les_trois_documents_reviennent(espace_gouverne, settings):
    """L'état d'AVANT `dacbf0a`, reproduit à l'identique.

    Quand `rag_space` ne parvient pas au `candidate_retriever`, les trois paramètres
    valent `None` — et c'est ce que produit cet appel. Le contraste avec le test
    précédent EST la mesure du défaut : 3 documents rendus au lieu d'1.
    """
    settings.EMBEDDINGS_PROVIDER = "stub"
    bonne = _famille("ai_gateway", "fr-en-retrieval")
    docs = [
        _document(espace_gouverne, texte="obligations et delais du contrat", famille=bonne),
        _document(espace_gouverne, texte="obligations et delais du contrat",
                  famille="legacy:stub:AUTRE-MODELE:768:legacy-v1"),
        _document(espace_gouverne, texte="obligations et delais du contrat",
                  famille=bonne, reindex=True),
    ]
    lot = embed_texts_with_identity(["obligations contractuelles"], dim=DIM,
                                    provider="ai_gateway", model="fr-en-retrieval",
                                    task=TASK_QUERY)
    sans_gardes = search_chunks_vector(
        rag_space_id=espace_gouverne.id, query_embedding=lot.vectors[0], top_k=50,
        embedding_provider=None, embedding_model=None, pipeline_family=None,
    )
    trouves = {h["source"]["document_id"] for h in sans_gardes}
    assert {str(d.id) for d in docs} <= trouves, (
        "gardes désarmées : les trois documents devaient revenir — si ce n'est plus le "
        "cas, le filtrage a changé de nature et ce test ne mesure plus le même défaut"
    )


def test_un_espace_non_gouverne_garde_son_comportement(db, settings):
    """§13 — la correction ne doit rien changer hors de son domaine.

    Un espace historique n'a pas de famille de pipeline sur ses fragments. Le filtre
    de famille ne doit donc PAS s'appliquer, sans quoi la correction rendrait invisible
    tout le corpus historique.
    """
    settings.EMBEDDINGS_PROVIDER = "stub"
    ws = Workspace.objects.create(name="GG-WS-LEG")
    space = RagSpace.objects.create(
        workspace=ws, name="GG-LEG",
        embedding_provider="openai", embedding_model="text-embedding-3-small",
    )
    doc = Document.objects.create(
        rag_space=space, filename="historique.pdf", status=DocumentStatus.READY,
        embedding_provider=space.embedding_provider,
        embedding_model=space.embedding_model, reindex_required=False,
    )
    texte = "obligations et delais du contrat"
    Chunk.objects.create(
        rag_space=space, document=doc, chunk_index=0, text=texte, page_number=1, meta={},
        embedding=embed_texts([texte], dim=DIM, provider=space.embedding_provider,
                              model=space.embedding_model)[0],
        pipeline_family=None,  # chunk HISTORIQUE : provenance inconnue, jamais rétro-étiquetée
    )

    from apps.llm_router.embeddings import space_is_governed

    assert not space_is_governed(space), "cet espace ne doit pas être gouverné"

    q = embed_texts([texte], dim=DIM, provider=space.embedding_provider,
                    model=space.embedding_model)[0]
    hits = search_chunks_vector(
        rag_space_id=space.id, query_embedding=q, top_k=50,
        embedding_provider=space.embedding_provider,
        embedding_model=space.embedding_model,
        pipeline_family=None,  # mode LEGACY : aucun filtre de famille
    )
    assert str(doc.id) in {h["source"]["document_id"] for h in hits}, (
        "un fragment historique (pipeline_family NULL) a disparu : la correction des "
        "espaces gouvernés a débordé sur les espaces qui ne le sont pas"
    )


def test_armer_les_gardes_ne_change_rien_hors_de_leur_domaine(db, settings):
    """§11 — le correctif ne doit pas déplacer le retrieval là où il n'a rien à dire.

    Fixture choisie pour ne RIEN déclencher : espace non gouverné, documents tous
    cohérents avec le modèle de l'espace, aucun `reindex_required`. Sur ce terrain,
    armer les gardes ne doit rien exclure — donc rendre exactement les mêmes
    candidats, dans le même ordre, aux mêmes scores.

    Vérifié le 22/08/2026 entre l'arbre de production et l'arbre candidat
    par exécution croisée : 6 candidats, scores identiques à la douzième décimale.
    Ce test-ci garde l'invariant sans exiger deux arbres.
    """
    settings.EMBEDDINGS_PROVIDER = "stub"
    ws = Workspace.objects.create(name="GG-WS-PAR")
    space = RagSpace.objects.create(
        workspace=ws, name="GG-PAR",
        embedding_provider="openai", embedding_model="text-embedding-3-small",
    )
    textes = [
        "Les obligations de l'entrepreneur en cas de retard sont definies au contrat.",
        "Le maitre d'ouvrage dispose de trente jours pour formuler ses reserves.",
        "La garantie de parfait achevement couvre un an a compter de la reception.",
        "Les penalites de retard sont plafonnees a cinq pour cent du marche.",
    ]
    for i, texte in enumerate(textes):
        doc = Document.objects.create(
            rag_space=space, filename=f"par-{i:02d}.pdf", status=DocumentStatus.READY,
            embedding_provider=space.embedding_provider,
            embedding_model=space.embedding_model, reindex_required=False,
        )
        Chunk.objects.create(
            rag_space=space, document=doc, chunk_index=0, text=texte, page_number=1, meta={},
            embedding=embed_texts([texte], dim=DIM, provider=space.embedding_provider,
                                  model=space.embedding_model)[0],
        )

    q = embed_texts(["quelles obligations en cas de retard"], dim=DIM,
                    provider=space.embedding_provider, model=space.embedding_model)[0]

    def _empreinte(**kw):
        return [
            (h["source"]["filename"], h["chunk_index"], round(float(h["score"]), 12))
            for h in search_chunks_vector(rag_space_id=space.id, query_embedding=q, top_k=20, **kw)
        ]

    desarmees = _empreinte()
    armees = _empreinte(embedding_provider=space.embedding_provider,
                        embedding_model=space.embedding_model)

    assert len(armees) == len(textes), "des candidats ont disparu alors que rien ne les exclut"
    assert desarmees == armees, (
        "armer les gardes a changé le résultat sur un fixture qui ne les déclenche pas :\n"
        f"  désarmées : {desarmees}\n  armées    : {armees}"
    )
