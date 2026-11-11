"""audio déposé → transcrit (Gateway SIMULÉ au contrat) → indexé →
retrouvé → cité avec sa plage temporelle ; réindexation sans ASR ; suppression ; RBAC ;
résilience de l'orchestration.

Le Gateway et ffmpeg sont SIMULÉS : ces tests prouvent l'orchestration, la persistance,
l'indexation et les garanties RagATM, PAS l'intégration réelle (qualifiée en staging).
"""
from __future__ import annotations

import hashlib
import io
import wave

import pytest

from apps.chat.services.chat_sources import normalize_source_for_api
from apps.rag_core.models import AudioTranscript, AudioTranscriptStatus, Chunk, Document, DocumentStatus
from apps.rag_core.services.ingest.audio import gateway_asr as G
from apps.rag_core.services.ingest.audio import media as M
from apps.rag_core.services.ingest.audio import orchestrator as O
from apps.rag_core.services.ingest.reindex import reindex_existing_document
from apps.rag_core.services.query import query_rag

pytestmark = pytest.mark.django_db

MARKER = "ZEPHYR4471"


def _wav(seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x01\x00" * int(seconds * 8000))
    return buf.getvalue()


class FakeGateway:
    """Contrat simulé : un job par dépôt, résultat au 2e relevé, segments RELATIFS à la partie."""

    def __init__(self, monkeypatch, *, duration_ms=600_000, parts_text=None, polls_before_done=1):
        self.submitted, self.deleted, self.polls = [], [], {}
        self.duration_ms = duration_ms
        self.parts_text = parts_text or {}
        self.polls_before_done = polls_before_done
        self.fetch_error = None
        self._paths = {}
        monkeypatch.setattr(M, "tools_available", lambda: True)
        monkeypatch.setattr(M, "inspect", lambda path, **k: M.MediaInfo(self.duration_ms, 1, 0, "pcm_s16le", 1, 8000, "wav"))
        monkeypatch.setattr(M, "detect_silences", lambda path, **k: [])

        def transcode(src, out, *, start_ms, end_ms, timeout=None):
            data = f"FLAC[{start_ms}-{end_ms}]".encode() * 50
            with open(out, "wb") as fh:
                fh.write(data)
            return len(data)
        monkeypatch.setattr(M, "transcode_part", transcode)
        monkeypatch.setattr(G, "check_health", lambda model: G.AsrHealth("staging", True, model, "rev-0a36", True))

        def submit(path, *, size, model_logical, chunk_bytes=1 << 20):
            job = f"job-{len(self.submitted) + 1}"
            self.submitted.append((job, size))
            self._paths[job] = path
            return job
        monkeypatch.setattr(G, "submit", submit)

        def fetch(job, *, model_logical):
            if self.fetch_error:
                err, self.fetch_error = self.fetch_error, None
                raise err
            self.polls[job] = self.polls.get(job, 0) + 1
            if self.polls[job] <= self.polls_before_done:
                return None
            idx = int(job.split("-")[1]) - 1
            text = self.parts_text.get(idx, f"Partie {idx} : le code {MARKER} est confirme.")
            with open(self._paths[job], "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            segs = (G.AsrSegment(1_000, 4_000, text, "speaker_00"), G.AsrSegment(5_000, 7_500, "Suite du propos.", None))
            return G.AsrResult(job_id=job, model_logical=model_logical, text=text, media_sha256=sha, upload_bytes=0,
                               duration_ms=0, language="fr", segments=segs if text else (), diarized=True, speaker_count=1)
        monkeypatch.setattr(G, "fetch", fetch)
        monkeypatch.setattr(G, "delete", lambda job: self.deleted.append(job) or True)


@pytest.fixture
def audio_space(settings, rag_space, tmp_path):
    settings.DOCUMENT_AUDIO_INGEST_ENABLED = True
    settings.DOCUMENT_AUDIO_RAGSPACE_IDS = [str(rag_space.id)]
    settings.AUDIO_SPOOL_DIR = str(tmp_path / "spool")
    return rag_space


def _upload(client, space, data: bytes, name: str, path="/api/v1/documents/upload-audio/"):
    stream = io.BytesIO(data)
    stream.name = name
    return client.post(path, data={"rag_space_id": str(space.id), "file": stream}, format="multipart")


def _drive(doc, max_steps=50) -> str:
    """Exécute les étapes comme le ferait le worker (les on_commit ne tournent pas ici)."""
    outcome = ""
    for _ in range(max_steps):
        t = AudioTranscript.objects.filter(document_id=doc.id).order_by("-created_at").first()
        if t is None or t.status not in (AudioTranscriptStatus.PREPARING, AudioTranscriptStatus.TRANSCRIBING):
            return outcome
        outcome = O.run_step(str(t.id), str(t.chain_token))
    raise AssertionError(f"chaîne non terminée (dernier résultat : {outcome})")


def _ingest_audio(client, space, gw, name="reunion.wav", data=None) -> Document:
    res = _upload(client, space, data or _wav(), name)
    assert res.status_code == 200, res.data
    assert res.data["queued"] is True
    doc = Document.objects.get(id=res.data["document_id"])
    assert doc.status == DocumentStatus.PENDING
    assert _drive(doc) == "ready"
    r = reindex_existing_document(document_id=str(doc.id))      # = ingestion standard ré-enfilée
    assert r.status == DocumentStatus.READY
    doc.refresh_from_db()
    return doc


def _cited(space, question: str, marker: str):
    payload = query_rag(rag_space_id=space.id, question=question)
    for rank, src in enumerate(payload.get("sources") or [], 1):
        if marker.lower() in ((src.get("text") or "") + str(src.get("snippet") or "")).lower():
            return normalize_source_for_api(src, rank)
    return None


class TestDepotEtValidation:
    def test_audio_accepte_stocke_en_flux_et_marque(self, api_client, audio_space, monkeypatch):
        FakeGateway(monkeypatch)
        data = _wav()
        res = _upload(api_client, audio_space, data, "reunion.wav")
        assert res.status_code == 200 and res.data["queued"] is True
        doc = Document.objects.get(id=res.data["document_id"])
        assert doc.content_type == "audio/wav" and doc.sha256 == hashlib.sha256(data).hexdigest()
        assert doc.size_bytes == len(data) and doc.meta["audio"]["format"] == "wav"
        with doc.storage_file.open("rb") as fh:
            assert fh.read() == data
        t = AudioTranscript.objects.get(document=doc)
        assert t.status == AudioTranscriptStatus.PREPARING and t.source_sha256 == doc.sha256

    def test_chemin_audio_refuse_tout_autre_format(self, api_client, audio_space):
        res = _upload(api_client, audio_space, b"%PDF-1.7\n" + b"x" * 200, "rapport.pdf")
        assert res.status_code == 415 and res.data["detail"] == "unsupported_audio"
        assert not Document.objects.filter(rag_space=audio_space).exists()

    # en-tête RÉEL d'un m4a 3GPP (ffmpeg `-f 3gp`, enregistreurs Android).
    M4A_3GPP_HEAD = bytes.fromhex("0000001c6674797033677034000002003367703469736f6d69736f32000000086672656500002102")

    @pytest.mark.parametrize("client_mime", ["audio/mp4", "audio/x-m4a", "audio/m4a", "application/mp4", "", "video/mp4"])
    def test_m4a_3gpp_admis_quel_que_soit_le_mime_client(self, api_client, audio_space, monkeypatch, client_mime):
        """Le MIME du client n'est jamais une preuve ni un motif de refus : le format est CONSTATÉ."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        FakeGateway(monkeypatch)
        data = self.M4A_3GPP_HEAD + b"\x00" * 4096
        f = SimpleUploadedFile("1.ca-meeting intro-governance.m4a", data, content_type=client_mime or "")
        res = api_client.post("/api/v1/documents/upload-audio/", data={"rag_space_id": str(audio_space.id), "file": f},
                              format="multipart")
        assert res.status_code == 200, res.data
        doc = Document.objects.get(id=res.data["document_id"])
        assert doc.content_type == "audio/mp4" and doc.meta["audio"]["format"] == "m4a"

    def test_m4a_3gpp_parcourt_la_chaine_asr(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc = _ingest_audio(api_client, audio_space, gw, name="reunion-android.m4a", data=self.M4A_3GPP_HEAD + b"\x00" * 4096)
        assert doc.status == DocumentStatus.READY and len(gw.submitted) == 1 and gw.deleted == ["job-1"]
        assert AudioTranscript.objects.get(document=doc).status == AudioTranscriptStatus.READY

    def test_flux_aac_brut_nomme_m4a_refuse_et_diagnostique(self, api_client, audio_space, caplog):
        adts = bytes.fromhex("fff1504023fffcde02004c61766335392e33372e313030000230ab5ce90c420ead4dce6f89ae9b83")
        with caplog.at_level("WARNING", logger="rag.audio.upload"):
            res = _upload(api_client, audio_space, adts + b"\x00" * 1024, "faux.m4a")
        assert res.status_code == 415 and res.data["detail"] == "unsupported_audio"
        assert not Document.objects.filter(rag_space=audio_space).exists()
        assert "signature=adts-aac" in caplog.text and "faux" not in caplog.text

    def test_signature_contredite_refusee(self, api_client, audio_space):
        res = _upload(api_client, audio_space, b"fLaC" + b"\x00" * 100, "faux.wav")
        assert res.status_code == 415 and res.data["detail"] == "unsupported_audio"

    def test_drapeau_ferme_ou_espace_non_liste(self, api_client, audio_space, settings):
        settings.DOCUMENT_AUDIO_RAGSPACE_IDS = []
        assert _upload(api_client, audio_space, _wav(), "a.wav").data["detail"] == "audio_ingestion_disabled"
        settings.DOCUMENT_AUDIO_RAGSPACE_IDS = [str(audio_space.id)]
        settings.DOCUMENT_AUDIO_INGEST_ENABLED = False
        res = _upload(api_client, audio_space, _wav(), "a.wav")
        assert res.status_code == 400 and res.data["detail"] == "audio_ingestion_disabled"

    def test_plafond_audio_propre_et_limite_generale_inchangee(self, api_client, audio_space, settings, monkeypatch):
        FakeGateway(monkeypatch)
        data = _wav(1.0)
        settings.UPLOAD_MAX_FILE_SIZE_MB = len(data) / (1024 * 1024) / 2      # limite générale < taille
        settings.DOCUMENT_AUDIO_MAX_UPLOAD_BYTES = len(data)                  # frontière audio = taille
        assert _upload(api_client, audio_space, data, "pile.wav").status_code == 200      # == plafond : accepté
        settings.DOCUMENT_AUDIO_MAX_UPLOAD_BYTES = len(data) - 1
        res = _upload(api_client, audio_space, _wav(1.0)[:-2] + b"\x02\x00", "plus.wav")
        assert res.status_code == 413 and res.data["detail"] == "audio_file_too_large"
        # Un document NON audio de même taille reste soumis à la limite générale.
        res = _upload(api_client, audio_space, b"a" * len(data), "notes.txt", path="/api/v1/documents/upload/")
        assert res.status_code == 413 and res.data["detail"] == "file_too_large"

    def test_constante_600_mo_decimaux(self):
        from apps.rag_core.services.ingest.extraction import config
        assert config.audio_max_upload_bytes() == 600_000_000

    def test_quota_workspace_applique(self, api_client, audio_space, monkeypatch):
        from apps.workspaces.models import WorkspaceQuota

        FakeGateway(monkeypatch)
        WorkspaceQuota.objects.update_or_create(workspace=audio_space.workspace,
                                                defaults={"max_document_count": 10, "max_document_bytes": 100})
        res = _upload(api_client, audio_space, _wav(), "a.wav")
        assert res.status_code == 409
        assert not Document.objects.filter(rag_space=audio_space).exists()

    def test_utilisateur_sans_droit_refuse(self, audio_space, foreign_setup):
        res = _upload(foreign_setup["client"], audio_space, _wav(), "a.wav")
        assert res.status_code in (403, 404)

    def test_capacites_et_espace(self, api_client, audio_space):
        cap = api_client.get("/api/v1/capabilities/").data["ingest"]["audio"]
        assert cap["status"] == "available" and cap["max_upload_bytes"] == 600_000_000
        assert cap["upload_path"] == "/api/v1/documents/upload-audio/" and "mp3" in cap["extensions"]
        assert "audio" in api_client.get("/api/v1/capabilities/").data["ingest"]["formats"]


class TestTranscriptionIndexationCitation:
    def test_bout_en_bout_plage_temporelle_et_preuve_non_probante(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch, duration_ms=600_000)
        doc = _ingest_audio(api_client, audio_space, gw)
        t = AudioTranscript.objects.get(document=doc)
        assert t.status == AudioTranscriptStatus.READY and t.model_revision == "rev-0a36"
        assert t.segment_count == 2 and t.timestamp_precision == "segment"
        assert gw.deleted == ["job-1"]                           # job libéré APRÈS persistance
        chunks = list(Chunk.objects.filter(document=doc).order_by("chunk_index"))
        assert chunks and all(c.embedding is not None for c in chunks)
        meta = chunks[0].meta
        assert meta["source_type"] == "AUDIO_ASR_TRANSCRIPT" and meta["start_ms"] == 1000 and meta["end_ms"] == 7500
        assert meta["transcript_segment_ids"] == [0, 1] and meta["asr_model"] == "asr-whisper-turbo"
        src = _cited(audio_space, f"Quel est le code {MARKER} ?", MARKER)
        assert src is not None, "transcription non retrouvée"
        smeta = src.get("meta") or {}
        assert smeta.get("locator", {}).get("identifier") == "00:00:01 → 00:00:07"
        assert smeta.get("start_ms") == 1000 and smeta.get("source_type") == "AUDIO_ASR_TRANSCRIPT"
        assert src.get("document_id") == str(doc.id) or str(doc.id) in str(src)
        doc.refresh_from_db()
        assert doc.size_bytes == len(_wav()) and doc.meta["audio"]["stage"] == "indexing"

    def test_long_audio_plusieurs_parties_sequentielles_offsets(self, api_client, audio_space, monkeypatch, settings):
        settings.AUDIO_ASR_PART_MAX_SECONDS = 3600
        gw = FakeGateway(monkeypatch, duration_ms=3 * 3600 * 1000 + 600_000)     # 3 h 10 → 4 parties
        doc = _ingest_audio(api_client, audio_space, gw)
        t = AudioTranscript.objects.get(document=doc)
        assert len(t.parts) == 4 and [p["start_ms"] for p in t.parts] == [0, 3_600_000, 7_200_000, 10_800_000]
        starts = [s["start_ms"] for s in t.segments]
        assert starts == sorted(starts) and starts[2] == 3_600_000 + 1_000     # décalage de la partie 2
        assert [j for j, _ in gw.submitted] == ["job-1", "job-2", "job-3", "job-4"]
        assert sorted(gw.deleted) == ["job-1", "job-2", "job-3", "job-4"]

    def test_un_seul_job_en_vol(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch, polls_before_done=99)
        a = Document.objects.get(id=_upload(api_client, audio_space, _wav(0.3), "a.wav").data["document_id"])
        b = Document.objects.get(id=_upload(api_client, audio_space, _wav(0.4), "b.wav").data["document_id"])
        ta, tb = (AudioTranscript.objects.get(document=d) for d in (a, b))
        for t in (ta, tb):
            O.run_step(str(t.id), str(t.chain_token))           # préparation
        assert O.run_step(str(ta.id), str(ta.chain_token)) == "submitted"
        assert O.run_step(str(tb.id), str(tb.chain_token)) == "waiting_for_asr"
        assert len(gw.submitted) == 1

    def test_transcription_vide(self, api_client, audio_space, monkeypatch):
        FakeGateway(monkeypatch, parts_text={0: ""})
        doc = Document.objects.get(id=_upload(api_client, audio_space, _wav(), "silence.wav").data["document_id"])
        assert _drive(doc) == "transcript_empty"
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.FAILED and doc.meta["audio"]["error_code"] == "transcript_empty"
        assert doc.error_message.startswith("transcript_empty:")


class TestReindexDedupSuppression:
    def test_reindexation_reutilise_la_transcription(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc = _ingest_audio(api_client, audio_space, gw)
        before = Chunk.objects.filter(document=doc).count()
        r = reindex_existing_document(document_id=str(doc.id))
        assert r.status == DocumentStatus.READY and len(gw.submitted) == 1        # aucun nouvel ASR
        assert Chunk.objects.filter(document=doc).count() == before
        assert AudioTranscript.objects.filter(document=doc).count() == 1

    def test_reindexation_drapeau_ferme_reste_possible(self, api_client, audio_space, monkeypatch, settings):
        gw = FakeGateway(monkeypatch)
        doc = _ingest_audio(api_client, audio_space, gw)
        settings.DOCUMENT_AUDIO_INGEST_ENABLED = False
        assert reindex_existing_document(document_id=str(doc.id)).status == DocumentStatus.READY
        assert Chunk.objects.filter(document=doc).exists()

    def test_reimport_identique_dedoublonne(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc = _ingest_audio(api_client, audio_space, gw)
        res = _upload(api_client, audio_space, _wav(), "copie.wav")
        assert res.status_code == 200 and res.data["deduped"] is True
        assert Document.objects.filter(rag_space=audio_space).count() == 1 and len(gw.submitted) == 1
        assert str(doc.id) in str(res.data)

    def test_suppression_apres_ready(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc = _ingest_audio(api_client, audio_space, gw)
        assert api_client.delete(f"/api/v1/documents/{doc.id}/delete/").status_code == 200
        assert not Chunk.objects.filter(document_id=doc.id).exists()
        assert not AudioTranscript.objects.filter(document_id=doc.id).exists()

    def test_suppression_pendant_la_transcription_libere_le_job(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch, polls_before_done=99)
        doc = Document.objects.get(id=_upload(api_client, audio_space, _wav(), "a.wav").data["document_id"])
        t = AudioTranscript.objects.get(document=doc)
        O.run_step(str(t.id), str(t.chain_token))
        assert O.run_step(str(t.id), str(t.chain_token)) == "submitted"
        assert api_client.delete(f"/api/v1/documents/{doc.id}/delete/").status_code == 200
        assert gw.deleted == ["job-1"]
        # Étape tardive : rien n'est recréé.
        assert O.run_step(str(t.id), str(t.chain_token)) == "gone"
        assert not Chunk.objects.filter(document_id=doc.id).exists()

    def test_isolation_inter_espaces(self, api_client, audio_space, monkeypatch, foreign_setup):
        gw = FakeGateway(monkeypatch)
        doc = _ingest_audio(api_client, audio_space, gw)
        payload = query_rag(rag_space_id=foreign_setup["rag_space"].id, question=f"Quel est le code {MARKER} ?")
        assert MARKER not in str(payload.get("sources") or [])
        assert foreign_setup["client"].get(f"/api/v1/documents/{doc.id}/").status_code == 404


class TestContexteAcces:
    def test_ingestion_finale_porte_l_utilisateur_demandeur(self, api_client, audio_space, monkeypatch):
        """Régression staging (owner-scope en enforcement) : l'ingestion ré-enfilée par la chaîne
        ASR doit porter le contexte d'accès de la demande d'origine, sinon `context_missing`."""
        from apps.rag_core.services.ingest import pipeline

        FakeGateway(monkeypatch)
        doc = Document.objects.get(id=_upload(api_client, audio_space, _wav(), "a.wav").data["document_id"])
        # Le dépôt eager passe sans contexte ; on simule la tâche d'ingestion réelle (avec contexte).
        t = AudioTranscript.objects.get(document=doc)
        AudioTranscript.objects.filter(id=t.id).delete()
        uid = Document.objects.get(id=doc.id).rag_space.workspace.memberships.first().user_id
        with pytest.raises(O.AudioTranscriptionDeferred):
            reindex_existing_document(document_id=str(doc.id), access_context={"user_id": uid, "request_id": "r"})
        doc.refresh_from_db()
        assert doc.meta["audio"]["requested_by_user_id"] == uid
        seen = []
        monkeypatch.setattr(pipeline, "_enqueue_after_commit", lambda **kw: seen.append(kw))
        assert _drive(doc) == "ready"
        assert seen and seen[0]["actor_user_id"] == uid
        assert "requested_by_user_id" not in str(api_client.get(f"/api/v1/documents/{doc.id}/").data)


class TestResilience:
    def _prepared(self, api_client, space, gw):
        doc = Document.objects.get(id=_upload(api_client, space, _wav(), "a.wav").data["document_id"])
        t = AudioTranscript.objects.get(document=doc)
        O.run_step(str(t.id), str(t.chain_token))
        return doc, t

    def test_perte_reseau_au_releve_puis_reprise(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc, t = self._prepared(api_client, audio_space, gw)
        O.run_step(str(t.id), str(t.chain_token))                     # dépôt
        gw.fetch_error = G.AsrError("asr_unavailable", retryable=True)
        assert O.run_step(str(t.id), str(t.chain_token)) == "poll_retry"
        assert _drive(doc) == "ready"

    def test_saturation_gpu_attend_puis_aboutit(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc, t = self._prepared(api_client, audio_space, gw)
        real_submit, calls, delays = G.submit, {"n": 0}, []

        def saturated_twice(path, **kw):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise G.AsrError("resource_saturated", retryable=True, status=503, retry_after=30)
            return real_submit(path, **kw)
        monkeypatch.setattr(G, "submit", saturated_twice)
        monkeypatch.setattr(O, "enqueue_step", lambda tid, tok, countdown=0: delays.append(countdown))
        assert O.run_step(str(t.id), str(t.chain_token)) == "saturated_retry"
        doc.refresh_from_db()
        assert doc.meta["audio"]["stage"] == "waiting_for_asr" and doc.status == DocumentStatus.PENDING
        assert O.run_step(str(t.id), str(t.chain_token)) == "saturated_retry"
        assert O.run_step(str(t.id), str(t.chain_token)) == "submitted"
        assert _drive(doc) == "ready"

    def test_saturation_au_dela_du_budget_echoue_proprement(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc, t = self._prepared(api_client, audio_space, gw)
        monkeypatch.setattr(G, "submit", lambda *a, **k: (_ for _ in ()).throw(
            G.AsrError("resource_saturated", retryable=True, status=503, retry_after=30)))
        t.refresh_from_db()
        parts = t.parts
        parts[0]["saturated_since"] = 1.0                              # saturé depuis « toujours »
        AudioTranscript.objects.filter(id=t.id).update(parts=parts)
        O.run_step(str(t.id), str(t.chain_token))
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.FAILED and doc.meta["audio"]["error_code"] == "asr_saturated"

    def test_job_terminal_en_echec(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc, t = self._prepared(api_client, audio_space, gw)
        O.run_step(str(t.id), str(t.chain_token))
        gw.fetch_error = G.AsrError("decoding_failed", terminal=True)
        assert O.run_step(str(t.id), str(t.chain_token)) == "decoding_failed"
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.FAILED and doc.meta["audio"]["error_code"] == "undecodable_audio"
        assert gw.deleted == ["job-1"]

    def test_job_expire_delai(self, api_client, audio_space, monkeypatch, settings):
        gw = FakeGateway(monkeypatch, polls_before_done=99)
        doc, t = self._prepared(api_client, audio_space, gw)
        O.run_step(str(t.id), str(t.chain_token))
        t.refresh_from_db()
        parts = t.parts
        parts[0]["submitted_at"] = 1.0                                  # très ancien
        AudioTranscript.objects.filter(id=t.id).update(parts=parts)
        assert O.run_step(str(t.id), str(t.chain_token)) == "deadline"
        doc.refresh_from_db()
        assert doc.meta["audio"]["error_code"] == "asr_timeout"

    def test_echec_au_milieu_de_plusieurs_parties(self, api_client, audio_space, monkeypatch, settings):
        settings.AUDIO_ASR_PART_MAX_SECONDS = 3600
        gw = FakeGateway(monkeypatch, duration_ms=3 * 3600 * 1000)
        doc = Document.objects.get(id=_upload(api_client, audio_space, _wav(), "long.wav").data["document_id"])
        t = AudioTranscript.objects.get(document=doc)
        for _ in range(4):                                              # prépa + partie 1 complète
            O.run_step(str(t.id), str(t.chain_token))
        O.run_step(str(t.id), str(t.chain_token))                       # dépôt partie 2
        gw.fetch_error = G.AsrError("asr_timeout", terminal=True)
        O.run_step(str(t.id), str(t.chain_token))
        t.refresh_from_db()
        assert t.status == AudioTranscriptStatus.FAILED
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.FAILED and not Chunk.objects.filter(document=doc).exists()

    def test_redemarrage_apres_traitement_partiel(self, api_client, audio_space, monkeypatch, settings):
        settings.AUDIO_ASR_PART_MAX_SECONDS = 3600
        gw = FakeGateway(monkeypatch, duration_ms=2 * 3600 * 1000)
        doc = Document.objects.get(id=_upload(api_client, audio_space, _wav(), "long.wav").data["document_id"])
        t = AudioTranscript.objects.get(document=doc)
        for _ in range(4):
            O.run_step(str(t.id), str(t.chain_token))                   # partie 1 terminée
        # « redémarrage » : le chien de garde relance la chaîne inactive ; l'état persisté suffit.
        AudioTranscript.objects.filter(id=t.id).update(updated_at=t.created_at.replace(year=2000))
        enq = []
        monkeypatch.setattr(O, "enqueue_step", lambda tid, tok, countdown=0: enq.append(tid))
        assert O.watchdog() == 1 and enq == [str(t.id)]
        assert _drive(doc) == "ready" and len(gw.submitted) == 2        # partie 1 jamais retranscrite

    def test_depot_interrompu_conclut_sans_second_depot(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc, t = self._prepared(api_client, audio_space, gw)
        t.refresh_from_db()
        parts = t.parts
        parts[0].update(status="submitting", submitting_at=1.0)        # worker tué pendant l'envoi
        AudioTranscript.objects.filter(id=t.id).update(parts=parts)
        assert O.run_step(str(t.id), str(t.chain_token)) == "submit_interrupted"
        assert gw.submitted == []

    def test_drapeau_coupe_en_cours_annule_explicitement(self, api_client, audio_space, monkeypatch, settings):
        gw = FakeGateway(monkeypatch, polls_before_done=99)
        doc, t = self._prepared(api_client, audio_space, gw)
        O.run_step(str(t.id), str(t.chain_token))
        settings.DOCUMENT_AUDIO_INGEST_ENABLED = False
        assert O.run_step(str(t.id), str(t.chain_token)) == "disabled"
        assert gw.deleted == ["job-1"]
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.FAILED and doc.meta["audio"]["error_code"] == "audio_ingestion_disabled"

    def test_fichier_corrompu(self, api_client, audio_space, monkeypatch):
        FakeGateway(monkeypatch)

        def bad(path, **k):
            raise M.AudioMediaError("undecodable_audio")
        monkeypatch.setattr(M, "inspect", bad)
        doc = Document.objects.get(id=_upload(api_client, audio_space, _wav(), "casse.wav").data["document_id"])
        assert _drive(doc) == "undecodable_audio"
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.FAILED and "décodé" in doc.error_message

    def test_chaine_perimee_ignoree(self, api_client, audio_space, monkeypatch):
        FakeGateway(monkeypatch)
        doc = Document.objects.get(id=_upload(api_client, audio_space, _wav(), "a.wav").data["document_id"])
        t = AudioTranscript.objects.get(document=doc)
        assert O.run_step(str(t.id), "00000000-0000-0000-0000-000000000000") == "stale_chain"


class TestDisponibiliteGenerale29C:
    """DOCUMENT_AUDIO_GLOBAL_ENABLED — tout espace, droits/quotas/plafond inchangés."""

    @pytest.fixture
    def global_on(self, settings, audio_space):
        settings.DOCUMENT_AUDIO_RAGSPACE_IDS = []          # l'espace n'est PLUS listé
        settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = True
        return settings

    # --- Matrice des modes (porte de configuration) ------------------------------------
    @pytest.mark.parametrize("enabled,global_,listed,expected", [
        (False, False, False, False),
        (False, True, True, False),          # le mode général n'ouvre RIEN sans le drapeau audio
        (True, False, False, False),
        (True, False, True, True),
        (True, True, False, True),
    ])
    def test_matrice_des_modes(self, settings, rag_space, enabled, global_, listed, expected):
        from apps.rag_core.services.ingest.extraction import config
        settings.DOCUMENT_AUDIO_INGEST_ENABLED = enabled
        settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = global_
        settings.DOCUMENT_AUDIO_RAGSPACE_IDS = [str(rag_space.id)] if listed else []
        assert config.audio_admitted_for(rag_space.id) is expected

    def test_mode_general_exige_une_identite_d_espace(self, global_on):
        from apps.rag_core.services.ingest.extraction import config
        for bad in (None, "", "pas-un-uuid", "815bd3aa"):
            assert config.audio_admitted_for(bad) is False

    @pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "maybe", "enabled", "2", "*", "[]"])
    def test_valeur_non_booleenne_ne_vaut_jamais_vrai(self, settings, rag_space, raw):
        """Lecteur : seule une valeur booléenne explicite ouvre ; jamais une chaîne arbitraire."""
        from apps.rag_core.services.ingest.extraction import config
        settings.DOCUMENT_AUDIO_INGEST_ENABLED = True
        settings.DOCUMENT_AUDIO_RAGSPACE_IDS = []
        settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = raw
        assert config.audio_global_enabled() is False
        assert config.audio_admitted_for(rag_space.id) is False

    def test_environnement_absent_ferme_et_invalide_refuse_au_demarrage(self):
        """Projection : absent/vide → défaut FERMÉ ; valeur invalide → démarrage refusé."""
        from django.core.exceptions import ImproperlyConfigured
        from config.settings.from_registry import env_value
        assert env_value("DOCUMENT_AUDIO_GLOBAL_ENABLED", environ={}) is False
        assert env_value("DOCUMENT_AUDIO_GLOBAL_ENABLED", environ={"DOCUMENT_AUDIO_GLOBAL_ENABLED": ""}) is False
        assert env_value("DOCUMENT_AUDIO_GLOBAL_ENABLED", environ={"DOCUMENT_AUDIO_GLOBAL_ENABLED": "0"}) is False
        assert env_value("DOCUMENT_AUDIO_GLOBAL_ENABLED", environ={"DOCUMENT_AUDIO_GLOBAL_ENABLED": "1"}) is True
        with pytest.raises((ImproperlyConfigured, ValueError)):
            env_value("DOCUMENT_AUDIO_GLOBAL_ENABLED", environ={"DOCUMENT_AUDIO_GLOBAL_ENABLED": "maybe"})

    # --- Bout en bout HTTP ---------------------------------------------------------------
    def test_espace_hors_liste_accepte_et_cite(self, api_client, audio_space, global_on, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc = _ingest_audio(api_client, audio_space, gw)
        assert doc.status == DocumentStatus.READY
        src = _cited(audio_space, f"Quel est le code {MARKER} ?", MARKER)
        assert src is not None and (src.get("meta") or {}).get("locator", {}).get("type") == "time_range"

    def test_espace_cree_apres_activation_admis_sans_liste(self, api_client, workspace, global_on, monkeypatch):
        from apps.rag_core.models import RagSpace
        FakeGateway(monkeypatch)
        neuf = RagSpace.objects.create(name="cree-apres", workspace=workspace)
        res = _upload(api_client, neuf, _wav(), "neuf.wav")
        assert res.status_code == 200 and res.data["queued"] is True
        assert str(neuf.id) not in (global_on.DOCUMENT_AUDIO_RAGSPACE_IDS or [])

    @pytest.mark.parametrize("role,allowed", [("owner", True), ("admin", True), ("member", True), ("viewer", False)])
    def test_droits_d_import_par_role(self, rag_space, workspace, global_on, monkeypatch, role, allowed):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from apps.workspaces.models import Membership
        FakeGateway(monkeypatch)
        u = get_user_model().objects.create_user(username=f"r-{role}", password="pw")
        Membership.objects.create(workspace=workspace, user=u, role=role)
        c = APIClient(); c.force_authenticate(user=u)
        res = _upload(c, rag_space, _wav(), f"{role}.wav")
        if allowed:
            assert res.status_code == 200, res.data
        else:
            assert res.status_code == 403
            assert not Document.objects.filter(rag_space=rag_space, filename=f"{role}.wav").exists()

    def test_non_membre_refuse_par_l_api(self, audio_space, foreign_setup, global_on):
        res = _upload(foreign_setup["client"], audio_space, _wav(), "intrus.wav")
        assert res.status_code in (403, 404)
        assert not Document.objects.filter(rag_space=audio_space).exists()

    def test_quota_et_plafond_toujours_appliques(self, api_client, audio_space, global_on, monkeypatch):
        from apps.workspaces.models import WorkspaceQuota
        FakeGateway(monkeypatch)
        global_on.DOCUMENT_AUDIO_MAX_UPLOAD_BYTES = 100
        res = _upload(api_client, audio_space, _wav(), "gros.wav")
        assert res.status_code == 413 and res.data["detail"] == "audio_file_too_large"
        global_on.DOCUMENT_AUDIO_MAX_UPLOAD_BYTES = 600_000_000
        WorkspaceQuota.objects.update_or_create(workspace=audio_space.workspace,
                                                defaults={"max_document_count": 10, "max_document_bytes": 100})
        assert _upload(api_client, audio_space, _wav(), "q.wav").status_code == 409
        assert not Document.objects.filter(rag_space=audio_space).exists()

    def test_isolation_document_transcript_chunks_retrieval(self, api_client, audio_space, global_on,
                                                            monkeypatch, foreign_setup):
        gw = FakeGateway(monkeypatch)
        doc = _ingest_audio(api_client, audio_space, gw)
        other = foreign_setup
        assert other["client"].get(f"/api/v1/documents/{doc.id}/").status_code == 404
        payload = query_rag(rag_space_id=other["rag_space"].id, question=f"Quel est le code {MARKER} ?")
        assert MARKER not in str(payload.get("sources") or []) and str(doc.id) not in str(payload.get("sources") or [])
        assert not Chunk.objects.filter(rag_space=other["rag_space"], document=doc).exists()
        assert not AudioTranscript.objects.filter(document__rag_space=other["rag_space"]).exists()

    def test_preuve_numerique_reste_non_probante(self, api_client, audio_space, global_on, monkeypatch):
        from apps.rag_core.services.query import numeric_evidence as NE
        gw = FakeGateway(monkeypatch, parts_text={0: "Le budget est de 1 250 000 euros, soit 12,5 % au 3 mars 2026."})
        doc = _ingest_audio(api_client, audio_space, gw)
        chunks = list(Chunk.objects.filter(document=doc))
        assert chunks and {NE.classify({"meta": c.meta}, {})[0] for c in chunks} == {NE.ASR_TRANSCRIPT}

    def test_capacite_annonce_le_mode_general(self, api_client, audio_space, global_on, settings):
        cap = api_client.get("/api/v1/capabilities/").data["ingest"]["audio"]
        assert cap["status"] == "available" and cap["space_scoped"] is False
        settings.DOCUMENT_AUDIO_GLOBAL_ENABLED = False
        assert api_client.get("/api/v1/capabilities/").data["ingest"]["audio"]["space_scoped"] is True

    def test_espace_annonce_l_audio_dans_la_liste_des_espaces(self, api_client, audio_space, global_on):
        res = api_client.get(f"/api/v1/rag-spaces/?workspace_id={audio_space.workspace_id}")
        rows = res.data if isinstance(res.data, list) else res.data.get("results", [])
        row = next(r for r in rows if str(r["id"]) == str(audio_space.id))
        assert row["audio_upload_enabled"] is True

    def test_fermeture_du_mode_general_revient_a_la_liste(self, api_client, audio_space, global_on):
        global_on.DOCUMENT_AUDIO_GLOBAL_ENABLED = False
        res = _upload(api_client, audio_space, _wav(), "a.wav")
        assert res.status_code == 400 and res.data["detail"] == "audio_ingestion_disabled"
