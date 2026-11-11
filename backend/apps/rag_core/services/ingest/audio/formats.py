"""Formats audio admis et reconnaissance par SIGNATURE du conteneur.

Doctrine du dépôt : on n'ACCEPTE jamais sur l'extension seule. Un fichier audio n'est
admis que si son extension appartient à la liste fermée ET que ses premiers octets
portent la signature du conteneur attendu. Les signatures reprennent celles que
un autre client interne valide en production (`attachment_inspection.py`).

La signature prouve le CONTENEUR, pas le contenu : décodabilité, présence d'une piste
audio, absence de vidéo et durée sont vérifiées par ffprobe sur le worker audio,
avant toute transcription (`media.inspect`).
"""
from __future__ import annotations

from typing import Optional

#: Sous-formats V1. `.opus` nu et la vidéo (`.mp4`, `.mov`, `.mkv`) sont HORS périmètre.
AUDIO_SUFFIXES: dict[str, str] = {
    ".mp3": "mp3",
    ".m4a": "m4a",
    ".wav": "wav",
    ".flac": "flac",
    ".ogg": "ogg",
    ".webm": "webm",
}

#: MIME imposé par le format CONSTATÉ, jamais celui du client.
AUDIO_MIMES: dict[str, str] = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "webm": "audio/webm",
}

#: Octets lus pour la reconnaissance (en-tête ID3 + première trame MPEG comprise).
HEAD_BYTES = 64 * 1024

#: Marques ISO-BMFF d'un conteneur audio m4a. Une marque vidéo (`qt  `, `mp41`…) reste
#: admise par la signature : la piste vidéo éventuelle est refusée par ffprobe.
_M4A_BRANDS = {b"M4A ", b"M4B ", b"mp42", b"isom", b"iso2", b"mp41", b"dash", b"M4V "}
#: Marques MAJEURES 3GPP/3GPP2 : conteneur ISO-BMFF des enregistreurs
#: Android (`ftyp3gp4`, `ftyp3g2a`…), audio AAC compris. Même garde que ci-dessus : une piste vidéo
#: est refusée par ffprobe (`audio_contains_video`).
_3GPP_MAJOR_BRANDS = {b"3gp4", b"3gp5", b"3gp6", b"3gp7", b"3gp8", b"3gp9", b"3g2a", b"3g2b", b"3g2c"}
#: Marques majeures JAMAIS admises, même si elles déclarent une marque compatible ISO : QuickTime.
_REFUSED_MAJOR_BRANDS = {b"qt  "}
#: Nombre maximal de marques compatibles lues (borne l'analyse d'un en-tête hostile).
_MAX_COMPATIBLE_BRANDS = 32


def _ftyp_brands(head: bytes) -> tuple[bytes, tuple[bytes, ...]] | None:
    """(marque majeure, marques compatibles) de la boîte `ftyp` initiale, ou ``None``.

    Structure ISO/IEC 14496-12 : taille (u32 BE), type `ftyp`, marque majeure, version mineure,
    puis marques compatibles de 4 octets. Une taille incohérente invalide la boîte."""
    if len(head) < 16 or head[4:8] != b"ftyp":
        return None
    size = int.from_bytes(head[:4], "big")
    if size < 16 or (size - 16) % 4:
        return None
    end = min(size, len(head), 16 + 4 * _MAX_COMPATIBLE_BRANDS)
    compatible = tuple(head[i:i + 4] for i in range(16, end - 3, 4))       # marques entières seulement
    return head[8:12], compatible


def _is_m4a_container(head: bytes) -> bool:
    """Conteneur ISO-BMFF audio : marque majeure connue, ou marque majeure non refusée
    déclarant une marque compatible audio (`isom`, `M4A `, `mp42`…)."""
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _M4A_BRANDS:
        return True                                  # chemin historique, inchangé
    brands = _ftyp_brands(head)
    if brands is None:
        return False
    major, compatible = brands
    if major in _REFUSED_MAJOR_BRANDS:
        return False
    return major in _3GPP_MAJOR_BRANDS or any(b in _M4A_BRANDS for b in compatible)


def describe_signature(head: bytes) -> str:
    """Description NON SENSIBLE de l'en-tête pour le journal d'un refus (aucun contenu audio)."""
    head = head or b""
    brands = _ftyp_brands(head)
    if brands is not None:
        major, compatible = brands
        # ASCII IMPRIMABLE seulement : un en-tête hostile ne peut pas injecter de ligne de journal.
        fmt = lambda b: "".join(chr(c) if 32 <= c < 127 else "?" for c in b).strip() or "?"  # noqa: E731
        return f"iso-bmff major={fmt(major)} compatible={','.join(fmt(b) for b in compatible[:8])}"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
        return "adts-aac (flux AAC brut, sans conteneur m4a)"
    return f"unknown head={head[:8].hex()}"


def audio_suffix(filename: str | None) -> Optional[str]:
    """Sous-format désigné par l'EXTENSION (ou ``None``). Ne prouve rien à lui seul."""
    name = str(filename or "").strip().lower()
    for suffix, fmt in AUDIO_SUFFIXES.items():
        if name.endswith(suffix):
            return fmt
    return None


def is_audio_filename(filename: str | None) -> bool:
    return audio_suffix(filename) is not None


def _mpeg_frame_sync(head: bytes) -> bool:
    # Synchronisation de trame MPEG audio : 11 bits à 1 (0xFFE…), couche ≠ réservée.
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0 and (head[1] & 0x06) != 0


def _skip_id3(head: bytes) -> bytes:
    if head[:3] != b"ID3" or len(head) < 10:
        return head
    size = (head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F)
    return head[10 + size:]


def container_signature(head: bytes) -> Optional[str]:
    """Sous-format prouvé par les premiers octets, ou ``None``."""
    head = head or b""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        # EBML : Matroska OU WebM ; le DocType est porté dans l'en-tête.
        return "webm" if b"webm" in head[:64] else None
    if _is_m4a_container(head):
        return "m4a"
    if head[:3] == b"ID3":
        rest = _skip_id3(head)
        return "mp3" if (not rest or _mpeg_frame_sync(rest)) else None
    if _mpeg_frame_sync(head):
        return "mp3"
    return None


def sniff_audio(head: bytes, filename: str | None) -> Optional[str]:
    """Sous-format si l'extension ET la signature concordent, sinon ``None``.

    Un `.wav` qui porte une signature FLAC n'est pas « un peu audio » : il est refusé,
    comme tout fichier dont le nom et le contenu se contredisent.
    """
    by_name = audio_suffix(filename)
    if by_name is None:
        return None
    by_content = container_signature(head)
    return by_name if by_content == by_name else None
