# backend/apps/rag_core/services/ingest/extraction/mime.py
from __future__ import annotations

"""
Détection fiable du type RÉEL d'un fichier, par signature/contenu.

Principe de sécurité : on ne fait JAMAIS confiance à l'extension ni au
Content-Type envoyé par le client. Ils ne servent qu'à départager des formats
texte ambigus (markdown vs csv vs txt), jamais à autoriser un binaire.
"""

import io
import zipfile
from typing import Callable, Optional

from .base import ExtractionContext

# MIME canoniques (valeurs renvoyées par detect_mime)
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCM = "application/vnd.ms-word.document.macroEnabled.12"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSM = "application/vnd.ms-excel.sheet.macroEnabled.12"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PPTM = "application/vnd.ms-powerpoint.presentation.macroEnabled.12"
ODS = "application/vnd.oasis.opendocument.spreadsheet"
ODP = "application/vnd.oasis.opendocument.presentation"
ODT = "application/vnd.oasis.opendocument.text"
# Conteneur OLE2/CFB : famille Office binaire HÉRITÉE (.xls/.doc/.ppt). La signature
# est commune à tous ces formats : on ne prétend PAS distinguer lequel — SAUF le
# message Outlook, identifié par ses flux MAPI (cf. `_detect_ole2`).
LEGACY_OFFICE = "application/x-ole-storage"
# Message Outlook (.msg) : conteneur CFB porteur de flux MAPI. MIME canonique retenu
# côté Microsoft ; `application/x-msg` et `application/octet-stream` circulent aussi
# côté client, mais le Content-Type client n'autorise jamais rien (cf. docstring).
OUTLOOK_MSG = "application/vnd.ms-outlook"
ZIP = "application/zip"
HTML = "text/html"
CSV = "text/csv"
MARKDOWN = "text/markdown"
PLAIN = "text/plain"
PNG = "image/png"
JPEG = "image/jpeg"
GIF = "image/gif"
TIFF = "image/tiff"
BMP = "image/bmp"
WEBP = "image/webp"
OCTET = "application/octet-stream"
EXECUTABLE = "application/x-executable"
# XML générique `.xml` : classé par le détecteur structuré
# `xml_signature.detect_xml_signature` (extension `.xml` + signature), jamais par le
# Content-Type client.
XML = "application/xml"
# DXF `.dxf` : classé par `dxf_signature.detect_dxf_signature`
# (extension `.dxf` + en-tête binaire ou paires `0/SECTION`) — jamais par le Content-Type
# client (`image/vnd.dxf` ne route jamais vers le pipeline image).
DXF = "application/dxf"
# IFC `.ifc` : classé par `ifc_signature.detect_ifc_signature`
# (extension `.ifc` + en-tête STEP `ISO-10303-21;`). Aucun type IANA pour IFC ;
# `application/x-step` est la valeur observée en pratique — dans RagATM elle signifie
# « IFC » puisqu'un STEP générique n'est jamais classé.
IFC = "application/x-step"
# DWG `.dwg` : classé par `dwg_signature.detect_dwg_signature`
# (extension `.dwg` + en-tête `ACxxxx` connu suivi des cinq octets nuls). Aucun type
# IANA ; `application/acad` est la valeur historique d'Autodesk. `image/vnd.dwg`,
# valeur déclarée par certains clients, n'est JAMAIS produite : un dessin n'entre
# jamais dans le pipeline image/OCR.
DWG = "application/acad"
# Courriel MIME `.eml` : classé par `eml_signature.detect_eml_signature`
# (extension `.eml` + bloc d'en-têtes RFC 5322), jamais par le Content-Type client.
EML = "message/rfc822"
# Sentinelle : un détecteur STRUCTURÉ a levé ou renvoyé une valeur
# invalide. `detect_mime` ne lève jamais ; c'est `validation.validate_and_classify` qui
# transforme cette valeur en refus (`structured_detection_failed`) — échec FERMÉ, jamais
# un repli silencieux vers `text/plain`.
STRUCTURED_DETECTION_FAILED = "application/x-structured-detection-failed"

IMAGE_MIMES = {PNG, JPEG, GIF, TIFF, BMP, WEBP}


# ---------------------------------------------------------------------------
# Registre INTERNE de détecteurs de signatures STRUCTURÉES
# ---------------------------------------------------------------------------
# Un détecteur reçoit (data, ctx) et renvoie un MIME canonique (str) ou None. Il est
# évalué APRÈS les signatures fortes (exécutable, image, OLE2, PDF, famille ZIP) et
# AVANT les replis génériques (octet-stream, texte). Il ne peut donc JAMAIS surclasser
# un binaire reconnu. Le registre est VIDE dans ce lot : les signatures réelles
# (`<?xml`, `ISO-10303-21`, DXF…) sont enregistrées par les lots de format, jamais ici.
#
# Ordre déterministe : (priorité croissante, nom croissant) ; le PREMIER détecteur qui
# renvoie un MIME gagne. Un détecteur qui lève, ou renvoie autre chose qu'une chaîne
# non vide, fait échouer la détection FERMÉE (`STRUCTURED_DETECTION_FAILED`).
StructuredDetector = Callable[[bytes, ExtractionContext], Optional[str]]
_STRUCTURED_DETECTORS: list[tuple[int, str, StructuredDetector]] = []


def register_structured_detector(name: str, fn: StructuredDetector, *, priority: int = 100) -> None:
    """Enregistre un détecteur structuré sous un nom UNIQUE (registre interne)."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("structured detector name must be a non-empty string")
    if not callable(fn):
        raise ValueError("structured detector must be callable")
    if any(n == name for _, n, _ in _STRUCTURED_DETECTORS):
        raise ValueError(f"structured detector already registered: {name!r}")
    _STRUCTURED_DETECTORS.append((int(priority), name, fn))
    _STRUCTURED_DETECTORS.sort(key=lambda t: (t[0], t[1]))


def unregister_structured_detector(name: str) -> bool:
    """Retire un détecteur par nom ; True s'il existait."""
    before = len(_STRUCTURED_DETECTORS)
    _STRUCTURED_DETECTORS[:] = [t for t in _STRUCTURED_DETECTORS if t[1] != name]
    return len(_STRUCTURED_DETECTORS) != before


def _clear_structured_detectors() -> None:
    """Vide le registre — usage TESTS uniquement."""
    _STRUCTURED_DETECTORS.clear()


def structured_detectors() -> tuple[tuple[int, str], ...]:
    """Vue en lecture seule : ``((priorité, nom), …)`` dans l'ordre d'évaluation."""
    return tuple((p, n) for p, n, _ in _STRUCTURED_DETECTORS)


def _run_structured_detectors(data: bytes, ctx: ExtractionContext) -> Optional[str]:
    """Premier MIME renvoyé par un détecteur, None si aucun, sentinelle si l'un lève."""
    if not _STRUCTURED_DETECTORS:
        return None
    import logging

    logger = logging.getLogger(__name__)
    for _, name, fn in _STRUCTURED_DETECTORS:
        try:
            out = fn(data, ctx)
        except Exception:
            # Fermé : on ne sait pas ce qu'est ce fichier, on ne le laisse pas passer
            # pour du texte. Le détail de l'exception reste dans les journaux serveur.
            logger.exception("structured detector %r failed (fail-closed)", name)
            return STRUCTURED_DETECTION_FAILED
        if out is None:
            continue
        if not isinstance(out, str) or not out.strip():
            logger.error("structured detector %r returned an invalid value %r (fail-closed)", name, out)
            return STRUCTURED_DETECTION_FAILED
        return out
    return None


def _has_nul(sample: bytes) -> bool:
    return b"\x00" in sample


def looks_like_text(data: bytes, *, sample: int = 4096) -> bool:
    """Heuristique "est-ce du texte ?" : pas d'octet NUL, peu de caractères de
    contrôle non imprimables sur l'échantillon de tête."""
    if not data:
        return True
    chunk = data[:sample]
    if _has_nul(chunk):
        return False
    # caractères de contrôle hors \t \n \r
    ctrl = sum(1 for b in chunk if b < 0x09 or (0x0E <= b < 0x20))
    return ctrl <= max(1, len(chunk) // 100)


def _detect_executable(head: bytes) -> Optional[str]:
    if head.startswith(b"\x7fELF"):       # ELF (Linux)
        return EXECUTABLE
    if head.startswith(b"MZ"):            # PE/DOS (Windows .exe/.dll)
        return EXECUTABLE
    if head[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):  # Mach-O
        return EXECUTABLE
    if head.startswith(b"#!"):            # script shebang
        return EXECUTABLE
    if head[:4] == b"\xca\xfe\xba\xbe":   # Java class / Mach-O fat
        return EXECUTABLE
    return None


# Signature du conteneur OLE2 / Compound File Binary : famille Office binaire héritée
# (.xls, .doc, .ppt, .msg). Elle NE PERMET PAS de dire lequel — d'où un type unique.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# Noms de flux MAPI, tels qu'ils apparaissent dans le répertoire CFB : les entrées de
# répertoire stockent leur nom en UTF-16LE. Ces flux sont propres au format message
# Outlook — un .xls/.doc/.ppt n'en contient aucun (il porte `Workbook`, `WordDocument`,
# `PowerPoint Document`). On EXIGE deux marqueurs distincts : un fichier quelconque
# pourrait contenir fortuitement une de ces chaînes, deux est hors de portée du hasard.
_MSG_MAPI_MARKERS = (
    "__properties_version1.0".encode("utf-16-le"),
    "__substg1.0_".encode("utf-16-le"),
    "__recip_version1.0".encode("utf-16-le"),
    "__nameid_version1.0".encode("utf-16-le"),
)
_MSG_MIN_MARKERS = 2

# Borne de lecture pour le sniffing MAPI. Sur le corpus de référence (7 messages
# Outlook réels, 0,9 à 2,5 Mo), les quatre marqueurs se situent entre les offsets 640
# et 2816 : le répertoire CFB est écrit en tête. On lit large (1 Mio) pour absorber les
# variantes d'écriture sans jamais parcourir un fichier de 25 Mo à chaque détection.
_MSG_SNIFF_BYTES = 1_048_576


def _looks_like_outlook_msg(data: bytes) -> bool:
    """Un conteneur CFB porte-t-il des flux MAPI ? (inspection d'octets, jamais de parsing)

    On ne DÉCODE pas le répertoire CFB : on cherche les noms de flux MAPI encodés en
    UTF-16LE dans la zone d'en-tête. C'est volontairement plus grossier qu'un parsing
    de FAT — et donc incapable de faire détoner quoi que ce soit. Ne lève jamais : la
    détection doit rester totale, le refus se décide dans `validation`.
    """
    window = data[:_MSG_SNIFF_BYTES]
    found = sum(1 for marker in _MSG_MAPI_MARKERS if marker in window)
    return found >= _MSG_MIN_MARKERS


def _detect_ole2(data: bytes) -> Optional[str]:
    """Discrimine le message Outlook du reste de la famille Office binaire.

    Reçoit les octets COMPLETS (et non `head`) : la distinction se joue dans le
    répertoire CFB, pas dans les 8 octets de signature. Même esprit que
    `_detect_zip_family`, qui exige la partie canonique d'un OOXML avant de trancher.
    """
    if not data.startswith(_OLE2_MAGIC):
        return None
    return OUTLOOK_MSG if _looks_like_outlook_msg(data) else LEGACY_OFFICE


def _detect_image(head: bytes) -> Optional[str]:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return GIF
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return TIFF
    if head.startswith(b"BM"):
        return BMP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP
    return None


# Bornes de lecture pour les petites parties inspectées à la DÉTECTION (anti zip-bomb :
# on ne décompresse jamais une entrée dont la taille déclarée dépasse ces seuils).
_MAX_CONTENT_TYPES_BYTES = 1_048_576   # [Content_Types].xml : quelques Ko en pratique
_MAX_ODF_MIMETYPE_BYTES = 256          # entrée `mimetype` d'OpenDocument : ~50 octets


def _has_macro_marker(names: set, content_types: bytes) -> bool:
    """Archive OOXML macro-enabled ? Décision par CONTENU uniquement.

    Deux signaux, historiquement utilisés pour DOCM et désormais partagés avec Excel
    et PowerPoint :
    - un binaire VBA embarqué (`*/vbaProject.bin`) ;
    - un Content-Type `…macroEnabled…` déclaré dans `[Content_Types].xml`
      (ex. `application/vnd.ms-excel.sheet.macroEnabled.main+xml`).
    """
    if any("vbaProject" in n for n in names):
        return True
    return b"macroenabled" in content_types.lower()


def _detect_zip_family(data: bytes) -> str:
    """Inspecte un conteneur ZIP (sans extraction sur disque) pour distinguer
    docx / docm / xlsx / xlsm / pptx / pptm / ods / zip générique.

    Ne lève jamais : la détection doit rester totale (le refus est décidé plus haut,
    dans `validation`). On ne lit que le répertoire central + deux petites parties
    bornées ; aucun XML n'est PARSÉ ici (simple recherche d'octets), donc aucune
    bombe à entités ne peut détoner à ce stade.
    """
    from .zip_security import read_bounded_entry

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            content_types = read_bounded_entry(
                zf, "[Content_Types].xml", max_bytes=_MAX_CONTENT_TYPES_BYTES
            )
            odf_mimetype = read_bounded_entry(
                zf, "mimetype", max_bytes=_MAX_ODF_MIMETYPE_BYTES
            ).strip()
    except Exception:
        return ZIP

    # 1) OpenDocument : l'entrée `mimetype` est normative et porte le type exact.
    #    Sans entrée dédiée, un document OpenDocument retombait en ZIP générique et
    #    l'utilisateur lisait « envoyez les fichiers individuellement » — une consigne
    #    qui n'a aucun sens pour un texte, un classeur ou une présentation. Les trois
    #    familles sont donc nommées ici, MÊME quand l'extraction n'est pas supportée :
    #    nommer le format est ce qui rend le refus compréhensible.
    if odf_mimetype == ODS.encode("ascii"):
        return ODS
    if odf_mimetype == ODP.encode("ascii"):
        return ODP
    if odf_mimetype == ODT.encode("ascii"):
        return ODT

    has_macro = _has_macro_marker(names, content_types)

    # 2) WordprocessingML — comportement historique inchangé.
    if any(n.startswith("word/") for n in names) or "word/document.xml" in names:
        return DOCM if has_macro else DOCX

    # 3) SpreadsheetML — on exige la partie CANONIQUE `xl/workbook.xml` : un ZIP
    #    arbitraire contenant un dossier `xl/` incomplet n'est pas un classeur.
    if "xl/workbook.xml" in names:
        return XLSM if has_macro else XLSX

    # 4) PresentationML — on exige la partie CANONIQUE `ppt/presentation.xml`, comme
    #    pour `xl/workbook.xml` : un ZIP arbitraire portant un dossier `ppt/` incomplet
    #    (des médias sans présentation, p. ex.) n'est pas une présentation, et ne doit
    #    surtout pas être routé vers l'extracteur PPTX.
    #
    #    `has_macro` est consulté ICI comme pour Word et Excel. Tant que PPTX était
    #    refusé en bloc, l'omettre était sans effet visible ; avec la porte ouverte
    #    un `.pptm` renommé `.pptx` serait devenu indétectable et aurait
    #    contourné le refus des macros appliqué à `.docm`/`.xlsm`.
    if "ppt/presentation.xml" in names:
        return PPTM if has_macro else PPTX

    return ZIP


def _looks_like_html(text_head: str) -> bool:
    low = text_head.lstrip().lower()
    if low.startswith("<!doctype html") or low.startswith("<html"):
        return True
    # balises structurantes fréquentes en tête
    for tag in ("<html", "<head", "<body", "<!doctype html"):
        if tag in low[:2048]:
            return True
    return False


def detect_mime(data: bytes, ctx: ExtractionContext) -> str:
    """Renvoie le MIME canonique RÉEL du fichier (best-effort, jamais d'exception)."""
    data = data or b""
    head = data[:64]

    # 1) Binaires à signature forte
    exe = _detect_executable(head)
    if exe:
        return exe

    img = _detect_image(head)
    if img:
        return img

    # `data` complet (et non `head`) : distinguer un .msg d'un .xls/.doc/.ppt suppose
    # d'inspecter le répertoire CFB, hors des 64 premiers octets.
    ole = _detect_ole2(data)
    if ole:
        return ole

    if head.startswith(b"%PDF-"):
        return PDF

    # 2) Conteneurs ZIP (docx/docm/xlsx/xlsm/pptx/ods/zip)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return _detect_zip_family(data)

    # 2 bis) Signatures STRUCTURÉES : après les signatures fortes,
    # avant les replis génériques. Registre vide dans ce lot => strictement neutre.
    structured = _run_structured_detectors(data, ctx)
    if structured is not None:
        return structured

    # 3) Texte : on lève l'ambiguïté avec extension + contenu
    if not looks_like_text(data):
        return OCTET

    try:
        text_head = data[:8192].decode("utf-8", errors="replace")
    except Exception:
        text_head = ""

    lower = ctx.lower_name
    declared = (ctx.declared_content_type or "").lower()

    if _looks_like_html(text_head) or lower.endswith((".html", ".htm")) or "html" in declared:
        # confirme que ça ressemble bien à du markup pour éviter les faux positifs
        if "<" in text_head and ">" in text_head:
            return HTML

    if lower.endswith((".md", ".markdown")) or declared in ("text/markdown", "text/x-markdown"):
        return MARKDOWN

    if lower.endswith((".csv", ".tsv")) or declared in ("text/csv", "application/csv"):
        return CSV

    # CSV par contenu (si pas d'indice d'extension) : plusieurs lignes "séparées"
    if _looks_like_csv(text_head):
        return CSV

    return PLAIN


def _looks_like_csv(text_head: str) -> bool:
    lines = [ln for ln in text_head.splitlines() if ln.strip()][:5]
    if len(lines) < 2:
        return False
    for sep in (",", ";", "\t"):
        counts = [ln.count(sep) for ln in lines]
        if counts[0] >= 1 and all(c == counts[0] for c in counts):
            return True
    return False
