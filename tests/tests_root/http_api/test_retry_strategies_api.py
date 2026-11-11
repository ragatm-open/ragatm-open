"""Stratégies de reprise — contrats API, propagation jusqu'à l'extracteur, timeouts.

La table de décision elle-même est couverte à l'unité (`unit/test_retry_strategy.py`).
Ici on vérifie qu'elle est réellement BRANCHÉE : que la stratégie survit au snapshot,
qu'elle atteint l'extracteur sous forme de drapeau OCR et de budget de temps, et
qu'aucune limite de sécurité n'est relâchée au passage.
"""
from __future__ import annotations

import pytest

from apps.rag_core.models import RagSpace
from apps.rag_core.models.document import Document, DocumentStatus
from apps.rag_core.models.reindex_job import (
    RagSpaceReindexJob,
    RagSpaceReindexJobItem,
    ReindexItemStatus,
    ReindexJobStatus,
)
from apps.rag_core.services.ingest import retry_strategy as rs
from apps.workspaces.models import Membership

pytestmark = pytest.mark.django_db

RETRY_URL = "/api/v1/rag-spaces/{}/retry-failed-documents/"
PREVIEW_URL = "/api/v1/rag-spaces/{}/retry-failed-documents/preview/"


@pytest.fixture
def owner_client(db, user, workspace):
    from rest_framework.test import APIClient

    Membership.objects.update_or_create(
        workspace=workspace, user=user, defaults={"role": Membership.ROLE_OWNER}
    )
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def space(db, workspace):
    return RagSpace.objects.create(name="strategy-space", workspace=workspace)


@pytest.fixture(autouse=True)
def _no_enqueue(monkeypatch):
    monkeypatch.setattr(
        "apps.rag_core.services.ingest.reindex_jobs._enqueue_orchestrator", lambda job_id: None
    )


def _doc(space, name, code, *, ctype="application/pdf", with_file=True):
    d = Document.objects.create(
        rag_space=space, filename=name, content_type=ctype, size_bytes=10,
        sha256=f"{abs(hash(name)):064x}"[:64], status=DocumentStatus.FAILED,
        error_message=f"{code}: détail", meta={"extraction": {"error_code": code}},
    )
    if with_file:
        d.storage_file.name = f"rag_docs/{space.id}/{d.id}/{name}"
        d.save(update_fields=["storage_file"])
    return d


def _filter(**kw):
    return {"selection": {"mode": "filter"}, **kw}


def _items(job):
    return {str(i.document_id): i for i in RagSpaceReindexJobItem.objects.filter(job=job)}


# --------------------------------------------------------------------------- #
# Prévisualisation
# --------------------------------------------------------------------------- #
class TestPreview:
    @pytest.fixture
    def mixed(self, space):
        return {
            "ocr": _doc(space, "scan.pdf", "pdf_scanned_requires_ocr"),
            "timeout": _doc(space, "lourd.pdf", "extraction_timeout"),
            "std": _doc(space, "reseau.pdf", "transient_ingest_error"),
            "dead": _doc(space, "macro.docx", "macro_enabled_document_refused",
                         ctype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "noocr": _doc(space, "illisible.pdf", "ocr_no_text"),
        }

    def test_repartition_par_strategie(self, owner_client, space, mixed):
        r = owner_client.post(PREVIEW_URL.format(space.id), _filter(strategy="auto"), format="json")
        assert r.status_code == 200
        body = r.json()
        assert body["requested_strategy"] == "auto"
        assert body["total_documents"] == 5
        assert body["actionable_documents"] == 3
        assert body["skipped_documents"] == 2
        assert body["strategy_breakdown"] == {"force_ocr": 1, "extended_timeout": 1, "standard": 1}
        assert body["skip_reasons"] == {"non_retryable": 1, "replace_source_recommended": 1}
        assert body["extended_timeout_seconds"] == rs.extended_timeout_seconds()

    def test_la_prevision_ne_cree_rien(self, owner_client, space, mixed):
        owner_client.post(PREVIEW_URL.format(space.id), _filter(strategy="auto"), format="json")
        assert not RagSpaceReindexJob.objects.exists()
        assert not RagSpaceReindexJobItem.objects.exists()

    def test_la_prevision_annonce_ce_que_la_creation_execute(self, owner_client, space, mixed):
        preview = owner_client.post(
            PREVIEW_URL.format(space.id), _filter(strategy="auto"), format="json"
        ).json()
        created = owner_client.post(
            RETRY_URL.format(space.id), _filter(strategy="auto"), format="json"
        ).json()
        assert created["strategy_breakdown"] == preview["strategy_breakdown"]
        assert created["skip_reasons"] == preview["skip_reasons"]

    def test_strategie_explicite_dans_la_prevision(self, owner_client, space, mixed):
        body = owner_client.post(
            PREVIEW_URL.format(space.id), _filter(strategy="force_ocr"), format="json"
        ).json()
        # Les PDF acceptent l'OCR ; le .docx ne relève pas de l'OCR et reste écarté.
        assert body["strategy_breakdown"] == {"force_ocr": 3}
        assert body["skip_reasons"]["non_retryable"] == 1

    def test_permissions_identiques_a_la_creation(self, db, space, workspace):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        _doc(space, "a.pdf", "extraction_timeout")
        u = get_user_model().objects.create_user(username="mbr", password="pw")
        Membership.objects.create(workspace=workspace, user=u, role=Membership.ROLE_MEMBER)
        c = APIClient()
        c.force_authenticate(user=u)
        r = c.post(PREVIEW_URL.format(space.id), _filter(), format="json")
        assert r.status_code == 403
        assert "strategy_breakdown" not in r.content.decode()


# --------------------------------------------------------------------------- #
# Création : stratégie figée au snapshot
# --------------------------------------------------------------------------- #
class TestJobSnapshot:
    def test_strategie_demandee_et_effectives_persistees(self, owner_client, space):
        ocr = _doc(space, "scan.pdf", "pdf_scanned_requires_ocr")
        slow = _doc(space, "lourd.pdf", "extraction_timeout")
        r = owner_client.post(RETRY_URL.format(space.id), _filter(strategy="auto"), format="json")
        assert r.status_code == 202
        job = RagSpaceReindexJob.objects.get(id=r.json()["job_id"])
        assert job.requested_strategy == "auto"
        items = _items(job)
        assert items[str(ocr.id)].effective_strategy == "force_ocr"
        assert items[str(slow.id)].effective_strategy == "extended_timeout"
        assert items[str(slow.id)].strategy_meta["timeout_seconds"] == rs.extended_timeout_seconds()

    def test_les_documents_non_reprenables_deviennent_des_items_skipped(self, owner_client, space):
        ok = _doc(space, "a.pdf", "extraction_timeout")
        dead = _doc(space, "b.pdf", "unsupported_file_type")
        r = owner_client.post(RETRY_URL.format(space.id), _filter(strategy="auto"), format="json")
        job = RagSpaceReindexJob.objects.get(id=r.json()["job_id"])
        items = _items(job)
        # Retenus dans le snapshot pour la traçabilité, mais jamais traités.
        assert items[str(dead.id)].status == ReindexItemStatus.SKIPPED
        assert items[str(dead.id)].skip_reason == "non_retryable"
        assert items[str(ok.id)].status == ReindexItemStatus.PENDING
        job.refresh_from_db()
        assert job.skipped_count == 1

    def test_un_lot_sans_aucun_document_reprenable_est_refuse(self, owner_client, space):
        _doc(space, "a.pdf", "ocr_no_text")
        _doc(space, "b.pdf", "unsupported_file_type")
        r = owner_client.post(RETRY_URL.format(space.id), _filter(strategy="auto"), format="json")
        assert r.status_code == 409
        assert r.json()["detail"] == "no_retryable_documents"
        assert not RagSpaceReindexJob.objects.exists()

    def test_lot_mixte_en_ocr_force_saute_les_incompatibles(self, owner_client, space):
        pdf = _doc(space, "scan.pdf", "transient_ingest_error")
        txt = _doc(space, "notes.txt", "transient_ingest_error", ctype="text/plain")
        r = owner_client.post(RETRY_URL.format(space.id), _filter(strategy="force_ocr"), format="json")
        assert r.status_code == 202, "un incompatible ne fait pas échouer toute la campagne"
        items = _items(RagSpaceReindexJob.objects.get(id=r.json()["job_id"]))
        assert items[str(pdf.id)].status == ReindexItemStatus.PENDING
        assert items[str(txt.id)].status == ReindexItemStatus.SKIPPED
        assert items[str(txt.id)].skip_reason == "strategy_not_supported"
        assert r.json()["skip_reasons"] == {"strategy_not_supported": 1}

    def test_la_strategie_est_figee_meme_si_le_code_derreur_change_ensuite(self, owner_client, space):
        d = _doc(space, "lourd.pdf", "extraction_timeout")
        r = owner_client.post(RETRY_URL.format(space.id), _filter(strategy="auto"), format="json")
        job = RagSpaceReindexJob.objects.get(id=r.json()["job_id"])
        # Le code d'erreur du document change APRÈS le lancement : le job en cours
        # ne doit pas voir sa méthode bouger sous lui.
        Document.objects.filter(id=d.id).update(
            meta={"extraction": {"error_code": "pdf_scanned_requires_ocr"}}
        )
        assert _items(job)[str(d.id)].effective_strategy == "extended_timeout"

    def test_defaut_sans_strategie_explicite(self, owner_client, space):
        _doc(space, "scan.pdf", "pdf_scanned_requires_ocr")
        r = owner_client.post(RETRY_URL.format(space.id), {"selection": {"mode": "filter"}}, format="json")
        job = RagSpaceReindexJob.objects.get(id=r.json()["job_id"])
        # `auto` par défaut : c'est la seule valeur qui tient compte de la cause.
        assert job.requested_strategy == "auto"
        assert _items(job)[str(RagSpaceReindexJobItem.objects.first().document_id)].effective_strategy == "force_ocr"

    @pytest.mark.parametrize("bad", ["magique", "AUTO_ ", 42, ""])
    def test_strategie_inconnue_refusee(self, owner_client, space, bad):
        _doc(space, "a.pdf", "extraction_timeout")
        r = owner_client.post(RETRY_URL.format(space.id), _filter(strategy=bad), format="json")
        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_selection"

    def test_aucun_timeout_libre_accepte_du_client(self, owner_client, space):
        _doc(space, "a.pdf", "extraction_timeout")
        r = owner_client.post(
            RETRY_URL.format(space.id),
            {"selection": {"mode": "filter"}, "strategy": "extended_timeout",
             "timeout_seconds": 99999, "extraction_timeout_seconds": 99999},
            format="json",
        )
        assert r.status_code == 202
        item = RagSpaceReindexJobItem.objects.first()
        # La valeur vient de la CONFIGURATION, jamais du corps de requête : un délai
        # libre serait un levier pour occuper un worker aussi longtemps qu'on veut.
        assert item.strategy_meta["timeout_seconds"] == rs.extended_timeout_seconds()


# --------------------------------------------------------------------------- #
# Suivi
# --------------------------------------------------------------------------- #
class TestPolling:
    def test_le_polling_expose_les_repartitions(self, owner_client, space):
        _doc(space, "scan.pdf", "pdf_scanned_requires_ocr")
        _doc(space, "lourd.pdf", "extraction_timeout")
        _doc(space, "mort.pdf", "unsupported_file_type")
        job_id = owner_client.post(
            RETRY_URL.format(space.id), _filter(strategy="auto"), format="json"
        ).json()["job_id"]

        body = owner_client.get(f"/api/v1/reindex-jobs/{job_id}/").json()
        assert body["requested_strategy"] == "auto"
        assert body["strategy_breakdown"] == {
            "force_ocr": {"total": 1, "succeeded": 0, "failed": 0},
            "extended_timeout": {"total": 1, "succeeded": 0, "failed": 0},
        }
        assert body["skipped"] == 1
        assert body["skip_reasons"] == {"non_retryable": 1}

    def test_le_job_global_reste_lisible(self, owner_client, space):
        d = Document.objects.create(
            rag_space=space, filename="ok.pdf", content_type="application/pdf",
            size_bytes=1, sha256="a" * 64, status=DocumentStatus.READY,
        )
        d.storage_file.name = f"rag_docs/{space.id}/{d.id}/ok.pdf"
        d.save(update_fields=["storage_file"])
        job_id = owner_client.post(f"/api/v1/rag-spaces/{space.id}/reindex/").json()["job_id"]
        body = owner_client.get(f"/api/v1/reindex-jobs/{job_id}/").json()
        assert body["requested_strategy"] == "standard"
        assert body["skip_reasons"] == {}


# --------------------------------------------------------------------------- #
# Propagation jusqu'à l'extracteur
# --------------------------------------------------------------------------- #
class TestPropagation:
    def _run_items(self, job):
        from django.utils import timezone

        from apps.rag_core.tasks import reindex_job_item_task

        RagSpaceReindexJob.objects.filter(id=job.id).update(
            status=ReindexJobStatus.RUNNING, started_at=timezone.now()
        )
        for item_id in RagSpaceReindexJobItem.objects.filter(job=job).values_list("id", flat=True):
            reindex_job_item_task(item_id=str(item_id))

    def test_le_worker_transmet_ocr_et_timeout(self, owner_client, space, monkeypatch):
        from apps.rag_core.services.ingest.types import IngestResult

        captured = {}

        def fake(**k):
            captured[k["document_id"]] = k
            return IngestResult(document_id=k["document_id"], status=DocumentStatus.READY,
                                chunks_created=1, deduped=False)

        monkeypatch.setattr(
            "apps.rag_core.services.ingest_document.reindex_existing_document", fake
        )
        ocr = _doc(space, "scan.pdf", "pdf_scanned_requires_ocr")
        slow = _doc(space, "lourd.pdf", "extraction_timeout")
        std = _doc(space, "reseau.pdf", "transient_ingest_error")
        job = RagSpaceReindexJob.objects.get(
            id=owner_client.post(RETRY_URL.format(space.id), _filter(strategy="auto"), format="json").json()["job_id"]
        )
        self._run_items(job)

        assert captured[str(ocr.id)]["ocr_enabled"] is True
        assert captured[str(ocr.id)]["extraction_timeout_seconds"] is None
        assert captured[str(slow.id)]["extraction_timeout_seconds"] == rs.extended_timeout_seconds()
        assert captured[str(slow.id)]["ocr_enabled"] is None
        # `standard` ne doit PAS poser ocr_enabled=False : cela DÉSACTIVERAIT l'OCR
        # d'un environnement où il est activé globalement.
        assert captured[str(std.id)]["ocr_enabled"] is None
        assert captured[str(std.id)]["extraction_timeout_seconds"] is None

    def test_un_item_skipped_n_appelle_jamais_le_pipeline(self, owner_client, space, monkeypatch):
        from apps.rag_core.services.ingest.types import IngestResult

        seen = []
        monkeypatch.setattr(
            "apps.rag_core.services.ingest_document.reindex_existing_document",
            lambda **k: (seen.append(k["document_id"]),
                         IngestResult(document_id=k["document_id"], status=DocumentStatus.READY,
                                      chunks_created=1, deduped=False))[1],
        )
        ok = _doc(space, "a.pdf", "extraction_timeout")
        _doc(space, "b.pdf", "unsupported_file_type")
        job = RagSpaceReindexJob.objects.get(
            id=owner_client.post(RETRY_URL.format(space.id), _filter(strategy="auto"), format="json").json()["job_id"]
        )
        self._run_items(job)
        assert seen == [str(ok.id)]

    def test_le_budget_atteint_la_primitive_d_annulation(self, monkeypatch):
        """Le paramètre doit arriver jusqu'à la primitive de temps, pas s'arrêter en route.

        Le contrat est inchangé ; seule la primitive l'est. Depuis
        EXTRACTION-TIMEOUT-HARDENING-03 le budget document est tenu par
        `run_in_cancellable_process`, qui termine RÉELLEMENT le travail, et non plus par
        `run_with_timeout`, qui se contentait de cesser d'attendre.
        """
        from apps.rag_core.services.ingest import extraction as extraction_pkg

        seen = {}

        def fake_run(fn, timeout_s=None):
            seen["timeout"] = timeout_s
            return fn()

        monkeypatch.setattr(
            "apps.rag_core.services.ingest.process_isolation.run_in_cancellable_process",
            fake_run,
        )
        monkeypatch.setattr(
            extraction_pkg, "validate_and_classify", lambda data, ctx, **k: ("text/plain", "txt")
        )
        extraction_pkg.extract_document(b"bonjour", filename="a.txt", timeout_seconds=480)
        assert seen["timeout"] == 480

    def test_sans_strategie_le_budget_reste_celui_de_la_configuration(self, monkeypatch, settings):
        from apps.rag_core.services.ingest import extraction as extraction_pkg

        settings.DOCUMENT_EXTRACTION_TIMEOUT_SECONDS = 180
        seen = {}
        monkeypatch.setattr(
            "apps.rag_core.services.ingest.process_isolation.run_in_cancellable_process",
            lambda fn, timeout_s=None: (seen.__setitem__("timeout", timeout_s), fn())[1],
        )
        monkeypatch.setattr(
            extraction_pkg, "validate_and_classify", lambda data, ctx, **k: ("text/plain", "txt")
        )
        extraction_pkg.extract_document(b"bonjour", filename="a.txt")
        assert seen["timeout"] == 180


# --------------------------------------------------------------------------- #
# Reprise unitaire
# --------------------------------------------------------------------------- #
class TestUnitRetry:
    def _url(self, doc):
        return f"/api/v1/documents/{doc.id}/reindex/"

    @pytest.fixture(autouse=True)
    def _capture_enqueue(self, monkeypatch):
        from api.v1.documents import views_write

        captured = {}

        def fake(*, request, doc, ocr=None, extraction_timeout_seconds=None):
            captured.update({"ocr": ocr, "timeout": extraction_timeout_seconds})
            return None

        monkeypatch.setattr(views_write, "enqueue_async_or_503", fake)
        monkeypatch.setattr(views_write, "is_celery_eager_mode", lambda: False)
        return captured

    def test_strategie_explicite_force_ocr(self, owner_client, space, _capture_enqueue):
        d = _doc(space, "scan.pdf", "pdf_scanned_requires_ocr")
        r = owner_client.post(self._url(d), {"strategy": "force_ocr"}, format="json")
        assert r.status_code == 200
        assert r.json()["strategy"] == "force_ocr"
        assert _capture_enqueue["ocr"] is True

    def test_strategie_auto_choisit_le_delai_etendu(self, owner_client, space, _capture_enqueue):
        d = _doc(space, "lourd.pdf", "extraction_timeout")
        r = owner_client.post(self._url(d), {"strategy": "auto"}, format="json")
        assert r.json()["strategy"] == "extended_timeout"
        assert _capture_enqueue["timeout"] == rs.extended_timeout_seconds()
        assert _capture_enqueue["ocr"] is None

    def test_ocr_sur_un_format_incompatible_refuse(self, owner_client, space):
        d = _doc(space, "notes.txt", "transient_ingest_error", ctype="text/plain")
        r = owner_client.post(self._url(d), {"strategy": "force_ocr"}, format="json")
        assert r.status_code == 400
        assert r.json()["detail"] == "strategy_not_supported"

    def test_document_definitivement_en_echec_refuse(self, owner_client, space):
        d = _doc(space, "illisible.pdf", "ocr_no_text")
        r = owner_client.post(self._url(d), {"strategy": "auto"}, format="json")
        assert r.status_code == 400
        assert r.json()["detail"] == "document_not_eligible"
        assert r.json()["retryability"] == "replace_source_recommended"

    def test_strategie_inconnue_refusee(self, owner_client, space):
        d = _doc(space, "a.pdf", "extraction_timeout")
        r = owner_client.post(self._url(d), {"strategy": "magique"}, format="json")
        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_strategy"

    def test_compat_ocr_true_traduit_en_force_ocr(self, owner_client, space, _capture_enqueue):
        # Contrat DÉPRÉCIÉ mais conservé : les appels existants ne doivent pas casser.
        d = _doc(space, "scan.pdf", "pdf_scanned_requires_ocr")
        r = owner_client.post(self._url(d) + "?ocr=true")
        assert r.status_code == 200
        assert r.json()["ocr"] is True
        assert r.json()["strategy"] == "force_ocr"
        assert _capture_enqueue["ocr"] is True

    def test_sans_parametre_la_reprise_reste_standard(self, owner_client, space, _capture_enqueue):
        d = _doc(space, "a.pdf", "pdf_scanned_requires_ocr")
        r = owner_client.post(self._url(d) + "?async=true")
        assert r.json()["strategy"] == "standard", "aucun changement de comportement par défaut"
        assert _capture_enqueue["ocr"] is None


# --------------------------------------------------------------------------- #
# Recommandation exposée au frontend
# --------------------------------------------------------------------------- #
class TestDocumentHints:
    @pytest.mark.parametrize("code, retryability, recommended", [
        ("pdf_scanned_requires_ocr", "retryable_with_strategy", "force_ocr"),
        ("extraction_timeout", "retryable_with_strategy", "extended_timeout"),
        ("transient_ingest_error", "retryable", "standard"),
        ("ocr_no_text", "replace_source_recommended", None),
        ("unsupported_file_type", "non_retryable", None),
    ])
    def test_le_document_porte_sa_recommandation(self, owner_client, space, code,
                                                 retryability, recommended):
        d = _doc(space, f"{code}.pdf", code)
        body = owner_client.get(f"/api/v1/documents/{d.id}/").json()
        assert body["extraction"]["error_code"] == code
        assert body["extraction"]["retryability"] == retryability
        assert body["extraction"]["recommended_strategy"] == recommended

    def test_un_document_pret_ne_recommande_rien(self, owner_client, space):
        d = Document.objects.create(
            rag_space=space, filename="ok.pdf", content_type="application/pdf",
            size_bytes=1, sha256="b" * 64, status=DocumentStatus.READY,
            meta={"extraction": {"extraction_status": "success"}},
        )
        body = owner_client.get(f"/api/v1/documents/{d.id}/").json()
        assert body["extraction"]["retryability"] == ""
        assert body["extraction"]["recommended_strategy"] is None

    def test_un_echec_sans_metadonnees_reste_classe(self, owner_client, space):
        # Échec hors couche d'extraction (fichier source absent) : pas de
        # `meta.extraction`, mais une recommandation quand même.
        d = Document.objects.create(
            rag_space=space, filename="perdu.pdf", content_type="application/pdf",
            size_bytes=1, sha256="c" * 64, status=DocumentStatus.FAILED,
            error_message="no_storage_file_for_reindex",
        )
        body = owner_client.get(f"/api/v1/documents/{d.id}/").json()
        assert body["extraction"]["retryability"] == "non_retryable"


# --------------------------------------------------------------------------- #
# Relance des échecs
# --------------------------------------------------------------------------- #
class TestRetryFailed:
    def _terminal_job_with_failure(self, owner_client, space, doc):
        job = RagSpaceReindexJob.objects.get(
            id=owner_client.post(
                RETRY_URL.format(space.id), _filter(strategy="standard"), format="json"
            ).json()["job_id"]
        )
        RagSpaceReindexJobItem.objects.filter(job=job, document=doc).update(
            status=ReindexItemStatus.FAILED
        )
        RagSpaceReindexJob.objects.filter(id=job.id).update(
            status=ReindexJobStatus.COMPLETED_WITH_ERRORS
        )
        job.refresh_from_db()
        return job

    def test_relance_conserve_la_strategie_du_parent_par_defaut(self, owner_client, space):
        d = _doc(space, "lourd.pdf", "extraction_timeout")
        job = self._terminal_job_with_failure(owner_client, space, d)
        r = owner_client.post(f"/api/v1/reindex-jobs/{job.id}/retry-failed/")
        assert r.status_code == 202
        child = RagSpaceReindexJob.objects.get(id=r.json()["job_id"])
        assert child.requested_strategy == "standard"
        assert _items(child)[str(d.id)].effective_strategy == "standard"

    def test_relance_auto_recalcule_sur_le_code_a_jour(self, owner_client, space):
        d = _doc(space, "lourd.pdf", "extraction_timeout")
        job = self._terminal_job_with_failure(owner_client, space, d)
        r = owner_client.post(
            f"/api/v1/reindex-jobs/{job.id}/retry-failed/", {"strategy": "auto"}, format="json"
        )
        assert r.status_code == 202
        child = RagSpaceReindexJob.objects.get(id=r.json()["job_id"])
        assert child.requested_strategy == "auto"
        assert _items(child)[str(d.id)].effective_strategy == "extended_timeout"

    def test_relance_avec_une_strategie_inconnue_refusee(self, owner_client, space):
        d = _doc(space, "a.pdf", "extraction_timeout")
        job = self._terminal_job_with_failure(owner_client, space, d)
        r = owner_client.post(
            f"/api/v1/reindex-jobs/{job.id}/retry-failed/", {"strategy": "magique"}, format="json"
        )
        assert r.status_code == 400

    def test_les_items_skipped_ne_sont_jamais_relances(self, owner_client, space):
        ok = _doc(space, "a.pdf", "extraction_timeout")
        dead = _doc(space, "b.pdf", "unsupported_file_type")
        job = RagSpaceReindexJob.objects.get(
            id=owner_client.post(RETRY_URL.format(space.id), _filter(strategy="auto"), format="json").json()["job_id"]
        )
        RagSpaceReindexJobItem.objects.filter(job=job, document=ok).update(
            status=ReindexItemStatus.FAILED
        )
        RagSpaceReindexJob.objects.filter(id=job.id).update(
            status=ReindexJobStatus.COMPLETED_WITH_ERRORS
        )
        job.refresh_from_db()
        r = owner_client.post(f"/api/v1/reindex-jobs/{job.id}/retry-failed/")
        child = RagSpaceReindexJob.objects.get(id=r.json()["job_id"])
        assert set(_items(child)) == {str(ok.id)}
        assert str(dead.id) not in _items(child)


# --------------------------------------------------------------------------- #
# Intégrité & audit
# --------------------------------------------------------------------------- #
def test_aucune_strategie_ne_cree_de_document_ni_ne_consomme_de_quota(owner_client, space, workspace):
    from apps.workspaces.services import quota as quota_service

    docs = [
        _doc(space, "scan.pdf", "pdf_scanned_requires_ocr"),
        _doc(space, "lourd.pdf", "extraction_timeout"),
    ]
    before_count = Document.objects.count()
    before = quota_service.get_usage(workspace)
    before_files = {str(d.id): d.storage_file.name for d in docs}

    assert owner_client.post(
        RETRY_URL.format(space.id), _filter(strategy="auto"), format="json"
    ).status_code == 202

    after = quota_service.get_usage(workspace)
    assert Document.objects.count() == before_count
    assert after["used_document_count"] == before["used_document_count"]
    assert after["used_document_bytes"] == before["used_document_bytes"]
    for d in Document.objects.filter(id__in=[x.id for x in docs]):
        assert d.storage_file.name == before_files[str(d.id)]


def test_l_audit_repond_a_pourquoi_ce_document_a_ete_repris_avec_ocr(owner_client, space):
    from apps.audit.models import ActionAuditEvent

    _doc(space, "Rapport confidentiel.pdf", "pdf_scanned_requires_ocr")
    _doc(space, "lourd.pdf", "extraction_timeout")
    assert owner_client.post(
        RETRY_URL.format(space.id), _filter(strategy="auto"), format="json"
    ).status_code == 202

    ev = ActionAuditEvent.objects.filter(
        action_name="ragspace.retry_failed_documents", status="queued"
    ).first()
    meta = ev.metadata or {}
    assert meta["requested_strategy"] == "auto"
    assert meta["effective_strategy_breakdown"] == {"force_ocr": 1, "extended_timeout": 1}
    assert meta["ocr_forced_count"] == 1
    assert meta["extended_timeout_seconds"] == rs.extended_timeout_seconds()
    assert "Rapport confidentiel" not in str(meta)


def test_l_audit_unitaire_porte_la_raison_de_la_strategie(owner_client, space, monkeypatch):
    from api.v1.documents import views_write
    from apps.audit.models import ActionAuditEvent

    monkeypatch.setattr(
        views_write, "enqueue_async_or_503",
        lambda **k: None,
    )
    monkeypatch.setattr(views_write, "is_celery_eager_mode", lambda: False)
    d = _doc(space, "scan.pdf", "pdf_scanned_requires_ocr")
    assert owner_client.post(
        f"/api/v1/documents/{d.id}/reindex/", {"strategy": "auto"}, format="json"
    ).status_code == 200

    ev = ActionAuditEvent.objects.filter(
        action_name="document.reindex", status="queued"
    ).first()
    meta = ev.metadata or {}
    assert meta["effective_strategy"] == "force_ocr"
    assert meta["reason_code"] == "pdf_scanned_requires_ocr"
    assert meta["retryability"] == "retryable_with_strategy"


# --------------------------------------------------------------------------- #
# Cohérence de configuration
# --------------------------------------------------------------------------- #
class TestTimeoutConfiguration:
    def test_la_configuration_par_defaut_est_coherente(self):
        from config.checks import check_extraction_timeouts

        assert check_extraction_timeouts() == []

    def test_un_budget_etendu_au_dela_de_la_limite_celery_est_une_erreur(self, settings):
        settings.DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS = 600
        settings.CELERY_TASK_SOFT_TIME_LIMIT = 540
        from config.checks import check_extraction_timeouts

        issues = check_extraction_timeouts()
        assert [i.id for i in issues] == ["config.E012"]

    def test_un_budget_etendu_inutile_est_signale(self, settings):
        settings.DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS = 180
        settings.DOCUMENT_EXTRACTION_TIMEOUT_SECONDS = 180
        from config.checks import check_extraction_timeouts

        assert [i.id for i in check_extraction_timeouts()] == ["config.W013"]
