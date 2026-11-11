"""Recalculer les vecteurs sans toucher au corpus — la preuve, pas l'intention.

La primitive ne vaut que si l'on peut affirmer, après coup :

    mêmes documents · mêmes identifiants de fragments · même texte · mêmes métadonnées
    · même FTS  →  seuls le vecteur et la provenance ont changé

Chaque ligne de cette phrase a son test. Les rédiger comme un contrôle global
(« ça marche ») laisserait passer exactement le défaut qui coûte cher : un champ
modifié sans raison, invisible jusqu'au jour où une citation ne résout plus.
"""
import hashlib
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import override_settings

from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest import reembed as R
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

DIM = 768


def _vecteur(graine: float):
    return [graine] * DIM


# Identité OPAQUE (contrat Gateway v2) + famille attestée au format persisté.
PID = "emb-v2:9e883f0fe1d326ef17f86027a1d4a5d8"
FAMILLE = "legacy-768@0a109f42"


class _Lot:
    """Réponse d'embedding mimant `embed_texts_with_identity`."""

    def __init__(self, n, pipeline_id=PID, graine=0.5, dim=DIM, family=FAMILLE):
        self.vectors = [[graine] * dim for _ in range(n)]
        self.pipeline_id = pipeline_id
        # Famille ATTESTÉE à part de l'identifiant (opaque), comme le client gouverné.
        self.family = family


@pytest.fixture
def espace(db):
    ws = Workspace.objects.create(name="WS-REEMBED")
    return RagSpace.objects.create(
        workspace=ws, name="REEMBED",
        embedding_provider="ollama", embedding_model="nomic-embed-text",
    )


@pytest.fixture
def document(espace):
    doc = Document.objects.create(
        rag_space=espace, filename="contrat.pdf", status=DocumentStatus.READY,
        embedding_provider="ollama", embedding_model="nomic-embed-text",
        reindex_required=False,
    )
    for i in range(5):
        Chunk.objects.create(
            rag_space=espace, document=doc, chunk_index=i,
            text=f"Fragment {i} — obligations contractuelles et delais de livraison.",
            page_number=i + 1,
            meta={"chunk_index": i, "page_number": i + 1, "char_start": i * 100,
                  "char_end": (i + 1) * 100, "source": "contrat.pdf"},
            embedding=_vecteur(0.1),
            pipeline_family=None, pipeline_id=None,
        )
    return doc


def _empreinte(doc):
    """Tout ce qui NE DOIT PAS bouger, capturé en une valeur comparable."""
    lignes = []
    for c in Chunk.objects.filter(document=doc).order_by("chunk_index", "id"):
        lignes.append((
            str(c.id), c.chunk_index, hashlib.sha256((c.text or "").encode()).hexdigest(),
            c.page_number, repr(sorted((c.meta or {}).items())), str(c.rag_space_id),
        ))
    return lignes


def _fts(doc):
    """Colonne FTS générée, lue directement — c'est PostgreSQL qui la maintient."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id::text, text_tsv_french::text FROM rag_chunk "
            "WHERE document_id = %s ORDER BY chunk_index, id", [str(doc.id)],
        )
        return cur.fetchall()


@override_settings(RAG_EMBEDDING_DIM=DIM, RAG_EMBED_BATCH_SIZE=2)
def test_le_corpus_est_preserve_au_bit_pres(document):
    """§28-§30 — identifiants, texte, index, page, métadonnées : rien ne bouge."""
    avant = _empreinte(document)
    vecteurs_avant = {str(c.id): list(c.embedding) for c in Chunk.objects.filter(document=document)}

    with patch.object(R, "embed_texts_with_identity", side_effect=lambda t, **k: _Lot(len(t))):
        res = R.reembed_document(document, provider="ai_gateway", model="legacy-768")

    assert _empreinte(document) == avant, "un champ non lié à l'embedding a été modifié"
    assert res.chunks == 5 and not res.skipped

    # …et les vecteurs, eux, ont bien changé.
    for c in Chunk.objects.filter(document=document):
        assert list(c.embedding) != vecteurs_avant[str(c.id)]


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_le_vecteur_fts_genere_est_inchange(document):
    """§26 — garanti par le schéma : la colonne est GENERATED ALWAYS depuis `text`.

    Le test le vérifie tout de même : c'est la garantie qui autorise à dire qu'un
    ré-embedding ne touche pas la recherche lexicale.
    """
    avant = _fts(document)
    assert avant and all(v for _, v in avant), "fixture sans vecteur FTS : test creux"

    with patch.object(R, "embed_texts_with_identity", side_effect=lambda t, **k: _Lot(len(t))):
        R.reembed_document(document, provider="ai_gateway", model="legacy-768")

    assert _fts(document) == avant


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_la_provenance_est_mise_a_jour_sans_etre_fabriquee(document):
    """§8, §22 — `pipeline_family` est la famille ATTESTÉE portée par le lot d'embedding,
    jamais un littéral recomposé à la main ni un découpage de l'identifiant."""
    with patch.object(R, "embed_texts_with_identity", side_effect=lambda t, **k: _Lot(len(t))):
        res = R.reembed_document(document, provider="ai_gateway", model="legacy-768")

    assert res.pipeline_id == PID
    assert res.pipeline_family == FAMILLE
    for c in Chunk.objects.filter(document=document):
        assert c.pipeline_id == res.pipeline_id
        assert c.pipeline_family == res.pipeline_family

    document.refresh_from_db()
    assert (document.embedding_provider, document.embedding_model) == ("ai_gateway", "legacy-768")
    assert document.reindex_required is False


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_le_ragspace_n_est_jamais_modifie(document, espace):
    """§23 — la gouvernance d'un espace est une décision distincte de la provenance
    de ses fragments. Les confondre ferait basculer un espace entier au premier
    document traité."""
    avant = (espace.embedding_provider, espace.embedding_model)
    with patch.object(R, "embed_texts_with_identity", side_effect=lambda t, **k: _Lot(len(t))):
        R.reembed_document(document, provider="ai_gateway", model="legacy-768")
    espace.refresh_from_db()
    assert (espace.embedding_provider, espace.embedding_model) == avant


# --- Ce que la primitive ne doit JAMAIS faire ------------------------------

def test_le_module_n_appelle_ni_extraction_ni_decoupage():
    """§10, §11 — prouvé sur la SYNTAXE, pas sur le texte.

    Un mock prouve qu'un chemin donné n'a pas appelé l'extraction ce jour-là. Analyser
    la source prouve qu'aucun chemin ne le peut.

    L'analyse porte sur les APPELS et les IMPORTS, jamais sur le texte : une première
    version cherchait les noms interdits dans la source brute et échouait sur la
    docstring qui explique justement que ces fonctions ne sont pas appelées. Le même
    piège que le balayage lexical du registre de gel — un nom cité n'est pas un appel.
    """
    import ast
    import inspect

    arbre = ast.parse(inspect.getsource(R))
    interdits = {"extract_document", "chunk_text_deterministic", "resolve_chunk_budget"}

    vus: set[str] = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Call):
            f = noeud.func
            nom = getattr(f, "id", None) or getattr(f, "attr", None)
            if nom in interdits:
                vus.add(nom)
        elif isinstance(noeud, ast.ImportFrom):
            vus |= {a.name for a in noeud.names} & interdits
        elif isinstance(noeud, ast.Import):
            vus |= {a.name.split(".")[-1] for a in noeud.names} & interdits

    assert not vus, (
        "la primitive appelle ou importe l'extraction / le découpage : "
        + ", ".join(sorted(vus))
        + "\nElle ne doit recalculer QUE ce qui dépend du modèle d'embedding."
    )


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_l_extraction_n_est_pas_appelee_a_l_execution(document):
    from apps.rag_core.services.ingest import indexing

    with patch.object(indexing, "extract_document") as extraction:
        with patch.object(R, "embed_texts_with_identity", side_effect=lambda t, **k: _Lot(len(t))):
            R.reembed_document(document, provider="ai_gateway", model="legacy-768")
    extraction.assert_not_called()


# --- Atomicité -------------------------------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM, RAG_EMBED_BATCH_SIZE=2)
def test_un_echec_au_second_lot_ne_laisse_aucune_ecriture(document):
    """§16, §40 — la moitié d'un document migré serait pire qu'un échec franc :
    invisible au retrieval, sans erreur persistante pour le signaler."""
    avant = {str(c.id): (list(c.embedding), c.pipeline_family)
             for c in Chunk.objects.filter(document=document)}
    appels = {"n": 0}

    def _capricieux(textes, **kw):
        appels["n"] += 1
        if appels["n"] == 2:
            raise RuntimeError("Gateway indisponible")
        return _Lot(len(textes))

    with patch.object(R, "embed_texts_with_identity", side_effect=_capricieux):
        with pytest.raises(R.ReembedError, match="aucun fragment écrit"):
            R.reembed_document(document, provider="ai_gateway", model="legacy-768")

    assert appels["n"] == 2, "le second lot devait être tenté"
    apres = {str(c.id): (list(c.embedding), c.pipeline_family)
             for c in Chunk.objects.filter(document=document)}
    assert apres == avant
    document.refresh_from_db()
    assert document.embedding_provider == "ollama", "l'empreinte du document a bougé"


@override_settings(RAG_EMBEDDING_DIM=DIM, RAG_EMBED_BATCH_SIZE=2)
def test_une_identite_de_pipeline_changee_en_cours_de_document_est_refusee(document):
    """Un rechargement de modèle entre deux lots produirait un document dont une
    moitié est comparable et l'autre non."""
    suite = iter([
        _Lot(2, PID),
        _Lot(2, "emb-v2:differentdifferentdifferentdif", family="legacy-768@differen"),
    ])
    with patch.object(R, "embed_texts_with_identity", side_effect=lambda t, **k: next(suite)):
        with pytest.raises(R.ReembedError, match="identité de pipeline changée"):
            R.reembed_document(document, provider="ai_gateway", model="legacy-768")
    assert Chunk.objects.filter(document=document, pipeline_family__isnull=False).count() == 0


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_une_dimension_inattendue_est_refusee_avant_ecriture(document):
    """§21 — un vecteur d'une autre dimension ne dégrade pas la recherche, il la rend
    fausse : pgvector comparerait des espaces différents."""
    with patch.object(R, "embed_texts_with_identity",
                      side_effect=lambda t, **k: _Lot(len(t), dim=512)):
        with pytest.raises(R.ReembedError, match="dimension"):
            R.reembed_document(document, provider="ai_gateway", model="legacy-768")
    assert Chunk.objects.filter(document=document, pipeline_family__isnull=False).count() == 0


# --- Reprise et idempotence ------------------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM, RAG_EMBED_BATCH_SIZE=2)
def test_une_reprise_apres_echec_aboutit_a_un_seul_etat_coherent(document):
    """§41 — premier essai en échec avant commit, second réussi."""
    with patch.object(R, "embed_texts_with_identity",
                      side_effect=RuntimeError("Gateway KO")):
        with pytest.raises(R.ReembedError):
            R.reembed_document(document, provider="ai_gateway", model="legacy-768")

    with patch.object(R, "embed_texts_with_identity", side_effect=lambda t, **k: _Lot(len(t))):
        res = R.reembed_document(document, provider="ai_gateway", model="legacy-768")

    assert res.chunks == 5 and not res.skipped
    familles = set(Chunk.objects.filter(document=document).values_list("pipeline_family", flat=True))
    assert familles == {"legacy-768@0a109f42"}, "état final non homogène"


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_rejouer_un_reembedding_abouti_ne_refait_rien(document):
    """§31, §36 — c'est ce qui rend une reprise après échec sûre : un lot de migration
    interrompu peut être relancé en entier sans recalculer ce qui est déjà fait."""
    with patch.object(R, "embed_texts_with_identity", side_effect=lambda t, **k: _Lot(len(t))):
        R.reembed_document(document, provider="ai_gateway", model="legacy-768")

    with patch.object(R, "embed_texts_with_identity") as embed:
        res = R.reembed_document(document, provider="ai_gateway", model="legacy-768")
    embed.assert_not_called()
    assert res.skipped and res.skip_reason == R.SKIPPED_ALREADY_TARGET


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_force_permet_de_recalculer_une_identite_deja_atteinte(document):
    with patch.object(R, "embed_texts_with_identity", side_effect=lambda t, **k: _Lot(len(t))):
        R.reembed_document(document, provider="ai_gateway", model="legacy-768")
        res = R.reembed_document(document, provider="ai_gateway", model="legacy-768", force=True)
    assert not res.skipped and res.chunks == 5


# --- Éligibilité -----------------------------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM)
@pytest.mark.parametrize(
    ("mutation", "motif"),
    [
        ({"status": DocumentStatus.FAILED}, R.INELIGIBLE_NOT_READY),
        ({"reindex_required": True}, R.INELIGIBLE_REINDEX_REQUIRED),
    ],
)
def test_documents_ineligibles(document, mutation, motif):
    Document.objects.filter(id=document.id).update(**mutation)
    document.refresh_from_db()
    pf = R.preflight_document(document, provider="ai_gateway", model="legacy-768")
    assert not pf.eligible and pf.reason == motif
    with pytest.raises(R.ReembedError, match=motif):
        R.reembed_document(document, provider="ai_gateway", model="legacy-768")


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_un_fragment_trop_long_est_refuse_sans_troncature(document, espace):
    """§14, §15 — tronquer produirait un vecteur qui ne décrit pas le fragment qu'il
    prétend décrire. Le refus est explicite et intervient AVANT toute écriture."""
    # 5 984 et non 6 001 : la Gateway PUBLIE 6 000 mais préfixe le texte avant de
    # compter (« search_document: », 17 caractères). Le dernier accepté est 5 983,
    # mesuré par dichotomie le 23/08/2026. Un fragment de 5 984 passe donc la table
    # publiée et se fait refuser à l'indexation — c'est exactement le fragment que ce
    # préflight doit intercepter.
    trop = Chunk.objects.filter(document=document).first()
    trop.text = "x" * 5984
    trop.save(update_fields=["text"])

    pf = R.preflight_document(document, provider="ai_gateway", model="legacy-768")
    assert not pf.eligible and pf.reason == R.INELIGIBLE_CHUNK_TOO_LONG
    assert pf.max_chunk_chars == 5984 and pf.target_max_chars == 5983

    with patch.object(R, "embed_texts_with_identity") as embed:
        with pytest.raises(R.ReembedError, match=R.INELIGIBLE_CHUNK_TOO_LONG):
            R.reembed_document(document, provider="ai_gateway", model="legacy-768")
    embed.assert_not_called(), "la Gateway a été appelée malgré un fragment hors limite"


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_un_modele_gouverne_sans_plafond_connu_est_refuse(document):
    """« Inconnu » n'autorise pas à tenter : la Gateway refuserait le document entier."""
    pf = R.preflight_document(document, provider="ai_gateway", model="modele-inconnu")
    assert not pf.eligible and pf.reason == R.INELIGIBLE_UNKNOWN_MODEL_LIMIT


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_une_cible_non_gouvernee_n_est_pas_plafonnee(document):
    """Le plafond est une propriété des modèles gouvernés : un ré-embedding vers
    `ollama` n'a pas de registre de capacités à consulter."""
    trop = Chunk.objects.filter(document=document).first()
    trop.text = "x" * 9000
    trop.save(update_fields=["text"])
    pf = R.preflight_document(document, provider="ollama", model="nomic-embed-text")
    assert pf.eligible and pf.target_max_chars is None


# --- Préflight d'espace ----------------------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM, RAG_EMBED_BATCH_SIZE=2)
def test_le_preflight_d_espace_n_ecrit_rien_et_chiffre_le_travail(espace, document):
    Document.objects.create(
        rag_space=espace, filename="casse.pdf", status=DocumentStatus.FAILED,
        embedding_provider="", embedding_model="", reindex_required=True,
    )
    avant = _empreinte(document)

    agg = R.preflight_space(espace, provider="ai_gateway", model="legacy-768")

    assert _empreinte(document) == avant, "le préflight a écrit"
    assert agg.documents_total == 2
    assert agg.documents_eligible == 1
    assert agg.chunks_eligible == 5
    assert agg.estimated_embedding_calls == 3  # 5 fragments par lots de 2
    assert agg.ineligible_by_reason == {R.INELIGIBLE_NOT_READY: 1}
    assert agg.space_identity_matches_target is False, (
        "l'espace est encore ollama : le préflight doit le dire, sinon la migration "
        "rendrait ces documents invisibles au retrieval sans prévenir"
    )
