"""orchestration ASR et préflight de vidange face à une
bascule d'acheminement (AI Gateway directe ↔ AI Gateway Pool). Gateway SIMULÉ.

Prouve :
- un refus du pool à la relecture n'achève jamais la transcription ; aucun second dépôt ;
- à l'épuisement du budget borné, l'échec est NOMMÉ (suivi perdu), pas « fichier illisible » ;
- un rollback de l'URL pendant le budget retrouve le job (identifiant natif Gateway) ;
- redémarrage d'un worker : la chaîne relit l'état persisté, même job ;
- préflight de vidange : PASS / BLOCKED / UNKNOWN sur les états réels, en lecture seule.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.rag_core.models import AudioTranscript, AudioTranscriptStatus, Document, DocumentStatus
from apps.rag_core.services.ingest.audio import drain as D
from apps.rag_core.services.ingest.audio import gateway_asr as G
from apps.rag_core.services.ingest.audio import orchestrator as O
from tests_root.http_api.test_audio_ingestion_29b import FakeGateway, _drive, _upload, _wav

pytestmark = pytest.mark.django_db


@pytest.fixture
def audio_space(settings, rag_space, tmp_path):
    """Même espace audio que la suite 29B (drapeau, liste, spool dans `tmp_path`)."""
    settings.DOCUMENT_AUDIO_INGEST_ENABLED = True
    settings.DOCUMENT_AUDIO_RAGSPACE_IDS = [str(rag_space.id)]
    settings.AUDIO_SPOOL_DIR = str(tmp_path / "spool")
    return rag_space


def _affinity_unknown():
    return G.AsrError("pool_job_affinity_unknown", terminal=False, retryable=True, status=404)


def _prepared(api_client, space):
    doc = Document.objects.get(id=_upload(api_client, space, _wav(), "a.wav").data["document_id"])
    t = AudioTranscript.objects.get(document=doc)
    assert O.run_step(str(t.id), str(t.chain_token)) == "prepared"
    return doc, t


class TestBasculeEnCoursDeJob:
    def test_affinite_inconnue_relance_la_lecture_sans_redeposer(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc, t = _prepared(api_client, audio_space)
        assert O.run_step(str(t.id), str(t.chain_token)) == "submitted"
        gw.fetch_error = _affinity_unknown()
        assert O.run_step(str(t.id), str(t.chain_token)) == "poll_retry"
        t.refresh_from_db()
        assert t.status == AudioTranscriptStatus.TRANSCRIBING
        assert t.parts[0]["status"] == "submitted" and t.parts[0]["last_error_code"] == "pool_job_affinity_unknown"
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.PENDING
        # « rollback » de l'URL : le job est de nouveau lisible, la chaîne aboutit avec LE MÊME job.
        assert _drive(doc) == "ready"
        assert [j for j, _ in gw.submitted] == ["job-1"]

    def test_budget_epuise_echec_nomme_suivi_perdu(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch, polls_before_done=999)
        doc, t = _prepared(api_client, audio_space)
        O.run_step(str(t.id), str(t.chain_token))
        t.refresh_from_db()
        parts = t.parts
        parts[0]["transport_errors"] = O.MAX_TRANSPORT_ERRORS
        AudioTranscript.objects.filter(id=t.id).update(parts=parts)
        gw.fetch_error = _affinity_unknown()
        assert O.run_step(str(t.id), str(t.chain_token)) == "pool_job_affinity_unknown"
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.FAILED
        assert doc.meta["audio"]["error_code"] == "asr_job_tracking_lost"
        assert len(gw.submitted) == 1                                    # jamais de second dépôt

    def test_indisponibilite_gateway_classique_inchangee(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch, polls_before_done=999)
        doc, t = _prepared(api_client, audio_space)
        O.run_step(str(t.id), str(t.chain_token))
        t.refresh_from_db()
        parts = t.parts
        parts[0]["transport_errors"] = O.MAX_TRANSPORT_ERRORS
        AudioTranscript.objects.filter(id=t.id).update(parts=parts)
        gw.fetch_error = G.AsrError("asr_unavailable", retryable=True)
        assert O.run_step(str(t.id), str(t.chain_token)) == "asr_unavailable"
        doc.refresh_from_db()
        assert doc.meta["audio"]["error_code"] == "asr_unavailable"

    def test_refus_du_pool_au_depot_echec_sans_rejeu(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc, t = _prepared(api_client, audio_space)
        calls = []

        def refused(path, **kw):
            calls.append(path)
            raise G.AsrError("pool_affinity_store_unavailable", retryable=False, status=503)
        monkeypatch.setattr(G, "submit", refused)
        assert O.run_step(str(t.id), str(t.chain_token)) == "pool_affinity_store_unavailable"
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.FAILED and doc.meta["audio"]["error_code"] == "asr_unavailable"
        assert len(calls) == 1 and gw.submitted == []

    def test_reponse_perdue_au_depot_echec_sans_second_depot(self, api_client, audio_space, monkeypatch):
        FakeGateway(monkeypatch)
        doc, t = _prepared(api_client, audio_space)
        calls = []

        def lost(path, **kw):
            calls.append(path)
            raise G.AsrError("asr_submit_outcome_unknown", retryable=False)
        monkeypatch.setattr(G, "submit", lost)
        assert O.run_step(str(t.id), str(t.chain_token)) == "asr_submit_outcome_unknown"
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.FAILED and doc.meta["audio"]["error_code"] == "asr_unavailable"
        assert len(calls) == 1

    def test_redemarrage_worker_reprend_le_meme_job(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch, polls_before_done=2)
        doc, t = _prepared(api_client, audio_space)
        O.run_step(str(t.id), str(t.chain_token))                       # dépôt
        O.run_step(str(t.id), str(t.chain_token))                       # 1re relecture
        # « redémarrage » : nouvel objet, aucune mémoire de processus ; seul l'état persisté compte.
        t2 = AudioTranscript.objects.get(id=t.id)
        assert t2.parts[0]["job_id"] == "job-1" and t2.parts[0]["status"] == "submitted"
        assert _drive(doc) == "ready"
        assert len(gw.submitted) == 1 and gw.deleted == ["job-1"]


class TestPreflightVidange:
    def _status(self):
        return D.build_report()

    def test_rien_en_vol_pass(self):
        r = self._status()
        assert r.status == D.PASS and r.as_dict()["ASR_ACTIVE_JOBS"] == 0

    def test_tentative_en_preparation_bloque(self, api_client, audio_space, monkeypatch):
        FakeGateway(monkeypatch)
        Document.objects.get(id=_upload(api_client, audio_space, _wav(), "a.wav").data["document_id"])
        r = self._status()
        assert r.status == D.BLOCKED and r.pending_jobs == 1

    def test_job_en_vol_bloque(self, api_client, audio_space, monkeypatch):
        FakeGateway(monkeypatch)
        doc, t = _prepared(api_client, audio_space)
        O.run_step(str(t.id), str(t.chain_token))
        r = self._status()
        assert r.status == D.BLOCKED and r.active_jobs == 1
        assert "ASR_DRAIN_STATUS=BLOCKED" in r.as_lines()

    def test_etat_inconnu_jamais_un_faux_go(self, api_client, audio_space, monkeypatch):
        FakeGateway(monkeypatch)
        doc, t = _prepared(api_client, audio_space)
        t.refresh_from_db()
        parts = t.parts
        parts[0]["status"] = "cancelling"                                # état non reconnu
        AudioTranscript.objects.filter(id=t.id).update(parts=parts)
        r = self._status()
        assert r.status == D.UNKNOWN and r.unknown_jobs == 1

    def test_apres_vidange_pass_et_lecture_seule(self, api_client, audio_space, monkeypatch):
        gw = FakeGateway(monkeypatch)
        doc = Document.objects.get(id=_upload(api_client, audio_space, _wav(), "a.wav").data["document_id"])
        assert _drive(doc) == "ready"
        before = list(AudioTranscript.objects.values_list("id", "status", "updated_at", "parts"))
        r = self._status()
        # Transcription prête mais document pas encore indexé : information, pas un blocage ASR.
        assert r.status == D.PASS and r.ready_awaiting_indexing == 1
        assert list(AudioTranscript.objects.values_list("id", "status", "updated_at", "parts")) == before
        assert len(gw.submitted) == 1

    def test_commande_codes_de_sortie(self, api_client, audio_space, monkeypatch, capsys):
        call_command("asr_drain_preflight")                              # PASS : retour normal
        assert "ASR_DRAIN_STATUS=PASS" in capsys.readouterr().out
        FakeGateway(monkeypatch)
        doc, t = _prepared(api_client, audio_space)
        O.run_step(str(t.id), str(t.chain_token))
        with pytest.raises(SystemExit) as e:
            call_command("asr_drain_preflight", "--json")
        assert e.value.code == 2
        assert '"ASR_DRAIN_STATUS": "BLOCKED"' in capsys.readouterr().out


class TestClassificationPure:
    def test_parties(self):
        r = D.DrainReport()
        D.classify_transcript("transcribing", [{"status": "done"}, {"status": "submitted", "job_id": "j"},
                                               {"status": "pending"}], r, transcript_id="t1")
        assert (r.active_jobs, r.pending_jobs, r.unknown_jobs) == (1, 1, 0) and r.status == D.BLOCKED

    def test_soumise_sans_identifiant_inconnue(self):
        r = D.DrainReport()
        D.classify_transcript("transcribing", [{"status": "submitted"}], r, transcript_id="t1")
        assert r.status == D.UNKNOWN

    def test_parties_illisibles(self):
        r = D.DrainReport()
        D.classify_transcript("transcribing", None, r, transcript_id="t1")
        assert r.status == D.UNKNOWN

    def test_toutes_faites_assemblage_a_venir_bloque(self):
        r = D.DrainReport()
        D.classify_transcript("transcribing", [{"status": "done"}], r, transcript_id="t1")
        assert r.status == D.BLOCKED

    def test_erreur_de_lecture_inconnue(self):
        r = D.DrainReport(error="OperationalError")
        assert r.status == D.UNKNOWN
