"""Client ASR d'AI Gateway — conventions EXACTES du client un autre client interne.

Contrat (Gateway 2.0.0, relevé le 06/11/2026 ; corps POST non publié dans l'OpenAPI,
repris du client un autre client interne `integrations/ai_gateway.py`) :

- ``POST /v1/audio/transcriptions`` : multipart, parties ``request`` (JSON) PUIS
  ``file`` (l'ordre est imposé par le Gateway). ``request`` =
  ``{model, language: "auto", timestamps: "segment", diarize: true, mode: "async"}``.
  Le nom de fichier transmis est une CONSTANTE (``audio``) : jamais le nom utilisateur.
  Réponse : ``job_id`` / ``transcription_id``.
- ``GET /v1/audio/transcriptions/{id}?format=json&wait_ms=0`` : sans ``text`` = en cours ;
  avec ``text`` = résultat ; erreur HTTP portant ``error.code`` = job terminal.
- ``DELETE /v1/audio/transcriptions/{id}`` : libère le spool partagé du Gateway.

Aucune clé d'idempotence n'existe au dépôt : seuls ``resource_saturated`` (le Gateway
PROUVE n'avoir rien admis) et un échec de transport PROUVÉ antérieur à l'envoi (connexion
refusée, nom introuvable, délai de connexion) sont rejouables. Une réponse PERDUE (délai de
lecture, coupure après envoi) donne ``asr_submit_outcome_unknown``, jamais rejoué. Tout autre
échec de dépôt conclut en échec plutôt que de risquer un travail fantôme sur un GPU partagé
avec la production.

Jamais journalisé : le texte, les fragments, le corps d'une erreur. Seulement des codes.

AI Gateway Pool — le pool relaie la Gateway à l'identique mais
émet ses PROPRES refus (``X-AIGP-Error``, codes en majuscules, cf.
``apps.llm_router.gateway.pool_errors``). Ils sont rapportés sous ``pool_<code>`` et suivent
une règle unique : **seule la Gateway peut déclarer un job mort**. Un refus du pool sur une
relecture (``JOB_AFFINITY_UNKNOWN`` = job créé hors du pool, affinité expirée, mauvais
acheminement ; nœud indisponible…) n'est jamais terminal : il consomme le budget borné
d'erreurs de transport de l'orchestrateur. Un refus du pool sur un DÉPÔT n'est jamais
rejouable (l'affinité peut échouer APRÈS création du job par la Gateway). Un DELETE refusé
par le pool n'est pas une suppression confirmée.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Iterator, Optional

import requests
from django.conf import settings

from apps.llm_router.gateway.pool_errors import PoolErrorInfo, pool_error_from_response

logger = logging.getLogger(__name__)

SUBMIT_PATH = "/v1/audio/transcriptions"
RETRYABLE_SUBMIT_CODES = frozenset({"resource_saturated"})
TERMINAL_JOB_CODES = frozenset({
    "queue_timeout", "asr_timeout", "backend_restart", "transcription_failed", "invalid_audio",
    "ambiguous_audio_streams", "too_many_channels", "unsupported_media", "audio_too_large",
    "audio_too_long", "decoding_failed", "job_not_found", "job_expired", "job_cancelled",
    "model_not_registered", "model_not_allowed", "transcription_not_supported", "invalid_request",
    "field_requires_diarization",
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SPEAKER = re.compile(r"^speaker_0[0-3]$")
MAX_SPEAKERS = 4

#: Délais (secondes) : connexion, dépôt (le corps est transmis pendant ce délai), relecture, suppression.
CONNECT_TIMEOUT = 5
SUBMIT_TIMEOUT = 180
POLL_TIMEOUT = 30
DELETE_TIMEOUT = 30


class AsrError(Exception):
    """Échec ASR typé. ``code`` stable ; ``retryable`` = rejouer a du sens ; ``terminal`` = job mort."""

    def __init__(self, code: str, *, retryable: bool = False, terminal: bool = False, status: int | None = None,
                 retry_after: int | None = None, pool: PoolErrorInfo | None = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.terminal = terminal
        self.status = status
        self.retry_after = retry_after
        #: Refus PROPRE au pool (``None`` = réponse de la Gateway, directe ou relayée).
        self.pool = pool
        self.request_id = pool.request_id if pool is not None else None


@dataclass(frozen=True)
class AsrSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker: Optional[str] = None


@dataclass(frozen=True)
class AsrResult:
    job_id: str
    model_logical: str
    text: str
    media_sha256: str
    upload_bytes: int
    duration_ms: int
    language: str
    warnings: tuple = ()
    segments: tuple = ()
    diarized: bool = False
    speaker_count: Optional[int] = None


@dataclass(frozen=True)
class AsrHealth:
    environment: str
    allowed_here: bool
    model: str
    revision: str
    model_ready: bool
    raw: dict = field(default_factory=dict, repr=False)


class _MeasuredBody:
    """Itérable qui ANNONCE sa longueur : `requests` pose alors un Content-Length et
    streame les blocs (jamais de `Transfer-Encoding: chunked` superposé)."""

    def __init__(self, gen: Iterator[bytes], length: int):
        self._gen = gen
        self._length = length

    def __iter__(self):
        return self._gen

    def __len__(self):
        return self._length


def base_url() -> str:
    url = str(getattr(settings, "AI_GATEWAY_BASE_URL", "") or "").strip().rstrip("/")
    if not url:
        raise AsrError("asr_not_configured")
    return url


def _error_source(resp) -> dict:
    """Bloc d'erreur du Gateway : `error` (objet), sinon `detail` (objet — forme FastAPI
    rendue par l'ASR, constatée le 06/11/2026 : `{"detail": {"code": "resource_saturated",
    "retryable": true, ...}}`), sinon le corps lui-même. Jamais le texte du message."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    if not isinstance(body, dict):
        return {}
    for key in ("error", "detail"):
        if isinstance(body.get(key), dict):
            return body[key]
    return body


def _error_code(resp) -> str:
    src = _error_source(resp)
    val = src.get("code") or src.get("type") or (src.get("detail") if isinstance(src.get("detail"), str) else "")
    return val if isinstance(val, str) and _CODE.match(val) else ""


def _retryable_flag(resp) -> bool | None:
    """`retryable` publié par le Gateway (vrai, faux) ou `None` s'il ne dit rien."""
    val = _error_source(resp).get("retryable")
    return val if isinstance(val, bool) else None


def _retry_after(resp) -> int:
    """`Retry-After` borné [5, 300] s ; 30 s si absent ou illisible (convention un autre client interne)."""
    try:
        sec = int(float(str(resp.headers.get("Retry-After") or "").strip()))
    except (TypeError, ValueError, AttributeError):
        sec = 30
    return max(5, min(sec, 300))


def _nothing_sent(exc: BaseException) -> bool:
    """Vrai si l'échec de transport est PROUVÉ antérieur à tout envoi : connexion refusée,
    nom introuvable, délai de CONNEXION. Tout autre échec (délai de lecture, connexion coupée
    après envoi) laisse l'issue inconnue : la Gateway a pu admettre le job."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if not isinstance(exc, requests.ConnectionError):
        return False
    from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError

    cause = exc.args[0] if exc.args else None
    if isinstance(cause, MaxRetryError):
        cause = cause.reason
    return isinstance(cause, (NewConnectionError, ConnectTimeoutError))


def _request(method: str, path: str, *, timeout: int, idempotent: bool = True, **kw):
    try:
        return requests.request(method, base_url() + path, timeout=(CONNECT_TIMEOUT, timeout), **kw)
    except requests.RequestException as exc:
        if not idempotent and not _nothing_sent(exc):
            # Réponse PERDUE à un dépôt : elle ne prouve pas que
            # rien n'a été admis. Jamais rejouable — un second dépôt doublerait le travail sur un
            # GPU partagé ; un éventuel orphelin est purgé par la Gateway à expiration.
            raise AsrError("asr_submit_outcome_unknown", retryable=False) from exc
        if isinstance(exc, requests.Timeout):
            raise AsrError("asr_timeout_transport", retryable=True) from exc
        raise AsrError("asr_unavailable", retryable=True) from exc


def health() -> AsrHealth:
    """`/health/asr` : environnement, autorisation ici, modèle et révision observée."""
    resp = _request("GET", "/health/asr", timeout=POLL_TIMEOUT)
    if resp.status_code >= 400:
        raise AsrError("asr_unavailable", retryable=True, status=resp.status_code)
    try:
        data = resp.json()
    except ValueError as exc:
        raise AsrError("asr_protocol_error") from exc
    if not isinstance(data, dict):
        raise AsrError("asr_protocol_error")
    return AsrHealth(
        environment=str(data.get("environment") or ""),
        allowed_here=data.get("allowed_here") is True,
        model=str(data.get("model") or ""),
        revision=str(data.get("observed_revision") or data.get("expected_revision") or ""),
        model_ready=(data.get("checks") or {}).get("model_ready") is True if isinstance(data.get("checks"), dict) else False,
        raw={k: data.get(k) for k in ("status", "environment", "model", "artifact", "observed_revision")},
    )


def expected_environment() -> str:
    """Environnement Gateway attendu pour CE déploiement (`production` / `staging`), ou ''."""
    import os

    # Variable d'environnement du PROCESSUS (posée par le compose), pas un réglage Django.
    env = str(os.getenv("APP_ENV") or os.getenv("DJANGO_ENV") or "").strip().lower()
    return {"prod": "production", "production": "production", "staging": "staging"}.get(env, "")


def check_health(model_logical: str) -> AsrHealth:
    """Garde d'environnement (convention un autre client interne) : jamais un audio de prod vers le Gateway
    de staging, ni l'inverse ; modèle annoncé et autorisé ici."""
    h = health()
    expected = expected_environment()
    if expected and h.environment and h.environment != expected:
        raise AsrError("asr_environment_mismatch")
    if not h.allowed_here:
        raise AsrError("asr_not_allowed_here")
    if h.model and h.model != model_logical:
        raise AsrError("asr_model_mismatch")
    return h


def submit(path: str, *, size: int, model_logical: str, chunk_bytes: int = 1 << 20) -> str:
    """Dépose UNE partie préparée (fichier serveur). Rend l'identifiant du job."""
    if size <= 0:
        raise AsrError("audio_part_empty")
    boundary = f"----ragatm{secrets.token_hex(16)}"
    # `timestamps: "word"` (capacité `word_timestamps` déclarée par le Gateway, forme vérifiée le
    # 07/11/2026 : `segments[].words[{word, start_ms, end_ms}]`). ÉCART ASSUMÉ à la convention
    # un autre client interne (`segment`) : la VAD du Gateway rend des segments couvrant parfois des dizaines de
    # minutes de silence ; seuls les mots permettent une citation temporelle exploitable.
    req = json.dumps({"model": model_logical, "language": "auto", "timestamps": "word",
                      "diarize": True, "mode": "async"}, ensure_ascii=False).encode("utf-8")
    pre = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"request\"\r\n"
           "Content-Type: application/json\r\n\r\n").encode() + req + (
           f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio\"\r\n"
           "Content-Type: application/octet-stream\r\n\r\n").encode()
    post = f"\r\n--{boundary}--\r\n".encode()

    def body():
        yield pre
        sent = 0
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk_bytes)
                if not block:
                    break
                sent += len(block)
                if sent > size:
                    raise AsrError("audio_part_changed")
                yield block
        if sent != size:
            raise AsrError("audio_part_changed")
        yield post

    resp = _request("POST", SUBMIT_PATH, timeout=SUBMIT_TIMEOUT, idempotent=False,
                    data=_MeasuredBody(body(), len(pre) + size + len(post)),
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    if resp.status_code >= 400:
        pool = pool_error_from_response(resp)
        if pool is not None:
            # Refus du pool : JAMAIS rejouable au dépôt. `AFFINITY_STORE_UNAVAILABLE` et
            # `JOB_AFFINITY_CONFLICT` surviennent APRÈS l'acceptation du job par la Gateway
            # (compensation par DELETE ciblé, qui peut échouer) ; `UPSTREAM_*` ne prouve pas
            # que la Gateway n'a rien admis. L'identifiant éventuel d'un orphelin n'est pas lu.
            logger.warning("asr_submit_pool_refusal %s compensation=%s", pool.log_fields(),
                           pool.compensation or "-")
            raise AsrError(pool.normalized, retryable=False, status=resp.status_code, pool=pool)
        code = _error_code(resp) or f"http_{resp.status_code}"
        # Rejouable SEULEMENT si le Gateway prouve n'avoir rien admis (`resource_saturated`) et ne
        # dit pas le contraire (`retryable: false`) : aucune clé d'idempotence n'existe au dépôt.
        retryable = code in RETRYABLE_SUBMIT_CODES and _retryable_flag(resp) is not False
        raise AsrError(code, retryable=retryable, status=resp.status_code,
                       retry_after=_retry_after(resp) if retryable else None)
    try:
        data = resp.json()
    except ValueError as exc:
        raise AsrError("asr_protocol_error") from exc
    job = (data.get("job_id") or data.get("transcription_id")) if isinstance(data, dict) else None
    if not isinstance(job, str) or not job:
        raise AsrError("asr_protocol_error")
    return job


def fetch(job_id: str, *, model_logical: str) -> Optional[AsrResult]:
    """UNE relecture (`wait_ms=0`). ``None`` = en cours ; résultat validé ; ou `AsrError`."""
    resp = _request("GET", f"{SUBMIT_PATH}/{job_id}", timeout=POLL_TIMEOUT,
                    params={"format": "json", "wait_ms": 0})
    if resp.status_code >= 400:
        pool = pool_error_from_response(resp)
        if pool is not None:
            # Le pool ne sait rien de la VIE du job : il ne peut que refuser de l'acheminer.
            # Jamais terminal (cf. en-tête du module) ; l'orchestrateur borne les relectures.
            logger.warning("asr_poll_pool_refusal %s", pool.log_fields())
            raise AsrError(pool.normalized, terminal=False, retryable=True, status=resp.status_code,
                           pool=pool, retry_after=pool.retry_after_s)
        code = _error_code(resp) or f"http_{resp.status_code}"
        # Contrat Gateway DIRECT inchangé : 404/410 (ou code terminal publié) = job mort.
        terminal = code in TERMINAL_JOB_CODES or resp.status_code in (404, 410)
        raise AsrError(code, terminal=terminal, retryable=not terminal, status=resp.status_code)
    try:
        data = resp.json()
    except ValueError as exc:
        raise AsrError("asr_protocol_error", retryable=True) from exc
    if not isinstance(data, dict) or "text" not in data:
        return None
    return validate_result(data, model_logical=model_logical, expected_job_id=job_id)


def delete(job_id: str) -> bool:
    """Libère le job côté Gateway. Vrai si supprimé ou déjà absent ; jamais d'exception."""
    try:
        resp = _request("DELETE", f"{SUBMIT_PATH}/{job_id}", timeout=DELETE_TIMEOUT)
    except AsrError:
        return False
    if resp.status_code >= 400:
        pool = pool_error_from_response(resp)
        if pool is not None:
            # `404 JOB_AFFINITY_UNKNOWN` ne dit PAS que le job est absent de la Gateway :
            # le pool ne l'a simplement jamais acheminé. Pas de suppression confirmée.
            logger.warning("asr_delete_pool_refusal %s", pool.log_fields())
            return False
    return resp.status_code < 400 or resp.status_code in (404, 410)


def validate_result(data: dict, *, model_logical: str, expected_job_id: str) -> AsrResult:
    """Contrat du RÉSULTAT, FAIL CLOSED (identité, modèle, texte, empreinte média)."""
    tid = data.get("transcription_id")
    if not isinstance(tid, str) or tid != expected_job_id:
        raise AsrError("asr_protocol_error")
    echo = data.get("job_id")
    if echo is not None and echo != expected_job_id:
        raise AsrError("asr_protocol_error")
    if data.get("model_logical") != model_logical:
        raise AsrError("asr_model_mismatch")
    text = data.get("text")
    if not isinstance(text, str):
        raise AsrError("asr_protocol_error")
    media = data.get("media")
    if not isinstance(media, dict) or not isinstance(media.get("sha256"), str) or not _SHA256.match(media["sha256"]):
        raise AsrError("asr_protocol_error")
    warnings = tuple(w for w in (data.get("warnings") or []) if isinstance(w, str) and _CODE.match(w)) \
        if isinstance(data.get("warnings"), list) else ()
    diarized, speakers, segments = _segments(data)
    return AsrResult(
        job_id=expected_job_id, model_logical=model_logical, text=text,
        media_sha256=media["sha256"], upload_bytes=int(media.get("upload_bytes") or 0),
        duration_ms=int(media.get("duration_ms") or data.get("duration_ms") or 0),
        language=str(data.get("language") or ""), warnings=warnings,
        segments=segments, diarized=diarized, speaker_count=speakers,
    )


def _segments(data: dict) -> tuple:
    """(diarisé, nb locuteurs, segments). Segments STRICTS (start < end, texte non vide,
    ordonnés) ; un segment hors contrat les écarte TOUS (aucun horodatage inventé). Les
    libellés de locuteur ne sont gardés que si la diarisation est valide (≤ 4)."""
    raw = data.get("segments")
    if not isinstance(raw, list):
        return False, None, ()
    diarized = data.get("diarized") is True
    count = data.get("speaker_count")
    if not (isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= MAX_SPEAKERS):
        diarized = False
    out, last_start = [], -1
    for el in raw:
        if not isinstance(el, dict):
            return False, None, ()
        s, e, t = el.get("start_ms"), el.get("end_ms"), el.get("text")
        if not (isinstance(s, int) and isinstance(e, int) and not isinstance(s, bool) and 0 <= s < e):
            return False, None, ()
        if not isinstance(t, str) or not t.strip():
            continue  # un segment muet ne porte rien à indexer
        if s < last_start:
            return False, None, ()
        last_start = s
        sp = el.get("speaker")
        if not (diarized and isinstance(sp, str) and _SPEAKER.match(sp)):
            sp = None
        pieces = _split_on_words(el.get("words"), seg_start=s, seg_end=e)
        if pieces:
            for ps, pe, pt in pieces:
                out.append(AsrSegment(start_ms=ps, end_ms=pe, text=pt, speaker=sp))
        else:
            out.append(AsrSegment(start_ms=s, end_ms=e, text=t.strip(), speaker=sp))
    return diarized, (count if diarized else None), tuple(out)


#: Redécoupage d'un segment Gateway sur ses mots : nouvelle pièce après une pause ≥ 1,5 s
#: entre deux mots, ou quand la pièce dépasse 30 s. Les bornes sont celles des MOTS (aucune
#: interpolation) ; le texte est la suite exacte des mots rendus.
WORD_GAP_SPLIT_MS = 1500
PIECE_MAX_MS = 30_000


def _split_on_words(words, *, seg_start: int, seg_end: int) -> list:
    """Pièces `(start_ms, end_ms, texte)` d'un segment, ou ``[]`` si les mots sont absents ou
    hors contrat (entiers, start ≤ end, ordonnés, dans le segment ± 1 s) — on garde alors le
    segment tel quel, sans jamais inventer une précision que le moteur n'a pas donnée."""
    if not isinstance(words, list) or not words:
        return []
    clean, last = [], -1
    for w in words:
        if not isinstance(w, dict):
            return []
        ws, we, wt = w.get("start_ms"), w.get("end_ms"), w.get("word")
        if not (isinstance(ws, int) and isinstance(we, int) and not isinstance(ws, bool) and not isinstance(we, bool)
                and 0 <= ws <= we and ws >= last and seg_start - 1000 <= ws and we <= seg_end + 1000):
            return []
        if not isinstance(wt, str):
            return []
        last = ws
        if wt.strip():
            clean.append((ws, we, wt.strip()))
    if not clean:
        return []
    pieces, cur = [], [clean[0]]
    for prev, w in zip(clean, clean[1:]):
        if w[0] - prev[1] >= WORD_GAP_SPLIT_MS or w[1] - cur[0][0] > PIECE_MAX_MS:
            pieces.append(cur)
            cur = []
        cur.append(w)
    pieces.append(cur)
    out = []
    for p in pieces:
        start, end = p[0][0], max(x[1] for x in p)
        out.append((start, max(end, start + 1), " ".join(x[2] for x in p)))
    return out
