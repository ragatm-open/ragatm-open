"""Orchestration durable de la transcription audio.

Cycle d'un document audio :

    dépôt → PENDING (meta.audio.stage = queued)
    ingest_document_task → reindex_existing_document : aucune transcription prête
        → `schedule_transcription` : ligne AudioTranscript + 1re étape → document RENDU à PENDING
    étapes `audio_transcription_step` (file `ingest_audio`, courtes, re-planifiées) :
        PREPARING     ffprobe (décodable, piste audio, pas de vidéo, durée) + plan des parties
        TRANSCRIBING  par partie, SÉQUENTIELLEMENT : FLAC 16 kHz mono → dépôt Gateway →
                      relectures espacées → résultat persisté → DELETE du job → partie suivante
        READY         assemblage horodaté → ingest_document_task (ingestion STANDARD)
    ingest_document_task → reindex_existing_document : transcription prête → chunks → READY

Pourquoi PENDING et pas INGESTING pendant l'ASR : le reaper tient pour morte toute
tentative INGESTING plus vieille que le bail (900 s) ; une transcription de plusieurs
heures serait tuée. Les étapes courtes évitent aussi la limite Celery (540 s).

Invariants :
- au plus UN job ASR en vol par environnement RAGatm (`ASR_CONCURRENCY = 1`) ;
- une étape porte le `chain_token` de sa ligne : une étape d'une autre chaîne s'arrête ;
- toute écriture vérifie que la ligne existe encore (document supprimé = arrêt + DELETE
  du job Gateway + nettoyage du spool) ;
- un job Gateway n'est supprimé qu'APRÈS persistance de son résultat ;
- aucun texte, aucun nom de fichier dans les journaux : identifiants et codes seulement.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from typing import Optional

from django.db import transaction
from django.utils import timezone

from ..extraction import config
from . import gateway_asr, media, transcript as tr
from .formats import is_audio_filename

logger = logging.getLogger(__name__)

AUDIO_QUEUE = "ingest_audio"


class AudioTranscriptionDeferred(Exception):
    """L'ingestion d'un audio attend sa transcription : le document est rendu à PENDING
    et la chaîne ASR est planifiée. Ni un échec, ni une ingestion terminée."""

    def __init__(self, transcript_id: str):
        super().__init__("audio_transcription_deferred")
        self.transcript_id = transcript_id
#: Le GPU du Gateway est PARTAGÉ (chat, vision, un autre client interne, prod et staging) : une saturation
#: (`resource_saturated`, « budget GPU insuffisant ») est une attente normale, pas un échec.
#: Une partie attend au plus ce délai (au rythme du `Retry-After`) avant d'échouer `asr_saturated`.
SATURATION_WAIT_SECONDS = 3600
MAX_TRANSPORT_ERRORS = 30
SUBMITTING_STALE_SECONDS = gateway_asr.SUBMIT_TIMEOUT + 120
SPOOL_MARGIN_BYTES = 256 << 20

#: Messages utilisateur (FR) par code — jamais de détail technique ni de contenu.
MESSAGES = {
    "audio_ingestion_disabled": "L'ingestion audio est désactivée pour cet espace ou cet environnement.",
    "undecodable_audio": "Le fichier audio est illisible ou corrompu : il n'a pas pu être décodé.",
    "no_audio_track": "Le fichier ne contient aucune piste audio.",
    "audio_contains_video": "Le fichier contient une piste vidéo : seuls les fichiers audio sont acceptés.",
    "audio_too_long": "L'enregistrement dépasse la durée maximale autorisée.",
    "audio_dependency_unavailable": "La préparation audio est indisponible sur cet environnement (ffmpeg absent).",
    "audio_preparation_timeout": "La préparation de l'audio a dépassé le délai autorisé.",
    "audio_spool_full": "Espace de travail temporaire insuffisant pour préparer l'audio. Réessayez plus tard.",
    "audio_part_too_large": "Une partie préparée de l'audio dépasse la taille acceptée par le service de transcription.",
    "asr_timeout": "La transcription a dépassé le délai autorisé.",
    "asr_unavailable": "Le service de transcription est indisponible. Réessayez plus tard.",
    "asr_saturated": "Le service de transcription est saturé. Réessayez plus tard.",
    "asr_failed": "La transcription a échoué.",
    "asr_environment_mismatch": "Le service de transcription ne correspond pas à cet environnement.",
    "transcript_empty": "Aucune parole n'a été reconnue dans l'enregistrement.",
    "audio_cancelled": "La transcription a été annulée.",
    "asr_job_tracking_lost": ("Le suivi de la transcription a été perdu (acheminement du service modifié "
                              "pendant le traitement). Relancez l'ingestion du fichier."),
}

#: Codes du Gateway → code RAGatm présenté à l'utilisateur.
_GATEWAY_CODE_MAP = {
    "decoding_failed": "undecodable_audio", "invalid_audio": "undecodable_audio",
    "unsupported_media": "undecodable_audio", "ambiguous_audio_streams": "undecodable_audio",
    "too_many_channels": "undecodable_audio", "audio_too_long": "audio_too_long",
    "audio_too_large": "audio_part_too_large", "asr_timeout": "asr_timeout", "queue_timeout": "asr_timeout",
    "resource_saturated": "asr_saturated", "asr_unavailable": "asr_unavailable",
    "asr_timeout_transport": "asr_unavailable", "asr_environment_mismatch": "asr_environment_mismatch",
    # Refus PROPRES à AI Gateway Pool (`pool_<code>`, cf. gateway_asr) : indisponibilité de
    # l'acheminement, jamais un défaut du fichier. `pool_job_affinity_unknown` épuisé = suivi perdu.
    "pool_no_eligible_node": "asr_unavailable", "pool_node_unavailable": "asr_unavailable",
    "pool_capability_not_available": "asr_unavailable", "pool_upstream_unavailable": "asr_unavailable",
    "pool_upstream_timeout": "asr_unavailable", "pool_affinity_store_unavailable": "asr_unavailable",
    "pool_job_affinity_unknown": "asr_job_tracking_lost",
    # Réponse perdue au DÉPÔT (délai de lecture, coupure après envoi) : jamais redéposé.
    "asr_submit_outcome_unknown": "asr_unavailable",
}


def user_code(code: str) -> str:
    return _GATEWAY_CODE_MAP.get(code, code if code in MESSAGES else "asr_failed")


# --------------------------------------------------------------------------- #
# Identification et réutilisation
# --------------------------------------------------------------------------- #
def is_audio_document(doc) -> bool:
    meta = getattr(doc, "meta", None) or {}
    if isinstance(meta, dict) and isinstance(meta.get("audio"), dict):
        return True
    ctype = str(getattr(doc, "content_type", "") or "")
    return is_audio_filename(getattr(doc, "filename", "")) and ctype.startswith("audio/")


def ready_transcript_for(doc):
    from apps.rag_core.models import AudioTranscript, AudioTranscriptStatus

    return (AudioTranscript.objects
            .filter(document_id=doc.id, status=AudioTranscriptStatus.READY, source_sha256=doc.sha256,
                    prep_version=media.PREP_VERSION, model_logical=config.audio_asr_model_logical())
            .order_by("-completed_at").first())


def _patch_audio_meta(document_id, **fields) -> bool:
    from apps.rag_core.models import Document

    with transaction.atomic():
        d = Document.objects.select_for_update().filter(id=document_id).first()
        if d is None:
            return False
        meta = dict(d.meta) if isinstance(d.meta, dict) else {}
        audio = dict(meta.get("audio") or {})
        audio.update({k: v for k, v in fields.items()})
        meta["audio"] = audio
        Document.objects.filter(id=d.id).update(meta=meta)
    return True


def schedule_transcription(doc, *, access_context: Optional[dict] = None) -> "object":
    """Crée (ou reprend) la tentative active et la première étape ; document → PENDING.

    Appelée par `reindex_existing_document` quand aucune transcription prête n'existe.
    Une tentative ACTIVE existante (préparation ou transcription en cours) est reprise
    telle quelle : jamais deux chaînes pour un même document.
    """
    from apps.rag_core.models import AudioTranscript, AudioTranscriptStatus, Document, DocumentStatus

    with transaction.atomic():
        active = (AudioTranscript.objects.select_for_update()
                  .filter(document_id=doc.id, status__in=[AudioTranscriptStatus.PREPARING,
                                                          AudioTranscriptStatus.TRANSCRIBING]).first())
        if active is None:
            # Une ligne `ready` d'une autre préparation/modèle ne bloque pas : elle est périmée.
            AudioTranscript.objects.filter(document_id=doc.id, status=AudioTranscriptStatus.READY).exclude(
                source_sha256=doc.sha256, prep_version=media.PREP_VERSION,
                model_logical=config.audio_asr_model_logical(),
            ).update(status=AudioTranscriptStatus.CANCELLED, error_code="superseded")
            active = AudioTranscript.objects.create(
                document_id=doc.id, source_sha256=doc.sha256 or "", prep_version=media.PREP_VERSION,
                model_logical=config.audio_asr_model_logical(), status=AudioTranscriptStatus.PREPARING,
            )
        Document.objects.filter(id=doc.id).update(status=DocumentStatus.PENDING, error_message="")
        tid, token = str(active.id), str(active.chain_token)
        transaction.on_commit(lambda: enqueue_step(tid, token))
    # Identité de l'utilisateur à l'origine de la demande : l'ingestion FINALE (ré-enfilée par
    # la chaîne ASR) doit porter le même contexte d'accès que la première, sinon la garde de
    # tâche (owner-scope en enforcement) la refuse (`context_missing`). Jamais exposée par l'API.
    uid = (access_context or {}).get("user_id")
    extra = {"requested_by_user_id": int(uid)} if isinstance(uid, int) and not isinstance(uid, bool) else {}
    _patch_audio_meta(doc.id, stage="queued", transcript_id=tid, error_code="", **extra)
    return active


def enqueue_step(transcript_id: str, token: str, countdown: int = 0) -> None:
    from apps.rag_core.tasks import audio_transcription_step

    audio_transcription_step.apply_async(args=[transcript_id, token], countdown=max(0, int(countdown)),
                                         queue=AUDIO_QUEUE)


# --------------------------------------------------------------------------- #
# Spool
# --------------------------------------------------------------------------- #
def _spool_root() -> str:
    root = config.audio_spool_dir()
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def spool_dir(transcript_id: str) -> str:
    d = os.path.join(_spool_root(), str(uuid.UUID(str(transcript_id))))
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def cleanup_spool(transcript_id: str) -> None:
    try:
        shutil.rmtree(os.path.join(_spool_root(), str(uuid.UUID(str(transcript_id)))), ignore_errors=True)
    except (ValueError, OSError):
        pass


def sweep_spool() -> int:
    """Retire les répertoires de spool sans tentative ACTIVE (document supprimé, échec,
    redémarrage). Exécuté par le worker audio, seul à voir son spool."""
    from apps.rag_core.models import AudioTranscript, AudioTranscriptStatus

    root = _spool_root()
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    ids = []
    for n in names:
        try:
            ids.append(str(uuid.UUID(n)))
        except ValueError:
            continue
    alive = {str(i) for i in AudioTranscript.objects.filter(
        id__in=ids, status__in=[AudioTranscriptStatus.PREPARING, AudioTranscriptStatus.TRANSCRIBING],
    ).values_list("id", flat=True)}
    removed = 0
    for i in ids:
        if i not in alive:
            cleanup_spool(i)
            removed += 1
    return removed


def _source_path(doc, t) -> str:
    """Chemin SERVEUR du fichier original (stockage fichier partagé). Jamais copié en
    mémoire ; copié par blocs dans le spool si le stockage n'expose pas de chemin."""
    try:
        return doc.storage_file.path
    except (NotImplementedError, ValueError, AttributeError):
        pass
    dest = os.path.join(spool_dir(str(t.id)), "source.bin")
    if not os.path.exists(dest):
        with doc.storage_file.open("rb") as src, open(dest + ".partial", "wb") as out:
            for block in iter(lambda: src.read(1 << 20), b""):
                out.write(block)
        os.replace(dest + ".partial", dest)
    return dest


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Écritures gardées
# --------------------------------------------------------------------------- #
def _save(t, **fields) -> bool:
    """Écrit si la ligne existe ENCORE avec le même jeton (sinon : chaîne abandonnée)."""
    from apps.rag_core.models import AudioTranscript

    n = AudioTranscript.objects.filter(id=t.id, chain_token=t.chain_token).update(**fields)
    for k, v in fields.items():
        setattr(t, k, v)
    return n == 1


def _fail(t, doc, code: str) -> None:
    from apps.rag_core.models import AudioTranscriptStatus
    from ..indexing import settle_terminal_failure

    ucode = user_code(code)
    for part in t.parts or []:
        job = part.get("job_id")
        if job and part.get("status") in ("submitted", "submitting"):
            gateway_asr.delete(job)
    _save(t, status=AudioTranscriptStatus.FAILED, error_code=ucode, completed_at=timezone.now())
    cleanup_spool(str(t.id))
    if doc is not None:
        settle_terminal_failure(
            doc.id, message=f"{ucode}: {MESSAGES.get(ucode, MESSAGES['asr_failed'])}",
            failure_meta_patch={"extraction": {"extraction_status": "failed", "error_code": ucode}},
        )
        _patch_audio_meta(doc.id, stage="failed", error_code=ucode)
    logger.warning("audio_transcription_failed transcript_id=%s code=%s gateway_code=%s", t.id, ucode, code)


# --------------------------------------------------------------------------- #
# Étapes
# --------------------------------------------------------------------------- #
def run_step(transcript_id: str, token: str) -> str:
    """UNE étape. Rend un code de résultat (journal/tests) ; ne lève jamais pour un cas métier."""
    from apps.rag_core.models import AudioTranscript, AudioTranscriptStatus, Document

    sweep_spool()
    t = AudioTranscript.objects.filter(id=transcript_id).first()
    if t is None:
        cleanup_spool(transcript_id)
        return "gone"
    if str(t.chain_token) != str(token):
        return "stale_chain"
    if t.status not in (AudioTranscriptStatus.PREPARING, AudioTranscriptStatus.TRANSCRIBING):
        return "not_active"
    doc = Document.objects.filter(id=t.document_id).first()
    if doc is None:
        _fail(t, None, "audio_cancelled")
        return "document_gone"
    if not config.audio_ingest_enabled():
        # Rollback fonctionnel : annulation EXPLICITE, jobs Gateway libérés, rien de publié.
        _fail(t, doc, "audio_ingestion_disabled")
        return "disabled"
    if t.source_sha256 and doc.sha256 and t.source_sha256 != doc.sha256:
        _fail(t, doc, "audio_cancelled")
        return "source_changed"
    try:
        if t.status == AudioTranscriptStatus.PREPARING:
            return _prepare(t, doc)
        return _advance(t, doc)
    except media.AudioMediaError as e:
        _fail(t, doc, e.code)
        return e.code


def _prepare(t, doc) -> str:
    from apps.rag_core.models import AudioTranscriptStatus

    _patch_audio_meta(doc.id, stage="preparing_audio")
    if not media.tools_available():
        raise media.AudioMediaError("audio_dependency_unavailable")
    src = _source_path(doc, t)
    info = media.inspect(src)
    if info.duration_ms > config.audio_max_duration_seconds() * 1000:
        raise media.AudioMediaError("audio_too_long")
    part_max = config.audio_part_max_seconds()
    silences = media.detect_silences(src, duration_ms=info.duration_ms) if info.duration_ms > part_max * 1000 else []
    plan = media.plan_parts(info.duration_ms, part_max_seconds=part_max, silences=silences)
    parts = [{"index": p.index, "start_ms": p.start_ms, "end_ms": p.end_ms, "cut": p.cut, "status": "pending",
              "attempts": 0, "transport_errors": 0} for p in plan]
    if not _save(t, status=AudioTranscriptStatus.TRANSCRIBING, parts=parts, duration_ms=info.duration_ms,
                 media={"codec": info.codec, "channels": info.channels, "sample_rate": info.sample_rate,
                        "format": info.format_name, "audio_streams": info.audio_streams}):
        return "abandoned"
    _patch_audio_meta(doc.id, stage="transcribing", duration_ms=info.duration_ms, parts_total=len(parts), parts_done=0)
    transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token)))
    return "prepared"


def _another_job_in_flight(t) -> bool:
    from apps.rag_core.models import AudioTranscript, AudioTranscriptStatus

    others = AudioTranscript.objects.filter(status=AudioTranscriptStatus.TRANSCRIBING).exclude(id=t.id)
    return others.filter(parts__contains=[{"status": "submitted"}]).exists() or \
        others.filter(parts__contains=[{"status": "submitting"}]).exists()


def _advance(t, doc) -> str:
    parts = list(t.parts or [])
    pending = [p for p in parts if p.get("status") != "done"]
    if not pending:
        return _finalize(t, doc)
    part = pending[0]
    status = part.get("status")
    if status == "submitted":
        return _poll(t, doc, parts, part)
    if status == "submitting":
        started = part.get("submitting_at")
        if started and (timezone.now().timestamp() - float(started)) > SUBMITTING_STALE_SECONDS:
            # Dépôt interrompu (worker tué pendant l'envoi) : rien ne prouve que le Gateway
            # ne l'a pas admis. Pas de second dépôt à l'aveugle : échec explicite, relançable.
            _fail(t, doc, "asr_unavailable")
            return "submit_interrupted"
        transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token), config.audio_poll_interval_seconds()))
        return "submitting_elsewhere"
    return _submit(t, doc, parts, part)


def _part_path(t, part) -> str:
    return os.path.join(spool_dir(str(t.id)), f"part-{int(part['index']):03d}.flac")


def _submit(t, doc, parts, part) -> str:
    if _another_job_in_flight(t):
        _patch_audio_meta(doc.id, stage="waiting_for_asr")
        transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token), config.audio_poll_interval_seconds() * 3))
        return "waiting_for_asr"
    model = config.audio_asr_model_logical()
    try:
        h = gateway_asr.check_health(model)
    except gateway_asr.AsrError as e:
        if e.retryable and part.get("transport_errors", 0) < MAX_TRANSPORT_ERRORS:
            part["transport_errors"] = int(part.get("transport_errors", 0)) + 1
            _save(t, parts=parts)
            transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token), config.audio_poll_interval_seconds() * 3))
            return "health_retry"
        _fail(t, doc, e.code)
        return e.code
    path = _part_path(t, part)
    if not os.path.exists(path):
        if media.free_bytes(os.path.dirname(path)) < config.audio_part_max_bytes() + SPOOL_MARGIN_BYTES:
            raise media.AudioMediaError("audio_spool_full")
        size = media.transcode_part(_source_path(doc, t), path, start_ms=int(part["start_ms"]), end_ms=int(part["end_ms"]))
        part["size"] = size
        part["sha256"] = _sha256_file(path)
    size = os.path.getsize(path)
    if size > config.audio_part_max_bytes():
        if _split_part(t, doc, parts, part):
            return "part_split"
        raise media.AudioMediaError("audio_part_too_large")
    part.update(status="submitting", submitting_at=timezone.now().timestamp())
    if not _save(t, parts=parts, model_revision=h.revision or t.model_revision):
        return "abandoned"
    try:
        job_id = gateway_asr.submit(path, size=size, model_logical=model)
    except gateway_asr.AsrError as e:
        part.update(status="pending", submitting_at=None, attempts=int(part.get("attempts", 0)) + 1)
        now = timezone.now().timestamp()
        since = float(part.get("saturated_since") or now)
        if e.retryable and (now - since) < SATURATION_WAIT_SECONDS:
            part["saturated_since"] = since
            _save(t, parts=parts)
            _patch_audio_meta(doc.id, stage="waiting_for_asr")
            delay = int(e.retry_after or 30)
            transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token), delay))
            logger.info("audio_part_saturated transcript_id=%s part=%s attempt=%s retry_in=%s",
                        t.id, part["index"], part["attempts"], delay)
            return "saturated_retry"
        _save(t, parts=parts)
        _fail(t, doc, e.code if not e.retryable else "asr_saturated")
        return e.code
    part.update(status="submitted", job_id=job_id, submitted_at=timezone.now().timestamp(), submitting_at=None,
                saturated_since=None)
    if not _save(t, parts=parts):
        gateway_asr.delete(job_id)  # document supprimé pendant le dépôt : rien ne doit survivre
        cleanup_spool(str(t.id))
        return "abandoned"
    _patch_audio_meta(doc.id, stage="transcribing", parts_done=sum(1 for p in parts if p.get("status") == "done"))
    logger.info("audio_part_submitted transcript_id=%s part=%s size=%s", t.id, part["index"], size)
    transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token), config.audio_poll_interval_seconds()))
    return "submitted"


def _split_part(t, doc, parts, part) -> bool:
    """Partie trop volumineuse après réencodage : coupée en deux (plan déterministe)."""
    dur = int(part["end_ms"]) - int(part["start_ms"])
    if dur < 120_000:
        return False
    try:
        os.remove(_part_path(t, part))
    except OSError:
        pass
    mid = int(part["start_ms"]) + dur // 2
    i = parts.index(part)
    a = {**part, "end_ms": mid, "cut": "hard", "status": "pending", "size": None, "sha256": None}
    b = {**part, "start_ms": mid, "status": "pending", "size": None, "sha256": None}
    new = parts[:i] + [a, b] + parts[i + 1:]
    for k, p in enumerate(new):
        p["index"] = k
    if not _save(t, parts=new):
        return True
    _patch_audio_meta(doc.id, parts_total=len(new))
    transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token)))
    return True


def _poll(t, doc, parts, part) -> str:
    job = part["job_id"]
    if (timezone.now().timestamp() - float(part.get("submitted_at") or 0)) > config.audio_job_deadline_seconds():
        _fail(t, doc, "asr_timeout")
        return "deadline"
    try:
        res = gateway_asr.fetch(job, model_logical=config.audio_asr_model_logical())
    except gateway_asr.AsrError as e:
        if e.terminal:
            _fail(t, doc, e.code)
            return e.code
        part["transport_errors"] = int(part.get("transport_errors", 0)) + 1
        # Dernier code NON terminal conservé (diagnostic) : un refus d'acheminement du pool
        # (`pool_job_affinity_unknown`) ne doit pas se confondre, à l'épuisement du budget, avec
        # une simple indisponibilité. Rien n'est relancé à l'aveugle : seule la LECTURE se répète.
        part["last_error_code"] = e.code
        if part["transport_errors"] > MAX_TRANSPORT_ERRORS:
            code = e.code if e.code == "pool_job_affinity_unknown" else "asr_unavailable"
            _fail(t, doc, code)
            return code
        _save(t, parts=parts)
        transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token), config.audio_poll_interval_seconds() * 2))
        return "poll_retry"
    if res is None:
        transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token), config.audio_poll_interval_seconds()))
        return "running"
    if part.get("sha256") and res.media_sha256 != part["sha256"]:
        _fail(t, doc, "asr_protocol_error")
        return "media_mismatch"
    part.update(status="done", result={
        "text": res.text, "language": res.language, "duration_ms": res.duration_ms,
        "media_sha256": res.media_sha256, "diarized": res.diarized, "speaker_count": res.speaker_count,
        "warnings": list(res.warnings),
        "segments": [{"start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text,
                      **({"speaker": s.speaker} if s.speaker else {})} for s in res.segments],
    })
    if not _save(t, parts=parts):
        gateway_asr.delete(job)
        cleanup_spool(str(t.id))
        return "abandoned"
    # Résultat PERSISTÉ : le job peut être libéré, la partie préparée aussi.
    gateway_asr.delete(job)
    try:
        os.remove(_part_path(t, part))
    except OSError:
        pass
    done = sum(1 for p in parts if p.get("status") == "done")
    _patch_audio_meta(doc.id, stage="transcribing" if done < len(parts) else "assembling_transcript",
                      parts_done=done)
    logger.info("audio_part_done transcript_id=%s part=%s segments=%s", t.id, part["index"], len(res.segments))
    transaction.on_commit(lambda: enqueue_step(str(t.id), str(t.chain_token)))
    return "part_done"


def _finalize(t, doc) -> str:
    from apps.rag_core.models import AudioTranscriptStatus
    from ..pipeline import _enqueue_after_commit

    try:
        out = tr.assemble(t.parts or [])
    except tr.TranscriptError as e:
        _fail(t, doc, e.code)
        return e.code
    if not out["segments"]:
        _fail(t, doc, "transcript_empty")
        return "transcript_empty"
    parts_slim = [{k: v for k, v in p.items() if k != "result"} for p in (t.parts or [])]
    with transaction.atomic():
        if not _save(t, status=AudioTranscriptStatus.READY, text=out["text"], segments=out["segments"],
                     language=out["language"][:16], segment_count=out["segment_count"],
                     timestamp_precision=out["precision"], parts=parts_slim, completed_at=timezone.now()):
            return "abandoned"
        uid = ((doc.meta or {}).get("audio") or {}).get("requested_by_user_id")
        _enqueue_after_commit(document_id=str(doc.id), request_id=f"audio-{t.id}", filename=doc.filename,
                              actor_user_id=uid if isinstance(uid, int) and not isinstance(uid, bool) else None)
    cleanup_spool(str(t.id))
    _patch_audio_meta(doc.id, stage="indexing", parts_done=len(parts_slim), segment_count=out["segment_count"],
                      language=out["language"][:16])
    logger.info("audio_transcript_ready transcript_id=%s segments=%s", t.id, out["segment_count"])
    return "ready"


def watchdog(*, idle_seconds: int = 600) -> int:
    """Relance les chaînes ACTIVES sans activité récente (worker redémarré, message perdu).
    Une relance en trop est inoffensive : l'étape relit l'état persisté."""
    from datetime import timedelta

    from apps.rag_core.models import AudioTranscript, AudioTranscriptStatus

    limit = timezone.now() - timedelta(seconds=max(idle_seconds, config.audio_poll_interval_seconds() * 10))
    n = 0
    for tid, token in AudioTranscript.objects.filter(
        status__in=[AudioTranscriptStatus.PREPARING, AudioTranscriptStatus.TRANSCRIBING], updated_at__lt=limit,
    ).values_list("id", "chain_token"):
        enqueue_step(str(tid), str(token))
        n += 1
    return n


def active_gateway_jobs(document_id) -> list[str]:
    """Jobs Gateway EN VOL appartenant aux tentatives de CE document (jamais d'autres)."""
    from apps.rag_core.models import AudioTranscript

    jobs = []
    for parts in AudioTranscript.objects.filter(document_id=document_id).values_list("parts", flat=True):
        for p in parts or []:
            if p.get("job_id") and p.get("status") in ("submitted", "submitting"):
                jobs.append(str(p["job_id"]))
    return jobs
