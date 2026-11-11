# backend/apps/rag_core/services/ingest/extraction/__init__.py
from __future__ import annotations

"""
Couche d'extraction documentaire : point d'entrée unique `extract_document`.

Flux : validation pré-extraction (taille + type réel + allowlist) -> sélection de
l'extracteur par format -> extraction sous timeout global -> ExtractionResult.

Erreurs typées propagées (à mapper par l'appelant) :
- RejectedFile / UnsupportedFormat : refus AVANT extraction (415/400) ;
- ExtractionTimeout / CorruptedFile / ExtractionError : échec d'extraction.
"""

import time as _time
from typing import Dict

from . import config, mime
from .base import (
    Block,
    BlockType,
    CorruptedFile,
    ExtractionComplexityExceeded,
    ExtractionContext,
    ExtractionError,
    ExtractionLimitExceeded,
    ExtractionResult,
    ExtractionStatus,
    ExtractionTimeout,
    RejectedFile,
    StructuredUnit,
    UnsupportedFormat,
)
from .csv_extractor import CsvExtractor
from .docx_extractor import DocxExtractor
from .dwg_extractor import DwgExtractor
from .dwg_signature import detect_dwg_signature
from .dxf_extractor import DxfExtractor
from .dxf_extractor import parser_available as dxf_parser_available
from .dxf_signature import detect_dxf_signature
from .email_extractor import EmlExtractor
from .eml_signature import detect_eml_signature
from .excel_extractor import ExcelExtractor
from .html_extractor import HtmlExtractor
from .ifc_extractor import IfcExtractor
from .ifc_signature import detect_ifc_signature
from .image_extractor import ImageExtractor
from .markdown_extractor import MarkdownExtractor
from .email_model import parser_available as msg_parser_available
from .pdf_extractor import PdfExtractor
from .pptx_extractor import PptxExtractor
from .text_extractor import TextExtractor
from .validation import (
    FMT_CSV,
    FMT_DOCX,
    FMT_DWG,
    FMT_DXF,
    FMT_AUDIO,
    FMT_EML,
    FMT_EXCEL,
    FMT_HTML,
    FMT_IFC,
    FMT_IMAGE,
    FMT_MARKDOWN,
    FMT_MSG,
    FMT_PDF,
    FMT_PPTX,
    FMT_TXT,
    FMT_XML,
    validate_and_classify,
)
from .audio_transcript_extractor import AudioTranscriptExtractor
from .audio_transcript_signature import detect_audio_transcript_signature
from .xml_extractor import XmlExtractor
from .xml_extractor import parser_available as xml_parser_available
from .xml_signature import detect_xml_signature

# Registre : un extracteur par format_key
_REGISTRY: Dict[str, object] = {
    FMT_TXT: TextExtractor(),
    FMT_MARKDOWN: MarkdownExtractor(),
    FMT_DOCX: DocxExtractor(),
    FMT_CSV: CsvExtractor(),
    FMT_HTML: HtmlExtractor(),
    FMT_PDF: PdfExtractor(),
    FMT_IMAGE: ImageExtractor(),
    FMT_EXCEL: ExcelExtractor(),
    FMT_PPTX: PptxExtractor(),
    FMT_XML: XmlExtractor(),
    FMT_DXF: DxfExtractor(),
    FMT_IFC: IfcExtractor(),
    FMT_DWG: DwgExtractor(),
    FMT_EML: EmlExtractor(),
    FMT_AUDIO: AudioTranscriptExtractor(),
}

# Détecteur de signature XML enregistré dans le hook structuré
# lors de la qualification. Idempotent : un rechargement du paquet ne double pas l'entrée.
if "xml" not in {name for _, name in mime.structured_detectors()}:
    mime.register_structured_detector("xml", detect_xml_signature, priority=100)
# Détecteur DXF : même hook, même priorité (extensions disjointes).
if "dxf" not in {name for _, name in mime.structured_detectors()}:
    mime.register_structured_detector("dxf", detect_dxf_signature, priority=100)
# Détecteur IFC : même hook (extension `.ifc` + en-tête STEP).
if "ifc" not in {name for _, name in mime.structured_detectors()}:
    mime.register_structured_detector("ifc", detect_ifc_signature, priority=100)
# Détecteur DWG : même hook (extension `.dwg` + en-tête ACxxxx).
if "dwg" not in {name for _, name in mime.structured_detectors()}:
    mime.register_structured_detector("dwg", detect_dwg_signature, priority=100)
# Détecteur EML : extension `.eml` + bloc d'en-têtes RFC 5322.
if "eml" not in {name for _, name in mime.structured_detectors()}:
    mime.register_structured_detector("eml", detect_eml_signature, priority=100)
# Transcription audio canonique : extension audio + marqueur JSON.
if "audio_transcript" not in {name for _, name in mime.structured_detectors()}:
    mime.register_structured_detector("audio_transcript", detect_audio_transcript_signature, priority=100)


def msg_capability() -> dict:
    """État machine-readable de la prise en charge Outlook (.msg).

    Distingue les trois raisons d'indisponibilité, qui appellent des actions
    différentes : `disabled_by_config` se règle par un flag, `dependency_missing`
    par un déploiement. Consommé par l'endpoint de capacités et le frontend, qui
    n'affiche le format en dépôt que si `status == "available"`.
    """
    if not config.msg_enabled():
        status = "disabled_by_config"
    elif not msg_parser_available():
        # RAGatm Open : parseur Outlook absent par construction (licence), pas par oubli.
        status = "unsupported"
    else:
        status = "available"
    return {
        "format": "msg",
        "status": status,
        "body_ingestion": status == "available",
        # contenu des pièces ingérable SUR CET ENVIRONNEMENT (porte
        # globale). Le dépôt reste borné aux espaces de DOCUMENT_EMAIL_RAGSPACE_IDS.
        "attachment_ingestion": status == "available" and config.email_attachment_ingest_enabled(),
        "inline_image_ocr": status == "available" and config.email_attachment_ingest_enabled(),
        # Faux en disponibilité générale (DOCUMENT_EMAIL_GLOBAL_ENABLED) ; vrai : dépôt borné
        # aux espaces de DOCUMENT_EMAIL_RAGSPACE_IDS.
        "space_scoped": not config.email_global_enabled(),
    }


def eml_capability() -> dict:
    """État machine-readable de la prise en charge des courriels MIME (.eml)."""
    status = "available" if config.eml_enabled() else "disabled_by_config"
    return {
        "format": "eml",
        "status": status,
        "body_ingestion": status == "available",
        "attachment_ingestion": status == "available" and config.email_attachment_ingest_enabled(),
        "space_scoped": not config.email_global_enabled(),
    }


def supported_formats() -> list[str]:
    """Formats réellement exploitables ICI ET MAINTENANT.

    `.msg` n'y figure que si le flag est ouvert ET le parseur déployé : une entrée
    statique promettrait un format que l'extraction refuserait ensuite.
    """
    formats = set(_REGISTRY.keys())
    if msg_capability()["status"] != "available":
        formats.discard(FMT_MSG)
    if not config.eml_enabled():
        formats.discard(FMT_EML)
    if not (config.xml_enabled() and xml_parser_available()):
        formats.discard(FMT_XML)
    if not (config.dxf_enabled() and dxf_parser_available()):
        formats.discard(FMT_DXF)
    # IFC : annoncé sur le seul flag — le processus web ne porte pas l'extra `[ifc]`,
    # c'est le worker dédié `ingest_ifc` qui l'a (invariant de
    # déploiement : flag ouvert seulement si worker_ifc tourne).
    if not config.ifc_enabled():
        formats.discard(FMT_IFC)
    # DWG : même règle que IFC — annoncé sur le seul flag. Le processus web ne porte pas
    # LibreDWG (image générale légère) ; c'est le worker `ingest_dwg` qui l'a
    # (invariant de déploiement : flag ouvert seulement si worker_dwg tourne).
    if not config.dwg_enabled():
        formats.discard(FMT_DWG)
    # Audio : annoncé sur le seul drapeau (ffmpeg vit sur le worker `ingest_audio`, pas
    # dans le processus web). Le dépôt est borné aux espaces de DOCUMENT_AUDIO_RAGSPACE_IDS,
    # sauf disponibilité générale (DOCUMENT_AUDIO_GLOBAL_ENABLED).
    if not config.audio_ingest_enabled():
        formats.discard(FMT_AUDIO)
    return sorted(formats)


def audio_capability() -> dict:
    """État machine-readable du dépôt audio, pour le frontend."""
    from ..audio.formats import AUDIO_SUFFIXES

    enabled = config.audio_ingest_enabled()
    return {
        "format": "audio",
        "status": "available" if enabled else "disabled_by_config",
        "extensions": sorted(s.lstrip(".") for s in AUDIO_SUFFIXES) if enabled else [],
        "max_upload_bytes": config.audio_max_upload_bytes(),
        "max_duration_seconds": config.audio_max_duration_seconds(),
        "upload_path": "/api/v1/documents/upload-audio/",
        # Faux en disponibilité générale (DOCUMENT_AUDIO_GLOBAL_ENABLED) ; vrai : dépôt borné
        # aux espaces de DOCUMENT_AUDIO_RAGSPACE_IDS. L'admission réelle reste par espace
        # (`audio_upload_enabled` sur chaque RagSpace) et le droit d'écriture est toujours exigé.
        "space_scoped": not config.audio_global_enabled(),
    }


def supported_image_formats() -> list[str]:
    """Sous-formats image réellement admis ICI ET MAINTENANT.

    `supported_formats()` annonce la FAMILLE (`image`) ; cette liste dit LESQUELS.
    Sans elle, le frontend devait deviner les extensions image — et continuait d'en
    proposer une retirée de `DOCUMENT_IMAGE_FORMATS`, que le backend refusait ensuite
    fichier par fichier. Vide si la porte image est fermée : aucune famille annoncée
    ne doit pouvoir survivre à son propre interrupteur.
    """
    if not config.image_ingest_enabled():
        return []
    return sorted(config.image_formats())


def extract_document(
    data: bytes,
    *,
    filename: str = "",
    content_type: str = "",
    ocr_enabled: bool | None = None,
    allow_excel: bool = True,
    allow_pptx: bool = True,
    timeout_seconds: int | None = None,
    ocr_page_timeout_seconds: int | None = None,
    visual_sources: frozenset | None = None,
    pdf_visual_max_assets: int | None = None,
    pdf_visual_max_per_page: int | None = None,
    rag_space_id: str | None = None,
) -> ExtractionResult:
    """Extrait le contenu d'un document (octets) en un `ExtractionResult`.

    Lève une `ExtractionError` typée en cas de refus ou d'échec.

    ``allow_excel=False`` / ``allow_pptx=False`` refusent respectivement les classeurs
    `.xlsx` et les présentations `.pptx` pour CET appel — utilisés par l'ingestion du
    corpus métier, qui est synchrone (cf. validation.validate_and_classify).

    ``timeout_seconds`` (lot 3) : budget d'extraction de CET appel, transmis
    explicitement plutôt que lu dans la configuration globale. C'est ce qui permet à
    une reprise « délai étendu » d'accorder plus de temps à UN document sans changer
    le budget de tous les autres. ``None`` = budget standard configuré.
    Les autres garde-fous (taille, pages, MIME, sécurité) sont appliqués en amont par
    ``validate_and_classify`` et ne sont **jamais** relâchés par ce paramètre.

    ``ocr_page_timeout_seconds`` (OCR-TIMEOUT-HARDENING-02) : budget OCR par PAGE de CET
    appel. Distinct de ``timeout_seconds``, qui borne le document entier : un document de
    40 pages consomme 40 budgets de page à l'intérieur d'un seul budget de document. Ce
    budget est désormais passé à Tesseract lui-même, donc réellement opposable : à son
    expiration le processus est terminé, au lieu d'être abandonné en continuant de brûler
    du CPU. ``None`` = budget de page standard configuré.

    ``visual_sources`` : natures de source dont l'extracteur doit
    DÉCOUVRIR les visuels pour le rail visuel. Aucun appel ici ; ``None`` = aucun
    visuel collecté (comportement historique, y compris l'échec `ocr_no_text`).

    ``pdf_visual_max_assets`` : borne de visuels PDF admis pour CET appel (``None`` =
    ``DOCUMENT_PDF_VISUAL_MAX_ASSETS``). Ne change que le budget de la sélection.
    ``pdf_visual_max_per_page`` : borne par PAGE pour CET appel (``None`` =
    ``DOCUMENT_PDF_VISUAL_MAX_PER_PAGE``).

    ``rag_space_id`` : RagSpace propriétaire du document. Seul porteur de
    l'identité pour la décision du rail tabulaire générique ; ``None`` (corpus métier, outils) = rail OFF.
    """
    ctx = ExtractionContext(
        filename=filename or "",
        declared_content_type=content_type or "",
        ocr_enabled=ocr_enabled,
        ocr_page_timeout_s=ocr_page_timeout_seconds,
        visual_sources=frozenset(visual_sources or ()),
        pdf_visual_max_assets=pdf_visual_max_assets,
        pdf_visual_max_per_page=pdf_visual_max_per_page,
        rag_space_id=str(rag_space_id) if rag_space_id not in (None, "") else None,
    )

    detected_mime, fmt = validate_and_classify(
        data or b"", ctx, allow_excel=allow_excel, allow_pptx=allow_pptx
    )
    extractor = _REGISTRY.get(fmt)
    if extractor is None:  # pragma: no cover - garde-fou
        raise UnsupportedFormat(f"Aucun extracteur pour le format '{fmt}'.")

    # Budget global d'extraction. Il s'exécute dans un processus RÉELLEMENT terminable :
    # le mécanisme précédent (thread + `run_with_timeout`) rendait la main sans arrêter
    # le travail, laissant l'extraction abandonnée consommer un cœur jusqu'à ce que la
    # limite dure de Celery tue le worker et, avec lui, les autres tâches en vol.
    #
    # La frontière est placée ICI et nulle part ailleurs : `validate_and_classify` est
    # déjà passée, et le découpage, les embeddings et la publication en base restent en
    # aval, dans le processus appelant. L'enfant n'écrit donc rien de persistant, ce qui
    # est la condition qui rend sa terminaison brutale sans conséquence.
    from ..process_isolation import (
        ProcessCrashed,
        ProcessTimeout,
        run_in_cancellable_process,
    )

    def _run() -> ExtractionResult:
        return extractor.extract(data or b"", ctx)  # type: ignore[attr-defined]

    budget = int(timeout_seconds) if timeout_seconds else config.extraction_timeout_s()
    _t_extraction = _time.monotonic()
    try:
        result = run_in_cancellable_process(_run, budget)
    except ProcessTimeout as e:
        raise ExtractionTimeout(str(e), code="extraction_timeout")
    except ProcessCrashed as e:
        # Distinct d'un dépassement : le budget n'est pas en cause, et le proposer à
        # l'exploitant comme remède l'enverrait sur une fausse piste.
        raise ExtractionError(
            str(e),
            code="extraction_worker_crashed",
            meta={"exitcode": e.exitcode},
        )
    ecoule = _time.monotonic() - _t_extraction

    # Rail hybride Document Vision — OPT-IN, éteint par défaut.
    #
    # Il est appelé ICI, dans le PARENT, et non dans l'enfant d'extraction. L'enfant est
    # tué brutalement à l'expiration, et c'est précisément parce qu'il « n'écrit rien de
    # persistant » que cette brutalité est sans conséquence. Un appel réseau y serait
    # laissé orphelin côté moteur par un SIGKILL, et ses exceptions devraient traverser
    # un pickle. Le parent, lui, dispose déjà de tout : les octets, `result.pages`, et le
    # texte OCR que l'enfant vient de produire — le repli est donc gratuit.
    #
    # Éteint, `result` est renvoyé SANS recomposition : le même objet, pas une copie.
    from .docvision_hook import appliquer_si_actif

    return appliquer_si_actif(
        result, data=data or b"", filename=filename or "",
        content_type=content_type or "", budget_extraction_s=budget,
        ecoule_extraction_s=ecoule,
    )


__all__ = [
    "extract_document",
    "supported_formats",
    "msg_capability",
    "eml_capability",
    "FMT_EML",
    "FMT_EXCEL",
    "FMT_PPTX",
    "FMT_MSG",
    "FMT_XML",
    "FMT_DXF",
    "FMT_DWG",
    "FMT_IFC",
    "ExtractionResult",
    "ExtractionContext",
    "ExtractionStatus",
    "Block",
    "BlockType",
    "ExtractionError",
    "UnsupportedFormat",
    "RejectedFile",
    "CorruptedFile",
    "ExtractionTimeout",
    "ExtractionLimitExceeded",
    "ExtractionComplexityExceeded",
    "StructuredUnit",
    "config",
]
