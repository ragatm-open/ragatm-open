"""L1 — statistiques du RagSpace : comptes, couverture, permissions, budget SQL.

Preuves incluses :
- aucun LLM / embedding / retrieval / rerank / Celery n'est touché (avec contrôle
  négatif prouvant que les patches sont bien posés) ;
- aucun document hors permission n'entre dans le dénominateur ;
- le mot « exhaustif » n'apparaît jamais quand la couverture est incomplète.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.rag_core.domain.coverage import render_coverage_markdown
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.coverage_service import compute_rag_space_statistics
from apps.workspaces.models import Membership, Workspace

pytestmark = pytest.mark.django_db


def _url(rag_space) -> str:
    return f"/api/v1/rag-spaces/{rag_space.id}/stats/"


def _doc(rag_space, *, status=DocumentStatus.READY, name="d.txt", sha=""):
    return Document.objects.create(
        rag_space=rag_space, filename=name, status=status, sha256=sha
    )


def _chunk(rag_space, doc, *, text="abcd", page=1, idx=0):
    return Chunk.objects.create(
        rag_space=rag_space, document=doc, chunk_index=idx, text=text, page_number=page
    )


# --------------------------------------------------------------------------- #
# S1 — espace vide
# --------------------------------------------------------------------------- #

def test_empty_rag_space(api_client, rag_space):
    res = api_client.get(_url(rag_space))
    assert res.status_code == 200

    assert res.data["documents"]["total"] == 0
    assert res.data["content"]["chunks_total"] == 0
    assert res.data["content"]["pages_numbered_total"] == 0
    assert res.data["content"]["characters_total"] == 0
    assert res.data["content"]["estimated_tokens_total"] == 0
    # Périmètre vide : ratios de statut à 0.0, jamais une division par zéro.
    assert res.data["ratios"]["ready"] == 0.0
    assert res.data["ratios"]["failed"] == 0.0

    cov = res.data["coverage"]
    assert cov["documents_considered"] == 0
    assert cov["coverage_level"] == "A"  # rien de textuel n'a été parcouru


# --------------------------------------------------------------------------- #
# S2 — documents prêts avec extraits
# --------------------------------------------------------------------------- #

def test_ready_documents_with_chunks(api_client, rag_space):
    d1 = _doc(rag_space, name="a.txt", sha="a")
    d2 = _doc(rag_space, name="b.txt", sha="b")
    _chunk(rag_space, d1, text="12345678", page=1, idx=0)  # 8 car.
    _chunk(rag_space, d1, text="1234", page=2, idx=1)      # 4 car.
    _chunk(rag_space, d2, text="123456", page=1, idx=0)    # 6 car.

    res = api_client.get(_url(rag_space))
    docs, content, cov = res.data["documents"], res.data["content"], res.data["coverage"]

    assert docs["total"] == 2 and docs["ready"] == 2
    assert docs["with_chunks"] == 2 and docs["without_chunks"] == 0
    assert content["chunks_total"] == 3
    # 3 pages distinctes : (d1,1), (d1,2), (d2,1) — le numéro 1 partagé compte 2 fois.
    assert content["pages_numbered_total"] == 3
    assert content["characters_total"] == 18
    assert content["estimated_tokens_total"] == 5  # ceil(18/4)

    assert cov["documents_analyzed"] == 2
    assert cov["coverage_complete"] is True
    assert cov["coverage_ratio"] == 1.0
    assert cov["coverage_level"] == "B"
    assert cov["documents_excluded"] == []
    assert res.data["scope"]["corpus_fingerprint"].startswith("sha256:")


# --------------------------------------------------------------------------- #
# S3 — document prêt SANS extrait
# --------------------------------------------------------------------------- #

def test_ready_document_without_chunks_is_not_analyzed(api_client, rag_space):
    d1 = _doc(rag_space, name="ok.txt", sha="a")
    _chunk(rag_space, d1)
    _doc(rag_space, name="vide.txt", sha="b")  # READY, zéro chunk

    res = api_client.get(_url(rag_space))
    docs, cov = res.data["documents"], res.data["coverage"]

    assert docs["ready"] == 2  # techniquement prêt…
    assert docs["without_chunks"] == 1
    # …mais PAS analysé : sans contenu textuel, il n'a rien pu être parcouru.
    assert cov["documents_analyzed"] == 1
    assert cov["coverage_complete"] is False
    assert {"reason": "no_text_content", "count": 1, "label": "aucun contenu textuel exploitable"} in cov[
        "documents_excluded"
    ]


# --------------------------------------------------------------------------- #
# S4 + TEST CRITIQUE — documents en échec
# --------------------------------------------------------------------------- #

def test_failed_documents_are_counted_and_excluded(api_client, rag_space):
    d = _doc(rag_space, name="ok.txt", sha="a")
    _chunk(rag_space, d)
    _doc(rag_space, name="ko.txt", status=DocumentStatus.FAILED, sha="")

    res = api_client.get(_url(rag_space))
    assert res.data["documents"]["failed"] == 1
    assert res.data["ratios"]["failed"] == 0.5
    assert res.data["coverage"]["coverage_complete"] is False


def test_coverage_never_claims_complete_when_documents_failed(rag_space):
    """TEST BLOQUANT — la couverture ne peut jamais se dire complète s'il reste
    un document en échec, et le rendu ne peut jamais dire « exhaustif »."""
    for i in range(3):
        d = _doc(rag_space, name=f"ok{i}.txt", sha=f"s{i}")
        _chunk(rag_space, d, text="contenu textuel", page=1)
    _doc(rag_space, name="ko.txt", status=DocumentStatus.FAILED, sha="")

    coverage = compute_rag_space_statistics(rag_space.id)["coverage"]

    assert coverage.coverage_complete is False
    assert any(e.reason == "extraction_failed" for e in coverage.documents_excluded)
    assert coverage.documents_analyzed < coverage.documents_considered
    assert "exhaustif" not in render_coverage_markdown(coverage).lower()


# --------------------------------------------------------------------------- #
# S5 / S6 — statuts mixtes, pages
# --------------------------------------------------------------------------- #

def test_mixed_statuses(api_client, rag_space):
    d = _doc(rag_space, name="r.txt", sha="r")
    _chunk(rag_space, d)
    _doc(rag_space, name="p.txt", status=DocumentStatus.PENDING, sha="p")
    _doc(rag_space, name="i.txt", status=DocumentStatus.INGESTING, sha="i")
    _doc(rag_space, name="f.txt", status=DocumentStatus.FAILED, sha="")

    res = api_client.get(_url(rag_space))
    docs, cov = res.data["documents"], res.data["coverage"]

    assert (docs["total"], docs["ready"], docs["pending"], docs["ingesting"], docs["failed"]) == (
        4, 1, 1, 1, 1,
    )
    assert cov["documents_analyzed"] == 1
    assert cov["documents_considered"] == 4
    reasons = {e["reason"]: e["count"] for e in cov["documents_excluded"]}
    assert reasons == {"extraction_failed": 1, "not_ready": 1, "processing": 1}
    assert cov["coverage_ratio"] == 0.25


def test_page_number_conventions(api_client, rag_space):
    d = _doc(rag_space, sha="a")
    _chunk(rag_space, d, page=None, idx=0)  # non paginé : NE compte PAS comme une page
    _chunk(rag_space, d, page=0, idx=1)     # 0 est un numéro de page VALIDE
    _chunk(rag_space, d, page=1, idx=2)

    res = api_client.get(_url(rag_space))
    content = res.data["content"]

    assert content["chunks_total"] == 3
    assert content["pages_numbered_total"] == 2  # pages 0 et 1 ; le NULL est ignoré
    assert content["documents_without_page_numbers"] == 0  # le doc a des pages numérotées


def test_document_with_only_unnumbered_chunks(api_client, rag_space):
    d = _doc(rag_space, sha="a")
    _chunk(rag_space, d, page=None, idx=0)

    res = api_client.get(_url(rag_space))
    assert res.data["content"]["pages_numbered_total"] == 0
    assert res.data["content"]["documents_without_page_numbers"] == 1
    # Il reste analysé : il a du contenu textuel, simplement pas de pagination.
    assert res.data["coverage"]["documents_analyzed"] == 1


# --------------------------------------------------------------------------- #
# S7 — isolation entre RagSpaces
# --------------------------------------------------------------------------- #

def test_rag_space_isolation(api_client, rag_space, workspace):
    other = RagSpace.objects.create(name="autre", workspace=workspace)
    d_other = _doc(other, name="other.txt", sha="o")
    _chunk(other, d_other, text="x" * 100)

    d_mine = _doc(rag_space, name="mine.txt", sha="m")
    _chunk(rag_space, d_mine, text="1234")

    res = api_client.get(_url(rag_space))
    assert res.data["documents"]["total"] == 1
    assert res.data["content"]["chunks_total"] == 1
    assert res.data["content"]["characters_total"] == 4  # rien de l'autre espace

    fp_mine = res.data["scope"]["corpus_fingerprint"]
    fp_other = api_client.get(_url(other)).data["scope"]["corpus_fingerprint"]
    assert fp_mine != fp_other


# --------------------------------------------------------------------------- #
# S8 / A4 — permissions et anti-énumération
# --------------------------------------------------------------------------- #

def test_user_without_access_learns_nothing(rag_space):
    U = get_user_model()
    stranger = U.objects.create_user(username="stranger", password="pw", email="s@e.com")
    _doc(rag_space, sha="a")

    client = APIClient()
    client.force_authenticate(user=stranger)
    res = client.get(_url(rag_space))

    assert res.status_code in (403, 404)
    body = str(res.data).lower()
    for leak in ("documents", "chunks", "coverage", "fingerprint", "total"):
        assert leak not in body


def test_anonymous_is_rejected(rag_space):
    res = APIClient().get(_url(rag_space))
    assert res.status_code in (401, 403)


def test_soft_deleted_space_is_not_readable(api_client, rag_space, user):
    rag_space.soft_delete(by_user=user)
    res = api_client.get(_url(rag_space))
    assert res.status_code in (403, 404)


def test_viewer_can_read_statistics(viewer_setup):
    """Lecture seule suffit : les statistiques ne sont pas une opération d'écriture."""
    rs = viewer_setup["rag_space"]
    d = _doc(rs, sha="a")
    _chunk(rs, d)

    res = viewer_setup["client"].get(_url(rs))
    assert res.status_code == 200
    assert res.data["documents"]["total"] == 1


def test_documents_of_another_workspace_never_enter_the_denominator(api_client, rag_space):
    """Le dénominateur est le périmètre visible — jamais le corpus réel."""
    U = get_user_model()
    foreign_user = U.objects.create_user(username="foreign", password="pw", email="f@e.com")
    foreign_ws = Workspace.objects.create(name="Foreign")
    Membership.objects.create(workspace=foreign_ws, user=foreign_user, role=Membership.ROLE_OWNER)
    foreign_space = RagSpace.objects.create(name="foreign", workspace=foreign_ws)
    for i in range(5):
        _doc(foreign_space, name=f"f{i}.txt", sha=f"f{i}")

    d = _doc(rag_space, sha="m")
    _chunk(rag_space, d)

    res = api_client.get(_url(rag_space))
    assert res.data["documents"]["total"] == 1
    assert res.data["coverage"]["documents_total"] == 1
    # Aucune trace des 5 documents étrangers, ni en compte ni en exclusion.
    assert res.data["coverage"]["documents_excluded"] == []


# --------------------------------------------------------------------------- #
# S9 / S10 — budget SQL et absence de N+1
# --------------------------------------------------------------------------- #

def test_statistics_service_sql_budget(rag_space, django_assert_num_queries):
    """3 requêtes : documents, extraits, empreinte. Constant, quel que soit le volume."""
    for i in range(20):
        d = _doc(rag_space, name=f"d{i}.txt", sha=f"s{i}")
        for j in range(5):
            _chunk(rag_space, d, page=j, idx=j)

    with django_assert_num_queries(3):
        compute_rag_space_statistics(rag_space.id)


def test_sql_budget_is_constant_at_scale(rag_space, django_assert_num_queries):
    """100 documents × 5 extraits : le nombre de requêtes ne bouge pas (aucun N+1)."""
    for i in range(100):
        d = _doc(rag_space, name=f"d{i}.txt", sha=f"s{i}")
        for j in range(5):
            _chunk(rag_space, d, text="abcd", page=j, idx=j)

    with django_assert_num_queries(3):
        stats = compute_rag_space_statistics(rag_space.id)

    assert stats["documents"]["total"] == 100
    assert stats["content"]["chunks_total"] == 500
    assert stats["content"]["pages_numbered_total"] == 500
    assert stats["content"]["characters_total"] == 2000
    assert stats["coverage"].documents_analyzed == 100


# --------------------------------------------------------------------------- #
# A1 / A2 / A3 — contrat API
# --------------------------------------------------------------------------- #

def test_payload_has_all_blocks(api_client, rag_space):
    res = api_client.get(_url(rag_space))
    for block in ("documents", "content", "ratios", "scope", "coverage"):
        assert block in res.data, f"bloc manquant : {block}"


def test_payload_types(api_client, rag_space):
    d = _doc(rag_space, sha="a")
    _chunk(rag_space, d, text="abcdefgh")

    data = api_client.get(_url(rag_space)).data

    for key in ("total", "ready", "failed", "with_chunks", "without_chunks"):
        assert isinstance(data["documents"][key], int)
    for key in ("chunks_total", "pages_numbered_total", "characters_total",
                "estimated_tokens_total"):
        assert isinstance(data["content"][key], int)
    for key in ("ready", "failed"):
        assert isinstance(data["ratios"][key], float)

    assert data["scope"]["corpus_fingerprint"].startswith("sha256:")
    assert len(data["scope"]["corpus_fingerprint"]) == len("sha256:") + 64
    # ISO 8601 parsable
    from datetime import datetime as _dt
    _dt.fromisoformat(data["scope"]["generated_at"])

    cov = data["coverage"]
    assert isinstance(cov["coverage_complete"], bool)
    assert isinstance(cov["coverage_ratio"], float)
    assert cov["coverage_level"] in ("A", "B")  # jamais C ni D dans ce lot


def test_legacy_fields_are_unchanged(api_client, rag_space):
    """Rétrocompatibilité : aucun champ historique retiré ni renommé."""
    d = _doc(rag_space, sha="a")
    _chunk(rag_space, d)

    data = api_client.get(_url(rag_space)).data

    for key in ("rag_space_id", "documents", "chunks_total", "indexing",
                "last_created_at", "documents_watch", "documents_watch_limit"):
        assert key in data, f"champ historique disparu : {key}"

    for key in ("total", "ready", "pending", "ingesting", "failed", "other"):
        assert key in data["documents"]
    assert set(data["indexing"]) == {"in_progress", "in_progress_count"}
    assert data["chunks_total"] == 1


def test_payload_contains_no_sensitive_content(api_client, rag_space):
    """A5 — aucun texte d'extrait, aucun nom technique dans les nouveaux blocs."""
    d = _doc(rag_space, name="secret-report.txt", sha="a")
    _chunk(rag_space, d, text="TEXTE_CONFIDENTIEL_DU_CHUNK")

    data = api_client.get(_url(rag_space)).data
    blob = str({k: data[k] for k in ("content", "ratios", "scope", "coverage")}).lower()

    assert "texte_confidentiel_du_chunk" not in blob
    assert "secret-report" not in blob  # aucun nom de fichier
    for banned in ("traceback", "exception", "ollama", "openai", "gateway",
                   "provider", "celery", "queue", "settings", "/api/"):
        assert banned not in blob, f"contenu technique exposé : {banned}"


# --------------------------------------------------------------------------- #
# Preuves no-LLM / no-embedding / no-retrieval / no-rerank / no-Celery
# --------------------------------------------------------------------------- #

class ForbiddenAIPath(AssertionError):
    pass


@pytest.fixture
def no_ai(monkeypatch):
    """Toute voie IA lève. Patché AU POINT D'USAGE réel."""

    def boom(*a, **kw):
        raise ForbiddenAIPath("FORBIDDEN_AI_PATH")

    targets = [
        ("apps.rag_core.services.query.orchestrator", "query_rag"),
        ("apps.rag_core.services.query.orchestrator", "embed_texts"),
        ("apps.llm_router.embeddings", "embed_texts"),
        ("apps.llm_router.router", "generate_text"),
        ("apps.llm_router.router", "generate_text_stream"),
        ("apps.chat.services.chat_service", "query_rag"),
        ("apps.chat.services.chat_service", "generate_text"),
        ("apps.rag_core.services.rerank", "rerank_chunks_observed"),
    ]
    for module_path, attr in targets:
        import importlib

        module = importlib.import_module(module_path)
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, boom, raising=True)

    # Aucune tâche ne doit être mise en file.
    from celery.app.task import Task

    monkeypatch.setattr(Task, "delay", boom, raising=True)
    monkeypatch.setattr(Task, "apply_async", boom, raising=True)
    return boom


def test_statistics_never_touch_ai_paths(api_client, rag_space, no_ai):
    d = _doc(rag_space, sha="a")
    _chunk(rag_space, d, text="du contenu")
    _doc(rag_space, name="ko.txt", status=DocumentStatus.FAILED, sha="")

    res = api_client.get(_url(rag_space))

    assert res.status_code == 200
    assert res.data["documents"]["total"] == 2
    assert res.data["coverage"]["coverage_complete"] is False


def test_coverage_service_never_touches_ai_paths(rag_space, no_ai):
    d = _doc(rag_space, sha="a")
    _chunk(rag_space, d)

    stats = compute_rag_space_statistics(rag_space.id)  # ne lève pas
    assert stats["coverage"].documents_analyzed == 1
    assert "exhaustif" not in render_coverage_markdown(stats["coverage"]).lower()


def test_control_a_real_ai_path_would_raise(rag_space, no_ai):
    """CONTRÔLE NÉGATIF — sans lui, les tests d'absence pourraient passer pour une
    mauvaise raison (patches mal posés). Un vrai appel IA DOIT lever."""
    from apps.llm_router import embeddings

    with pytest.raises(ForbiddenAIPath):
        embeddings.embed_texts(["une question quelconque"])
