"""D'où vient réellement l'identité d'embedding d'un espace — et d'où elle ne vient pas.

Le lot EMBEDDING-PARITY a montré qu'un même UUID d'espace était GOUVERNÉ en staging et
NON GOUVERNÉ en production, alors que le code était identique au bit près. La cause
n'est pas un réglage : c'est une VALEUR DE COLONNE sur la ligne `RagSpace`. Le manifeste
de parité, lui, classait `DEFAULT_RAGSPACE_EMBEDDING_*` en « divergence fonctionnelle
interdite », ce qui laissait croire que ces réglages pilotaient l'état des espaces
existants. Ils ne le pilotent pas.

Ces tests fixent la frontière, parce que la confondre coûte une campagne entière :
une qualification menée sur un espace gouverné ne dit rien d'un espace qui ne l'est pas.
"""
import pytest
from django.test import override_settings

from apps.llm_router.embeddings import resolve_space_embedding, space_is_governed
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest.utils import resolve_chunk_budget
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

DIM = 768


def _espace(provider, model, nom="EIR"):
    ws = Workspace.objects.create(name=f"WS-{nom}-{provider}")
    return RagSpace.objects.create(
        workspace=ws, name=f"{nom}-{provider}",
        embedding_provider=provider, embedding_model=model,
    )


# --- L'état gouverné vient de la LIGNE, pas des réglages --------------------

def test_l_etat_gouverne_est_porte_par_la_colonne_de_l_espace():
    assert space_is_governed(_espace("ai_gateway", "legacy-768")) is True
    assert space_is_governed(_espace("ollama", "nomic-embed-text")) is False
    assert space_is_governed(_espace("openai", "text-embedding-3-small")) is False


@override_settings(
    DEFAULT_RAGSPACE_EMBEDDING_PROVIDER="ai_gateway",
    DEFAULT_RAGSPACE_EMBEDDING_MODEL="legacy-768",
)
def test_les_defauts_ne_gouvernent_PAS_un_espace_existant():
    """C'est la question C1 du lot, et la réponse est NON.

    Un espace déjà créé porte ses propres valeurs. Changer les défauts d'environnement
    ne le rend ni gouverné, ni comparable à un espace qui l'est. C'est précisément
    pourquoi aligner `DEFAULT_RAGSPACE_EMBEDDING_*` entre staging et production ne
    corrigerait AUCUNE divergence de pipeline sur les espaces en service.
    """
    existant = _espace("ollama", "nomic-embed-text", nom="EXISTANT")
    existant.refresh_from_db()

    assert resolve_space_embedding(existant) == ("ollama", "nomic-embed-text")
    assert space_is_governed(existant) is False, (
        "le défaut d'environnement a déteint sur un espace existant : la source de "
        "vérité de l'identité d'embedding n'est plus la ligne RagSpace"
    )


@override_settings(
    DEFAULT_RAGSPACE_EMBEDDING_PROVIDER="ai_gateway",
    DEFAULT_RAGSPACE_EMBEDDING_MODEL="legacy-768",
)
def test_les_defauts_s_appliquent_a_la_CREATION_sans_valeur_explicite():
    """Le revers : ils ne sont pas inertes, ils agissent au moment de la création."""
    ws = Workspace.objects.create(name="WS-CREATION")
    nouveau = RagSpace.objects.create(workspace=ws, name="NOUVEAU")
    assert resolve_space_embedding(nouveau) == ("ai_gateway", "legacy-768")
    assert space_is_governed(nouveau) is True


# --- Le découpage suit le PROVIDER, pas le modèle ---------------------------

def test_le_budget_de_decoupage_suit_la_capacite_du_MODELE():
    """Le plafond de découpage appartient au modèle, pas au transport.

    Ce test affirmait l'inverse jusqu'au 22/08/2026 — « le budget suit le provider » —
    parce que c'était alors la règle du code. Elle était exacte tant qu'`ai_gateway`
    n'exposait qu'`fr-en-retrieval`, dont la limite est bien 1 200 caractères. Le
    provider était un proxy EXACT du modèle, et le lot qui l'a écrite le disait déjà :
    « la fenêtre du modèle étant une propriété du modèle et non de l'espace ».

    `legacy-768` a rompu l'équivalence : même provider, plafond de 6 000 caractères
    (vérifié contre les deux Gateway). Découper à 1 200 un espace qui accepte 6 000
    transformait une migration de PROVENANCE d'embedding en migration de STRUCTURE
    documentaire — nouveaux fragments, nouveaux identifiants, corpus non comparable.
    """
    standard = resolve_chunk_budget(_espace("ollama", "nomic-embed-text"))

    # `legacy-768` (6 000 car.) suit la granularité produit : rien à écrêter.
    assert resolve_chunk_budget(_espace("ai_gateway", "legacy-768")) == standard, (
        "un modèle gouverné qui accepte la granularité produit ne doit PAS être "
        "redécoupé : cela rendrait son corpus incomparable sans aucune raison technique"
    )

    # `fr-en-retrieval` (1 200 car.) l'écrête, parce que sa limite est DURE : au-delà,
    # la Gateway refuse le texte et l'indexation du document échoue.
    contraint = resolve_chunk_budget(_espace("ai_gateway", "fr-en-retrieval"))
    assert contraint[0] < standard[0]
    assert contraint[1] < contraint[0], "un chevauchement ≥ la taille n'avancerait plus"

    assert resolve_chunk_budget(None) == standard, "aucun espace -> comportement historique"


def test_un_modele_gouverne_inconnu_se_replie_sur_la_borne_conservatrice():
    """« Inconnu » ne vaut jamais « sans limite ».

    Un modèle absent de la table de capacités doit recevoir la borne prudente, pas la
    granularité produit : un fragment trop long ne dégrade pas la qualité, il fait
    ÉCHOUER son document entier.
    """
    standard = resolve_chunk_budget(_espace("ollama", "nomic-embed-text"))
    for modele in ("modele-jamais-declare", ""):
        budget = resolve_chunk_budget(_espace("ai_gateway", modele, nom=f"INC{len(modele)}"))
        assert budget[0] < standard[0], (
            f"modèle gouverné inconnu « {modele} » : la granularité produit a été "
            "accordée sans preuve que le modèle la supporte"
        )


# --- Le filtre de famille exclut réellement ---------------------------------

def test_un_espace_gouverne_exclut_les_fragments_d_une_autre_famille():
    """Deux familles dans un même espace : une seule doit survivre à la recherche.

    Le cas n'est pas théorique — c'est exactement ce que produirait une migration
    partielle, où une partie du corpus aurait été réindexée et l'autre non.
    """
    from apps.llm_router.embeddings import embed_texts
    from apps.rag_core.repositories.chunk_repository import search_chunks_vector

    space = _espace("ai_gateway", "legacy-768", nom="MIXTE")
    texte = "obligations contractuelles et delais de livraison du marche"
    attendus = {}
    for famille in ("legacy-768@0a109f42", "fr-en-retrieval@1a37926b", None):
        doc = Document.objects.create(
            rag_space=space, filename=f"{famille}.pdf", status=DocumentStatus.READY,
            embedding_provider=space.embedding_provider,
            embedding_model=space.embedding_model, reindex_required=False,
        )
        Chunk.objects.create(
            rag_space=space, document=doc, chunk_index=0, text=texte, page_number=1, meta={},
            embedding=embed_texts([texte], dim=DIM, provider="ollama",
                                  model="nomic-embed-text")[0],
            pipeline_family=famille,
        )
        attendus[famille] = str(doc.id)

    q = embed_texts([texte], dim=DIM, provider="ollama", model="nomic-embed-text")[0]
    hits = search_chunks_vector(
        rag_space_id=space.id, query_embedding=q, top_k=20,
        embedding_provider=space.embedding_provider,
        embedding_model=space.embedding_model,
        pipeline_family="legacy-768@0a109f42",
    )
    trouves = {h["source"]["document_id"] for h in hits}

    assert attendus["legacy-768@0a109f42"] in trouves
    assert attendus["fr-en-retrieval@1a37926b"] not in trouves, (
        "un fragment d'une autre famille a survécu : la question a été comparée à des "
        "vecteurs produits par d'autres poids"
    )
    assert attendus[None] not in trouves, (
        "un fragment SANS famille a survécu. NULL signifie « on ne sait pas », pas "
        "« compatible » — l'égalité SQL doit l'exclure"
    )
