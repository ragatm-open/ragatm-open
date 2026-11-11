"""Tests contractuels — PR 2 RagATM_Core (façade interne).

Vérifie que la façade ``apps.rag_core.core`` :
- délègue fidèlement aux services existants (parité avec query_rag / ingest) ;
- ne génère JAMAIS de réponse (retrieve = récupération seule) ;
- expose des DTOs stables ;
- préserve l'isolation par rag_space et la distinction statut technique/métier.

La façade n'est branchée nulle part (ni endpoint, ni chat_service) : c'est l'objet
même de cette PR (surface Python uniquement).
"""
import uuid

import pytest

from apps.rag_core import core


@pytest.mark.django_db
def test_health_reports_dim_768_and_providers():
    res = core.health()
    assert res.ok is True
    assert res.embedding_dim == 768
    assert "ai_gateway" in res.providers
    assert "ollama" in res.providers
    # as_dict() est le contrat sérialisable (PR 3).
    assert res.as_dict()["embedding_dim"] == 768


@pytest.mark.django_db
def test_ingest_text_facade_delegates_and_marks_ready(rag_space):
    from apps.rag_core.models.document import DocumentStatus

    res = core.ingest_text(
        rag_space_id=rag_space.id,
        filename="doc.txt",
        text="L'architecture repose sur PostgreSQL et pgvector.",
    )
    assert res.status == DocumentStatus.READY
    assert res.chunks_created > 0
    assert res.deduped is False


@pytest.mark.django_db
def test_ingest_upload_facade_handles_txt(rag_space):
    from apps.rag_core.models.document import DocumentStatus

    res = core.ingest_upload(
        rag_space_id=rag_space.id,
        filename="note.txt",
        content_type="text/plain",
        file_bytes=b"Le moteur documentaire indexe des chunks dans pgvector.",
    )
    assert res.status == DocumentStatus.READY
    assert res.chunks_created > 0


@pytest.mark.django_db
def test_document_status_distinguishes_technical_and_business(rag_space):
    from apps.rag_core.models.document import DocumentStatus

    ing = core.ingest_text(
        rag_space_id=rag_space.id,
        filename="doc.txt",
        text="Contenu indexable de référence.",
    )
    st = core.document_status(rag_space_id=rag_space.id, document_id=ing.document_id)

    assert st.found is True
    assert st.status == DocumentStatus.READY      # technique
    assert st.document_status == "active"          # métier (champ PR 1)
    assert st.status != st.document_status
    assert st.chunks_count > 0


@pytest.mark.django_db
def test_document_status_not_found_and_isolated(rag_space, workspace):
    # ID inexistant => found=False.
    st = core.document_status(rag_space_id=rag_space.id, document_id=uuid.uuid4())
    assert st.found is False

    # Isolation : un document d'un espace n'est pas visible depuis un autre espace.
    from apps.rag_core.models import RagSpace

    other = RagSpace.objects.create(name="space-b", workspace=workspace)
    ing = core.ingest_text(
        rag_space_id=rag_space.id, filename="d.txt", text="texte isolé"
    )
    st_cross = core.document_status(rag_space_id=other.id, document_id=ing.document_id)
    assert st_cross.found is False


@pytest.mark.django_db
def test_retrieve_matches_query_rag_directly(rag_space):
    """Parité : core.retrieve délègue à query_rag sans déformer le résultat."""
    from apps.rag_core.services.query.orchestrator import query_rag

    core.ingest_text(
        rag_space_id=rag_space.id,
        filename="archi.txt",
        text="L'architecture du système repose sur PostgreSQL et pgvector.",
    )
    question = "Quelle est l'architecture du système ?"

    # user=None => aucun log créé (pas d'effet de bord), comparaison déterministe.
    direct = query_rag(rag_space_id=rag_space.id, question=question, user=None)
    via = core.retrieve(rag_space_id=rag_space.id, question=question, user=None)

    assert via.status == direct["status"]
    assert via.sources == (direct.get("sources") or [])
    assert via.prompt == direct.get("prompt")
    assert via.answer_kind == direct.get("answer_kind")
    # is_answerable cohérent avec le statut.
    assert via.is_answerable == (via.status == "ok")
    # raw conserve la charge utile FONCTIONNELLE (forward-compat). La trace
    # d'observabilité P1-A0 (`retrieval_trace`) diffère LÉGITIMEMENT par appel
    # (entrypoint `core_api` vs `internal`, durées par leg) et est donc exclue de la
    # comparaison stricte — sa présence n'altère aucun champ fonctionnel.
    volatile = {"retrieval_trace"}
    assert {k: v for k, v in via.raw.items() if k not in volatile} == {
        k: v for k, v in direct.items() if k not in volatile
    }
    # Le noyau reste convergent : mêmes legs/outcome/degraded, seul l'entrypoint diffère.
    assert via.raw["retrieval_degraded"] == direct["retrieval_degraded"]
    via_trace, direct_trace = via.raw["retrieval_trace"], direct["retrieval_trace"]
    assert via_trace["entrypoint"] == "core_api"
    assert direct_trace["entrypoint"] == "internal"
    assert via_trace["outcome"] == direct_trace["outcome"]
    assert {leg["name"]: leg["status"] for leg in via_trace["legs"]} == {
        leg["name"]: leg["status"] for leg in direct_trace["legs"]
    }


@pytest.mark.django_db
def test_retrieve_never_generates(rag_space, monkeypatch):
    """retrieve ne doit déclencher AUCUNE génération LLM."""
    import apps.llm_router.router as router

    def _boom(*a, **k):  # pragma: no cover - ne doit jamais être appelé
        raise AssertionError("generate_text must not be called by retrieve()")

    monkeypatch.setattr(router, "generate_text", _boom, raising=False)

    core.ingest_text(
        rag_space_id=rag_space.id, filename="d.txt", text="texte de contexte"
    )
    res = core.retrieve(rag_space_id=rag_space.id, question="contexte ?", user=None)
    # Pas d'exception => aucune génération tentée.
    assert res.status in ("ok", "no_answer")
