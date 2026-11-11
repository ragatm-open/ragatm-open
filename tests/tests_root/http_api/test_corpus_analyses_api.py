"""L2 — API des analyses : création, consultation, annulation, permissions, Celery.

Preuves : queue `analytics` explicite, enqueue APRÈS commit, permissions
revérifiées à chaque accès, anti-énumération, aucun champ technique exposé.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.analysis.registry import COVERAGE_SNAPSHOT
from apps.rag_core.analysis.runner import execute_job
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.corpus_analysis_job import AnalysisStatus, CorpusAnalysisJob
from apps.rag_core.models.document import DocumentStatus
from apps.workspaces.models import Membership, Workspace

pytestmark = pytest.mark.django_db


def _doc(space, *, status=DocumentStatus.READY, name="d.txt", sha="a"):
    return Document.objects.create(rag_space=space, filename=name, status=status, sha256=sha)


def _chunk(space, doc, *, text="abcd", idx=0):
    return Chunk.objects.create(
        rag_space=space, document=doc, chunk_index=idx, text=text, page_number=1
    )


@pytest.fixture
def corpus(rag_space):
    for i in range(3):
        d = _doc(rag_space, name=f"ok{i}.txt", sha=f"s{i}")
        _chunk(rag_space, d, idx=i)
    _doc(rag_space, name="ko.txt", status=DocumentStatus.FAILED, sha="")
    return rag_space


@pytest.fixture
def captured_enqueue(monkeypatch):
    """Espionne apply_async : queue, args, et NOMBRE d'enqueues."""
    calls = []
    from apps.rag_core import tasks

    def fake(*args, **kwargs):
        calls.append({"args": kwargs.get("args") or (args[0] if args else None),
                      "queue": kwargs.get("queue")})

    monkeypatch.setattr(tasks.run_corpus_analysis_job, "apply_async", fake)
    return calls


def _create(client, space, **body):
    payload = {"analysis_type": COVERAGE_SNAPSHOT, "parameters": {}, **body}
    return client.post(
        f"/api/v1/rag-spaces/{space.id}/analyses/", payload, format="json"
    )


# --------------------------------------------------------------------------- #
# API1 / API2 — création
# --------------------------------------------------------------------------- #

def test_create_analysis(api_client, corpus, captured_enqueue):
    res = _create(api_client, corpus, request_id="rid-1")

    assert res.status_code == 202
    d = res.data
    assert d["analysis_type"] == COVERAGE_SNAPSHOT
    assert d["status"] == "pending"
    assert d["reused"] is False
    assert d["progress"] == {"phase": "queued", "current": 0, "total": 1, "percent": 0}
    assert d["corpus_fingerprint"].startswith("sha256:")

    p = d["preflight"]
    assert p["documents_total"] == 4 and p["documents_ready"] == 3
    assert p["documents_failed"] == 1 and p["chunks_total"] == 3
    assert p["within_limits"] is True and p["limits_exceeded"] == []
    assert d["is_stale"] is None  # non terminal


def test_unknown_analysis_type_is_400(api_client, corpus):
    res = _create(api_client, corpus, analysis_type="corpus.kpi_inventory")
    assert res.status_code == 400


def test_handler_path_cannot_be_injected(api_client, corpus):
    """Le registre est FERMÉ : aucune clé n'est résolue en chemin d'import."""
    for evil in ("apps.rag_core.tasks.ingest_document_task",
                 "os.system", "../../etc/passwd", "corpus.global_search"):
        assert _create(api_client, corpus, analysis_type=evil).status_code == 400


@pytest.mark.parametrize("field,value", [
    ("status", "succeeded"),
    ("result", {"fake": 1}),
    ("coverage", {"documents_analyzed": 999}),
    ("corpus_fingerprint", "sha256:forged"),
    ("cost_usd", 0),
    ("progress_percent", 100),
    ("requested_by", 1),
])
def test_client_cannot_inject_server_owned_fields(api_client, corpus, field, value):
    res = _create(api_client, corpus, **{field: value})
    assert res.status_code == 400, f"champ {field} accepté !"


# --------------------------------------------------------------------------- #
# CEL1 / CEL2 / CEL6 — queue dédiée, après commit
# --------------------------------------------------------------------------- #

def test_task_is_enqueued_on_the_analytics_queue(
    api_client, corpus, captured_enqueue, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        res = _create(api_client, corpus, request_id="rid-1")

    assert len(captured_enqueue) == 1
    call = captured_enqueue[0]
    assert call["queue"] == "analytics"  # JAMAIS la queue par défaut
    assert call["args"] == [res.data["analysis_id"]]


def test_task_declares_its_own_time_limits_and_queue():
    """CEL5 / CEL6 — le routage est déclaré à trois endroits (défense en profondeur)."""
    from django.conf import settings

    from apps.rag_core.tasks import run_corpus_analysis_job

    assert run_corpus_analysis_job.queue == "analytics"
    assert run_corpus_analysis_job.soft_time_limit == settings.CORPUS_ANALYTICS_TASK_SOFT_TIME_LIMIT
    assert run_corpus_analysis_job.time_limit == settings.CORPUS_ANALYTICS_TASK_TIME_LIMIT
    # Ces limites ne doivent PAS être les 540/600 s globaux, dimensionnés pour
    # l'ingestion d'un seul document.
    assert run_corpus_analysis_job.soft_time_limit > settings.CELERY_TASK_SOFT_TIME_LIMIT

    routes = settings.CELERY_TASK_ROUTES
    assert routes["apps.rag_core.tasks.run_corpus_analysis_job"] == {"queue": "analytics"}


def test_enqueue_happens_after_commit(
    corpus, user, captured_enqueue, django_capture_on_commit_callbacks
):
    """CEL2 — le worker ne doit pas pouvoir lire un job non committé.

    `django_capture_on_commit_callbacks` collecte les callbacks enregistrés et ne
    les exécute qu'à la sortie du bloc : c'est précisément le point de commit.
    """
    from apps.rag_core.analysis.service import create_analysis_job

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        create_analysis_job(user=user, rag_space=corpus,
                            analysis_type=COVERAGE_SNAPSHOT, parameters={},
                            request_id="rid")
        # Toujours AVANT le commit : rien n'a encore été posté au broker.
        assert captured_enqueue == []

    assert len(callbacks) == 1          # un callback enregistré via on_commit
    assert len(captured_enqueue) == 1   # tiré seulement APRÈS le commit
    assert captured_enqueue[0]["queue"] == "analytics"


def test_reuse_does_not_enqueue_again(api_client, corpus, captured_enqueue):
    """ID3 — un résultat réutilisé ne coûte AUCUN enqueue."""
    res = _create(api_client, corpus, request_id="rid-1")
    execute_job(res.data["analysis_id"])
    captured_enqueue.clear()

    again = _create(api_client, corpus, request_id="rid-2")

    assert again.status_code == 200          # rien à faire
    assert again.data["reused"] is True
    assert captured_enqueue == []            # zéro enqueue


def test_same_request_id_enqueues_once(
    api_client, corpus, captured_enqueue, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        a = _create(api_client, corpus, request_id="rid-1")
    with django_capture_on_commit_callbacks(execute=True):
        b = _create(api_client, corpus, request_id="rid-1")

    assert a.data["analysis_id"] == b.data["analysis_id"]
    assert b.status_code == 200
    assert len(captured_enqueue) == 1        # un seul enqueue
    assert CorpusAnalysisJob.objects.count() == 1


# --------------------------------------------------------------------------- #
# API3 / API7 / API8 — consultation
# --------------------------------------------------------------------------- #

def test_detail_after_execution(api_client, corpus, captured_enqueue):
    created = _create(api_client, corpus, request_id="rid-1")
    execute_job(created.data["analysis_id"])

    res = api_client.get(f"/api/v1/analyses/{created.data['analysis_id']}/")

    assert res.status_code == 200
    d = res.data
    assert d["status"] == "succeeded"
    assert d["progress"]["percent"] == 100
    assert d["progress"]["phase"] == "completed"
    assert d["coverage"]["documents_analyzed"] == 3
    assert d["coverage"]["coverage_complete"] is False
    assert d["result"]["statistics"]["documents"]["total"] == 4
    assert d["is_stale"] is False
    assert d["started_at"] and d["finished_at"] and d["expires_at"]
    assert d["error_code"] is None


def test_result_survives_a_reload(api_client, corpus, captured_enqueue):
    """Le résultat est en base : un nouveau client le retrouve intégralement."""
    created = _create(api_client, corpus, request_id="rid-1")
    execute_job(created.data["analysis_id"])

    fresh = APIClient()
    fresh.force_authenticate(user=api_client.handler._force_user)
    res = fresh.get(f"/api/v1/analyses/{created.data['analysis_id']}/")
    assert res.status_code == 200
    assert res.data["result"] is not None


def test_detail_payload_exposes_no_internals(api_client, corpus, captured_enqueue):
    created = _create(api_client, corpus, request_id="rid-1")
    execute_job(created.data["analysis_id"])
    d = api_client.get(f"/api/v1/analyses/{created.data['analysis_id']}/").data

    for banned in ("task_id", "queue", "worker", "handler", "provider",
                   "traceback", "celery"):
        assert banned not in d
    blob = str(d).lower()
    for banned in ("traceback", "rag_chunk", "select ", "ollama", "openai"):
        assert banned not in blob


def test_is_stale_after_corpus_change(api_client, corpus, captured_enqueue):
    """API8 — le corpus change : le résultat est marqué périmé, pas supprimé."""
    created = _create(api_client, corpus, request_id="rid-1")
    aid = created.data["analysis_id"]
    execute_job(aid)
    assert api_client.get(f"/api/v1/analyses/{aid}/").data["is_stale"] is False

    _doc(corpus, name="new.txt", sha="new")

    d = api_client.get(f"/api/v1/analyses/{aid}/").data
    assert d["is_stale"] is True
    assert d["result"] is not None       # jamais modifié rétroactivement
    assert d["status"] == "succeeded"    # ni requalifié en « invalide »


def test_polling_an_active_job_does_not_recompute_the_fingerprint(
    api_client, corpus, captured_enqueue, django_assert_max_num_queries
):
    """API9 — is_stale est None tant que le job n'est pas terminal : aucun SQL
    d'empreinte n'est payé à chaque tour de polling."""
    created = _create(api_client, corpus, request_id="rid-1")
    aid = created.data["analysis_id"]

    with django_assert_max_num_queries(5):
        res = api_client.get(f"/api/v1/analyses/{aid}/")
    assert res.data["is_stale"] is None


# --------------------------------------------------------------------------- #
# API6 — annulation
# --------------------------------------------------------------------------- #

def test_cancel_pending(api_client, corpus, captured_enqueue):
    created = _create(api_client, corpus, request_id="rid-1")
    aid = created.data["analysis_id"]

    res = api_client.post(f"/api/v1/analyses/{aid}/cancel/", {}, format="json")

    assert res.status_code == 202
    assert res.data["status"] == "cancelled"
    assert res.data["error_code"] == "cancelled"
    # Message SÛR, jamais une exception brute.
    assert res.data["error_message"] == "L’analyse a été annulée."


def test_cancel_is_idempotent_on_a_terminal_job(api_client, corpus, captured_enqueue):
    created = _create(api_client, corpus, request_id="rid-1")
    aid = created.data["analysis_id"]
    execute_job(aid)

    res = api_client.post(f"/api/v1/analyses/{aid}/cancel/", {}, format="json")

    assert res.status_code == 202
    assert res.data["status"] == "succeeded"   # inchangé
    assert res.data["result"] is not None


def test_a_cancelled_job_is_never_rerun(api_client, corpus, captured_enqueue):
    created = _create(api_client, corpus, request_id="rid-1")
    aid = created.data["analysis_id"]
    api_client.post(f"/api/v1/analyses/{aid}/cancel/", {}, format="json")

    execute_job(aid)  # la tâche arrive après coup

    d = api_client.get(f"/api/v1/analyses/{aid}/").data
    assert d["status"] == "cancelled"
    assert d["result"] is None


def test_another_member_cannot_cancel(api_client, corpus, workspace, captured_enqueue):
    """CAN4 — un simple membre lecteur n'annule pas l'analyse d'un autre."""
    created = _create(api_client, corpus, request_id="rid-1")
    aid = created.data["analysis_id"]

    U = get_user_model()
    other = U.objects.create_user(username="other", password="p", email="o@e.com")
    Membership.objects.create(workspace=workspace, user=other, role=Membership.ROLE_MEMBER)
    client = APIClient()
    client.force_authenticate(user=other)

    res = client.post(f"/api/v1/analyses/{aid}/cancel/", {}, format="json")
    assert res.status_code == 403
    assert CorpusAnalysisJob.objects.get(pk=aid).status == AnalysisStatus.PENDING


# --------------------------------------------------------------------------- #
# API4 / API5 — permissions et anti-énumération
# --------------------------------------------------------------------------- #

def test_no_access_cannot_create(rag_space):
    U = get_user_model()
    stranger = U.objects.create_user(username="stranger", password="p", email="s@e.com")
    client = APIClient()
    client.force_authenticate(user=stranger)

    res = _create(client, rag_space, request_id="rid")
    assert res.status_code in (403, 404)
    assert CorpusAnalysisJob.objects.count() == 0


def test_result_is_lost_when_access_is_revoked(api_client, corpus, user, workspace,
                                               captured_enqueue):
    """API4 — le job reste lié à son espace : perdre l'accès à l'espace, c'est
    perdre l'accès au résultat. La possession de l'UUID ne suffit JAMAIS."""
    created = _create(api_client, corpus, request_id="rid-1")
    aid = created.data["analysis_id"]
    execute_job(aid)
    assert api_client.get(f"/api/v1/analyses/{aid}/").status_code == 200

    Membership.objects.filter(workspace=workspace, user=user).delete()  # accès révoqué

    res = api_client.get(f"/api/v1/analyses/{aid}/")
    assert res.status_code == 404
    blob = str(res.data).lower()
    for leak in ("documents", "chunks", "coverage", "succeeded", "fingerprint"):
        assert leak not in blob


def test_a_job_of_another_space_is_indistinguishable_from_a_missing_one(
    api_client, corpus, captured_enqueue
):
    """API5 — anti-énumération."""
    U = get_user_model()
    foreign = U.objects.create_user(username="foreign", password="p", email="f@e.com")
    fws = Workspace.objects.create(name="Foreign")
    Membership.objects.create(workspace=fws, user=foreign, role=Membership.ROLE_OWNER)
    fspace = RagSpace.objects.create(name="fs", workspace=fws)
    fjob = CorpusAnalysisJob.objects.create(
        rag_space=fspace, requested_by=foreign, analysis_type=COVERAGE_SNAPSHOT,
        parameters={}, parameters_hash="h", corpus_fingerprint="sha256:x",
        status=AnalysisStatus.SUCCEEDED, result={"secret": 1},
    )

    existing = api_client.get(f"/api/v1/analyses/{fjob.id}/")
    missing = api_client.get("/api/v1/analyses/00000000-0000-0000-0000-000000000000/")

    assert existing.status_code == missing.status_code == 404
    assert existing.data == missing.data  # strictement indistinguables


def test_cancelling_a_foreign_job_reveals_nothing(api_client, captured_enqueue):
    U = get_user_model()
    foreign = U.objects.create_user(username="f2", password="p", email="f2@e.com")
    fws = Workspace.objects.create(name="F2")
    Membership.objects.create(workspace=fws, user=foreign, role=Membership.ROLE_OWNER)
    fspace = RagSpace.objects.create(name="fs2", workspace=fws)
    fjob = CorpusAnalysisJob.objects.create(
        rag_space=fspace, requested_by=foreign, analysis_type=COVERAGE_SNAPSHOT,
        parameters={}, parameters_hash="h", corpus_fingerprint="sha256:x",
    )

    res = api_client.post(f"/api/v1/analyses/{fjob.id}/cancel/", {}, format="json")
    assert res.status_code == 404
    fjob.refresh_from_db()
    assert fjob.status == AnalysisStatus.PENDING  # intact


def test_anonymous_is_rejected(corpus):
    res = APIClient().get("/api/v1/analyses/00000000-0000-0000-0000-000000000000/")
    assert res.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# API10 — rétrocompatibilité
# --------------------------------------------------------------------------- #

def test_existing_endpoints_are_unbroken(api_client, corpus):
    assert api_client.get(f"/api/v1/rag-spaces/{corpus.id}/stats/").status_code == 200
    assert api_client.get("/api/v1/rag-spaces/").status_code == 200


def test_l3_still_declares_the_full_analysis_unavailable(api_client, corpus):
    """Phase 17 — L2 ne fournit QUE les fondations. Le chat ne doit pas laisser
    croire que l'analyse complète est disponible."""
    res = api_client.post(
        "/api/v1/chat/",
        {"rag_space_id": str(corpus.id), "question": "analyse tous les documents"},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["answer_kind"] == "corpus_mode_choice"
    assert "n’est pas encore disponible" in res.data["answer"]
    # Aucun job n'a été créé par le chat.
    assert CorpusAnalysisJob.objects.count() == 0
