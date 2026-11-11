"""reconnaissance du conteneur m4a (ISO-BMFF) sur des en-têtes RÉELS.

Les 40 premiers octets proviennent de fichiers produits par ffmpeg (image worker_audio, AAC 1 s) :
`-f ipod` (M4A ), `-f mp4` (isom), `-brand mp42`, MP4 fragmenté, ALAC, `-f 3gp` (3gp4,
enregistreurs Android), `-f 3g2` (3g2a). Refus conservés : QuickTime (`qt  `), Smooth Streaming
(`isml`/`piff`), flux AAC brut ADTS nommé `.m4a`, boîte `ftyp` incohérente, extension vidéo.
La piste vidéo éventuelle d'un conteneur admis reste refusée par ffprobe (`audio_contains_video`)."""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.audio import formats as F

H = {k: bytes.fromhex(v) for k, v in {
    "ipod": "0000001c667479704d344120000002004d34412069736f6d69736f32000000086672656500002102",
    "mp4": "0000001c6674797069736f6d0000020069736f6d69736f326d703431000000086672656500002102",
    "mp42": "0000001c667479706d703432000002006d70343269736f326d703431000000086672656500002325",
    "frag": "000000206674797069736f6d0000020069736f6d69736f3669736f326d703431000002bd6d6f6f76",
    "alac": "0000001c667479704d344120000002004d34412069736f6d69736f320000000866726565000044c6",
    "3gp": "0000001c6674797033677034000002003367703469736f6d69736f32000000086672656500002102",
    "3g2": "0000001c6674797033673261000100003367326169736f6d69736f32000000086672656500002102",
    "mov": "00000014667479707174202000000200717420200000000877696465000021026d646174de02004c",
    "ismv": "000000186674797069736d6c0000020069736d6c70696666000002c16d6f6f760000006c6d766864",
    "adts": "fff1504023fffcde02004c61766335392e33372e313030000230ab5ce90c420ead4dce6f89ae9b83",
}.items()}


@pytest.mark.parametrize("kind", ["ipod", "mp4", "mp42", "frag", "alac", "3gp", "3g2"])
def test_m4a_reels_admis(kind):
    assert F.sniff_audio(H[kind], "1.ca-meeting intro-governance.m4a") == "m4a"


@pytest.mark.parametrize("kind", ["3gp", "3g2"])
def test_3gpp_etait_refuse_avant_le_correctif(kind):
    """Cause du défaut : la marque MAJEURE 3GPP n'appartenait pas à la liste historique."""
    assert H[kind][8:12] not in F._M4A_BRANDS
    assert F._ftyp_brands(H[kind])[1][1:] == (b"isom", b"iso2")


@pytest.mark.parametrize("kind,reason", [
    ("mov", "QuickTime refusé même s'il déclare une marque compatible"),
    ("ismv", "Smooth Streaming : aucune marque compatible audio"),
    ("adts", "flux AAC brut : le nom et le contenu se contredisent"),
])
def test_conteneurs_non_m4a_refuses(kind, reason):
    assert F.sniff_audio(H[kind], "a.m4a") is None, reason


@pytest.mark.parametrize("head", [
    b"\x00\x00\x00\x0cftyp3gp4",                                   # trop court pour une boîte ftyp
    b"\x00\x00\x00\x08ftyp3gp4\x00\x00\x02\x00isom" + b"\x00" * 20,  # taille < 16
    b"\x00\x00\x00\x1aftypabcd\x00\x00\x02\x00isom" + b"\x00" * 20,  # taille non alignée sur 4
    b"\x00\x00\x00\x18ftypabcd\x00\x00\x02\x00wxyz" + b"\x00" * 20,  # marque inconnue, rien de compatible
    b"\x00\x00\x00\x18moovftyp3gp4\x00\x00\x02\x00" + b"\x00" * 20,  # ftyp absente en tête
])
def test_ftyp_incoherente_ou_inconnue_refusee(head):
    assert F.sniff_audio(head, "a.m4a") is None


@pytest.mark.parametrize("name", ["a.mp4", "a.3gp", "a.mov", "a.m4v", "a.txt"])
def test_extension_non_audio_refusee_meme_avec_conteneur_valide(name):
    assert F.sniff_audio(H["3gp"], name) is None


def test_extension_et_signature_doivent_concorder():
    assert F.sniff_audio(H["3gp"], "a.mp3") is None and F.sniff_audio(H["3gp"], "a.wav") is None


def test_marques_compatibles_bornees():
    big = (16 + 4 * 1000).to_bytes(4, "big") + b"ftypabcd\x00\x00\x00\x00" + b"wxyz" * 1000
    major, compatible = F._ftyp_brands(big)
    assert major == b"abcd" and len(compatible) == F._MAX_COMPATIBLE_BRANDS


def test_diagnostic_non_sensible():
    assert F.describe_signature(H["3gp"]) == "iso-bmff major=3gp4 compatible=3gp4,isom,iso2"
    assert F.describe_signature(H["adts"]).startswith("adts-aac")
    assert F.describe_signature(b"%PDF-1.7\nxx") == "unknown head=255044462d312e37"


def test_diagnostic_sans_injection_de_ligne():
    hostile = b"\x00\x00\x00\x18ftyp\nINJ\x00\x00\x00\x00\r\n\x1b[" + b"\x00" * 20
    d = F.describe_signature(hostile)
    assert d.isprintable() and "\n" not in d and "\r" not in d and "\x1b" not in d
    assert F.sniff_audio(hostile, "a.m4a") is None
