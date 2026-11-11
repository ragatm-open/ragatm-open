"""Inspection et préparation audio par ffprobe / ffmpeg.

Sécurité des sous-processus :
- arguments en LISTE, jamais de shell, jamais de concaténation ;
- chemins d'entrée et de sortie = chemins SERVEUR (stockage, spool), jamais le nom de
  fichier fourni par l'utilisateur ;
- `-nostdin`, délai borné par appel, processus tué au-delà ;
- seule la PREMIÈRE piste audio est utilisée (`-map 0:a:0`), la vidéo n'est jamais lue.

Préparation (version `PREP_VERSION`) : chaque partie est réencodée en FLAC 16 kHz mono
16 bits. C'est sans perte pour l'ASR (Whisper travaille à 16 kHz mono) et ramène
600 Mo de WAV à quelques dizaines de mégaoctets. Un enregistrement n'est DÉCOUPÉ que
s'il dépasse la durée d'une partie (`AUDIO_ASR_PART_MAX_SECONDS`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

#: Version de la préparation : une transcription n'est réutilisée que pour la même.
#: v2 : horodatage par MOT + coupe dans le silence qui chevauche la fenêtre.
#: Changer cette valeur invalide les transcriptions existantes à leur prochaine réindexation.
PREP_VERSION = "audio-prep/2:flac16k-mono:silence-cut:word-ts"

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"

#: Fenêtre (secondes) avant la limite d'une partie où une coupe sur silence est cherchée.
CUT_SEARCH_WINDOW_SECONDS = 600
#: Silence retenu comme point de coupe : niveau et durée minimale.
SILENCE_NOISE_DB = -35
SILENCE_MIN_SECONDS = 0.5


class AudioMediaError(Exception):
    """Échec typé de l'inspection ou de la préparation (code stable, sans contenu)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int
    audio_streams: int
    video_streams: int
    codec: str
    channels: int
    sample_rate: int
    format_name: str


@dataclass
class PartPlan:
    index: int
    start_ms: int
    end_ms: int
    cut: str = "end"  # end | silence | hard


@dataclass
class Silence:
    start: float
    end: float

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2.0


def tools_available() -> bool:
    return bool(shutil.which(FFPROBE) and shutil.which(FFMPEG))


def _abs(path: str) -> str:
    """Chemin SERVEUR absolu : jamais un argument qui pourrait passer pour une option."""
    p = os.fspath(path)
    if not os.path.isabs(p):
        raise AudioMediaError("audio_internal_error", "chemin non absolu")
    return p


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AudioMediaError("audio_dependency_unavailable", "ffmpeg/ffprobe absent") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioMediaError("audio_preparation_timeout", f"{args[0]} > {timeout}s") from exc


def inspect(path: str, *, timeout: int = 120) -> MediaInfo:
    """Inspecte le conteneur. Lève `undecodable_audio`, `no_audio_track`, `audio_contains_video`."""
    proc = _run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", _abs(path)],
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AudioMediaError("undecodable_audio", "ffprobe a refusé le fichier")
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise AudioMediaError("undecodable_audio", "sortie ffprobe illisible") from exc
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []
    audio = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    # Une pochette (`attached_pic`) n'est pas une vidéo : courante dans les mp3/m4a.
    video = [
        s for s in streams
        if isinstance(s, dict) and s.get("codec_type") == "video"
        and not (isinstance(s.get("disposition"), dict) and s["disposition"].get("attached_pic") == 1)
    ]
    if not audio:
        raise AudioMediaError("no_audio_track", "aucune piste audio")
    if video:
        raise AudioMediaError("audio_contains_video", "piste vidéo présente")
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    duration = _float(fmt.get("duration")) or _float(audio[0].get("duration"))
    if not duration or duration <= 0:
        raise AudioMediaError("undecodable_audio", "durée inconnue")
    return MediaInfo(
        duration_ms=int(round(duration * 1000)),
        audio_streams=len(audio),
        video_streams=0,
        codec=str(audio[0].get("codec_name") or ""),
        channels=int(audio[0].get("channels") or 0),
        sample_rate=int(_float(audio[0].get("sample_rate")) or 0),
        format_name=str(fmt.get("format_name") or ""),
    )


def _float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_SIL_START = re.compile(rb"silence_start:\s*(-?[0-9.]+)")
_SIL_END = re.compile(rb"silence_end:\s*(-?[0-9.]+)")


def detect_silences(path: str, *, duration_ms: int) -> list[Silence]:
    """Silences de l'enregistrement (filtre `silencedetect`). Décodage seul, aucune sortie."""
    timeout = max(120, int(duration_ms / 1000 / 10))  # décodage ≫ 10× temps réel
    proc = _run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "info", "-i", _abs(path), "-map", "0:a:0", "-vn",
         "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_SECONDS}", "-f", "null", "-"],
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AudioMediaError("undecodable_audio", "décodage interrompu")
    return parse_silences(proc.stderr)


def parse_silences(stderr: bytes) -> list[Silence]:
    out: list[Silence] = []
    start: Optional[float] = None
    for line in (stderr or b"").splitlines():
        m = _SIL_START.search(line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = _SIL_END.search(line)
        if m and start is not None:
            end = float(m.group(1))
            if end > start:
                out.append(Silence(start, end))
            start = None
    return out


def plan_parts(duration_ms: int, *, part_max_seconds: int, silences: list[Silence] | None = None) -> list[PartPlan]:
    """Découpe DÉTERMINISTE en parties contiguës, sans recouvrement.

    Une seule partie si la durée tient dans `part_max_seconds` (pas de découpe inutile).
    Sinon, chaque coupe se place au milieu du PLUS LONG silence trouvé dans la fenêtre
    `[limite - 10 min, limite]` ; à défaut, coupe franche à la limite (un mot peut alors
    être tranché : rare, tracé `cut=hard`).
    """
    total = duration_ms / 1000.0
    part_max = float(part_max_seconds)
    if total <= part_max:
        return [PartPlan(index=0, start_ms=0, end_ms=duration_ms, cut="end")]
    silences = sorted(silences or [], key=lambda s: s.start)
    parts: list[PartPlan] = []
    start = 0.0
    idx = 0
    while total - start > part_max:
        limit = start + part_max
        lo = limit - min(CUT_SEARCH_WINDOW_SECONDS, part_max / 2)
        # Silences qui CHEVAUCHENT la fenêtre (pas seulement ceux dont le milieu y tombe : un long
        # silence débordant la limite est le meilleur point de coupe) ; coupe au milieu de la partie
        # du silence contenue dans la fenêtre.
        overlaps = [(max(s.start, lo), min(s.end, limit)) for s in silences if s.end > lo and s.start < limit]
        overlaps = [o for o in overlaps if o[1] > o[0]]
        if overlaps:
            a, b = max(overlaps, key=lambda o: (o[1] - o[0], -o[0]))
            cut, kind = (a + b) / 2.0, "silence"
        else:
            cut, kind = limit, "hard"
        parts.append(PartPlan(index=idx, start_ms=int(round(start * 1000)), end_ms=int(round(cut * 1000)), cut=kind))
        start = cut
        idx += 1
    parts.append(PartPlan(index=idx, start_ms=int(round(start * 1000)), end_ms=duration_ms, cut="end"))
    return parts


def transcode_part(src: str, out: str, *, start_ms: int, end_ms: int, timeout: int | None = None) -> int:
    """Réencode [start, end[ de la 1re piste audio en FLAC 16 kHz mono. Rend la taille."""
    dur = max(0.001, (end_ms - start_ms) / 1000.0)
    timeout = timeout or max(120, int(dur / 5))
    tmp = out + ".partial"
    proc = _run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{start_ms / 1000.0:.3f}", "-t", f"{dur:.3f}", "-i", _abs(src),
         "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
         "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-c:a", "flac", "-f", "flac", _abs(tmp)],
        timeout=timeout,
    )
    if proc.returncode != 0 or not os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise AudioMediaError("undecodable_audio", "réencodage impossible")
    os.replace(tmp, out)
    return os.path.getsize(out)


def free_bytes(path: str) -> int:
    try:
        st = os.statvfs(path)
        return int(st.f_bavail * st.f_frsize)
    except OSError:
        return 0
