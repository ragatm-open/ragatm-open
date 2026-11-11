"""Reprise en masse des documents en ERREUR — sélection, snapshot, intégrité.

Ce que ces tests verrouillent, au-delà du chemin nominal : une reprise ne doit
**jamais** ressembler à un import. Aucun `Document` créé, aucun octet consommé,
aucun fichier source touché — c'est ce qui distingue une réingestion d'un nouvel
upload, et c'est la propriété qu'un refactor pourrait casser sans que rien d'autre
ne le signale.

La machine à états du job (lease, finalisation, reprise des items perdus) est déjà
couverte par `test_ragspace_reindex_jobs.py` : on ne la re-teste pas, on vérifie
qu'elle est bien RÉUTILISÉE.
"""
from __future__ import annotations

import uuid

import pytest

from apps.rag_core.models import RagSpace
from apps.rag_core.models.document import Document, DocumentStatus
from apps.rag_core.models.reindex_job import (
    RagSpaceReindexJob,
    RagSpaceReindexJobItem,
    ReindexItemStatus,
    ReindexJobKind,
    ReindexJobStatus,
)
from apps.workspaces.models import Membership

pytestmark = pytest.mark.django_db

URL = "/api/v1/rag-spaces/{}/retry-failed-documents/"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def owner_client(db, user, workspace):
    from rest_framework.test import APIClient

    Membership.objects.update_or_create(
        workspace=workspace, user=user, defaults={"role": Membership.ROLE_OWNER}
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def space(db, workspace):
    return RagSpace.objects.create(name="retry-space", workspace=workspace)


@pytest.fixture(autouse=True)
def _no_enqueue(monkeypatch):
    """Aucun broker dans les tests : le fan-out est neutralisé, pas simulé."""
    monkeypatch.setattr(
        "apps.rag_core.services.ingest.reindex_jobs._enqueue_orchestrator", lambda job_id: None
    )


def _doc(space, name="a.pdf", status=DocumentStatus.FAILED, with_file=True, size=1234):
    d = Document.objects.create(
        rag_space=space, filename=name, content_type="application/pdf",
        size_bytes=size, sha256=f"{abs(hash(name)):064x}"[:64], status=status,
        error_message="extraction_timeout: …" if status == DocumentStatus.FAILED else "",
    )
    if with_file:
        d.storage_file.name = f"rag_docs/{space.id}/{d.id}/{name}"
        d.save(update_fields=["storage_file"])
    return d


def _explicit(*docs):
    return {"selection": {"mode": "explicit", "document_ids": [str(d.id) for d in docs]}}


def _filter(**kw):
    return {"selection": {"mode": "filter", **kw}}


def _job_of(resp) -> RagSpaceReindexJob:
    return RagSpaceReindexJob.objects.get(id=resp.json()["job_id"])


def _item_doc_ids(job) -> set[str]:
    return {
        str(x) for x in RagSpaceReindexJobItem.objects.filter(job=job).values_list("document_id", flat=True)
    }


# --------------------------------------------------------------------------- #
# Création du job
# --------------------------------------------------------------------------- #
class TestCreation:
    def test_selection_explicite(self, owner_client, space):
        a, b = _doc(space, "a.pdf"), _doc(space, "b.pdf")
        _doc(space, "c.pdf")  # non sélectionné
        resp = owner_client.post(URL.format(space.id), _explicit(a, b), format="json")
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["kind"] == ReindexJobKind.FAILED_DOCUMENTS_RETRY
        assert body["total_documents"] == 2
        assert body["skipped_documents"] == 0
        job = _job_of(resp)
        assert job.kind == ReindexJobKind.FAILED_DOCUMENTS_RETRY
        assert _item_doc_ids(job) == {str(a.id), str(b.id)}

    def test_selection_par_filtre_prend_toutes_les_erreurs(self, owner_client, space):
        errs = [_doc(space, f"err-{i}.pdf") for i in range(5)]
        _doc(space, "ok.pdf", status=DocumentStatus.READY)
        resp = owner_client.post(URL.format(space.id), _filter(), format="json")
        assert resp.status_code == 202
        assert resp.json()["total_documents"] == 5
        assert _item_doc_ids(_job_of(resp)) == {str(d.id) for d in errs}

    def test_selection_par_filtre_avec_recherche(self, owner_client, space):
        keep = _doc(space, "Rapport annuel.pdf")
        _doc(space, "budget.pdf")
        resp = owner_client.post(URL.format(space.id), _filter(search="rapport"), format="json")
        assert resp.status_code == 202
        assert _item_doc_ids(_job_of(resp)) == {str(keep.id)}
        assert _job_of(resp).selection_meta["search"] == "rapport"

    def test_selection_par_filtre_avec_exclusions(self, owner_client, space):
        a, b, c = _doc(space, "a.pdf"), _doc(space, "b.pdf"), _doc(space, "c.pdf")
        resp = owner_client.post(
            URL.format(space.id),
            _filter(excluded_document_ids=[str(b.id)]),
            format="json",
        )
        assert resp.status_code == 202
        assert _item_doc_ids(_job_of(resp)) == {str(a.id), str(c.id)}

    def test_aucun_document_eligible(self, owner_client, space):
        _doc(space, "ok.pdf", status=DocumentStatus.READY)
        resp = owner_client.post(URL.format(space.id), _filter(), format="json")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "no_eligible_documents"
        assert not RagSpaceReindexJob.objects.exists()

    @pytest.mark.parametrize(
        "kwargs, motif",
        [
            ({"status": DocumentStatus.READY}, "document prêt"),
            ({"status": DocumentStatus.INGESTING}, "document en cours d'ingestion"),
            ({"status": DocumentStatus.PENDING}, "document en attente"),
            ({"with_file": False}, "document sans fichier source"),
        ],
    )
    def test_documents_ineligibles_ignores(self, owner_client, space, kwargs, motif):
        ok = _doc(space, "ok-failed.pdf")
        bad = _doc(space, "bad.pdf", **kwargs)
        resp = owner_client.post(URL.format(space.id), _explicit(ok, bad), format="json")
        assert resp.status_code == 202, motif
        body = resp.json()
        assert body["total_documents"] == 1
        # L'écart est dit explicitement — sans nommer le document écarté.
        assert body["skipped_documents"] == 1
        assert _item_doc_ids(_job_of(resp)) == {str(ok.id)}

    def test_document_d_un_autre_ragspace_ignore_sans_fuite(self, owner_client, space, workspace):
        other_space = RagSpace.objects.create(name="autre", workspace=workspace)
        mine = _doc(space, "mine.pdf")
        theirs = _doc(other_space, "theirs.pdf")
        resp = owner_client.post(URL.format(space.id), _explicit(mine, theirs), format="json")
        assert resp.status_code == 202
        assert _item_doc_ids(_job_of(resp)) == {str(mine.id)}
        # La réponse ne dit RIEN de l'existence du document hors périmètre.
        assert str(theirs.id) not in resp.content.decode()

    def test_uuid_inexistant_ignore(self, owner_client, space):
        ok = _doc(space, "ok.pdf")
        payload = {"selection": {"mode": "explicit",
                                 "document_ids": [str(ok.id), str(uuid.uuid4())]}}
        resp = owner_client.post(URL.format(space.id), payload, format="json")
        assert resp.status_code == 202
        assert resp.json()["total_documents"] == 1

    def test_snapshot_fige_au_lancement(self, owner_client, space):
        for i in range(3):
            _doc(space, f"err-{i}.pdf")
        resp = owner_client.post(URL.format(space.id), _filter(), format="json")
        job = _job_of(resp)
        assert job.total_count == 3

        # Un document qui tombe en erreur APRÈS le lancement n'entre pas dans le job :
        # sinon le total bougerait pendant le traitement.
        tardif = _doc(space, "tardif.pdf")
        job.refresh_from_db()
        assert job.total_count == 3
        assert str(tardif.id) not in _item_doc_ids(job)

    @pytest.mark.parametrize("payload", [
        {},
        {"selection": "error"},
        {"selection": {"mode": "magique"}},
        {"selection": {"mode": "explicit"}},
        {"selection": {"mode": "explicit", "document_ids": ["pas-un-uuid"]}},
        {"selection": {"mode": "explicit", "document_ids": "abc"}},
        {"selection": {"mode": "filter", "status": "ready"}},
    ])
    def test_selection_invalide_rejetee(self, owner_client, space, payload):
        _doc(space, "a.pdf")
        resp = owner_client.post(URL.format(space.id), payload, format="json")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "invalid_selection"
        assert not RagSpaceReindexJob.objects.exists()

    def test_selection_trop_grande_refusee(self, owner_client, space, settings, monkeypatch):
        monkeypatch.setattr(
            "apps.rag_core.services.ingest.reindex_jobs.MAX_RETRY_SELECTION", 2
        )
        for i in range(3):
            _doc(space, f"e{i}.pdf")
        resp = owner_client.post(URL.format(space.id), _filter(), format="json")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "selection_too_large"


# --------------------------------------------------------------------------- #
# Intégrité : une reprise n'est PAS un import
# --------------------------------------------------------------------------- #
class TestIntegrity:
    def test_aucun_document_cree_aucun_octet_consomme(self, owner_client, space, workspace):
        from apps.workspaces.services import quota as quota_service

        docs = [_doc(space, f"e{i}.pdf", size=1000) for i in range(4)]
        before_count = Document.objects.count()
        before_usage = quota_service.get_usage(workspace)
        before_files = {str(d.id): d.storage_file.name for d in docs}

        resp = owner_client.post(URL.format(space.id), _filter(), format="json")
        assert resp.status_code == 202

        assert Document.objects.count() == before_count, "aucun Document créé"
        after_usage = quota_service.get_usage(workspace)
        assert after_usage["used_document_count"] == before_usage["used_document_count"]
        assert after_usage["used_document_bytes"] == before_usage["used_document_bytes"]
        for d in Document.objects.filter(id__in=[x.id for x in docs]):
            assert d.storage_file.name == before_files[str(d.id)], "fichier source inchangé"

    def test_les_identifiants_de_documents_sont_conserves(self, owner_client, space):
        docs = [_doc(space, f"e{i}.pdf") for i in range(3)]
        resp = owner_client.post(URL.format(space.id), _filter(), format="json")
        assert _item_doc_ids(_job_of(resp)) == {str(d.id) for d in docs}


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
class TestPermissions:
    def test_owner_autorise(self, owner_client, space):
        _doc(space, "a.pdf")
        assert owner_client.post(URL.format(space.id), _filter(), format="json").status_code == 202

    def test_admin_autorise(self, db, space, workspace):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        u = get_user_model().objects.create_user(username="adm", password="pw")
        Membership.objects.create(workspace=workspace, user=u, role=Membership.ROLE_ADMIN)
        c = APIClient()
        c.force_authenticate(user=u)
        _doc(space, "a.pdf")
        assert c.post(URL.format(space.id), _filter(), format="json").status_code == 202

    @pytest.mark.parametrize("role", [Membership.ROLE_MEMBER, Membership.ROLE_VIEWER])
    def test_member_et_viewer_refuses_sans_fuite(self, db, space, workspace, role):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        _doc(space, "secret-rapport.pdf")
        u = get_user_model().objects.create_user(username=f"u-{role}", password="pw")
        Membership.objects.create(workspace=workspace, user=u, role=role)
        c = APIClient()
        c.force_authenticate(user=u)
        resp = c.post(URL.format(space.id), _filter(), format="json")
        assert resp.status_code == 403
        body = resp.content.decode()
        # Ni compteurs, ni identifiants, ni noms de fichiers.
        assert "secret-rapport" not in body
        assert "total_documents" not in body
        assert not RagSpaceReindexJob.objects.exists()

    def test_espace_etranger_refuse(self, owner_client, foreign_setup):
        _doc(foreign_setup["rag_space"], "a.pdf")
        resp = owner_client.post(URL.format(foreign_setup["rag_space"].id), _filter(), format="json")
        assert resp.status_code in (403, 404)
        assert not RagSpaceReindexJob.objects.exists()


# --------------------------------------------------------------------------- #
# Concurrence et exclusivité
# --------------------------------------------------------------------------- #
class TestConcurrency:
    def test_job_global_actif_bloque_la_reprise(self, owner_client, space):
        _doc(space, "a.pdf")
        globale = RagSpaceReindexJob.objects.create(
            rag_space=space, status=ReindexJobStatus.RUNNING, kind=ReindexJobKind.FULL_REINDEX
        )
        resp = owner_client.post(URL.format(space.id), _filter(), format="json")
        assert resp.status_code == 409
        body = resp.json()
        assert body["status"] == "already_running"
        assert body["job_id"] == str(globale.id)
        assert body["job"]["kind"] == ReindexJobKind.FULL_REINDEX
        assert RagSpaceReindexJob.objects.count() == 1

    def test_reprise_active_bloque_le_job_global(self, owner_client, space):
        _doc(space, "a.pdf")
        assert owner_client.post(URL.format(space.id), _filter(), format="json").status_code == 202
        RagSpaceReindexJob.objects.update(status=ReindexJobStatus.RUNNING)
        resp = owner_client.post(f"/api/v1/rag-spaces/{space.id}/reindex/")
        assert resp.status_code == 409
        assert resp.json()["status"] == "already_running"
        assert RagSpaceReindexJob.objects.count() == 1

    def test_deux_lancements_successifs_ne_creent_pas_deux_jobs(self, owner_client, space):
        _doc(space, "a.pdf")
        first = owner_client.post(URL.format(space.id), _filter(), format="json")
        second = owner_client.post(URL.format(space.id), _filter(), format="json")
        assert first.status_code == 202
        assert second.status_code == 409
        assert second.json()["job_id"] == first.json()["job_id"]
        assert RagSpaceReindexJob.objects.count() == 1

    def test_document_deja_pris_par_un_item_actif_est_ignore(self, owner_client, space):
        pris = _doc(space, "pris.pdf")
        libre = _doc(space, "libre.pdf")
        autre_job = RagSpaceReindexJob.objects.create(
            rag_space=RagSpace.objects.create(name="ailleurs", workspace=space.workspace),
            status=ReindexJobStatus.RUNNING,
        )
        RagSpaceReindexJobItem.objects.create(
            job=autre_job, document=pris, status=ReindexItemStatus.RUNNING
        )
        resp = owner_client.post(URL.format(space.id), _filter(), format="json")
        assert resp.status_code == 202
        # Deux items concurrents sur le même document = la course « ancien écrase
        # récent ». On ne la crée pas.
        assert _item_doc_ids(_job_of(resp)) == {str(libre.id)}


# --------------------------------------------------------------------------- #
# Exécution (worker existant, réutilisé tel quel)
# --------------------------------------------------------------------------- #
class TestExecution:
    def _run(self, job):
        from django.utils import timezone

        from apps.rag_core.tasks import reindex_job_item_task

        RagSpaceReindexJob.objects.filter(id=job.id).update(
            status=ReindexJobStatus.RUNNING, started_at=timezone.now()
        )
        for item_id in RagSpaceReindexJobItem.objects.filter(job=job).values_list("id", flat=True):
            reindex_job_item_task(item_id=str(item_id))

    def test_succes_met_le_job_a_completed(self, owner_client, space, monkeypatch):
        from apps.rag_core.services.ingest.types import IngestResult

        docs = [_doc(space, f"e{i}.pdf") for i in range(3)]
        monkeypatch.setattr(
            "apps.rag_core.services.ingest_document.reindex_existing_document",
            lambda **k: IngestResult(document_id=k["document_id"],
                                     status=DocumentStatus.READY, chunks_created=2, deduped=False),
        )
        job = _job_of(owner_client.post(URL.format(space.id), _filter(), format="json"))
        self._run(job)
        job.refresh_from_db()
        assert job.status == ReindexJobStatus.COMPLETED
        assert job.succeeded_count == len(docs)
        assert job.failed_count == 0

    def test_echec_isole_le_job_continue(self, owner_client, space, monkeypatch):
        from apps.rag_core.services.ingest.types import IngestResult

        docs = [_doc(space, f"e{i}.pdf") for i in range(3)]
        boom = str(docs[1].id)

        def fake(**k):
            if k["document_id"] == boom:
                raise RuntimeError("extraction impossible")
            return IngestResult(document_id=k["document_id"], status=DocumentStatus.READY,
                                chunks_created=1, deduped=False)

        monkeypatch.setattr(
            "apps.rag_core.services.ingest_document.reindex_existing_document", fake
        )
        job = _job_of(owner_client.post(URL.format(space.id), _filter(), format="json"))
        self._run(job)
        job.refresh_from_db()
        assert job.status == ReindexJobStatus.COMPLETED_WITH_ERRORS
        assert job.succeeded_count == 2 and job.failed_count == 1

    def test_document_supprime_avant_execution(self, owner_client, space, monkeypatch):
        from apps.rag_core.services.ingest.types import IngestResult

        a, b = _doc(space, "a.pdf"), _doc(space, "b.pdf")
        monkeypatch.setattr(
            "apps.rag_core.services.ingest_document.reindex_existing_document",
            lambda **k: IngestResult(document_id=k["document_id"],
                                     status=DocumentStatus.READY, chunks_created=1, deduped=False),
        )
        job = _job_of(owner_client.post(URL.format(space.id), _explicit(a, b), format="json"))
        b.delete()  # cascade : l'item disparaît avec le document
        self._run(job)
        job.refresh_from_db()
        assert job.status in (ReindexJobStatus.COMPLETED, ReindexJobStatus.COMPLETED_WITH_ERRORS)
        assert job.succeeded_count == 1

    def test_document_redevenu_ready_est_quand_meme_retraite(self, owner_client, space, monkeypatch):
        # Décision assumée : le snapshot fait foi. Un document passé READY entre la
        # sélection et l'exécution est retraité — le retraiter est inoffensif
        # (mêmes invariants d'indexation), l'ignorer laisserait l'utilisateur devant
        # un job dont le total ne correspond à rien de ce qu'il a demandé.
        from apps.rag_core.services.ingest.types import IngestResult

        seen = []
        monkeypatch.setattr(
            "apps.rag_core.services.ingest_document.reindex_existing_document",
            lambda **k: (seen.append(k["document_id"]),
                         IngestResult(document_id=k["document_id"], status=DocumentStatus.READY,
                                      chunks_created=1, deduped=False))[1],
        )
        a = _doc(space, "a.pdf")
        job = _job_of(owner_client.post(URL.format(space.id), _explicit(a), format="json"))
        Document.objects.filter(id=a.id).update(status=DocumentStatus.READY)
        self._run(job)
        assert seen == [str(a.id)]


# --------------------------------------------------------------------------- #
# Relance des échecs
# --------------------------------------------------------------------------- #
class TestRetryFailed:
    def test_relance_ne_reprend_que_les_echecs_et_herite_du_kind(self, owner_client, space):
        a, b = _doc(space, "a.pdf"), _doc(space, "b.pdf")
        job = _job_of(owner_client.post(URL.format(space.id), _explicit(a, b), format="json"))
        items = {str(i.document_id): i for i in RagSpaceReindexJobItem.objects.filter(job=job)}
        items[str(a.id)].status = ReindexItemStatus.SUCCEEDED
        items[str(a.id)].save(update_fields=["status"])
        items[str(b.id)].status = ReindexItemStatus.FAILED
        items[str(b.id)].save(update_fields=["status"])
        RagSpaceReindexJob.objects.filter(id=job.id).update(
            status=ReindexJobStatus.COMPLETED_WITH_ERRORS
        )

        resp = owner_client.post(f"/api/v1/reindex-jobs/{job.id}/retry-failed/")
        assert resp.status_code == 202
        child = RagSpaceReindexJob.objects.get(id=resp.json()["job_id"])
        assert child.parent_job_id == job.id
        assert child.kind == ReindexJobKind.FAILED_DOCUMENTS_RETRY
        assert _item_doc_ids(child) == {str(b.id)}, "le succès n'est pas rejoué"


# --------------------------------------------------------------------------- #
# Contrat de suivi et audit
# --------------------------------------------------------------------------- #
class TestPollingAndAudit:
    def test_polling_expose_kind_et_selection(self, owner_client, space):
        _doc(space, "a.pdf")
        job_id = owner_client.post(URL.format(space.id), _filter(search="a"), format="json").json()["job_id"]
        resp = owner_client.get(f"/api/v1/reindex-jobs/{job_id}/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == ReindexJobKind.FAILED_DOCUMENTS_RETRY
        assert body["selection"]["mode"] == "filter"
        assert body["selection"]["eligible_count"] == 1
        # Contrat commun aux deux types de jobs : mêmes clés de progression.
        for key in ("status", "total", "succeeded", "failed", "running", "progress_percent"):
            assert key in body

    def test_job_global_expose_le_kind_par_defaut(self, owner_client, space):
        _doc(space, "a.pdf", status=DocumentStatus.READY)
        job_id = owner_client.post(f"/api/v1/rag-spaces/{space.id}/reindex/").json()["job_id"]
        body = owner_client.get(f"/api/v1/reindex-jobs/{job_id}/").json()
        assert body["kind"] == ReindexJobKind.FULL_REINDEX

    def test_audit_a_la_creation_sans_contenu_documentaire(self, owner_client, space):
        from apps.audit.models import ActionAuditEvent

        _doc(space, "Rapport confidentiel.pdf")
        resp = owner_client.post(URL.format(space.id), _filter(), format="json")
        assert resp.status_code == 202

        ev = ActionAuditEvent.objects.filter(
            action_name="ragspace.retry_failed_documents", status="queued"
        ).first()
        assert ev is not None
        meta = ev.metadata or {}
        assert meta["kind"] == ReindexJobKind.FAILED_DOCUMENTS_RETRY
        assert meta["selection_mode"] == "filter"
        assert meta["eligible_count"] == 1
        # Aucun nom de fichier, aucun contenu extrait dans l'audit.
        assert "Rapport confidentiel" not in str(meta)

    def test_audit_du_refus(self, db, space, workspace):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        from apps.audit.models import ActionAuditEvent

        _doc(space, "a.pdf")
        u = get_user_model().objects.create_user(username="mbr", password="pw")
        Membership.objects.create(workspace=workspace, user=u, role=Membership.ROLE_MEMBER)
        c = APIClient()
        c.force_authenticate(user=u)
        assert c.post(URL.format(space.id), _filter(), format="json").status_code == 403
        assert ActionAuditEvent.objects.filter(
            action_name="ragspace.retry_failed_documents", status="denied"
        ).exists()


# --------------------------------------------------------------------------- #
# Cohérence avec la liste documentaire
# --------------------------------------------------------------------------- #
def test_le_perimetre_erreur_est_celui_de_la_liste():
    """La sélection « erreurs » doit être EXACTEMENT ce que l'onglet Erreurs montre.

    Deux implémentations de « en erreur » finiraient par diverger : ce test échoue
    au premier écart entre la sémantique de la liste et celle de la reprise.
    """
    from django.db.models import Q

    from api.v1.documents import listing

    assert listing.status_filter(listing.UI_ERROR) == Q(status=DocumentStatus.FAILED)


def test_selection_par_filtre_utilise_la_recherche_de_la_liste(owner_client, space):
    """Même terme, même résultat que la liste — accents et casse compris."""
    keep = _doc(space, "Synthèse ANNUELLE.pdf")
    _doc(space, "budget.pdf")
    listed = owner_client.get(
        f"/api/v1/documents/?rag_space_id={space.id}&status=error&search=synthèse"
    ).json()
    assert [i["id"] for i in listed["items"]] == [str(keep.id)]

    resp = owner_client.post(URL.format(space.id), _filter(search="synthèse"), format="json")
    assert _item_doc_ids(_job_of(resp)) == {str(keep.id)}
