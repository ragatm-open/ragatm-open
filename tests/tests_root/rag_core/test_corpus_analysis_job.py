"""L2 — modèle, registre, pré-vol, progression, annulation, purge, handler.

Arbre legacy : modèle et services. L'API HTTP est couverte dans l'arbre racine.
"""
import importlib
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.rag_core.analysis import preflight as pf
from apps.rag_core.analysis.progress import (
    AnalysisCancelled,
    compute_percent,
    raise_if_cancel_requested,
    request_cancel,
    update_job_progress,
)
from apps.rag_core.analysis.purge import purge_expired_analysis_jobs
from apps.rag_core.analysis.registry import (
    COVERAGE_SNAPSHOT,
    AnalysisTypeDefinition,
    UnknownAnalysisType,
    get_analysis_type,
    is_known_analysis_type,
    list_analysis_types,
    register_analysis_type,
)
from apps.rag_core.analysis.runner import execute_job
from apps.rag_core.analysis.service import (
    PreflightLimitExceeded,
    canonical_parameters,
    create_analysis_job,
    is_stale,
    parameters_hash,
)
from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.corpus_analysis_job import (
    AnalysisStatus,
    CorpusAnalysisJob,
    InvalidTransition,
)
from apps.rag_core.models.document import DocumentStatus

pytestmark = pytest.mark.django_db


def _doc(space, *, status=DocumentStatus.READY, name="d.txt", sha="a"):
    return Document.objects.create(rag_space=space, filename=name, status=status, sha256=sha)


def _chunk(space, doc, *, text="abcd", page=1, idx=0):
    return Chunk.objects.create(
        rag_space=space, document=doc, chunk_index=idx, text=text, page_number=page
    )


@pytest.fixture
def corpus(rag_space):
    for i in range(3):
        d = _doc(rag_space, name=f"ok{i}.txt", sha=f"s{i}")
        _chunk(rag_space, d, idx=i)
    _doc(rag_space, name="ko.txt", status=DocumentStatus.FAILED, sha="")
    return rag_space


def _job(space, user, **kw):
    defaults = dict(
        rag_space=space, requested_by=user, analysis_type=COVERAGE_SNAPSHOT,
        parameters={}, parameters_hash="h", corpus_fingerprint="sha256:x",
    )
    defaults.update(kw)
    return CorpusAnalysisJob.objects.create(**defaults)


# --------------------------------------------------------------------------- #
# MOD1..MOD5 — modèle
# --------------------------------------------------------------------------- #

def test_defaults(rag_space, user):
    job = _job(rag_space, user)
    assert job.status == AnalysisStatus.PENDING
    assert job.phase == "queued"
    assert (job.progress_current, job.progress_percent) == (0, 0)
    assert (job.tokens_in, job.tokens_out) == (0, 0)
    assert job.cost_usd is None
    assert job.result is None and job.coverage is None
    assert job.is_terminal is False and job.is_active is True


@pytest.mark.parametrize("start,target", [
    (AnalysisStatus.PENDING, AnalysisStatus.RUNNING),
    (AnalysisStatus.PENDING, AnalysisStatus.CANCELLED),
    (AnalysisStatus.PENDING, AnalysisStatus.FAILED),
    (AnalysisStatus.RUNNING, AnalysisStatus.SUCCEEDED),
    (AnalysisStatus.RUNNING, AnalysisStatus.PARTIAL),
    (AnalysisStatus.RUNNING, AnalysisStatus.FAILED),
    (AnalysisStatus.RUNNING, AnalysisStatus.CANCELLED),
])
def test_allowed_transitions(rag_space, user, start, target):
    job = _job(rag_space, user, status=start)
    job.transition_to(target)
    assert job.status == target


@pytest.mark.parametrize("start,target", [
    (AnalysisStatus.SUCCEEDED, AnalysisStatus.RUNNING),
    (AnalysisStatus.CANCELLED, AnalysisStatus.RUNNING),
    (AnalysisStatus.FAILED, AnalysisStatus.RUNNING),
    (AnalysisStatus.FAILED, AnalysisStatus.PENDING),
    (AnalysisStatus.SUCCEEDED, AnalysisStatus.PENDING),
    (AnalysisStatus.PENDING, AnalysisStatus.SUCCEEDED),  # jamais sans passer par running
])
def test_forbidden_transitions(rag_space, user, start, target):
    job = _job(rag_space, user, status=start)
    with pytest.raises(InvalidTransition):
        job.transition_to(target)


@pytest.mark.parametrize("status,terminal", [
    (AnalysisStatus.PENDING, False), (AnalysisStatus.RUNNING, False),
    (AnalysisStatus.SUCCEEDED, True), (AnalysisStatus.PARTIAL, True),
    (AnalysisStatus.FAILED, True), (AnalysisStatus.CANCELLED, True),
])
def test_is_terminal(rag_space, user, status, terminal):
    assert _job(rag_space, user, status=status).is_terminal is terminal


def test_request_id_uniqueness(rag_space, user):
    _job(rag_space, user, request_id="rid", parameters_hash="h1")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _job(rag_space, user, request_id="rid", parameters_hash="h2")


def test_blank_request_id_is_not_unique(rag_space, user):
    """La contrainte est PARTIELLE : plusieurs jobs sans request_id coexistent."""
    _job(rag_space, user, request_id="", parameters_hash="h1")
    _job(rag_space, user, request_id="", parameters_hash="h2")
    assert CorpusAnalysisJob.objects.count() == 2


def test_only_one_active_identical_job(rag_space, user):
    """Contrainte PARTIELLE sur (espace, type, params, empreinte) x actif."""
    _job(rag_space, user, status=AnalysisStatus.PENDING)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _job(rag_space, user, status=AnalysisStatus.RUNNING)


def test_a_new_job_is_allowed_once_the_previous_is_terminal(rag_space, user):
    _job(rag_space, user, status=AnalysisStatus.FAILED)
    _job(rag_space, user, status=AnalysisStatus.CANCELLED)
    _job(rag_space, user, status=AnalysisStatus.PENDING)  # ne lève pas
    assert CorpusAnalysisJob.objects.count() == 3


# --------------------------------------------------------------------------- #
# REG1..REG4 — registre
# --------------------------------------------------------------------------- #

def test_known_type():
    d = get_analysis_type(COVERAGE_SNAPSHOT)
    assert d.key == COVERAGE_SNAPSHOT
    assert is_known_analysis_type(COVERAGE_SNAPSHOT)


def test_unknown_type_is_rejected():
    with pytest.raises(UnknownAnalysisType):
        get_analysis_type("corpus.kpi_inventory")  # pas encore implémenté
    assert not is_known_analysis_type("../../etc/passwd")
    assert not is_known_analysis_type("apps.rag_core.tasks.ingest_document_task")


def test_duplicate_registration_is_rejected():
    with pytest.raises(ValueError):
        register_analysis_type(
            AnalysisTypeDefinition(
                key=COVERAGE_SNAPSHOT, handler=lambda **kw: {}, requires_llm=False,
                requires_embeddings=False, supports_cancel=True, result_version="1",
            )
        )


def test_coverage_snapshot_declares_no_ai():
    d = get_analysis_type(COVERAGE_SNAPSHOT)
    assert d.requires_llm is False
    assert d.requires_embeddings is False
    assert d.supports_cancel is True


def test_registry_listing_never_exposes_the_callable():
    for item in list_analysis_types():
        assert "handler" not in item
        assert not any(callable(v) for v in item.values())


def test_only_implemented_types_are_registered():
    """Annoncer un type non implémenté reproduirait le problème de confiance
    que ces lots suppriment. Seuls les moteurs RÉELS sont enregistrés."""
    from apps.rag_core.analysis.registry import GLOBAL_SEARCH

    keys = {t["key"] for t in list_analysis_types()}
    assert keys == {COVERAGE_SNAPSHOT, GLOBAL_SEARCH}
    for not_yet in ("corpus.summary", "corpus.kpi_inventory",
                    "corpus.entities", "corpus.timeline", "corpus.report"):
        assert not_yet not in keys


# --------------------------------------------------------------------------- #
# PRE1..PRE5 — pré-vol
# --------------------------------------------------------------------------- #

def test_preflight_empty_space(rag_space):
    d = get_analysis_type(COVERAGE_SNAPSHOT)
    p = pf.compute_preflight(rag_space=rag_space, definition=d)
    assert p["documents_total"] == 0 and p["chunks_total"] == 0
    assert p["estimated_tokens_total"] == 0
    assert p["within_limits"] is True and p["limits_exceeded"] == []


def test_preflight_mixed_documents(corpus):
    d = get_analysis_type(COVERAGE_SNAPSHOT)
    p = pf.compute_preflight(rag_space=corpus, definition=d)
    assert p["documents_total"] == 4
    assert p["documents_ready"] == 3
    assert p["documents_failed"] == 1
    assert p["documents_without_chunks"] == 1
    assert p["chunks_total"] == 3


def test_preflight_fingerprint_matches_l0_l1(corpus):
    from apps.rag_core.services.coverage_service import compute_rag_space_statistics

    d = get_analysis_type(COVERAGE_SNAPSHOT)
    p = pf.compute_preflight(rag_space=corpus, definition=d)
    stats = compute_rag_space_statistics(corpus.id)
    assert p["corpus_fingerprint"] == stats["coverage"].scope.corpus_fingerprint


@pytest.mark.parametrize("limit_key,setting,value", [
    (pf.LIMIT_MAX_DOCUMENTS, "CORPUS_ANALYTICS_MAX_DOCUMENTS", 1),
    (pf.LIMIT_MAX_CHUNKS, "CORPUS_ANALYTICS_MAX_CHUNKS", 1),
    (pf.LIMIT_MAX_ESTIMATED_TOKENS, "CORPUS_ANALYTICS_MAX_ESTIMATED_TOKENS", 1),
])
def test_each_limit_is_enforced_independently(corpus, settings, limit_key, setting, value):
    setattr(settings, setting, value)
    d = AnalysisTypeDefinition(
        key="t", handler=lambda **kw: {}, requires_llm=True, requires_embeddings=True,
        supports_cancel=True, result_version="1", enforced_limits=(limit_key,),
    )
    p = pf.compute_preflight(rag_space=corpus, definition=d)
    assert p["within_limits"] is False
    assert [e["limit"] for e in p["limits_exceeded"]] == [limit_key]


def test_coverage_snapshot_is_not_blocked_by_an_ai_limit(corpus, settings):
    """Une limite destinée aux analyses IA ne doit pas refuser un instantané SQL."""
    settings.CORPUS_ANALYTICS_MAX_ESTIMATED_TOKENS = 1
    settings.CORPUS_ANALYTICS_MAX_DOCUMENTS = 1
    d = get_analysis_type(COVERAGE_SNAPSHOT)
    p = pf.compute_preflight(rag_space=corpus, definition=d)
    assert p["within_limits"] is True  # aucun plafond IA ne s'applique à ce type


# --------------------------------------------------------------------------- #
# Paramètres canoniques
# --------------------------------------------------------------------------- #

def test_canonical_parameters_is_order_independent():
    assert canonical_parameters({"b": 1, "a": 2}) == canonical_parameters({"a": 2, "b": 1})
    assert parameters_hash({"b": 1, "a": 2}) == parameters_hash({"a": 2, "b": 1})
    assert parameters_hash({"a": 1}) != parameters_hash({"a": 2})
    # Jamais str(dict) : sa représentation dépend de l'ordre d'insertion.
    assert canonical_parameters({"a": 1}) == '{"a":1}'


# --------------------------------------------------------------------------- #
# ID1..ID7 — création, idempotence, déduplication, réutilisation
# --------------------------------------------------------------------------- #

def _create(user, space, request_id="", params=None):
    return create_analysis_job(
        user=user, rag_space=space, analysis_type=COVERAGE_SNAPSHOT,
        parameters=params or {}, request_id=request_id,
    )


def test_same_request_id_creates_one_job(corpus, user):
    a = _create(user, corpus, request_id="rid-1")
    b = _create(user, corpus, request_id="rid-1")
    assert a.job.id == b.job.id
    assert a.created is True and b.created is False
    assert CorpusAnalysisJob.objects.count() == 1


def test_identical_active_job_is_joined(corpus, user):
    a = _create(user, corpus, request_id="rid-1")
    b = _create(user, corpus, request_id="rid-2")  # même type, params, corpus
    assert b.job.id == a.job.id
    assert b.created is False and b.joined_active is True
    assert CorpusAnalysisJob.objects.count() == 1


def test_successful_result_is_reused(corpus, user):
    a = _create(user, corpus, request_id="rid-1")
    execute_job(a.job.id)

    b = _create(user, corpus, request_id="rid-2")
    assert b.reused is True and b.created is False
    assert b.job.id == a.job.id
    assert CorpusAnalysisJob.objects.count() == 1


def test_changed_corpus_creates_a_new_job(corpus, user):
    a = _create(user, corpus, request_id="rid-1")
    execute_job(a.job.id)

    _doc(corpus, name="new.txt", sha="new")  # le corpus change -> empreinte change

    b = _create(user, corpus, request_id="rid-2")
    assert b.created is True and b.reused is False
    assert b.job.id != a.job.id
    assert b.job.corpus_fingerprint != a.job.corpus_fingerprint


def test_different_parameters_create_a_new_job(corpus, user):
    a = _create(user, corpus, request_id="rid-1", params={"x": 1})
    execute_job(a.job.id)
    b = _create(user, corpus, request_id="rid-2", params={"x": 2})
    assert b.created is True and b.job.id != a.job.id


def test_expired_result_is_not_reused(corpus, user):
    a = _create(user, corpus, request_id="rid-1")
    execute_job(a.job.id)
    CorpusAnalysisJob.objects.filter(pk=a.job.id).update(
        expires_at=timezone.now() - timedelta(days=1)
    )
    b = _create(user, corpus, request_id="rid-2")
    assert b.reused is False and b.created is True


@pytest.mark.parametrize("bad_status", [
    AnalysisStatus.FAILED, AnalysisStatus.CANCELLED, AnalysisStatus.PARTIAL,
])
def test_only_succeeded_results_are_reused(corpus, user, bad_status):
    a = _create(user, corpus, request_id="rid-1")
    CorpusAnalysisJob.objects.filter(pk=a.job.id).update(
        status=bad_status, result={"x": 1}, finished_at=timezone.now()
    )
    b = _create(user, corpus, request_id="rid-2")
    assert b.reused is False and b.created is True


def test_incompatible_result_version_is_not_reused(corpus, user):
    a = _create(user, corpus, request_id="rid-1")
    execute_job(a.job.id)
    CorpusAnalysisJob.objects.filter(pk=a.job.id).update(result_version="0")
    b = _create(user, corpus, request_id="rid-2")
    assert b.reused is False and b.created is True


def test_concurrent_creation_never_yields_two_active_jobs(corpus, user):
    """ID7 — la contrainte PARTIELLE tranche la course, pas un SELECT non verrouillé.

    On simule la course en insérant directement le job « concurrent » avant que le
    service n'insère le sien : il doit récupérer une IntegrityError et REJOINDRE le
    job existant, sans exposer l'erreur.
    """
    from apps.rag_core.analysis.preflight import compute_preflight
    from apps.rag_core.analysis.service import parameters_hash as ph

    d = get_analysis_type(COVERAGE_SNAPSHOT)
    fingerprint = compute_preflight(rag_space=corpus, definition=d)["corpus_fingerprint"]
    rival = _job(corpus, user, parameters_hash=ph({}), corpus_fingerprint=fingerprint,
                 status=AnalysisStatus.PENDING, request_id="rival")

    out = _create(user, corpus, request_id="mine")

    assert out.created is False
    assert out.joined_active is True
    assert out.job.id == rival.id
    assert CorpusAnalysisJob.objects.filter(status__in=["pending", "running"]).count() == 1


def test_unknown_type_is_rejected_at_creation(corpus, user):
    with pytest.raises(UnknownAnalysisType):
        create_analysis_job(user=user, rag_space=corpus,
                            analysis_type="corpus.kpi_inventory",
                            parameters={}, request_id="")


def test_preflight_limit_blocks_creation(corpus, user, settings, monkeypatch):
    settings.CORPUS_ANALYTICS_MAX_DOCUMENTS = 1
    d = get_analysis_type(COVERAGE_SNAPSHOT)
    monkeypatch.setattr(
        "apps.rag_core.analysis.service.get_analysis_type",
        lambda k: AnalysisTypeDefinition(
            key=d.key, handler=d.handler, requires_llm=True, requires_embeddings=True,
            supports_cancel=True, result_version="1",
            enforced_limits=(pf.LIMIT_MAX_DOCUMENTS,),
        ),
    )
    with pytest.raises(PreflightLimitExceeded):
        _create(user, corpus, request_id="rid")
    assert CorpusAnalysisJob.objects.count() == 0  # aucun job créé


# --------------------------------------------------------------------------- #
# PROG1..PROG4 — progression
# --------------------------------------------------------------------------- #

def test_percent_is_computed_server_side():
    assert compute_percent(current=0, total=0) == 0
    assert compute_percent(current=1, total=4) == 25
    assert compute_percent(current=4, total=4) == 100


def test_progress_is_monotonic(rag_space, user):
    job = _job(rag_space, user, status=AnalysisStatus.RUNNING)
    update_job_progress(job.id, phase="a", current=8, total=10)
    update_job_progress(job.id, phase="b", current=2, total=10)  # recul -> ignoré
    job.refresh_from_db()
    assert job.progress_percent == 80
    assert job.phase == "a"


@pytest.mark.parametrize("current,total", [(-1, 10), (5, -1), (11, 10)])
def test_progress_bounds(rag_space, user, current, total):
    job = _job(rag_space, user, status=AnalysisStatus.RUNNING)
    with pytest.raises(ValueError):
        update_job_progress(job.id, phase="x", current=current, total=total)


def test_terminal_job_progress_is_frozen(rag_space, user):
    job = _job(rag_space, user, status=AnalysisStatus.SUCCEEDED, progress_percent=100)
    update_job_progress(job.id, phase="x", current=0, total=10)
    job.refresh_from_db()
    assert job.progress_percent == 100
    assert job.phase == "queued"


# --------------------------------------------------------------------------- #
# CAN1..CAN5 — annulation
# --------------------------------------------------------------------------- #

def test_cancel_pending_is_immediate(rag_space, user):
    job = _job(rag_space, user, status=AnalysisStatus.PENDING)
    request_cancel(job)
    job.refresh_from_db()
    assert job.status == AnalysisStatus.CANCELLED
    assert job.cancel_requested_at is not None
    assert job.finished_at is not None


def test_cancel_running_is_cooperative(rag_space, user):
    job = _job(rag_space, user, status=AnalysisStatus.RUNNING)
    request_cancel(job)
    job.refresh_from_db()
    assert job.status == AnalysisStatus.RUNNING       # pas de kill brutal
    assert job.cancel_requested_at is not None

    with pytest.raises(AnalysisCancelled):
        raise_if_cancel_requested(job.id)             # le handler s'arrête ici


def test_cancel_terminal_is_idempotent(rag_space, user):
    job = _job(rag_space, user, status=AnalysisStatus.SUCCEEDED, result={"x": 1})
    request_cancel(job)
    job.refresh_from_db()
    assert job.status == AnalysisStatus.SUCCEEDED     # résultat jamais modifié
    assert job.result == {"x": 1}


def test_cancelled_job_is_never_rerun(rag_space, user, corpus):
    job = _job(corpus, user, status=AnalysisStatus.PENDING)
    request_cancel(job)
    out = execute_job(job.id)
    job.refresh_from_db()
    assert job.status == AnalysisStatus.CANCELLED
    assert job.result is None
    assert out["ok"] is True


def test_cancel_before_execution_stops_the_handler(corpus, user):
    """CAN2 / SNAP5 — annulation avant une phase : cancelled, aucun résultat."""
    a = _create(user, corpus, request_id="rid")
    CorpusAnalysisJob.objects.filter(pk=a.job.id).update(
        status=AnalysisStatus.PENDING, result=None, coverage=None,
        cancel_requested_at=timezone.now(),
    )
    execute_job(a.job.id)
    job = CorpusAnalysisJob.objects.get(pk=a.job.id)
    assert job.status == AnalysisStatus.CANCELLED
    assert job.result is None and job.coverage is None


# --------------------------------------------------------------------------- #
# CEL3/CEL4 — double exécution
# --------------------------------------------------------------------------- #

def test_double_execution_produces_one_treatment(corpus, user):
    a = _create(user, corpus, request_id="rid")
    first = execute_job(a.job.id)
    job = CorpusAnalysisJob.objects.get(pk=a.job.id)
    finished_first = job.finished_at

    second = execute_job(a.job.id)  # rejeu
    job.refresh_from_db()

    assert second.get("reason") == "already_terminal"
    assert job.finished_at == finished_first  # rien n'a bougé
    assert first["ok"] is True


def test_terminal_job_exits_cleanly(corpus, user):
    a = _create(user, corpus, request_id="rid")
    CorpusAnalysisJob.objects.filter(pk=a.job.id).update(status=AnalysisStatus.FAILED)
    out = execute_job(a.job.id)
    assert out == {"ok": True, "reason": "already_terminal", "status": "failed"}


# --------------------------------------------------------------------------- #
# SNAP1..SNAP4 — handler coverage_snapshot
# --------------------------------------------------------------------------- #

def test_snapshot_result_is_exact(corpus, user):
    a = _create(user, corpus, request_id="rid")
    execute_job(a.job.id)
    job = CorpusAnalysisJob.objects.get(pk=a.job.id)

    assert job.status == AnalysisStatus.SUCCEEDED
    assert job.phase == "completed"
    assert job.progress_percent == 100
    assert job.finished_at is not None and job.started_at is not None

    docs = job.result["statistics"]["documents"]
    assert docs["total"] == 4 and docs["ready"] == 3 and docs["failed"] == 1

    assert job.coverage["documents_considered"] == 4
    assert job.coverage["documents_analyzed"] == 3
    assert job.coverage["coverage_complete"] is False
    assert job.corpus_fingerprint.startswith("sha256:")


def test_snapshot_result_contains_no_sensitive_content(corpus, user):
    d = _doc(corpus, name="secret-dossier.pdf", sha="zz")
    _chunk(corpus, d, text="TEXTE_CONFIDENTIEL_DU_CHUNK", idx=9)

    a = _create(user, corpus, request_id="rid")
    execute_job(a.job.id)
    job = CorpusAnalysisJob.objects.get(pk=a.job.id)

    blob = str(job.result).lower() + str(job.coverage).lower()
    assert "texte_confidentiel_du_chunk" not in blob
    assert "secret-dossier" not in blob
    for banned in ("rag_chunk", "select", "postgres", "ollama", "openai", "celery"):
        assert banned not in blob


def test_handler_module_imports_no_ai(monkeypatch):
    """SNAP3 (structurel) — le graphe d'imports du handler ne contient AUCUN
    module de génération, d'embeddings ou de retrieval."""
    mod = importlib.import_module("apps.rag_core.analysis.handlers")
    src = open(mod.__file__).read()
    for banned in ("generate_text", "embed_texts", "query_rag", "rerank", "llm_router"):
        assert banned not in src, f"import IA dans le handler : {banned}"


class ForbiddenAIPath(AssertionError):
    pass


@pytest.fixture
def no_ai(monkeypatch):
    def boom(*a, **kw):
        raise ForbiddenAIPath("FORBIDDEN_AI_PATH")

    for mod_path, attr in [
        ("apps.rag_core.services.query.orchestrator", "query_rag"),
        ("apps.rag_core.services.query.orchestrator", "embed_texts"),
        ("apps.llm_router.embeddings", "embed_texts"),
        ("apps.llm_router.router", "generate_text"),
        ("apps.llm_router.router", "generate_text_stream"),
        ("apps.rag_core.services.rerank", "rerank_chunks_observed"),
        ("apps.rag_core.core.facade", "query_rag"),
    ]:
        try:
            m = importlib.import_module(mod_path)
        except Exception:
            continue
        if hasattr(m, attr):
            monkeypatch.setattr(m, attr, boom, raising=True)
    return boom


def test_snapshot_never_touches_ai_paths(corpus, user, no_ai):
    a = _create(user, corpus, request_id="rid")
    out = execute_job(a.job.id)  # ne lève pas
    job = CorpusAnalysisJob.objects.get(pk=a.job.id)
    assert out["ok"] is True
    assert job.status == AnalysisStatus.SUCCEEDED
    assert job.tokens_in == 0 and job.tokens_out == 0  # aucun token dépensé


def test_control_a_real_embeddings_path_would_raise(no_ai):
    """SNAP4 — CONTRÔLE NÉGATIF : sans lui, le test ci-dessus pourrait passer pour
    une mauvaise raison (patches mal posés)."""
    from apps.llm_router import embeddings

    with pytest.raises(ForbiddenAIPath):
        embeddings.embed_texts(["une question quelconque"])


# --------------------------------------------------------------------------- #
# Péremption
# --------------------------------------------------------------------------- #

def test_is_stale_is_none_while_active(corpus, user):
    a = _create(user, corpus, request_id="rid")
    assert is_stale(a.job) is None  # pas de recalcul d'empreinte en polling


def test_is_stale_detects_a_changed_corpus(corpus, user):
    a = _create(user, corpus, request_id="rid")
    execute_job(a.job.id)
    job = CorpusAnalysisJob.objects.get(pk=a.job.id)
    assert is_stale(job) is False

    _doc(corpus, name="new.txt", sha="new")
    assert is_stale(job) is True
    job.refresh_from_db()
    assert job.result is not None  # le résultat n'est PAS modifié rétroactivement


# --------------------------------------------------------------------------- #
# PURGE1..PURGE4
# --------------------------------------------------------------------------- #

def test_purge_removes_expired_terminal_jobs(rag_space, user):
    old = _job(rag_space, user, status=AnalysisStatus.SUCCEEDED, parameters_hash="h1",
               expires_at=timezone.now() - timedelta(days=1))
    assert purge_expired_analysis_jobs() == 1
    assert not CorpusAnalysisJob.objects.filter(pk=old.id).exists()


@pytest.mark.parametrize("status", [AnalysisStatus.PENDING, AnalysisStatus.RUNNING])
def test_purge_never_removes_an_active_job(rag_space, user, status):
    job = _job(rag_space, user, status=status,
               expires_at=timezone.now() - timedelta(days=1))
    assert purge_expired_analysis_jobs() == 0
    assert CorpusAnalysisJob.objects.filter(pk=job.id).exists()


def test_purge_keeps_unexpired_results(rag_space, user):
    job = _job(rag_space, user, status=AnalysisStatus.SUCCEEDED,
               expires_at=timezone.now() + timedelta(days=10))
    assert purge_expired_analysis_jobs() == 0
    assert CorpusAnalysisJob.objects.filter(pk=job.id).exists()


def test_purge_logs_no_result_content(rag_space, user, caplog):
    _job(rag_space, user, status=AnalysisStatus.SUCCEEDED,
         result={"statistics": {"secret": "NE_DOIT_PAS_APPARAITRE"}},
         expires_at=timezone.now() - timedelta(days=1))
    with caplog.at_level("INFO"):
        purge_expired_analysis_jobs()
    assert "NE_DOIT_PAS_APPARAITRE" not in caplog.text
