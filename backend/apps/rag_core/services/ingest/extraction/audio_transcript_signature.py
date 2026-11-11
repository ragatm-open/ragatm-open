"""Signature de la TRANSCRIPTION CANONIQUE d'un document audio.

Un document audio n'est jamais extrait depuis ses octets sonores : la réindexation
passe à l'extraction le JSON canonique de sa transcription persistée
(`audio.transcript.canonical_bytes`). Ce détecteur le reconnaît, et lui seul :

- l'extension doit être AUDIO (le JSON porte le nom du fichier audio) ;
- ET les octets doivent commencer par le JSON canonique (clés triées) portant le
  marqueur `ragatm_audio_transcript` dans l'en-tête.

Un JSON forgé renommé en `.mp3` n'arrive jamais ici : le DÉPÔT exige la signature du
conteneur audio, qu'aucun JSON ne porte. Le contenu seul ne reclasse jamais un fichier.
"""
from __future__ import annotations

from typing import Optional

from .base import ExtractionContext

AUDIO_TRANSCRIPT_MIME = "application/vnd.ragatm.audio-transcript+json"

_HEAD = 4096
_MARKER = b'"ragatm_audio_transcript":1'


def detect_audio_transcript_signature(data: bytes, ctx: ExtractionContext) -> Optional[str]:
    from ..audio.formats import is_audio_filename

    if not is_audio_filename(getattr(ctx, "filename", "") or getattr(ctx, "lower_name", "")):
        return None
    head = (data or b"")[:_HEAD]
    if head.startswith(b'{"') and _MARKER in head:
        return AUDIO_TRANSCRIPT_MIME
    return None
