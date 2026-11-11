"""Extracteur de la transcription audio canonique.

Pur : aucun réseau, aucune base (il tourne dans le processus d'extraction isolé). Produit
des UNITÉS (`result.units`) horodatées, une par groupe de segments consécutifs : le rail
`units` de l'indexation recopie leur méta sur chaque chunk (plage, provenance ASR,
locator `time_range`). Rien n'est résumé ni reformulé.
"""
from __future__ import annotations

import json

from .base import ExtractionContext, ExtractionResult, ExtractionStatus, StructuredUnit
from .base import CorruptedFile


class AudioTranscriptExtractor:
    name = "audio_transcript"

    def can_handle(self, ctx: ExtractionContext) -> bool:  # pragma: no cover - dispatch par format_key
        return True

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        from ..audio import transcript as tr
        from .audio_transcript_signature import AUDIO_TRANSCRIPT_MIME

        try:
            doc = json.loads((data or b"").decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CorruptedFile("Transcription audio illisible.", code="audio_transcript_corrupted") from exc
        if not isinstance(doc, dict) or doc.get(tr.MAGIC_KEY) != tr.SCHEMA_VERSION or not isinstance(doc.get("segments"), list):
            raise CorruptedFile("Transcription audio au format inattendu.", code="audio_transcript_corrupted")
        units = [StructuredUnit(text=u["text"], meta=u["meta"]) for u in tr.build_units(doc)]
        text = "\n".join(u.text for u in units)
        res = ExtractionResult(
            text=text,
            units=units,
            status=ExtractionStatus.SUCCESS,
            detected_mime=AUDIO_TRANSCRIPT_MIME,
            method="audio_asr_transcript",
            metadata={"audio": {
                "duration_ms": int(doc.get("audio_duration_ms") or 0),
                "language": str(doc.get("detected_language") or ""),
                "asr_model": str(doc.get("asr_model") or ""),
                "asr_model_revision": str(doc.get("asr_model_revision") or ""),
                "transcript_version": str(doc.get("transcript_version") or ""),
                "timestamp_precision": str(doc.get("timestamp_precision") or ""),
                "segment_count": len(doc.get("segments") or []),
                "unit_count": len(units),
                "source_type": tr.SOURCE_TYPE,
            }},
        )
        return res
