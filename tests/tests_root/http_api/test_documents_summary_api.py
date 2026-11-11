"""Tests de l'API de résumé de document (Étape 1).

Couvre : accès légitime, anti-énumération (404 doc étranger), droits viewer,
refus si document non READY / sans chunk, respect du provider de l'espace,
isolation (seuls les chunks du document ciblé entrent dans le prompt), et
indisponibilité du provider LLM.

Le LLM est toujours stubé : aucun appel réseau réel (OpenAI/Ollama).
"""
import pytest

import apps.rag_core.services.summary_service as svc
from apps.rag_core.models import Document
from apps.rag_core.services.ingest_facade import ingest_plain_text

pytestmark = pytest.mark.django_db


class _FakeLLM:
    """Stub capturant les kwargs passés à generate_text."""

    def __init__(self, *, text="RÉSUMÉ STUB", provider="openai", model="gpt-stub", usage=None, tokens=42):
        self.calls = []
        self._text = text
        self._provider = provider
        self._model = model
        self._usage = usage
        self._tokens = tokens

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "text": self._text,
            "tokens": self._tokens,
            "cost": 0.0,
            "provider": self._provider,
            "model": self._model,
            "usage": self._usage,
            "raw": None,
        }


def _seed_ready_doc(rag_space, *, filename="art.txt", text="Paris est la capitale de la France."):
    ingest_plain_text(rag_space_id=rag_space.id, filename=filename, text=text)
    return Document.objects.get(rag_space=rag_space, filename=filename)


# ---------------------------------------------------------------------------
# Cas nominal
# ---------------------------------------------------------------------------
def test_summary_accessible_document_returns_200(api_client, rag_space, monkeypatch):
    fake = _FakeLLM(text="Voici un résumé.")
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {"mode": "bullets"}, format="json")

    assert res.status_code == 200
    assert res.data["ok"] is True
    assert res.data["summary"] == "Voici un résumé."
    assert res.data["scope"] == "document"
    assert res.data["mode"] == "bullets"
    assert len(fake.calls) == 1

    # Audit : une entrée RagQueryLog policy=summary
    from apps.audit.models import RagQueryLog

    log = RagQueryLog.objects.filter(rag_space=rag_space, policy="summary").first()
    assert log is not None
    assert log.llm_total_tokens == 42
    assert str(res.data["log_id"]) == str(log.id)


# ---------------------------------------------------------------------------
# Anti-énumération / isolation workspace
# ---------------------------------------------------------------------------
def test_summary_foreign_document_returns_404(api_client, foreign_setup, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(foreign_setup["rag_space"], filename="secret.txt", text="Données confidentielles.")

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.status_code == 404


def test_summary_missing_and_foreign_indistinguishable(api_client, foreign_setup, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(foreign_setup["rag_space"], filename="secret.txt")

    missing = api_client.post("/api/v1/documents/00000000-0000-0000-0000-000000000000/summary/", {}, format="json")
    foreign = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert missing.status_code == foreign.status_code == 404
    assert missing.data == foreign.data


# ---------------------------------------------------------------------------
# Droits viewer : peut résumer (lecture) mais pas supprimer (écriture)
# ---------------------------------------------------------------------------
def test_viewer_can_summarize_but_cannot_delete(viewer_setup, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Résumé viewer."))
    doc = _seed_ready_doc(viewer_setup["rag_space"])
    client = viewer_setup["client"]

    summ = client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")
    assert summ.status_code == 200
    assert summ.data["summary"] == "Résumé viewer."

    delete = client.delete(f"/api/v1/documents/{doc.id}/delete/")
    assert delete.status_code == 403
    assert Document.objects.filter(id=doc.id).exists()


# ---------------------------------------------------------------------------
# États invalides
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_status", ["PENDING", "INGESTING", "FAILED"])
def test_summary_refused_when_not_ready(api_client, rag_space, monkeypatch, bad_status):
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = Document.objects.create(rag_space=rag_space, filename="x.txt", status=bad_status)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.status_code == 409
    assert res.data["detail"] == "document_not_ready"
    assert fake.calls == []  # le LLM n'est jamais appelé


def test_summary_no_chunks_returns_clear_error(api_client, rag_space, monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = Document.objects.create(rag_space=rag_space, filename="empty.txt", status="READY")

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.status_code == 409
    assert res.data["detail"] == "document_no_content"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Respect du provider configuré sur le RagSpace
# ---------------------------------------------------------------------------
def test_summary_respects_space_provider(api_client, rag_space, monkeypatch):
    rag_space.llm_provider = "ollama"
    rag_space.llm_model = "llama3:latest"
    rag_space.save(update_fields=["llm_provider", "llm_model"])

    fake = _FakeLLM(provider="ollama", model="llama3:latest")
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.status_code == 200
    assert fake.calls[0]["provider"] == "ollama"
    assert fake.calls[0]["model"] == "llama3:latest"
    assert res.data["provider"] == "ollama"


# ---------------------------------------------------------------------------
# Isolation : seuls les chunks du document ciblé entrent dans le prompt
# ---------------------------------------------------------------------------
def test_summary_only_uses_target_document_chunks(api_client, rag_space, monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)

    target = _seed_ready_doc(rag_space, filename="cible.txt", text="Le ciel est bleu aujourd'hui.")
    _seed_ready_doc(rag_space, filename="autre.txt", text="ZZZSECRETMARKER ne doit pas apparaître.")

    res = api_client.post(f"/api/v1/documents/{target.id}/summary/", {}, format="json")

    assert res.status_code == 200
    prompt = fake.calls[0]["prompt"]
    assert "ciel" in prompt
    assert "ZZZSECRETMARKER" not in prompt


# ---------------------------------------------------------------------------
# Périmètre page (scope=page) — Étape 2
# ---------------------------------------------------------------------------
def _seed_paged_doc(rag_space):
    from apps.rag_core.models import Chunk

    doc = Document.objects.create(rag_space=rag_space, filename="multi.pdf", status="READY")
    Chunk.objects.create(rag_space=rag_space, document=doc, chunk_index=0, text="Page une ALPHACONTENU.", page_number=1)
    Chunk.objects.create(rag_space=rag_space, document=doc, chunk_index=1, text="Page deux BETACONTENU.", page_number=2)
    return doc


def test_summary_page_scope_filters_to_requested_page(api_client, rag_space, monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_paged_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {"scope": "page", "page": 2}, format="json")

    assert res.status_code == 200
    assert res.data["scope"] == "page"
    assert res.data["page"] == 2
    prompt = fake.calls[0]["prompt"]
    assert "BETACONTENU" in prompt
    assert "ALPHACONTENU" not in prompt  # la page 1 n'entre pas dans le résumé


def test_summary_page_without_chunks_returns_409(api_client, rag_space, monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_paged_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {"scope": "page", "page": 99}, format="json")

    assert res.status_code == 409
    assert res.data["detail"] == "document_no_content"
    assert fake.calls == []


def test_summary_page_scope_without_page_falls_back_to_document(api_client, rag_space, monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_paged_doc(rag_space)

    # scope=page sans numéro exploitable → repli document complet (pas d'erreur).
    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {"scope": "page"}, format="json")

    assert res.status_code == 200
    assert res.data["scope"] == "document"
    assert res.data["page"] is None
    prompt = fake.calls[0]["prompt"]
    assert "ALPHACONTENU" in prompt and "BETACONTENU" in prompt


# ---------------------------------------------------------------------------
# Cache, invalidation et coûts — Étape 4a
# ---------------------------------------------------------------------------
def test_summary_is_cached_second_call_skips_llm(api_client, rag_space, monkeypatch):
    fake = _FakeLLM(text="Résumé en cache.")
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)
    url = f"/api/v1/documents/{doc.id}/summary/"

    r1 = api_client.post(url, {"mode": "bullets"}, format="json")
    assert r1.status_code == 200
    assert r1.data["cached"] is False
    assert r1.data["generated_at"]

    r2 = api_client.post(url, {"mode": "bullets"}, format="json")
    assert r2.status_code == 200
    assert r2.data["cached"] is True
    assert r2.data["summary"] == r1.data["summary"]
    assert len(fake.calls) == 1  # le LLM n'est appelé qu'une fois

    from apps.rag_core.models import DocumentSummary

    assert DocumentSummary.objects.filter(document=doc).count() == 1


def test_summary_refresh_bypasses_cache(api_client, rag_space, monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)
    url = f"/api/v1/documents/{doc.id}/summary/"

    api_client.post(url, {"mode": "bullets"}, format="json")
    r2 = api_client.post(url, {"mode": "bullets", "refresh": True}, format="json")

    assert r2.status_code == 200
    assert r2.data["cached"] is False
    assert len(fake.calls) == 2


def test_summary_cache_invalidated_when_chunks_change(api_client, rag_space, monkeypatch):
    from apps.rag_core.models import Chunk

    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)
    url = f"/api/v1/documents/{doc.id}/summary/"

    api_client.post(url, {"mode": "bullets"}, format="json")
    assert len(fake.calls) == 1

    # Simule une réindexation : un nouveau chunk (nouvel UUID) → empreinte changée.
    Chunk.objects.create(rag_space=rag_space, document=doc, chunk_index=99, text="Nouveau contenu réindexé.")

    r2 = api_client.post(url, {"mode": "bullets"}, format="json")
    assert r2.status_code == 200
    assert r2.data["cached"] is False  # cache invalidé
    assert len(fake.calls) == 2


def test_summary_modes_and_scopes_cached_separately(api_client, rag_space, monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)
    url = f"/api/v1/documents/{doc.id}/summary/"

    api_client.post(url, {"mode": "bullets"}, format="json")
    api_client.post(url, {"mode": "short"}, format="json")
    api_client.post(url, {"mode": "bullets"}, format="json")  # rejoue bullets → cache

    from apps.rag_core.models import DocumentSummary

    assert DocumentSummary.objects.filter(document=doc).count() == 2  # bullets + short
    assert len(fake.calls) == 2  # bullets (1) + short (1), le 3e est servi du cache




def test_summary_deleted_with_document(api_client, rag_space, monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)
    api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    from apps.rag_core.models import DocumentSummary

    assert DocumentSummary.objects.filter(document=doc).count() == 1
    api_client.delete(f"/api/v1/documents/{doc.id}/delete/")
    assert DocumentSummary.objects.filter(document_id=doc.id).count() == 0  # CASCADE


# ---------------------------------------------------------------------------
# Documents longs : map-reduce + async — Étape 4b
# ---------------------------------------------------------------------------
class _C:
    """Faux chunk minimal pour tester le découpage en blocs."""

    def __init__(self, text, page=None):
        self.text = text
        self.page_number = page


def test_split_into_blocks_packs_multiple(settings):
    settings.SUMMARY_MAX_CONTEXT_CHARS = 8
    settings.SUMMARY_MAX_CHARS_PER_CHUNK = 100
    blocks, capped = svc._split_into_blocks([_C("aaaa"), _C("bbbb"), _C("cccc")])
    assert len(blocks) == 3  # chaque chunk (6 unités) dépasse le budget combiné
    assert capped is False


def test_split_into_blocks_single_when_fits(settings):
    settings.SUMMARY_MAX_CONTEXT_CHARS = 1000
    blocks, _ = svc._split_into_blocks([_C("aaaa"), _C("bbbb")])
    assert len(blocks) == 1


def _seed_long_doc(rag_space, n=3):
    from apps.rag_core.models import Chunk

    doc = Document.objects.create(rag_space=rag_space, filename="long.txt", status="READY")
    for i in range(n):
        Chunk.objects.create(rag_space=rag_space, document=doc, chunk_index=i, text=f"Bloc numero {i} contenu.")
    return doc


def test_long_document_map_reduce_inline_in_eager(api_client, rag_space, settings, monkeypatch):
    settings.SUMMARY_MAX_CONTEXT_CHARS = 1  # force 1 chunk par bloc
    settings.CELERY_TASK_ALWAYS_EAGER = True  # pas de broker → génération inline
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_long_doc(rag_space, n=3)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.status_code == 200
    assert res.data["status"] == "ready"
    assert res.data["truncated"] is False  # aucun chunk abandonné (map-reduce)
    assert len(fake.calls) == 4  # 3 map + 1 reduce


def test_long_document_async_enqueues_then_task_completes(api_client, user, rag_space, settings, monkeypatch):
    import apps.rag_core.tasks as tasks
    from apps.rag_core.models import DocumentSummary

    settings.SUMMARY_MAX_CONTEXT_CHARS = 1
    settings.CELERY_TASK_ALWAYS_EAGER = False  # simule un vrai broker
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    monkeypatch.setattr(tasks.generate_document_summary_task, "delay", lambda **kw: None)  # n'exécute pas

    doc = _seed_long_doc(rag_space, n=3)

    r1 = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")
    assert r1.status_code == 202
    assert r1.data["status"] == "pending"
    assert r1.data["summary"] == ""
    assert len(fake.calls) == 0  # rien généré tant que la tâche ne tourne pas

    row = DocumentSummary.objects.get(document=doc)
    assert row.status == "PENDING"

    # Exécute la tâche manuellement (comme le worker le ferait).
    tasks.generate_document_summary_task(summary_id=str(row.id), user_id=user.id)

    row.refresh_from_db()
    assert row.status == "READY"
    assert row.summary
    assert len(fake.calls) == 4

    # GET de polling renvoie l'état prêt.
    g = api_client.get(f"/api/v1/documents/{doc.id}/summary/")
    assert g.status_code == 200
    assert g.data["status"] == "ready"
    assert g.data["summary"]


def test_long_document_async_task_failure_marks_failed(api_client, user, rag_space, settings, monkeypatch):
    import apps.rag_core.tasks as tasks
    from celery.exceptions import MaxRetriesExceededError

    from apps.rag_core.models import DocumentSummary

    settings.SUMMARY_MAX_CONTEXT_CHARS = 1
    settings.CELERY_TASK_ALWAYS_EAGER = False
    monkeypatch.setattr(tasks.generate_document_summary_task, "delay", lambda **kw: None)

    def _boom(**kwargs):
        from apps.llm_router.router import LLMError

        raise LLMError("provider down")

    monkeypatch.setattr(svc, "generate_text", _boom)
    # Résilience (Priorité C) : une erreur provider est transitoire et déclenche un
    # retry. On simule l'épuisement des retries pour vérifier l'état final FAILED.
    monkeypatch.setattr(
        tasks.generate_document_summary_task,
        "retry",
        lambda *a, **k: (_ for _ in ()).throw(MaxRetriesExceededError()),
    )
    doc = _seed_long_doc(rag_space, n=3)

    api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")
    row = DocumentSummary.objects.get(document=doc)
    tasks.generate_document_summary_task(summary_id=str(row.id), user_id=user.id)

    row.refresh_from_db()
    assert row.status == "FAILED"
    assert row.error_message

    g = api_client.get(f"/api/v1/documents/{doc.id}/summary/")
    assert g.data["status"] == "failed"


def test_get_summary_status_none_when_absent(api_client, rag_space, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)
    g = api_client.get(f"/api/v1/documents/{doc.id}/summary/")
    assert g.status_code == 200
    assert g.data["status"] == "none"


def test_get_summary_foreign_document_returns_404(api_client, foreign_setup, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(foreign_setup["rag_space"])
    g = api_client.get(f"/api/v1/documents/{doc.id}/summary/")
    assert g.status_code == 404  # anti-énumération aussi sur le GET


# ---------------------------------------------------------------------------
# Provider indisponible
# ---------------------------------------------------------------------------
def test_summary_provider_unavailable_returns_502(api_client, rag_space, monkeypatch):
    from apps.llm_router.router import LLMError

    def _boom(**kwargs):
        raise LLMError("provider down")

    monkeypatch.setattr(svc, "generate_text", _boom)
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.status_code == 502
    assert res.data["detail"] == "summary_provider_unavailable"
