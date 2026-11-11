"""Migrer l'identité d'un espace sans qu'aucun lecteur ne voie d'état intermédiaire.

Le lot précédent a échoué sur une mesure : le lecteur voyait `4 → 0 → 4`. La garde P1-D
compare l'empreinte du DOCUMENT à l'identité de l'ESPACE, et un document ne porte qu'une
empreinte — aucun ordre de deux opérations séparées n'évite la fenêtre.

Ces tests vérifient qu'il n'y a plus deux opérations. La propriété centrale, celle qui
manquait, est la dernière du fichier : `4 → 4`, jamais `0`.
"""
import hashlib
from array import array
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import override_settings

from apps.llm_router.embeddings import space_is_governed
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.repositories.chunk_repository import search_chunks_vector
from apps.rag_core.services.ingest import space_migration as M
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db(transaction=True)

DIM = 768
CIBLE = {"provider": "ai_gateway", "model": "legacy-768"}
# Identité OPAQUE (contrat Gateway v2) + famille attestée au format persisté.
PID = "emb-v2:9e883f0fe1d326ef17f86027a1d4a5d8"
FAMILLE = "legacy-768@0a109f42"


class _Lot:
    def __init__(self, n, pipeline_id=PID, graine=0.5, dim=DIM, family=FAMILLE):
        self.vectors = [[graine + i * 1e-6] * dim for i in range(n)]
        self.pipeline_id = pipeline_id
        # Famille ATTESTÉE à part de l'identifiant (opaque), comme le client gouverné.
        self.family = family


def _embed(n_par_appel=_Lot):
    return lambda textes, **kw: n_par_appel(len(textes))


@pytest.fixture
def espace(db):
    ws = Workspace.objects.create(name="WS-ATOMIC")
    sp = RagSpace.objects.create(workspace=ws, name="ATOMIC",
                                 embedding_provider="ollama", embedding_model="nomic-embed-text")
    doc = Document.objects.create(rag_space=sp, filename="a.pdf", status=DocumentStatus.READY,
                                  embedding_provider="ollama", embedding_model="nomic-embed-text")
    for i in range(4):
        Chunk.objects.create(rag_space=sp, document=doc, chunk_index=i,
                             text=f"Fragment {i} — obligations contractuelles et delais.",
                             page_number=1, meta={"chunk_index": i},
                             embedding=[0.1 + i * 1e-3] * DIM, pipeline_family=None)
    return sp


def _servis(space):
    """Fragments réellement visibles : le chemin exact du candidate_retriever."""
    space.refresh_from_db()
    from apps.llm_router.embeddings import resolve_space_embedding
    p, m = resolve_space_embedding(space)
    famille = FAMILLE if space_is_governed(space) else None
    return search_chunks_vector(
        rag_space_id=space.id, query_embedding=[0.1] * DIM, top_k=50,
        embedding_provider=p, embedding_model=m, pipeline_family=famille,
    )


def _etat(space):
    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.id::text, md5(c.text), c.text_tsv_french::text, c.page_number, "
            "       c.meta::text, c.pipeline_family, md5(c.embedding::text) "
            "FROM rag_chunk c WHERE c.rag_space_id=%s ORDER BY c.chunk_index", [str(space.id)])
        return cur.fetchall()


# --- PREPARE n'écrit rien ---------------------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_prepare_n_ecrit_rien(espace):
    avant = _etat(espace)
    doc_avant = list(Document.objects.filter(rag_space=espace)
                     .values_list("embedding_provider", "embedding_model", "reindex_required"))
    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        plan = M.prepare_space_migration(espace, **CIBLE)
    espace.refresh_from_db()
    assert _etat(espace) == avant
    assert list(Document.objects.filter(rag_space=espace)
                .values_list("embedding_provider", "embedding_model", "reindex_required")) == doc_avant
    assert (espace.embedding_provider, espace.embedding_model) == ("ollama", "nomic-embed-text")
    assert len(plan.chunk_ids) == 4 and len(plan.vectors) == 4
    assert all(isinstance(v, array) for v in plan.vectors), (
        "les vecteurs doivent être en array('f') : une liste Python coûte 8× la mémoire"
    )


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_le_plan_ne_serialise_ni_vecteur_ni_texte(espace):
    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        plan = M.prepare_space_migration(espace, **CIBLE)
    resume = plan.summary()
    import json
    texte = json.dumps(resume)
    assert "0.5" not in texte and "Fragment" not in texte
    assert set(resume) >= {"source", "target", "chunks", "source_state_hash"}


# --- LA propriété qui manquait ---------------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_le_lecteur_ne_voit_jamais_zero(espace):
    """§58 — la séquence doit être 4 → 4, jamais 4 → 0 → 4.

    C'est la mesure exacte qui avait produit le NO-GO du lot précédent.
    """
    avant = len(_servis(espace))
    assert avant == 4, "fixture invalide : le corpus n'est pas servi avant migration"

    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        plan = M.prepare_space_migration(espace, **CIBLE)

    pendant = len(_servis(espace))
    assert pendant == 4, (
        f"le corpus est devenu invisible pendant la PRÉPARATION ({pendant}/4) : "
        "la préparation a écrit quelque chose"
    )

    M.commit_space_migration(plan)

    apres = len(_servis(espace))
    espace.refresh_from_db()
    assert space_is_governed(espace)
    assert apres == 4, f"le corpus est incomplet après bascule ({apres}/4)"


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_le_rollback_transactionnel_restaure_immediatement_le_service(espace):
    """§60 — la propriété qui manquait au plan précédent.

    Un échec de la transaction finale doit rendre l'ancien monde intact ET servi, pas
    un état hybride qu'il faudrait réparer.

    Le test VÉRIFIE que les fragments avaient bien été écrits avant l'explosion : sans
    cela il passerait pour la mauvaise raison — une exception levée avant toute
    écriture prouverait seulement qu'on n'a rien fait.
    """
    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        plan = M.prepare_space_migration(espace, **CIBLE)
    avant = _etat(espace)

    ecritures = {"chunks": 0}
    vrai_bulk = Chunk.objects.bulk_update

    def _bulk(objets, champs, **kw):
        ecritures["chunks"] += len(objets)
        return vrai_bulk(objets, champs, **kw)

    def _explose(*a, **k):
        raise RuntimeError("panne juste avant l'écriture de l'espace")

    with patch.object(Chunk.objects, "bulk_update", side_effect=_bulk):
        with patch.object(M.RagSpace.objects, "filter", side_effect=_explose):
            with pytest.raises(RuntimeError, match="panne"):
                M.commit_space_migration(plan)

    assert ecritures["chunks"] == 4, (
        "les fragments n'avaient pas été écrits : le test ne prouve pas le rollback"
    )
    espace.refresh_from_db()
    assert (espace.embedding_provider, espace.embedding_model) == ("ollama", "nomic-embed-text")
    assert _etat(espace) == avant, "des fragments ont survécu au rollback"
    assert len(_servis(espace)) == 4, "le service n'est pas restauré immédiatement"


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_une_panne_au_milieu_des_fragments_annule_tout(espace):
    """§28 — exception après une partie des UPDATE, avant l'écriture de l'espace."""
    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        plan = M.prepare_space_migration(espace, **CIBLE)
    avant = _etat(espace)

    vrai_bulk = Chunk.objects.bulk_update
    appels = {"n": 0}

    def _mi_chemin(objets, champs, **kw):
        appels["n"] += 1
        vrai_bulk(objets[:2], champs, **kw)   # la moitié passe…
        raise RuntimeError("panne au milieu des fragments")

    with patch.object(Chunk.objects, "bulk_update", side_effect=_mi_chemin):
        with pytest.raises(RuntimeError, match="milieu"):
            M.commit_space_migration(plan)

    assert appels["n"] == 1
    espace.refresh_from_db()
    assert _etat(espace) == avant, "une moitié de migration a survécu"
    assert (espace.embedding_provider, espace.embedding_model) == ("ollama", "nomic-embed-text")
    assert len(_servis(espace)) == 4


# --- Concurrence : le sceau doit mordre ------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM)
@pytest.mark.parametrize("mutation", ["ingestion", "suppression", "retexte", "reindex"])
def test_une_mutation_concurrente_annule_la_migration(espace, mutation):
    """§22-§25 — tout changement de l'ensemble source invalide le plan."""
    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        plan = M.prepare_space_migration(espace, **CIBLE)

    doc = Document.objects.filter(rag_space=espace).first()
    if mutation == "ingestion":
        autre = Document.objects.create(rag_space=espace, filename="neuf.pdf",
                                        status=DocumentStatus.READY,
                                        embedding_provider="ollama",
                                        embedding_model="nomic-embed-text")
        Chunk.objects.create(rag_space=espace, document=autre, chunk_index=0,
                             text="nouveau fragment", page_number=1, meta={},
                             embedding=[0.2] * DIM)
    elif mutation == "suppression":
        Chunk.objects.filter(document=doc).order_by("-chunk_index").first().delete()
    elif mutation == "retexte":
        c = Chunk.objects.filter(document=doc).first()
        c.text = c.text + " (modifié)"
        c.save(update_fields=["text"])
    elif mutation == "reindex":
        Document.objects.filter(id=doc.id).update(reindex_required=True)

    avant = _etat(espace)
    with pytest.raises(M.SourceStateChanged):
        M.commit_space_migration(plan)

    espace.refresh_from_db()
    assert (espace.embedding_provider, espace.embedding_model) == ("ollama", "nomic-embed-text")
    assert _etat(espace) == avant, "une migration partielle a été écrite malgré l'abandon"


# --- Scellement de la cible -------------------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM, RAG_EMBED_BATCH_SIZE=2)
def test_un_changement_de_revision_pendant_la_preparation_annule_le_plan(espace):
    """§37 — deux révisions dans un même corpus : une part comparable, l'autre non."""
    suite = iter([_Lot(2, PID), _Lot(2, "emb-v2:autreautreautreautreautreautreau", family="legacy-768@autreaut")])
    with patch.object(M, "embed_texts_with_identity", side_effect=lambda t, **k: next(suite)):
        with pytest.raises(M.SpaceMigrationError, match="révision|revision|pipeline"):
            M.prepare_space_migration(espace, **CIBLE)
    espace.refresh_from_db()
    assert (espace.embedding_provider, espace.embedding_model) == ("ollama", "nomic-embed-text")


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_une_dimension_inattendue_annule_le_plan(espace):
    with patch.object(M, "embed_texts_with_identity",
                      side_effect=lambda t, **k: _Lot(len(t), dim=512)):
        with pytest.raises(M.SpaceMigrationError, match="[Dd]imension"):
            M.prepare_space_migration(espace, **CIBLE)


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_un_vecteur_non_fini_annule_le_plan(espace):
    """Un NaN ne lève pas à l'écriture : il produit une distance NULL et le fragment
    sort silencieusement de tout classement."""
    class _Nan(_Lot):
        def __init__(self, n, **kw):
            super().__init__(n, **kw)
            self.vectors[0] = [float("nan")] * DIM

    with patch.object(M, "embed_texts_with_identity", side_effect=lambda t, **k: _Nan(len(t))):
        with pytest.raises(M.SpaceMigrationError, match="non fini"):
            M.prepare_space_migration(espace, **CIBLE)


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_une_cible_incomplete_est_refusee(espace):
    """§13 du lot précédent : laisser le modèle implicite ferait dépendre la migration
    d'un défaut d'environnement — en production, un défaut INVALIDE."""
    with pytest.raises(M.SpaceMigrationError, match="cible incomplète"):
        M.prepare_space_migration(espace, provider="ai_gateway", model="")


# --- La porte du §43 --------------------------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_un_fragment_servi_non_migrable_bloque_toute_la_migration(espace):
    """§43 — basculer l'espace le rendrait définitivement invisible."""
    doc = Document.objects.create(rag_space=espace, filename="trop-long.pdf",
                                  status=DocumentStatus.READY,
                                  embedding_provider="ollama",
                                  embedding_model="nomic-embed-text")
    Chunk.objects.create(rag_space=espace, document=doc, chunk_index=0,
                         text="x" * 6001, page_number=1, meta={}, embedding=[0.3] * DIM)

    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()) as embed:
        with pytest.raises(M.SpaceMigrationError, match="actuellement servis"):
            M.prepare_space_migration(espace, **CIBLE)
    embed.assert_not_called(), "la Gateway a été appelée malgré une porte fermée"


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_un_document_sans_fragment_servi_n_empeche_pas_la_migration(espace):
    """§44 — un document FAILED sans fragment ne peut pas être servi : il reste hors
    périmètre, et le dire explicitement vaut mieux que le taire."""
    Document.objects.create(rag_space=espace, filename="casse.pdf",
                            status=DocumentStatus.FAILED, embedding_provider="",
                            embedding_model="", reindex_required=True)
    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        plan = M.prepare_space_migration(espace, **CIBLE)
    motifs = {e["reason"] for e in plan.excluded_documents}
    assert motifs and all(e["chunk_count"] == 0 for e in plan.excluded_documents)
    M.commit_space_migration(plan)
    assert len(_servis(espace)) == 4


# --- Invariants du corpus ---------------------------------------------------

@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_identifiants_texte_metadonnees_et_fts_sont_preserves(espace):
    avant = [(a[0], a[1], a[2], a[3], a[4]) for a in _etat(espace)]
    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        M.commit_space_migration(M.prepare_space_migration(espace, **CIBLE))
    apres = [(a[0], a[1], a[2], a[3], a[4]) for a in _etat(espace)]
    assert apres == avant
    familles = {a[5] for a in _etat(espace)}
    assert familles == {FAMILLE}


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_un_espace_deja_a_la_cible_ne_fait_rien(espace):
    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        M.commit_space_migration(M.prepare_space_migration(espace, **CIBLE))
        with pytest.raises(M.SpaceMigrationError) as exc:
            M.prepare_space_migration(espace, **CIBLE)
    assert exc.value.code == M.ABORT_ALREADY_TARGET


def test_le_sceau_couvre_ce_qui_rendrait_le_plan_caduc(espace):
    """Un sceau qui ne bouge pas quand la source bouge ne scelle rien."""
    base = M.source_state_hash(espace)
    assert M.source_state_hash(espace) == base, "sceau non déterministe"

    c = Chunk.objects.filter(rag_space=espace).first()
    c.text += " x"
    c.save(update_fields=["text"])
    assert M.source_state_hash(espace) != base

    c.text = c.text[:-2]
    c.save(update_fields=["text"])
    assert M.source_state_hash(espace) == base, "le sceau n'est pas réversible"

    RagSpace.objects.filter(pk=espace.pk).update(embedding_model="autre")
    assert M.source_state_hash(espace) != base


def test_le_sceau_ne_contient_aucun_texte(espace):
    """Un sceau ne doit pas devenir un stockage."""
    import inspect
    src = inspect.getsource(M.source_state_hash)
    assert "hashlib.sha256((texte or" in src, (
        "le texte doit être HACHÉ avant d'entrer dans le sceau, jamais inclus"
    )


# --- §30 / §59 — ce qu'un lecteur concurrent peut observer ------------------

@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_un_lecteur_concurrent_ne_voit_jamais_d_etat_hybride(espace):
    """La preuve la plus exigeante du lot : pendant la transaction de bascule, un
    lecteur d'une AUTRE connexion ne doit voir que l'ancien monde complet.

    Le test suspend le commit juste avant l'écriture de l'identité de l'espace —
    l'instant où les fragments portent déjà la cible et où l'espace ne l'a pas encore.
    C'est exactement la combinaison que le lot précédent rendait observable, et qui
    donnait `0 fragment servi`.

    L'isolation READ COMMITTED de PostgreSQL garantit qu'une transaction concurrente ne
    voit que les données COMMITÉES. Ici, aucune des écritures n'est commitée : le
    lecteur doit donc voir l'ancien monde, entier.
    """
    import threading

    from django.db import connections

    with patch.object(M, "embed_texts_with_identity", side_effect=_embed()):
        plan = M.prepare_space_migration(espace, **CIBLE)

    au_milieu = threading.Event()
    peut_finir = threading.Event()
    observe: dict[str, object] = {}
    erreur: dict[str, BaseException] = {}

    vrai_filter = M.RagSpace.objects.filter

    def _suspend(*a, **k):
        # Appelé UNE fois, pour l'écriture finale de l'identité de l'espace.
        au_milieu.set()
        peut_finir.wait(timeout=20)
        return vrai_filter(*a, **k)

    def _migrer():
        try:
            with patch.object(M.RagSpace.objects, "filter", side_effect=_suspend):
                M.commit_space_migration(plan)
        except BaseException as exc:  # noqa: BLE001 - remonté au thread principal
            erreur["e"] = exc
            au_milieu.set()
        finally:
            connections.close_all()

    t = threading.Thread(target=_migrer)
    t.start()
    try:
        assert au_milieu.wait(timeout=20), "la transaction n'a pas atteint le point suspendu"
        assert "e" not in erreur, f"la migration a échoué avant le point suspendu : {erreur.get('e')}"
        # Lecture depuis la connexion du thread principal, hors de la transaction.
        espace.refresh_from_db()
        observe["identite"] = (espace.embedding_provider, espace.embedding_model)
        observe["servis"] = len(_servis(espace))
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM rag_chunk WHERE rag_space_id=%s "
                        "AND pipeline_family IS NOT NULL", [str(espace.id)])
            observe["fragments_cible"] = cur.fetchone()[0]
    finally:
        peut_finir.set()
        t.join(timeout=30)

    assert "e" not in erreur, f"la migration a échoué : {erreur.get('e')}"

    assert observe["identite"] == ("ollama", "nomic-embed-text"), (
        "le lecteur a vu la NOUVELLE identité alors que la transaction n'était pas "
        "commitée"
    )
    assert observe["fragments_cible"] == 0, (
        "le lecteur a vu des fragments portant la provenance cible avant le commit"
    )
    assert observe["servis"] == 4, (
        f"le lecteur a vu un corpus incomplet pendant la transaction "
        f"({observe['servis']}/4) — c'est exactement la fenêtre que ce lot supprime"
    )

    # …et après le commit, le nouveau monde, entier.
    espace.refresh_from_db()
    assert space_is_governed(espace)
    assert len(_servis(espace)) == 4


# --- §46 — préflight bon marché, distinct de la préparation ----------------

@override_settings(RAG_EMBEDDING_DIM=DIM, RAG_EMBED_BATCH_SIZE=2)
def test_le_preflight_chiffre_sans_appeler_la_gateway(espace):
    """On doit pouvoir décider d'une migration sans en payer le calcul — et sans
    qu'une Gateway indisponible empêche de préparer une décision."""
    with patch.object(M, "embed_texts_with_identity") as embed:
        pf = M.preflight_space_migration(espace, **CIBLE)
    embed.assert_not_called()

    assert pf["documents_eligible"] == 1
    assert pf["chunks_to_migrate"] == 4
    assert pf["estimated_gateway_calls"] == 2      # 4 fragments par lots de 2
    assert pf["retrievable_chunks_not_migratable"] == 0
    assert pf["migratable"] is True
    assert pf["estimated_vector_memory_mb"] == round(4 * DIM * 4 / 1e6, 1)
    assert pf["source_state_hash"] == M.source_state_hash(espace)


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_le_preflight_signale_un_fragment_servi_non_migrable(espace):
    doc = Document.objects.create(rag_space=espace, filename="long.pdf",
                                  status=DocumentStatus.READY,
                                  embedding_provider="ollama",
                                  embedding_model="nomic-embed-text")
    Chunk.objects.create(rag_space=espace, document=doc, chunk_index=0,
                         text="x" * 6001, page_number=1, meta={}, embedding=[0.3] * DIM)
    pf = M.preflight_space_migration(espace, **CIBLE)
    assert pf["retrievable_chunks_not_migratable"] == 1
    assert pf["migratable"] is False


@override_settings(RAG_EMBEDDING_DIM=DIM)
def test_le_preflight_n_ecrit_rien(espace):
    avant = _etat(espace)
    M.preflight_space_migration(espace, **CIBLE)
    assert _etat(espace) == avant
