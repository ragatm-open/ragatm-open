"""tests unitaires SANS base : formats, découpe, client ASR (contrat
simulé), assemblage horodaté, unités, preuve numérique ASR.

Les simulations du Gateway reproduisent le CONTRAT (relevé sur le client un autre client interne et
l'OpenAPI du Gateway) : elles ne prouvent pas l'intégration réelle, qualifiée en staging.
"""
from __future__ import annotations

import io
import json
import shutil
import struct
import subprocess
import wave
from decimal import Decimal

import pytest

from apps.rag_core.services.ingest.audio import formats as F
from apps.rag_core.services.ingest.audio import gateway_asr as G
from apps.rag_core.services.ingest.audio import media as M
from apps.rag_core.services.ingest.audio import transcript as T
from apps.rag_core.services.query import numeric_evidence as NE

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe absents (poste de dev)")


def _wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


HEADS = {
    "wav": _wav(0.1)[:64],
    "flac": b"fLaC\x00\x00\x00\x22" + b"\x00" * 40,
    "ogg": b"OggS\x00\x02" + b"\x00" * 40,
    "webm": b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm" + b"\x00" * 30,
    "m4a": b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + b"\x00" * 30,
    "mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x64" + b"\x00" * 30,
}


# --------------------------------------------------------------------------- formats
class TestFormats:
    @pytest.mark.parametrize("fmt", sorted(HEADS))
    def test_extension_et_signature_concordantes_admises(self, fmt):
        assert F.sniff_audio(HEADS[fmt], f"reunion.{fmt}") == fmt

    def test_mp3_sans_id3_par_synchro_de_trame(self):
        assert F.sniff_audio(b"\xff\xfb\x90\x64" + b"\x00" * 60, "a.mp3") == "mp3"

    @pytest.mark.parametrize("name,head", [
        ("a.wav", HEADS["flac"]),          # nom et contenu se contredisent
        ("a.mp3", b"%PDF-1.7\n" + b"\x00" * 40),
        ("a.webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 10 + b"matroska"),  # Matroska ≠ WebM
        ("a.m4a", b"\x00\x00\x00\x20ftypqt  " + b"\x00" * 30),          # marque QuickTime
        ("a.opus", HEADS["ogg"]),          # .opus nu hors V1
        ("a.mp4", HEADS["m4a"]),           # vidéo hors périmètre
        ("note.txt", HEADS["wav"]),        # signature audio sous un nom non audio
        ('{"ragatm_audio_transcript":1}.mp3', b'{"asr_model":"x","ragatm_audio_transcript":1}'),
    ])
    def test_refus(self, name, head):
        assert F.sniff_audio(head, name) is None

    def test_mimes_imposes_par_le_format(self):
        assert F.AUDIO_MIMES["m4a"] == "audio/mp4" and F.AUDIO_MIMES["mp3"] == "audio/mpeg"
        assert set(F.AUDIO_MIMES) == set(F.AUDIO_SUFFIXES.values())


# --------------------------------------------------------------------------- découpe
class TestPlan:
    def test_audio_court_une_seule_partie_sans_decoupe(self):
        plan = M.plan_parts(3_000_000, part_max_seconds=5400)
        assert [(p.start_ms, p.end_ms) for p in plan] == [(0, 3_000_000)]

    def test_coupe_sur_le_plus_long_silence_de_la_fenetre(self):
        # 3 h ; silences candidats à 85 et 88 min (le second plus long) → coupe à ~88 min.
        sil = [M.Silence(5100.0, 5101.0), M.Silence(5280.0, 5283.0), M.Silence(9000.0, 9000.6)]
        plan = M.plan_parts(10_800_000, part_max_seconds=5400, silences=sil)
        assert plan[0].cut == "silence" and plan[0].end_ms == 5_281_500
        # contiguïté, ordre, pas de recouvrement, couverture totale
        assert plan[0].start_ms == 0 and plan[-1].end_ms == 10_800_000
        for a, b in zip(plan, plan[1:]):
            assert a.end_ms == b.start_ms and a.start_ms < a.end_ms
        assert all(p.end_ms - p.start_ms <= 5_400_000 for p in plan)

    def test_long_silence_debordant_la_limite_sert_de_coupe(self):
        # Constat staging 2 h 10 : parole à 88:30 puis silence jusqu'à 92:00 — son milieu (90:15)
        # dépasse la limite, mais le silence chevauche la fenêtre : la coupe y tombe.
        sil = [M.Silence(5315.0, 5520.0)]
        plan = M.plan_parts(7_800_000, part_max_seconds=5400, silences=sil)
        assert plan[0].cut == "silence" and 5_315_000 < plan[0].end_ms < 5_400_000

    def test_sans_silence_coupe_franche_a_la_limite(self):
        plan = M.plan_parts(12_000_000, part_max_seconds=5400, silences=[])
        assert [p.cut for p in plan] == ["hard", "hard", "end"]
        assert plan[0].end_ms == 5_400_000

    def test_parties_respectent_les_limites_gateway(self):
        from apps.rag_core.services.ingest.extraction import config
        assert config.audio_part_max_seconds() <= config.GATEWAY_ASR_MAX_DURATION_SECONDS - 300
        assert config.audio_part_max_bytes() <= config.GATEWAY_ASR_MAX_UPLOAD_BYTES - (12 << 20)

    def test_parse_silences(self):
        err = (b"[silencedetect @ 0x1] silence_start: 12.5\n"
               b"[silencedetect @ 0x1] silence_end: 14.25 | silence_duration: 1.75\n"
               b"[silencedetect @ 0x1] silence_start: 40\n")
        assert [(s.start, s.end) for s in M.parse_silences(err)] == [(12.5, 14.25)]


class TestMediaProcess:
    def test_chemin_relatif_refuse(self):
        with pytest.raises(M.AudioMediaError) as e:
            M.inspect("relatif.mp3")
        assert e.value.code == "audio_internal_error"

    def test_inspect_rejette_video_et_absence_audio(self, monkeypatch, tmp_path):
        def fake(streams):
            out = json.dumps({"streams": streams, "format": {"duration": "12.0", "format_name": "x"}}).encode()
            return lambda *a, **k: subprocess.CompletedProcess(a, 0, out, b"")
        monkeypatch.setattr(subprocess, "run", fake([{"codec_type": "video"}, {"codec_type": "audio"}]))
        with pytest.raises(M.AudioMediaError) as e:
            M.inspect(str(tmp_path / "a.webm"))
        assert e.value.code == "audio_contains_video"
        monkeypatch.setattr(subprocess, "run", fake([{"codec_type": "video", "disposition": {"attached_pic": 1}},
                                                     {"codec_type": "audio", "codec_name": "mp3", "channels": 2}]))
        info = M.inspect(str(tmp_path / "a.mp3"))
        assert info.duration_ms == 12000 and info.codec == "mp3"
        monkeypatch.setattr(subprocess, "run", fake([{"codec_type": "subtitle"}]))
        with pytest.raises(M.AudioMediaError) as e:
            M.inspect(str(tmp_path / "a.mp3"))
        assert e.value.code == "no_audio_track"

    def test_commandes_sans_shell_et_arguments_fixes(self, monkeypatch, tmp_path):
        seen = {}

        def fake(args, **kw):
            seen["args"], seen["kw"] = args, kw
            (tmp_path / "p.flac.partial").write_bytes(b"fLaC")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        monkeypatch.setattr(subprocess, "run", fake)
        M.transcode_part(str(tmp_path / "src.wav"), str(tmp_path / "p.flac"), start_ms=60_000, end_ms=120_000)
        assert isinstance(seen["args"], list) and "shell" not in seen["kw"] and seen["kw"]["timeout"] > 0
        assert seen["args"][seen["args"].index("-ar") + 1] == "16000" and "-nostdin" in seen["args"]
        assert seen["args"][seen["args"].index("-map") + 1] == "0:a:0"

    def test_outil_absent_code_dedie(self, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise FileNotFoundError
        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(M.AudioMediaError) as e:
            M.inspect(str(tmp_path / "a.mp3"))
        assert e.value.code == "audio_dependency_unavailable"

    @needs_ffmpeg
    def test_ffmpeg_reel_wav_vers_flac(self, tmp_path):
        src = tmp_path / "s.wav"
        src.write_bytes(_wav(3.0, rate=44100))
        info = M.inspect(str(src))
        assert 2900 <= info.duration_ms <= 3100
        size = M.transcode_part(str(src), str(tmp_path / "p.flac"), start_ms=1000, end_ms=2500)
        assert size > 0 and (tmp_path / "p.flac").read_bytes()[:4] == b"fLaC"
        assert 1400 <= M.inspect(str(tmp_path / "p.flac")).duration_ms <= 1600


# --------------------------------------------------------------------------- client ASR
class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


SHA = "a" * 64


def _result(job="j1", **over):
    body = {"transcription_id": job, "job_id": job, "model_logical": "asr-whisper-turbo", "text": "Bonjour.",
            "media": {"sha256": SHA, "upload_bytes": 10, "duration_ms": 5000}, "language": "fr",
            "segments": [{"start_ms": 0, "end_ms": 1200, "text": "Bonjour.", "speaker": "speaker_00"}],
            "diarized": True, "speaker_count": 1}
    body.update(over)
    return body


class TestGatewayClient:
    @pytest.fixture(autouse=True)
    def _url(self, settings):
        settings.AI_GATEWAY_BASE_URL = "http://gw.test:8200"

    def test_depot_multipart_contrat_client_tiers(self, monkeypatch, tmp_path):
        part = tmp_path / "part.flac"
        part.write_bytes(b"fLaC" + b"\x01" * 3000)
        captured = {}

        def fake(method, url, **kw):
            captured.update(method=method, url=url, headers=kw["headers"], length=len(kw["data"]),
                            body=b"".join(kw["data"]), timeout=kw["timeout"])
            return _Resp(202, {"job_id": "job-42", "status": "queued"})
        monkeypatch.setattr(G.requests, "request", fake)
        job = G.submit(str(part), size=part.stat().st_size, model_logical="asr-whisper-turbo")
        assert job == "job-42" and captured["method"] == "POST"
        assert captured["url"] == "http://gw.test:8200/v1/audio/transcriptions"
        body = captured["body"]
        assert captured["length"] == len(body)                       # Content-Length exact
        assert body.index(b'name="request"') < body.index(b'name="file"')  # ordre imposé
        assert b'filename="audio"' in body and b"part.flac" not in body    # nom constant
        req = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0])
        assert req == {"model": "asr-whisper-turbo", "language": "auto", "timestamps": "word",
                       "diarize": True, "mode": "async"}

    def test_fichier_modifie_pendant_envoi(self, monkeypatch, tmp_path):
        part = tmp_path / "p.flac"
        part.write_bytes(b"x" * 100)
        monkeypatch.setattr(G.requests, "request", lambda m, u, **kw: (b"".join(kw["data"]), _Resp(202, {"job_id": "j"}))[1])
        with pytest.raises(G.AsrError) as e:
            G.submit(str(part), size=50, model_logical="m")
        assert e.value.code == "audio_part_changed"

    @pytest.mark.parametrize("status,code,retryable", [(503, "resource_saturated", True), (400, "invalid_request", False),
                                                       (413, "audio_too_large", False)])
    def test_depot_refuse(self, monkeypatch, tmp_path, status, code, retryable):
        part = tmp_path / "p.flac"
        part.write_bytes(b"x" * 10)
        monkeypatch.setattr(G.requests, "request", lambda *a, **k: _Resp(status, {"error": {"code": code}}))
        with pytest.raises(G.AsrError) as e:
            G.submit(str(part), size=10, model_logical="m")
        assert e.value.code == code and e.value.retryable is retryable

    def test_saturation_gpu_forme_reelle_du_gateway(self, monkeypatch, tmp_path):
        """Corps EXACT relevé en staging le 06/11/2026 (FastAPI : `detail` objet)."""
        part = tmp_path / "p.flac"
        part.write_bytes(b"x" * 10)
        body = {"detail": {"status": "error", "provider": "ai_gateway", "message": "budget GPU insuffisant pour la transcription",
                           "code": "resource_saturated", "retryable": True}}
        monkeypatch.setattr(G.requests, "request", lambda *a, **k: _Resp(503, body, {"Retry-After": "30"}))
        with pytest.raises(G.AsrError) as e:
            G.submit(str(part), size=10, model_logical="m")
        assert e.value.code == "resource_saturated" and e.value.retryable and e.value.retry_after == 30
        body["detail"]["retryable"] = False                           # le Gateway dit « ne rejoue pas »
        with pytest.raises(G.AsrError) as e:
            G.submit(str(part), size=10, model_logical="m")
        assert not e.value.retryable
        monkeypatch.setattr(G.requests, "request", lambda *a, **k: _Resp(503, {"detail": {"code": "resource_saturated"}},
                                                                       {"Retry-After": "99999"}))
        with pytest.raises(G.AsrError) as e:
            G.submit(str(part), size=10, model_logical="m")
        assert e.value.retry_after == 300                            # borné
        monkeypatch.setattr(G.requests, "request", lambda *a, **k: _Resp(503, None))
        with pytest.raises(G.AsrError) as e:
            G.submit(str(part), size=10, model_logical="m")
        assert e.value.code == "http_503" and not e.value.retryable  # 503 muet : jamais rejoué à l'aveugle

    def test_releve_en_cours_puis_resultat(self, monkeypatch):
        calls = []
        monkeypatch.setattr(G.requests, "request",
                            lambda m, u, **kw: calls.append(kw.get("params")) or _Resp(200, {"status": "running"}))
        assert G.fetch("j1", model_logical="asr-whisper-turbo") is None
        assert calls[-1] == {"format": "json", "wait_ms": 0}
        monkeypatch.setattr(G.requests, "request", lambda *a, **k: _Resp(200, _result()))
        res = G.fetch("j1", model_logical="asr-whisper-turbo")
        assert res.text == "Bonjour." and res.segments[0].speaker == "speaker_00" and res.diarized

    @pytest.mark.parametrize("over,code", [({"transcription_id": "autre"}, "asr_protocol_error"),
                                           ({"model_logical": "autre"}, "asr_model_mismatch"),
                                           ({"media": {}}, "asr_protocol_error")])
    def test_resultat_fail_closed(self, over, code):
        with pytest.raises(G.AsrError) as e:
            G.validate_result(_result(**over), model_logical="asr-whisper-turbo", expected_job_id="j1")
        assert e.value.code == code

    def test_segments_hors_contrat_ecartes_sans_inventer(self):
        res = G.validate_result(_result(segments=[{"start_ms": 500, "end_ms": 200, "text": "x"}]),
                                model_logical="asr-whisper-turbo", expected_job_id="j1")
        assert res.segments == () and res.text == "Bonjour."
        res = G.validate_result(_result(speaker_count=9), model_logical="asr-whisper-turbo", expected_job_id="j1")
        assert res.segments[0].speaker is None and not res.diarized

    @pytest.mark.parametrize("status,code,terminal", [(404, "job_not_found", True), (422, "decoding_failed", True),
                                                      (502, "", False)])
    def test_releve_erreurs(self, monkeypatch, status, code, terminal):
        monkeypatch.setattr(G.requests, "request", lambda *a, **k: _Resp(status, {"error": {"code": code}} if code else None))
        with pytest.raises(G.AsrError) as e:
            G.fetch("j1", model_logical="m")
        assert e.value.terminal is terminal

    def test_delai_reseau(self, monkeypatch):
        def boom(*a, **k):
            raise G.requests.Timeout
        monkeypatch.setattr(G.requests, "request", boom)
        with pytest.raises(G.AsrError) as e:
            G.fetch("j1", model_logical="m")
        assert e.value.retryable and not e.value.terminal

    def test_redecoupage_sur_les_mots(self):
        # Forme réelle relevée le 07/11/2026 : un segment de 2,6 s à 47,5 s portant 3 phrases.
        words = [{"word": w, "start_ms": a, "end_ms": b} for w, a, b in [
            ("Deuxième", 2640, 3460), ("réunion.", 3460, 3900), ("Le", 25000, 25300), ("taux", 25300, 25800),
            ("atteint", 25800, 26400), ("82%.", 26400, 27100), ("La", 45000, 45200), ("revue.", 45200, 47500)]]
        res = G.validate_result(_result(segments=[{"start_ms": 2640, "end_ms": 47500, "text": "x", "speaker": "speaker_00",
                                                   "words": words}]), model_logical="asr-whisper-turbo", expected_job_id="j1")
        assert [(s.start_ms, s.end_ms, s.text) for s in res.segments] == [
            (2640, 3900, "Deuxième réunion."), (25000, 27100, "Le taux atteint 82%."), (45000, 47500, "La revue.")]
        assert all(s.speaker == "speaker_00" for s in res.segments)

    def test_mots_hors_contrat_segment_conserve(self):
        bad = [{"word": "a", "start_ms": 5000, "end_ms": 4000}]
        res = G.validate_result(_result(segments=[{"start_ms": 0, "end_ms": 9000, "text": "Texte.", "words": bad}]),
                                model_logical="asr-whisper-turbo", expected_job_id="j1")
        assert [(s.start_ms, s.end_ms, s.text) for s in res.segments] == [(0, 9000, "Texte.")]

    def test_piece_plafonnee_a_30_s(self):
        words = [{"word": f"m{i}", "start_ms": i * 1000, "end_ms": i * 1000 + 900} for i in range(70)]
        res = G.validate_result(_result(segments=[{"start_ms": 0, "end_ms": 70_000, "text": "x", "words": words}]),
                                model_logical="asr-whisper-turbo", expected_job_id="j1")
        assert len(res.segments) >= 3 and all(s.end_ms - s.start_ms <= 30_900 for s in res.segments)

    def test_suppression_tolerante(self, monkeypatch):
        monkeypatch.setattr(G.requests, "request", lambda *a, **k: _Resp(404, {}))
        assert G.delete("j1") is True

        def boom(*a, **k):
            raise G.requests.ConnectionError
        monkeypatch.setattr(G.requests, "request", boom)
        assert G.delete("j1") is False

    def test_garde_environnement(self, monkeypatch):
        health = {"environment": "production", "allowed_here": True, "model": "asr-whisper-turbo",
                  "observed_revision": "0a36", "checks": {"model_ready": True}}
        monkeypatch.setattr(G.requests, "request", lambda *a, **k: _Resp(200, health))
        monkeypatch.setenv("APP_ENV", "staging")
        with pytest.raises(G.AsrError) as e:
            G.check_health("asr-whisper-turbo")
        assert e.value.code == "asr_environment_mismatch"
        monkeypatch.setenv("APP_ENV", "prod")
        assert G.check_health("asr-whisper-turbo").revision == "0a36"


# --------------------------------------------------------------------------- assemblage
def _part(index, start, end, segments=None, text=None, language="fr"):
    return {"index": index, "start_ms": start, "end_ms": end, "status": "done",
            "result": {"segments": segments or [], "text": text or "", "language": language}}


class TestAssemblage:
    def test_decalage_par_partie_et_monotonie(self):
        out = T.assemble([
            _part(1, 5_400_000, 9_000_000, [{"start_ms": 1000, "end_ms": 4000, "text": "Deuxième partie."}]),
            _part(0, 0, 5_400_000, [{"start_ms": 0, "end_ms": 2000, "text": " Première. "},
                                    {"start_ms": 5_398_000, "end_ms": 5_399_500, "text": "Fin un."}]),
        ])
        assert [(s["start_ms"], s["end_ms"]) for s in out["segments"]] == [(0, 2000), (5_398_000, 5_399_500),
                                                                          (5_401_000, 5_404_000)]
        assert [s["segment_index"] for s in out["segments"]] == [0, 1, 2]
        assert out["text"] == "Première.\nFin un.\nDeuxième partie." and out["precision"] == "segment"

    def test_non_monotone_refuse(self):
        with pytest.raises(T.TranscriptError) as e:
            T.assemble([_part(0, 0, 10_000, [{"start_ms": 5000, "end_ms": 6000, "text": "b"},
                                             {"start_ms": 1000, "end_ms": 2000, "text": "a"}])])
        assert e.value.code == "timestamp_not_monotonic"

    def test_sans_segments_precision_partie_jamais_inventee(self):
        out = T.assemble([_part(0, 0, 60_000, [], text="Texte sans horodatage.")])
        assert out["segments"] == [{"segment_index": 0, "start_ms": 0, "end_ms": 60_000,
                                    "text": "Texte sans horodatage.", "precision": "part"}]
        assert out["precision"] == "part"

    def test_vide(self):
        assert T.assemble([_part(0, 0, 1000, [], text="")])["segments"] == []

    def test_timecode(self):
        assert T.format_timecode(0) == "00:00:00" and T.format_timecode(3_725_999) == "01:02:05"
        assert T.format_timecode(-5) == "00:00:00" and T.format_timecode(13 * 3600 * 1000) == "13:00:00"


class TestUnites:
    def _doc(self, n=40, length=60):
        segs = [{"segment_index": i, "start_ms": i * 10_000, "end_ms": i * 10_000 + 9_000,
                 "text": f"Phrase {i} " + "x" * length, **({"speaker_id": "speaker_01"} if i % 2 else {})}
                for i in range(n)]
        return {"segments": segs, "asr_model": "asr-whisper-turbo", "asr_model_revision": "r",
                "transcript_version": "v", "detected_language": "fr"}

    def test_plages_exactes_sans_recouvrement_ni_perte(self):
        doc = self._doc()
        units = T.build_units(doc)
        ids = [i for u in units for i in u["meta"]["transcript_segment_ids"]]
        assert ids == list(range(40))                                  # aucun segment perdu ni dupliqué
        for u in units:
            first, last = u["meta"]["transcript_segment_ids"][0], u["meta"]["transcript_segment_ids"][-1]
            assert u["meta"]["start_ms"] == doc["segments"][first]["start_ms"]
            assert u["meta"]["end_ms"] == doc["segments"][last]["end_ms"]
            assert len(u["text"]) <= T.UNIT_MAX_CHARS
            assert u["meta"]["source_type"] == "AUDIO_ASR_TRANSCRIPT" and u["meta"]["source_is_asr"] is True
            assert u["meta"]["locator"]["type"] == "time_range"
        assert units[0]["meta"]["locator"]["identifier"].startswith("00:00:00 → ")
        assert units[0]["meta"]["speaker_ids"] == ["speaker_01"]

    def test_jamais_d_unite_a_travers_un_long_silence(self):
        # Reproduction du défaut staging : 3 courts segments distants de plusieurs minutes.
        doc = {"segments": [{"segment_index": 0, "start_ms": 59_730, "end_ms": 63_000, "text": "Jalon Alpha."},
                            {"segment_index": 1, "start_ms": 1_800_000, "end_ms": 1_804_000, "text": "Jalon Bravo."},
                            {"segment_index": 2, "start_ms": 5_520_000, "end_ms": 5_524_000, "text": "Jalon Delta."}]}
        units = T.build_units(doc)
        assert [u["meta"]["locator"]["identifier"] for u in units] == [
            "00:00:59 → 00:01:03", "00:30:00 → 00:30:04", "01:32:00 → 01:32:04"]

    def test_unite_plafonnee_a_deux_minutes(self):
        segs = [{"segment_index": i, "start_ms": i * 4000, "end_ms": i * 4000 + 3500, "text": "Mot."} for i in range(80)]
        units = T.build_units({"segments": segs})
        assert all(u["meta"]["end_ms"] - u["meta"]["start_ms"] <= T.UNIT_MAX_SPAN_MS for u in units)

    def test_canonique_lu_par_le_detecteur(self):
        from types import SimpleNamespace

        from apps.rag_core.services.ingest.extraction.audio_transcript_extractor import AudioTranscriptExtractor
        from apps.rag_core.services.ingest.extraction.audio_transcript_signature import (
            AUDIO_TRANSCRIPT_MIME, detect_audio_transcript_signature)
        from apps.rag_core.services.ingest.extraction.base import ExtractionContext

        t = SimpleNamespace(document_id="d", duration_ms=400_000, language="fr", model_logical="asr-whisper-turbo",
                            model_revision="r", prep_version=M.PREP_VERSION, id="t1", timestamp_precision="segment",
                            segments=self._doc(5)["segments"])
        data = T.canonical_bytes(transcript=t, audio_filename="reunion.m4a")
        ctx = ExtractionContext(filename="reunion.m4a")
        assert detect_audio_transcript_signature(data, ctx) == AUDIO_TRANSCRIPT_MIME
        assert detect_audio_transcript_signature(data, ExtractionContext(filename="reunion.json")) is None
        res = AudioTranscriptExtractor().extract(data, ctx)
        assert res.units and res.metadata["audio"]["segment_count"] == 5
        assert all(u.meta["start_ms"] < u.meta["end_ms"] for u in res.units)


# --------------------------------------------------------------------------- preuve numérique
def _src(text, **meta):
    return {"text": text, "source": {"filename": meta.pop("filename", "reunion.m4a"), "document_id": "d1", "meta": meta}}


class TestPreuveNumeriqueAsr:
    def test_transcription_jamais_fiable(self):
        s = _src("Le budget est de 125 000 euros.", source_type="AUDIO_ASR_TRANSCRIPT", source_is_asr=True)
        assert NE.classify(s, {})[0] == NE.ASR_TRANSCRIPT
        assert NE.ASR_TRANSCRIPT in NE.UNVERIFIED and NE.ASR_TRANSCRIPT not in NE.RELIABLE

    def test_repli_fail_closed_provenance_perdue(self):
        # Méta perdue (chunking, retrieval, assemblage) : le nom du document audio suffit.
        s = _src("Le budget est de 125 000 euros.")
        assert NE.classify(s, {}) == (NE.ASR_TRANSCRIPT, "audio_document_provenance_missing")
        corrupted = {"text": "125", "source": {"filename": "x.wav", "meta": "corrompu"}}
        assert NE.classify(corrupted, {})[0] == NE.ASR_TRANSCRIPT
        # Un PDF sans déclaration garde son comportement historique.
        assert NE.classify(_src("125", filename="r.pdf"), {})[0] == NE.UNKNOWN

    def test_contexte_audio_seul_est_protege(self):
        ann = NE.annotate_numeric_evidence([_src("Seuil de 85 degrés.", source_is_asr=True)], table_pages_by_doc={})
        assert NE.context_is_protected(ann)

    def test_nombre_asr_jamais_supporte(self):
        ann = NE.annotate_numeric_evidence([_src("Le budget est de 125000 euros.", source_is_asr=True)],
                                           table_pages_by_doc={})
        claim = NE.numeric_claims("Le budget est de 125000 euros [1].")[0]
        status, proof = NE.evaluate_claim(claim, context={"budget"}, sources=ann)
        assert status == NE.CLAIM_UNVERIFIED and proof == []

    def test_contexte_mixte_le_pdf_natif_prouve_lasr_non(self):
        pdf = {"text": "Le budget est de 125000 euros.",
               "source": {"filename": "r.pdf", "document_id": "d2", "meta": {"source_is_ocr": False}}}
        asr = _src("Le budget est de 130000 euros.", source_is_asr=True)
        ann = NE.annotate_numeric_evidence([pdf, asr], table_pages_by_doc={})
        assert NE.context_is_protected(ann)
        st_pdf, _ = NE.evaluate_claim(NE.numeric_claims("budget 125000 euros")[0], context={"budget"}, sources=ann)
        st_asr, _ = NE.evaluate_claim(NE.numeric_claims("budget 130000 euros")[0], context={"budget"}, sources=ann)
        assert st_pdf == NE.CLAIM_SUPPORTED and st_asr == NE.CLAIM_UNVERIFIED

    @pytest.mark.parametrize("value", ["17 %", "2 450 €", "12/03/2027", "4,5 kW"])
    def test_pourcentage_montant_date_unite_non_probants(self, value):
        ann = NE.annotate_numeric_evidence([_src(f"Valeur annoncée : {value}.", source_is_asr=True)],
                                           table_pages_by_doc={})
        for claim in NE.numeric_claims(f"La valeur annoncée est {value} [1]."):
            assert NE.evaluate_claim(claim, context={"valeur"}, sources=ann)[0] != NE.CLAIM_SUPPORTED

    def test_valeurs_contradictoires_audio_non_probantes(self):
        ann = NE.annotate_numeric_evidence([_src("Budget 125000.", source_is_asr=True),
                                            _src("Budget 152000.", source_is_asr=True)], table_pages_by_doc={})
        for v in ("125000", "152000"):
            assert NE.evaluate_claim(NE.numeric_claims(f"budget {v}")[0], context={"budget"}, sources=ann)[0] \
                != NE.CLAIM_SUPPORTED

    def test_horodatage_hhmmss_est_un_repere_pas_une_valeur(self):
        assert NE.numeric_claims("Le budget est évoqué à 00:12:35 [1].") == []
        assert NE.numeric_claims("De 01:02:03 à 01:04:00.") == []
        # « 14:30 » (deux groupes) reste un nombre contrôlé comme avant.
        assert {c.value for c in NE.numeric_claims("Réunion à 14:30 pour 250 personnes.")} >= {Decimal(250)}

    def test_garde_reponse_contexte_audio(self):
        ann = NE.annotate_numeric_evidence([_src("Le seuil d'alarme est fixé à 85 degrés.", source_is_asr=True)],
                                           table_pages_by_doc={})
        decision = NE.guard_answer("Le seuil d'alarme est fixé à 85 degrés [1].", question="Quel seuil d'alarme ?",
                                   sources=ann)
        assert decision.verdict != NE.VERDICT_PASS or decision.counts()[NE.CLAIM_SUPPORTED] == 0
