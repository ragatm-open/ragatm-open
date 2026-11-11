"""Assemblage et forme canonique de la transcription audio.

- **Assemblage** : les segments de chaque partie sont décalés de l'offset de la partie,
  dans l'ordre des parties ; la monotonie des débuts est VÉRIFIÉE (jamais réparée en
  silence) ; aucun texte n'est résumé, reformulé ni retiré (seuls les blancs de bord
  sont normalisés).
- **Horodatage absent** : si le Gateway ne rend pas de segments valides pour une partie,
  la partie devient UN segment couvrant ses bornes réelles, marqué `precision="part"`.
  Aucune précision supérieure à celle du moteur n'est inventée.
- **Forme canonique** (`canonical_bytes`) : JSON déterministe, lu par l'extracteur
  `AudioTranscriptExtractor`. Marqueur `ragatm_audio_transcript` = version du schéma.
"""
from __future__ import annotations

import json
from typing import Iterable

SCHEMA_VERSION = 1
MAGIC_KEY = "ragatm_audio_transcript"
SOURCE_TYPE = "AUDIO_ASR_TRANSCRIPT"
#: Taille cible d'une unité (≤ budget de chunk GOUVERNÉ, 1 200 car.) : une unité = un chunk.
UNIT_MAX_CHARS = 1000
#: Une unité ne regroupe jamais deux segments séparés de plus de 5 s, ni ne couvre plus de 2 min :
#: sa plage temporelle doit rester une citation exploitable (constat staging : sans ces bornes,
#: trois phrases distantes de 90 min formaient une seule citation « 00:00:59 → 02:05:02 »).
UNIT_MAX_GAP_MS = 5_000
UNIT_MAX_SPAN_MS = 120_000


class TranscriptError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def assemble(parts: Iterable[dict]) -> dict:
    """Parties terminées → {text, segments, language, precision, segment_count}."""
    segments, texts, languages, precision = [], [], [], "segment"
    last_start = -1
    for part in sorted(parts, key=lambda p: int(p["index"])):
        res = part.get("result") or {}
        offset = int(part["start_ms"])
        part_end = int(part["end_ms"])
        segs = res.get("segments") or []
        if res.get("language"):
            languages.append(str(res["language"]))
        if not segs:
            text = (res.get("text") or "").strip()
            if text:
                precision = "part"
                segs = [{"start_ms": 0, "end_ms": max(1, part_end - offset), "text": text, "precision": "part"}]
        for s in segs:
            start = offset + int(s["start_ms"])
            end = offset + int(s["end_ms"])
            if start < 0 or end <= start:
                raise TranscriptError("timestamp_invalid")
            if start < last_start:
                raise TranscriptError("timestamp_not_monotonic")
            last_start = start
            seg = {"segment_index": len(segments), "start_ms": start, "end_ms": end, "text": str(s["text"]).strip()}
            if s.get("speaker"):
                seg["speaker_id"] = s["speaker"]
            if s.get("precision"):
                seg["precision"] = s["precision"]
            segments.append(seg)
            texts.append(seg["text"])
    language = max(set(languages), key=languages.count) if languages else ""
    return {"text": "\n".join(t for t in texts if t), "segments": segments, "language": language,
            "precision": precision if segments else "", "segment_count": len(segments)}


def canonical_bytes(*, transcript, audio_filename: str) -> bytes:
    """JSON canonique d'une transcription `ready` (ordre de clés stable)."""
    doc = {
        MAGIC_KEY: SCHEMA_VERSION,
        "source_document_id": str(transcript.document_id),
        "audio_filename": audio_filename,
        "audio_duration_ms": int(transcript.duration_ms or 0),
        "detected_language": transcript.language or "",
        "asr_model": transcript.model_logical,
        "asr_model_revision": transcript.model_revision or "",
        "prep_version": transcript.prep_version,
        "transcript_version": str(transcript.id),
        "timestamp_precision": transcript.timestamp_precision or "",
        "segments": list(transcript.segments or []),
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def format_timecode(ms: int) -> str:
    """`HH:MM:SS` (heures toujours présentes, jamais négatif)."""
    total = max(0, int(ms)) // 1000
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def build_units(doc: dict) -> list[dict]:
    """Unités d'indexation : segments CONSÉCUTIFS regroupés jusqu'à `UNIT_MAX_CHARS`.

    La plage d'une unité couvre EXACTEMENT ses segments (début du premier, fin du dernier).
    Un segment plus long que la cible forme seul son unité (le chunker la fenêtrera ; la
    plage reste celle du segment). Aucun recouvrement : deux unités ne partagent aucun
    segment, donc aucune citation n'est dupliquée.
    """
    segs = doc.get("segments") or []
    units, group = [], []

    def flush():
        if not group:
            return
        start, end = int(group[0]["start_ms"]), int(group[-1]["end_ms"])
        speakers = sorted({g["speaker_id"] for g in group if g.get("speaker_id")})
        precision = "part" if any(g.get("precision") == "part" for g in group) else "segment"
        meta = {
            "source_type": SOURCE_TYPE,
            "source_is_asr": True,
            "kind": "audio_transcript",
            "start_ms": start,
            "end_ms": end,
            "transcript_segment_ids": [int(g["segment_index"]) for g in group],
            "asr_model": doc.get("asr_model") or "",
            "asr_model_revision": doc.get("asr_model_revision") or "",
            "transcript_version": doc.get("transcript_version") or "",
            "language": doc.get("detected_language") or "",
            "timestamp_precision": precision,
            "locator": {"type": "time_range", "identifier": f"{format_timecode(start)} → {format_timecode(end)}"},
        }
        if speakers:
            meta["speaker_ids"] = speakers
        units.append({"text": "\n".join(g["text"] for g in group), "meta": meta})
        group.clear()

    size = 0
    for s in segs:
        t = str(s.get("text") or "").strip()
        if not t:
            continue
        if group and (size + len(t) + 1 > UNIT_MAX_CHARS
                      or int(s["start_ms"]) - int(group[-1]["end_ms"]) > UNIT_MAX_GAP_MS
                      or int(s["end_ms"]) - int(group[0]["start_ms"]) > UNIT_MAX_SPAN_MS):
            flush()
            size = 0
        group.append({**s, "text": t})
        size += len(t) + 1
    flush()
    return units
