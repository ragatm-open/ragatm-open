"""Transcription audio PERSISTÉE — source primaire d'un document audio.

Une ligne = une TENTATIVE de transcription d'un document, pour une empreinte d'audio, une
version de préparation et un modèle ASR logique. Elle porte aussi l'état de
l'orchestration (parties, jobs Gateway) : la reprise après redémarrage de worker relit
cet état, jamais la mémoire d'un processus.

- CASCADE avec le document : supprimer l'audio supprime sa transcription ;
- la réindexation RÉUTILISE une ligne `ready` de même (empreinte, préparation, modèle) :
  Whisper n'est jamais relancé pour reconstruire des chunks ;
- au plus UNE ligne active (non `failed`/`cancelled`) par document ;
- jamais de contenu dans les journaux : le texte vit ici et dans les chunks seulement.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone


class AudioTranscriptStatus(models.TextChoices):
    PREPARING = "preparing", "Préparation"
    TRANSCRIBING = "transcribing", "Transcription"
    READY = "ready", "Prête"
    FAILED = "failed", "Échec"
    CANCELLED = "cancelled", "Annulée"


ACTIVE_STATUSES = (AudioTranscriptStatus.PREPARING, AudioTranscriptStatus.TRANSCRIBING, AudioTranscriptStatus.READY)


class AudioTranscript(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey("rag_core.Document", on_delete=models.CASCADE, related_name="audio_transcripts")
    source_sha256 = models.CharField(max_length=64)
    prep_version = models.CharField(max_length=64)
    model_logical = models.CharField(max_length=64)
    model_revision = models.CharField(max_length=64, blank=True, default="")

    status = models.CharField(max_length=16, choices=AudioTranscriptStatus.choices,
                              default=AudioTranscriptStatus.PREPARING)
    #: Jeton de la chaîne d'étapes vivante : une étape portant un autre jeton s'arrête.
    chain_token = models.UUIDField(default=uuid.uuid4)
    #: Parties : [{index, start_ms, end_ms, cut, status, job_id, submitted_at, attempts,
    #: transport_errors, size, result{segments, language, duration_ms, media_sha256}}].
    parts = models.JSONField(default=list, blank=True)

    duration_ms = models.BigIntegerField(null=True, blank=True)
    media = models.JSONField(default=dict, blank=True)  # codec, canaux, fréquence (ffprobe)
    language = models.CharField(max_length=16, blank=True, default="")
    segment_count = models.PositiveIntegerField(default=0)
    timestamp_precision = models.CharField(max_length=16, blank=True, default="")  # segment | part
    text = models.TextField(blank=True, default="")
    segments = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "rag_audio_transcript"
        indexes = [models.Index(fields=["document", "status"], name="rag_audio_tr_doc_status")]
        constraints = [
            models.UniqueConstraint(
                fields=["document"],
                condition=Q(status__in=[s.value for s in ACTIVE_STATUSES]),
                name="uniq_audio_transcript_active_per_document",
            ),
            models.CheckConstraint(condition=Q(status__in=AudioTranscriptStatus.values),
                                   name="audio_transcript_status_valid"),
        ]

    def __str__(self) -> str:  # jamais de contenu
        return f"AudioTranscript({self.id}, {self.status})"
