"""Persistance des résumés de document dans le fil de discussion.

Le défaut corrigé : le résumé était écrit comme ``DocumentSummary`` mais jamais
comme ``ChatMessage``. Il s'affichait donc dans le chat, puis disparaissait dès
que le frontend rechargeait l'historique (nouvelle question, refresh,
réouverture du fil), puisque le serveur n'avait rien à restituer.

Ces tests vérifient le CYCLE COMPLET côté serveur : création à la date de la
demande, finalisation sync et async, restitution par l'API d'historique,
idempotence garantie par contrainte, permissions, et compatibilité des messages
antérieurs à la migration.

Le LLM est toujours stubé : aucun appel réseau réel.
"""
import uuid

import pytest

import apps.rag_core.services.summary_service as svc
from apps.chat.models import ChatMessage, ChatThread
from apps.rag_core.models import Document
from apps.rag_core.models.summary import DocumentSummary
from apps.rag_core.services.ingest_facade import ingest_plain_text

pytestmark = pytest.mark.django_db


class _FakeLLM:
    def __init__(self, *, text="RÉSUMÉ STUB"):
        self.calls = []
        self._text = text

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "text": self._text, "tokens": 42, "cost": 0.0,
            "provider": "openai", "model": "gpt-stub", "usage": None, "raw": None,
        }


def _seed_ready_doc(rag_space, *, filename="art.txt", text="Paris est la capitale de la France."):
    ingest_plain_text(rag_space_id=rag_space.id, filename=filename, text=text)
    return Document.objects.get(rag_space=rag_space, filename=filename)


def _thread(user, rag_space, title="Fil"):
    return ChatThread.objects.create(user=user, rag_space=rag_space, title=title)


def _post(client, doc, *, thread=None, request_id=None, **extra):
    body = {"mode": "bullets", **extra}
    if thread is not None:
        body["thread_id"] = str(thread.id)
    if request_id is not None:
        body["request_id"] = request_id
    return client.post(f"/api/v1/documents/{doc.id}/summary/", body, format="json")


def _assistant(thread):
    return ChatMessage.objects.filter(
        thread=thread, role="assistant", kind=ChatMessage.KIND_DOCUMENT_SUMMARY
    ).first()


# ---------------------------------------------------------------------------
# Persistance — chemin synchrone
# ---------------------------------------------------------------------------
def test_summary_with_thread_persists_assistant_message(api_client, user, rag_space, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Voici le résumé."))
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    res = _post(api_client, doc, thread=th, request_id="rq-1")

    assert res.status_code == 200
    msg = _assistant(th)
    assert msg is not None
    assert msg.status == ChatMessage.STATUS_COMPLETED
    assert msg.content == "Voici le résumé."
    assert str(msg.document_id) == str(doc.id)
    assert msg.summary_id is not None
    assert msg.thread_id == th.id
    assert msg.request_id == "rq-1"
    # La réponse doit permettre au frontend de réconcilier par identité.
    assert str(res.data["message_id"]) == str(msg.id)
    assert res.data["request_id"] == "rq-1"


def test_summary_persists_user_message_because_ui_shows_one(api_client, user, rag_space, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    _post(api_client, doc, thread=th, request_id="rq-1")

    um = ChatMessage.objects.filter(thread=th, role="user").first()
    assert um is not None
    # Libellé écrit par le SERVEUR, à partir du document réellement résumé.
    assert doc.filename in um.content


def test_summary_message_links_the_right_summary_row(api_client, user, rag_space, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    _post(api_client, doc, thread=th, request_id="rq-1")

    row = DocumentSummary.objects.get(document=doc)
    assert str(_assistant(th).summary_id) == str(row.id)


def test_summary_without_thread_persists_nothing(api_client, rag_space, monkeypatch):
    """Parcours historique (modale Documents) : inchangé, aucun message créé."""
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.status_code == 200
    assert res.data["summary"]
    assert ChatMessage.objects.count() == 0
    assert res.data["message_id"] is None


def test_thread_id_without_request_id_is_refused(api_client, user, rag_space, monkeypatch):
    """Sans request_id, chaque retry réseau créerait un second message."""
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    res = _post(api_client, doc, thread=th)

    assert res.status_code == 400
    assert res.data["detail"] == "request_id_required"
    assert ChatMessage.objects.count() == 0


# ---------------------------------------------------------------------------
# Restitution par l'historique
# ---------------------------------------------------------------------------
def test_history_restores_summary_with_all_fields(api_client, user, rag_space, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Résumé restitué."))
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)
    _post(api_client, doc, thread=th, request_id="rq-1")

    hist = api_client.get(f"/api/v1/chat/threads/{th.id}/messages/")

    assert hist.status_code == 200
    assistant = [m for m in hist.data if m["role"] == "assistant"]
    assert len(assistant) == 1
    m = assistant[0]
    assert m["content"] == "Résumé restitué."
    assert m["kind"] == "document_summary"
    assert m["status"] == "completed"
    assert str(m["document_id"]) == str(doc.id)
    assert m["summary_id"]
    assert m["request_id"] == "rq-1"


def test_history_keeps_summary_after_a_new_question(api_client, user, rag_space, monkeypatch):
    """Scénario A : c'est exactement le symptôme rapporté par l'utilisateur."""
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Résumé persistant."))
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)
    _post(api_client, doc, thread=th, request_id="rq-1")

    # Une question ultérieure s'ajoute au fil sans rien effacer.
    ChatMessage.objects.create(thread=th, role="user", content="Et ensuite ?", request_id="rq-2")
    ChatMessage.objects.create(thread=th, role="assistant", content="Réponse.", request_id="rq-2")

    hist = api_client.get(f"/api/v1/chat/threads/{th.id}/messages/")
    contents = [m["content"] for m in hist.data]
    assert "Résumé persistant." in contents
    # Ordre chronologique : le résumé reste AVANT la question suivante.
    assert contents.index("Résumé persistant.") < contents.index("Réponse.")


def test_history_order_is_stable_across_reads(api_client, user, rag_space, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)
    _post(api_client, doc, thread=th, request_id="rq-1")

    first = [str(m["id"]) for m in api_client.get(f"/api/v1/chat/threads/{th.id}/messages/").data]
    for _ in range(3):
        again = [str(m["id"]) for m in api_client.get(f"/api/v1/chat/threads/{th.id}/messages/").data]
        assert again == first
    # La demande précède la réponse.
    assert len(first) == 2


def test_legacy_messages_without_new_fields_stay_readable(api_client, user, rag_space):
    """Compatibilité : les messages antérieurs à la migration 0007."""
    th = _thread(user, rag_space)
    ChatMessage.objects.create(thread=th, role="user", content="Ancienne question")
    ChatMessage.objects.create(thread=th, role="assistant", content="Ancienne réponse")

    hist = api_client.get(f"/api/v1/chat/threads/{th.id}/messages/")

    assert hist.status_code == 200
    assert [m["content"] for m in hist.data] == ["Ancienne question", "Ancienne réponse"]
    for m in hist.data:
        assert m["kind"] == ""
        assert m["status"] == ""
        assert m["document_id"] is None
        assert m["summary_id"] is None


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------
def test_replay_same_request_id_returns_same_message(api_client, user, rag_space, monkeypatch):
    fake = _FakeLLM(text="Résumé unique.")
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    first = _post(api_client, doc, thread=th, request_id="rq-1")
    second = _post(api_client, doc, thread=th, request_id="rq-1")

    assert first.status_code == second.status_code == 200
    assert str(first.data["message_id"]) == str(second.data["message_id"])
    assert ChatMessage.objects.filter(thread=th, role="assistant").count() == 1
    assert ChatMessage.objects.filter(thread=th, role="user").count() == 1
    # Rejeu strict : aucune seconde génération LLM.
    assert len(fake.calls) == 1


def test_refresh_overrides_strict_replay_and_rewrites_the_same_message(
    api_client, user, rag_space, monkeypatch
):
    """`refresh=true` est une demande explicite : elle prime sur le rejeu."""
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Première version."))
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)
    _post(api_client, doc, thread=th, request_id="rq-1")

    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Version régénérée."))
    res = _post(api_client, doc, thread=th, request_id="rq-1", refresh=True)

    assert res.status_code == 200
    assert ChatMessage.objects.filter(thread=th, role="assistant").count() == 1
    msg = _assistant(th)
    assert msg.content == "Version régénérée."
    assert msg.status == ChatMessage.STATUS_COMPLETED


def test_distinct_request_ids_create_distinct_messages(api_client, user, rag_space, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    _post(api_client, doc, thread=th, request_id="rq-1")
    _post(api_client, doc, thread=th, request_id="rq-2")

    assert ChatMessage.objects.filter(thread=th, role="assistant").count() == 2


def test_database_constraint_blocks_duplicate_summary_message(user, rag_space):
    """Preuve que l'idempotence tient en BASE, pas seulement dans le code applicatif.

    Un check-then-act applicatif laisse passer deux insertions concurrentes ;
    la contrainte partielle, elle, en rejette une inconditionnellement.
    """
    from django.db import IntegrityError, transaction

    th = _thread(user, rag_space)
    ChatMessage.objects.create(
        thread=th, role="assistant", content="x", request_id="rq-dup",
        kind=ChatMessage.KIND_DOCUMENT_SUMMARY, status=ChatMessage.STATUS_PENDING,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ChatMessage.objects.create(
                thread=th, role="assistant", content="y", request_id="rq-dup",
                kind=ChatMessage.KIND_DOCUMENT_SUMMARY, status=ChatMessage.STATUS_PENDING,
            )


def test_constraint_does_not_apply_to_plain_chat_messages(user, rag_space):
    """La contrainte est PARTIELLE : le chat standard garde son comportement."""
    th = _thread(user, rag_space)
    ChatMessage.objects.create(thread=th, role="assistant", content="a", request_id="rq-plain")
    ChatMessage.objects.create(thread=th, role="assistant", content="b", request_id="rq-plain")
    assert ChatMessage.objects.filter(thread=th, request_id="rq-plain").count() == 2


def test_concurrent_creation_recovers_without_500(user, rag_space, monkeypatch):
    """Course perdue : IntegrityError récupérée, message du gagnant relu."""
    from apps.chat.services import summary_messages as sm

    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)
    winner, created = sm.get_or_create_summary_exchange(
        thread=th, request_id="rq-race", document=doc, user_label="Résume"
    )
    assert created is True

    # Simule le concurrent : le SELECT initial ne voit rien, l'INSERT échoue.
    original = sm.ChatMessage.objects.filter

    class _BlindFirst:
        """Rend le premier `.first()` aveugle, comme dans une vraie course."""

        def __init__(self):
            self.calls = 0

        def __call__(self, *a, **kw):
            qs = original(*a, **kw)
            self.calls += 1
            if self.calls == 1:
                return qs.none()
            return qs

    monkeypatch.setattr(sm.ChatMessage.objects, "filter", _BlindFirst())
    loser, created2 = sm.get_or_create_summary_exchange(
        thread=th, request_id="rq-race", document=doc, user_label="Résume"
    )

    assert created2 is False
    assert loser.id == winner.id
    monkeypatch.undo()
    assert ChatMessage.objects.filter(thread=th, role="assistant").count() == 1


# ---------------------------------------------------------------------------
# Chemin asynchrone (Celery) et reprises
# ---------------------------------------------------------------------------
def test_async_message_is_pending_then_completed_by_the_worker(
    api_client, user, rag_space, settings, monkeypatch
):
    """Le message existe DÈS la demande, et c'est le worker qui le termine."""
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Résumé long."))
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    # Force la bascule async sans broker : on capture l'enqueue.
    enqueued = {}
    settings.SUMMARY_ASYNC_ENABLED = True
    settings.CELERY_TASK_ALWAYS_EAGER = False
    monkeypatch.setattr(svc, "_should_run_async", lambda long, heavy=False: True)

    import apps.rag_core.tasks as tasks

    monkeypatch.setattr(
        tasks.generate_document_summary_task,
        "delay",
        lambda **kw: enqueued.update(kw),
    )

    res = _post(api_client, doc, thread=th, request_id="rq-async")

    assert res.status_code == 202
    msg = _assistant(th)
    assert msg.status == ChatMessage.STATUS_PENDING
    assert msg.summary_id is not None  # rattaché AVANT la fin du job
    created_at_before = msg.created_at

    # Le worker termine plus tard : le MÊME message passe à completed.
    row = DocumentSummary.objects.get(id=enqueued["summary_id"])
    svc.run_summary_generation_for_row(row, user_id=user.id)

    msg.refresh_from_db()
    assert msg.status == ChatMessage.STATUS_COMPLETED
    assert msg.content == "Résumé long."
    # Position chronologique conservée : date de DEMANDE, pas de finalisation.
    assert msg.created_at == created_at_before
    assert ChatMessage.objects.filter(thread=th, role="assistant").count() == 1


def test_worker_retry_does_not_create_a_second_message(user, rag_space, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Résumé."))
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    from apps.chat.services import summary_messages as sm

    msg, _ = sm.get_or_create_summary_exchange(
        thread=th, request_id="rq-retry", document=doc, user_label="Résume"
    )
    row = DocumentSummary.objects.create(
        rag_space=rag_space, document=doc, variant_key="bullets:document::fr",
        mode="bullets", scope="document", language="fr", status="PENDING",
        summary="", source_fingerprint="fp",
    )
    sm.attach_summary(msg, row.id)

    # Deux exécutions successives du même job (retry / double livraison).
    svc.run_summary_generation_for_row(row, user_id=user.id)
    svc.run_summary_generation_for_row(row, user_id=user.id)

    assert ChatMessage.objects.filter(thread=th, role="assistant").count() == 1
    msg.refresh_from_db()
    assert msg.status == ChatMessage.STATUS_COMPLETED


def test_fanout_completes_every_thread_waiting_on_a_shared_summary(
    user, other_user, rag_space, monkeypatch
):
    """Un DocumentSummary est un CACHE partagé : plusieurs fils peuvent l'attendre.

    C'est la raison pour laquelle le lien est porté par le message et non par le
    résumé : un ``result_message_id`` unique n'en servirait qu'un seul.
    """
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Résumé partagé."))
    doc = _seed_ready_doc(rag_space)
    from apps.chat.services import summary_messages as sm

    th1 = _thread(user, rag_space, title="A")
    th2 = _thread(user, rag_space, title="B")
    row = DocumentSummary.objects.create(
        rag_space=rag_space, document=doc, variant_key="bullets:document::fr",
        mode="bullets", scope="document", language="fr", status="PENDING",
        summary="", source_fingerprint="fp",
    )
    for th, rq in ((th1, "rq-a"), (th2, "rq-b")):
        m, _ = sm.get_or_create_summary_exchange(
            thread=th, request_id=rq, document=doc, user_label="Résume"
        )
        sm.attach_summary(m, row.id)

    svc.run_summary_generation_for_row(row, user_id=user.id)

    for th in (th1, th2):
        m = _assistant(th)
        assert m.status == ChatMessage.STATUS_COMPLETED
        assert m.content == "Résumé partagé."


def test_reaper_marks_abandoned_summary_messages_as_failed(user, rag_space):
    """Un job perdu doit produire un échec explicite, jamais un « en cours » éternel."""
    from datetime import timedelta

    from django.utils import timezone

    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)
    from apps.chat.services import summary_messages as sm

    msg, _ = sm.get_or_create_summary_exchange(
        thread=th, request_id="rq-stale", document=doc, user_label="Résume"
    )
    row = DocumentSummary.objects.create(
        rag_space=rag_space, document=doc, variant_key="bullets:document::fr",
        mode="bullets", scope="document", language="fr", status="PENDING",
        summary="", source_fingerprint="fp",
    )
    sm.attach_summary(msg, row.id)
    DocumentSummary.objects.filter(id=row.id).update(
        updated_at=timezone.now() - timedelta(seconds=99999)
    )

    assert svc.reap_stale_summaries(timeout_s=60) == 1

    msg.refresh_from_db()
    assert msg.status == ChatMessage.STATUS_FAILED


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------
def test_generation_failure_marks_message_failed_never_completed(
    api_client, user, rag_space, monkeypatch
):
    def _boom(**kwargs):
        raise svc.LLMError("provider down")

    monkeypatch.setattr(svc, "generate_text", _boom)
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    res = _post(api_client, doc, thread=th, request_id="rq-fail")

    assert res.status_code == 502
    msg = _assistant(th)
    assert msg.status == ChatMessage.STATUS_FAILED
    assert msg.status != ChatMessage.STATUS_COMPLETED


def test_retry_after_failure_reuses_the_same_message(api_client, user, rag_space, monkeypatch):
    """Retry après timeout : un seul message, pas de résumé jumeau."""
    def _boom(**kwargs):
        raise svc.LLMError("provider down")

    monkeypatch.setattr(svc, "generate_text", _boom)
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)
    _post(api_client, doc, thread=th, request_id="rq-same")
    assert _assistant(th).status == ChatMessage.STATUS_FAILED

    # Le provider revient ; le client relance AVEC LE MÊME request_id.
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Enfin résumé."))
    res = _post(api_client, doc, thread=th, request_id="rq-same")

    assert res.status_code == 200
    assert ChatMessage.objects.filter(thread=th, role="assistant").count() == 1
    msg = _assistant(th)
    assert msg.status == ChatMessage.STATUS_COMPLETED
    assert msg.content == "Enfin résumé."


def test_message_survives_deletion_of_its_summary_row(api_client, user, rag_space, monkeypatch):
    """Le contenu est un instantané autonome : lien logique, pas FK dure."""
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Résumé conservé."))
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)
    _post(api_client, doc, thread=th, request_id="rq-1")

    DocumentSummary.objects.all().delete()

    hist = api_client.get(f"/api/v1/chat/threads/{th.id}/messages/")
    assert hist.status_code == 200
    assert "Résumé conservé." in [m["content"] for m in hist.data]


def test_message_survives_deletion_of_its_document(api_client, user, rag_space, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Résumé orphelin."))
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)
    _post(api_client, doc, thread=th, request_id="rq-1")

    Document.objects.filter(id=doc.id).delete()

    hist = api_client.get(f"/api/v1/chat/threads/{th.id}/messages/")
    assert hist.status_code == 200
    assert "Résumé orphelin." in [m["content"] for m in hist.data]


# ---------------------------------------------------------------------------
# Permissions / IDOR
# ---------------------------------------------------------------------------
def test_foreign_thread_id_is_refused_and_writes_nothing(
    api_client, rag_space, foreign_setup, monkeypatch
):
    """IDOR : un thread_id deviné ne doit jamais permettre d'écrire chez autrui."""
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)
    victim_thread = ChatThread.objects.create(
        user=foreign_setup["user"], rag_space=foreign_setup["rag_space"], title="Privé"
    )

    res = _post(api_client, doc, thread=victim_thread, request_id="rq-idor")

    assert res.status_code == 404
    assert ChatMessage.objects.filter(thread=victim_thread).count() == 0
    assert ChatMessage.objects.count() == 0


def test_unknown_thread_and_foreign_thread_are_indistinguishable(
    api_client, rag_space, foreign_setup, monkeypatch
):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)
    victim = ChatThread.objects.create(
        user=foreign_setup["user"], rag_space=foreign_setup["rag_space"], title="Privé"
    )

    unknown = api_client.post(
        f"/api/v1/documents/{doc.id}/summary/",
        {"thread_id": str(uuid.uuid4()), "request_id": "rq-x"},
        format="json",
    )
    foreign = _post(api_client, doc, thread=victim, request_id="rq-y")

    assert unknown.status_code == foreign.status_code == 404
    assert unknown.data == foreign.data


def test_thread_from_another_rag_space_is_refused(api_client, user, rag_space, workspace, monkeypatch):
    """Isolation d'espace : un fil ne reçoit pas le résumé d'un autre RagSpace."""
    from apps.rag_core.models import RagSpace

    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)
    other_space = RagSpace.objects.create(name="autre", workspace=workspace)
    th_other = ChatThread.objects.create(user=user, rag_space=other_space, title="Ailleurs")

    res = _post(api_client, doc, thread=th_other, request_id="rq-cross")

    assert res.status_code == 404
    assert ChatMessage.objects.count() == 0


def test_history_of_a_foreign_thread_is_not_readable(api_client, foreign_setup):
    victim = ChatThread.objects.create(
        user=foreign_setup["user"], rag_space=foreign_setup["rag_space"], title="Privé"
    )
    ChatMessage.objects.create(
        thread=victim, role="assistant", content="Résumé confidentiel",
        kind=ChatMessage.KIND_DOCUMENT_SUMMARY, status=ChatMessage.STATUS_COMPLETED,
    )

    res = api_client.get(f"/api/v1/chat/threads/{victim.id}/messages/")

    assert res.status_code == 404


def test_viewer_can_persist_a_summary_in_their_own_thread(viewer_setup, monkeypatch):
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text="Résumé viewer."))
    doc = _seed_ready_doc(viewer_setup["rag_space"])
    th = ChatThread.objects.create(
        user=viewer_setup["user"], rag_space=viewer_setup["rag_space"], title="V"
    )

    res = _post(viewer_setup["client"], doc, thread=th, request_id="rq-v")

    assert res.status_code == 200
    assert _assistant(th).status == ChatMessage.STATUS_COMPLETED


# ---------------------------------------------------------------------------
# Observabilité
# ---------------------------------------------------------------------------
def test_logs_never_contain_the_summary_text(api_client, user, rag_space, monkeypatch, caplog):
    secret = "TENEUR CONFIDENTIELLE DU DOCUMENT"
    monkeypatch.setattr(svc, "generate_text", _FakeLLM(text=secret))
    doc = _seed_ready_doc(rag_space)
    th = _thread(user, rag_space)

    with caplog.at_level("INFO"):
        _post(api_client, doc, thread=th, request_id="rq-log")

    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert "document_summary_chat_message" in emitted
    assert secret not in emitted
