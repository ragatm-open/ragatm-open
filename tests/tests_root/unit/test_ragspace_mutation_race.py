"""La race d'insertion concurrente : la reproduire, ou prouver qu'elle est fermée.

Le lot précédent avait consigné une limite : « l'insertion d'un document ENTIÈREMENT
NOUVEAU n'est bloquée par aucun verrou de ligne ». C'était une déduction — un document
qui n'existe pas ne peut pas être verrouillé.

Elle ignore une propriété de PostgreSQL : une INSERTION portant une clé étrangère prend
un verrou ``FOR KEY SHARE`` sur la ligne PARENTE, pour empêcher qu'elle disparaisse sous
la nouvelle ligne. Or ``FOR KEY SHARE`` **entre en conflit** avec ``FOR UPDATE``.

Ces tests interrogent PostgreSQL, avec deux connexions réelles, plutôt que de raisonner.
"""
import threading
import time

import pytest
from django.db import connection, connections, transaction

from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db(transaction=True)

DIM = 768


@pytest.fixture
def espace(db):
    ws = Workspace.objects.create(name="WS-RACE")
    sp = RagSpace.objects.create(workspace=ws, name="RACE",
                                 embedding_provider="ollama", embedding_model="nomic-embed-text")
    doc = Document.objects.create(rag_space=sp, filename="a.pdf", status=DocumentStatus.READY,
                                  embedding_provider="ollama", embedding_model="nomic-embed-text")
    for i in range(2):
        Chunk.objects.create(rag_space=sp, document=doc, chunk_index=i, text=f"f{i}",
                             page_number=1, meta={}, embedding=[0.1] * DIM)
    return sp


def test_le_niveau_d_isolation_est_bien_read_committed():
    """Toute la démonstration repose dessus : autant le lire, pas le supposer."""
    with connection.cursor() as cur:
        cur.execute("SHOW transaction_isolation")
        assert cur.fetchone()[0] == "read committed"


def _insere_document(space_id, resultat, demarre, timeout_s=8.0):
    """Insère un document + fragment dans une transaction, sur SA propre connexion."""
    try:
        demarre.set()
        t0 = time.perf_counter()
        with transaction.atomic():
            doc = Document.objects.create(
                rag_space_id=space_id, filename="concurrent.pdf",
                status=DocumentStatus.READY, embedding_provider="ollama",
                embedding_model="nomic-embed-text",
            )
            Chunk.objects.create(rag_space_id=space_id, document=doc, chunk_index=0,
                                 text="fragment concurrent", page_number=1, meta={},
                                 embedding=[0.9] * DIM)
        resultat["attente_ms"] = (time.perf_counter() - t0) * 1000
        resultat["commit"] = True
    except Exception as exc:  # noqa: BLE001 - remonté au thread principal
        resultat["erreur"] = f"{type(exc).__name__}: {exc}"
    finally:
        connections.close_all()


def test_une_insertion_concurrente_est_bloquee_par_le_verrou_de_l_espace(espace):
    """LE test du lot : `SELECT rag_space FOR UPDATE` bloque-t-il une INSERTION ?

    Une insertion de document porte une clé étrangère vers `rag_space`. PostgreSQL
    prend alors un verrou `FOR KEY SHARE` sur la ligne parente — et ce verrou entre en
    conflit avec `FOR UPDATE`.

    Si c'est vrai, la « dernière race » consignée au lot précédent n'existe pas : elle
    était déduite du fait qu'une ligne inexistante ne peut pas être verrouillée, ce qui
    est exact mais hors sujet — c'est la ligne PARENTE qui est verrouillée.
    """
    resultat: dict = {}
    demarre = threading.Event()

    with transaction.atomic():
        # Exactement ce que fait `commit_space_migration`.
        RagSpace.objects.select_for_update().get(pk=espace.pk)

        t = threading.Thread(target=_insere_document, args=(espace.pk, resultat, demarre))
        t.start()
        assert demarre.wait(timeout=5)
        # Laisser à l'insertion le temps d'aboutir SI elle le peut.
        time.sleep(1.5)

        pendant = dict(resultat)
        # Vue depuis NOTRE transaction : l'insertion a-t-elle pu passer ?
        vus = Document.objects.filter(rag_space_id=espace.pk).count()

    t.join(timeout=10)

    assert "erreur" not in resultat, f"l'insertion a échoué : {resultat.get('erreur')}"
    assert not pendant.get("commit"), (
        "L'INSERTION A COMMITÉ pendant que la migration tenait le verrou de l'espace : "
        "la race est RÉELLE et un verrou explicite est nécessaire."
    )
    assert vus == 1, "un document concurrent est apparu dans la transaction de migration"
    # Une fois notre transaction terminée, l'insertion doit aboutir.
    assert resultat.get("commit") is True
    assert resultat["attente_ms"] > 1000, (
        f"l'insertion n'a attendu que {resultat['attente_ms']:.0f} ms : elle n'était "
        "pas bloquée par le verrou"
    )
    assert Document.objects.filter(rag_space_id=espace.pk).count() == 2


def test_le_verrou_ne_bloque_pas_un_AUTRE_espace(espace, db):
    """§35 — le verrou doit être assez fin pour que deux espaces soient indépendants."""
    autre = RagSpace.objects.create(workspace=espace.workspace, name="RACE-B",
                                    embedding_provider="ollama",
                                    embedding_model="nomic-embed-text")
    resultat: dict = {}
    demarre = threading.Event()

    with transaction.atomic():
        RagSpace.objects.select_for_update().get(pk=espace.pk)
        t = threading.Thread(target=_insere_document, args=(autre.pk, resultat, demarre))
        t.start()
        assert demarre.wait(timeout=5)
        t.join(timeout=10)
        assert resultat.get("commit") is True, (
            f"l'insertion sur un AUTRE espace a été bloquée : {resultat}"
        )
        assert resultat["attente_ms"] < 1000


def test_un_lecteur_n_est_jamais_bloque_par_le_verrou(espace):
    """§26/§36 — le retrieval ne prend pas ce verrou et ne doit pas l'attendre."""
    lu: dict = {}

    def _lire():
        try:
            t0 = time.perf_counter()
            lu["documents"] = Document.objects.filter(rag_space_id=espace.pk).count()
            with connections["default"].cursor() as cur:
                cur.execute("SELECT count(*) FROM rag_chunk WHERE rag_space_id=%s",
                            [str(espace.pk)])
                lu["chunks"] = cur.fetchone()[0]
            lu["ms"] = (time.perf_counter() - t0) * 1000
        finally:
            connections.close_all()

    with transaction.atomic():
        RagSpace.objects.select_for_update().get(pk=espace.pk)
        t = threading.Thread(target=_lire)
        t.start()
        t.join(timeout=10)

    assert lu.get("documents") == 1 and lu.get("chunks") == 2
    assert lu["ms"] < 1000, f"le lecteur a attendu {lu['ms']:.0f} ms"


# ---------------------------------------------------------------------------
# La race qui reste : une identité PÉRIMÉE publiée après la migration
# ---------------------------------------------------------------------------

def _ecrit_comme_l_ingestion(space_id, identite, barriere, resultat, timeout_s=15.0):
    """Reproduit fidèlement la fin du chemin d'ingestion.

    `index_extraction_result` capture `resolve_space_embedding(space)` puis, dans la
    même transaction, insère les fragments et écrit l'empreinte du document AVEC CETTE
    VALEUR CAPTURÉE. Il ne relit jamais l'espace.
    """
    prov, mod = identite
    try:
        barriere.wait(timeout=timeout_s)
        t0 = time.perf_counter()
        with transaction.atomic():
            doc = Document.objects.create(
                rag_space_id=space_id, filename="tardif.pdf",
                status=DocumentStatus.INGESTING, embedding_provider="", embedding_model="",
            )
            Chunk.objects.create(rag_space_id=space_id, document=doc, chunk_index=0,
                                 text="fragment ingéré pendant la migration",
                                 page_number=1, meta={}, embedding=[0.7] * DIM,
                                 pipeline_id=None, pipeline_family=None)
            Document.objects.filter(id=doc.id).update(
                status=DocumentStatus.READY, embedding_provider=prov,
                embedding_model=mod, reindex_required=False,
            )
        resultat["attente_ms"] = (time.perf_counter() - t0) * 1000
        resultat["document_id"] = str(doc.id)
        resultat["commit"] = True
    except Exception as exc:  # noqa: BLE001
        resultat["erreur"] = f"{type(exc).__name__}: {exc}"
    finally:
        connections.close_all()


def test_une_ingestion_a_identite_perimee_est_refusee(espace):
    """§19-§21 — la race qui restait, et que le verrou SEUL ne fermait pas.

    Sérialiser n'est pas revalider. L'ingestion capture l'identité de l'espace AVANT
    d'attendre le verrou ; quand elle l'obtient, la migration a commité. Sans
    revalidation, elle publiait son résultat ancien dans un espace devenu gouverné —
    document invisible, sans erreur et sans trace.

    Ce test a d'abord été écrit pour PROUVER le défaut : il vérifiait que le document
    tardif était incohérent. Il vérifie désormais que l'écriture est refusée.
    """
    from apps.rag_core.services.ragspace_mutation import (
        RagSpaceIdentityChanged,
        lock_ragspace_for_mutation,
    )

    from apps.llm_router.embeddings import resolve_space_embedding

    identite_perimee = resolve_space_embedding(espace)   # ollama / nomic-embed-text
    barriere = threading.Event()
    resultat: dict = {}

    def _ingere_avec_revalidation():
        try:
            barriere.wait(timeout=15)
            with transaction.atomic():
                doc = Document.objects.create(
                    rag_space_id=espace.pk, filename="tardif.pdf",
                    status=DocumentStatus.INGESTING, embedding_provider="",
                    embedding_model="",
                )
                # Exactement ce que fait désormais `index_extraction_result` :
                # verrou + revalidation, juste avant d'écrire les fragments.
                lock_ragspace_for_mutation(
                    espace.pk, expected_identity=identite_perimee)
                Chunk.objects.create(rag_space_id=espace.pk, document=doc, chunk_index=0,
                                     text="fragment tardif", page_number=1, meta={},
                                     embedding=[0.7] * DIM)
                Document.objects.filter(id=doc.id).update(
                    status=DocumentStatus.READY,
                    embedding_provider=identite_perimee[0],
                    embedding_model=identite_perimee[1], reindex_required=False)
            resultat["commit"] = True
        except RagSpaceIdentityChanged as exc:
            resultat["refuse"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            resultat["erreur"] = f"{type(exc).__name__}: {exc}"
        finally:
            connections.close_all()

    t = threading.Thread(target=_ingere_avec_revalidation)
    t.start()
    try:
        with transaction.atomic():
            RagSpace.objects.select_for_update().get(pk=espace.pk)
            barriere.set()
            time.sleep(0.8)
            RagSpace.objects.filter(pk=espace.pk).update(
                embedding_provider="ai_gateway", embedding_model="legacy-768")
    finally:
        t.join(timeout=20)

    assert "erreur" not in resultat, resultat.get("erreur")
    assert not resultat.get("commit"), (
        "l'ingestion a publié un résultat préparé sous l'ancienne identité dans un "
        "espace devenu gouverné"
    )
    assert "refuse" in resultat and "identité du RagSpace changée" in resultat["refuse"]

    # §44 — aucun état intermédiaire permanent : le document tardif n'existe pas.
    espace.refresh_from_db()
    assert (espace.embedding_provider, espace.embedding_model) == ("ai_gateway", "legacy-768")
    assert not Document.objects.filter(rag_space_id=espace.pk,
                                       filename="tardif.pdf").exists(), (
        "la transaction refusée a laissé un document derrière elle"
    )


def test_le_verrou_refuse_d_etre_pris_hors_transaction(espace):
    """Un verrou de ligne pris hors transaction serait relâché aussitôt : purement
    décoratif, et pire qu'absent puisqu'il rassurerait."""
    from apps.rag_core.services.ragspace_mutation import lock_ragspace_for_mutation

    with pytest.raises(RuntimeError, match="DANS une transaction"):
        lock_ragspace_for_mutation(espace.pk)


def test_sans_identite_attendue_le_verrou_serialise_sans_revalider(espace):
    """Légitime pour une opération qui ne dépend pas de l'identité — une suppression."""
    from apps.rag_core.services.ragspace_mutation import lock_ragspace_for_mutation

    with transaction.atomic():
        sp = lock_ragspace_for_mutation(espace.pk)
        assert sp.pk == espace.pk


# ---------------------------------------------------------------------------
# Les deux ordres, avec la migration atomique réelle
# ---------------------------------------------------------------------------

def _plan(espace, cible=("ai_gateway", "legacy-768")):
    from unittest.mock import patch

    from apps.rag_core.services.ingest import space_migration as M

    class _Lot:
        def __init__(self, n):
            self.vectors = [[0.5] * DIM for _ in range(n)]
            self.pipeline_id = "emb-v2:9e883f0fe1d326ef17f86027a1d4a5d8"  # opaque
            self.family = "legacy-768@0a109f42"  # attestée à part

    with patch.object(M, "embed_texts_with_identity", side_effect=lambda t, **k: _Lot(len(t))):
        return M.prepare_space_migration(espace, provider=cible[0], model=cible[1])


def test_ingestion_d_abord_puis_migration_le_sceau_mord(espace):
    """§30/§43 — l'ingestion gagne la course : la migration doit abandonner."""
    from apps.rag_core.services.ingest import space_migration as M

    plan = _plan(espace)

    # L'ingestion commite AVANT que la migration ne tente son commit.
    resultat: dict = {}
    demarre = threading.Event()
    t = threading.Thread(target=_insere_document, args=(espace.pk, resultat, demarre))
    t.start(); t.join(timeout=15)
    assert resultat.get("commit"), resultat

    avant = list(Chunk.objects.filter(rag_space_id=espace.pk)
                 .values_list("id", "pipeline_family"))
    with pytest.raises(M.SourceStateChanged):
        M.commit_space_migration(plan)

    espace.refresh_from_db()
    assert (espace.embedding_provider, espace.embedding_model) == ("ollama", "nomic-embed-text")
    assert list(Chunk.objects.filter(rag_space_id=espace.pk)
                .values_list("id", "pipeline_family")) == avant
    # §44 — état final cohérent : tout ce qui est servi l'est sous l'identité de l'espace.
    incoherents = Document.objects.filter(
        rag_space_id=espace.pk, status=DocumentStatus.READY,
    ).exclude(embedding_provider="ollama", embedding_model="nomic-embed-text").count()
    assert incoherents == 0


def test_deux_migrations_simultanees_une_seule_aboutit(espace):
    """§34 — deux migrations du même espace ne peuvent pas committer ensemble."""
    from apps.rag_core.services.ingest import space_migration as M

    plan_a = _plan(espace)
    plan_b = _plan(espace)          # préparé sur le MÊME état source
    assert plan_a.source_state_hash == plan_b.source_state_hash

    res_a = M.commit_space_migration(plan_a)
    assert res_a.chunks_written == 2

    # Le second plan a été scellé sur l'état d'AVANT : il doit être refusé.
    with pytest.raises(M.SourceStateChanged):
        M.commit_space_migration(plan_b)

    espace.refresh_from_db()
    assert (espace.embedding_provider, espace.embedding_model) == ("ai_gateway", "legacy-768")
    familles = set(Chunk.objects.filter(rag_space_id=espace.pk)
                   .values_list("pipeline_family", flat=True))
    assert familles == {"legacy-768@0a109f42"}, "corpus hétérogène après double migration"


def test_l_ordre_de_verrouillage_est_le_meme_partout(espace):
    """§29 — un ordre de verrous divergent entre écrivains produit des interblocages.

    L'ordre canonique du domaine est : RagSpace, puis documents par clé primaire. Ce
    test lit la source des écrivains plutôt que d'énumérer une liste, parce qu'une
    liste se périme.
    """
    import ast
    import inspect

    from apps.rag_core.services.ingest import reembed, space_migration

    for module in (space_migration, reembed):
        src = inspect.getsource(module)
        arbre = ast.parse(src)
        appels = [
            (n.lineno, getattr(n.func, "attr", None) or getattr(n.func, "id", None))
            for n in ast.walk(arbre) if isinstance(n, ast.Call)
        ]
        verrou = [l for l, nom in appels if nom == "lock_ragspace_for_mutation"]
        docs = [l for l, nom in appels if nom == "select_for_update"]
        assert verrou, f"{module.__name__} n'utilise pas la primitive de verrou commune"
        if docs:
            assert min(verrou) < min(docs), (
                f"{module.__name__} verrouille des documents AVANT le RagSpace : "
                "ordre non canonique, risque d'interblocage"
            )
