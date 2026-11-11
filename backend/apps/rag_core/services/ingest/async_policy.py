# backend/apps/rag_core/services/ingest/async_policy.py
from __future__ import annotations

"""
Décision **unique** sync/async pour l'ingestion documentaire.

Tous les chemins (upload multipart, ingest texte, reindex, pipeline) DOIVENT
passer par ici afin d'éviter toute divergence. Ordre de priorité :

1. OCR activé, OU document EXCEL, OU présentation PPTX -> async forcé, NON
   surchargeable (tous coûteux/longs et à ne jamais exécuter dans le cycle d'une
   requête HTTP : un classeur de 500 k cellules prend ~7 s, mesuré en E1) ;
2. override explicite (query param ``?async=...``, header ``x-rag-async`` ou
   champ ``async`` du body JSON) ;
3. setting ``RAG_INGEST_ASYNC_DEFAULT``.
"""

from django.conf import settings

from config.settings.access import get_registered_setting


def _boolish(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def ocr_forces_async() -> bool:
    """True si l'OCR est activé : l'ingestion doit alors être asynchrone."""
    try:
        from .extraction import config as _xcfg

        return bool(_xcfg.ocr_forces_async())
    except Exception:
        return False


def excel_forces_async(format_key=None) -> bool:
    """True si CE document est un classeur Excel : l'ingestion doit être asynchrone.

    Mesuré (spike E1) : 50 k cellules ≈ 0,7 s, 500 k ≈ 7 s, 1 M ≈ 17 s. On ne met pas
    ça dans le cycle d'une requête HTTP. Même motif que l'OCR, même endroit.
    """
    if not format_key:
        return False
    try:
        from .extraction import config as _xcfg
        from .extraction.validation import FMT_EXCEL

        return str(format_key) == FMT_EXCEL and bool(_xcfg.excel_forces_async())
    except Exception:
        return False


def pptx_forces_async(format_key=None) -> bool:
    """True si CE document est une présentation PPTX : l'ingestion doit être asynchrone.

    Même motif qu'Excel : archive compressée, nombre de formes imprévisible, tableaux à
    aplatir. Le coût n'est pas bornable a priori depuis la couche HTTP.
    """
    if not format_key:
        return False
    try:
        from .extraction import config as _xcfg
        from .extraction.validation import FMT_PPTX

        return str(format_key) == FMT_PPTX and bool(_xcfg.pptx_forces_async())
    except Exception:
        return False


def dxf_forces_async(format_key=None) -> bool:
    """True si CE document est un dessin DXF : l'ingestion doit être asynchrone.

    Même motif qu'Excel/PPTX : un grand dessin demande plusieurs secondes de parsing
    — un coût non bornable a priori depuis la couche HTTP.
    """
    if not format_key:
        return False
    try:
        from .extraction import config as _xcfg
        from .extraction.validation import FMT_DXF

        return str(format_key) == FMT_DXF and bool(_xcfg.dxf_forces_async())
    except Exception:
        return False


def ifc_forces_async(format_key=None) -> bool:
    """True si CE document est une maquette IFC : asynchrone ABSOLU.

    Parsing IfcOpenShell (import ~115 Mo, secondes) sur une file dédiée `ingest_ifc` :
    aucun client ne peut l'imposer dans le cycle d'une requête HTTP.
    """
    if not format_key:
        return False
    try:
        from .extraction import config as _xcfg
        from .extraction.validation import FMT_IFC

        return str(format_key) == FMT_IFC and bool(_xcfg.ifc_forces_async())
    except Exception:
        return False


def dwg_forces_async(format_key=None) -> bool:
    """DWG : async ABSOLU.

    Conversion par un sous-processus natif (LibreDWG) sur une source binaire non fiable,
    DXF intermédiaire potentiellement volumineux : jamais dans une requête HTTP, et
    toujours sur la file dédiée `ingest_dwg`.
    """
    try:
        from apps.rag_core.services.ingest.extraction import config as _xcfg
        from apps.rag_core.services.ingest.extraction.validation import FMT_DWG
    except Exception:  # pragma: no cover - import circulaire improbable
        return False
    try:
        return str(format_key) == FMT_DWG and bool(_xcfg.dwg_forces_async())
    except Exception:  # pragma: no cover
        return False


def email_forces_async(format_key=None) -> bool:
    """Courriel (.msg, .eml) : async ABSOLU.

    `config.msg_forces_async()` existait mais n'était appelé nulle part : un `.msg` pouvait
    être extrait dans la requête web. Avec l'ingestion des pièces jointes, un courriel
    peut déclencher plusieurs extractions (PDF + OCR, classeurs, présentations) : jamais
    dans le cycle d'une requête HTTP.
    """
    try:
        from .extraction import config as _xcfg

        return bool(_xcfg.email_forces_async(str(format_key))) if format_key else False
    except Exception:  # pragma: no cover
        return False


def visual_forces_async(format_key=None) -> bool:
    """Source visuelle ACTIVE : async forcé (1B image, PDF en 1C, DOCX en 1D, PPTX en PPTX-1B).

    Un appel au Gateway vision n'a pas sa place dans le cycle d'une requête HTTP. Rail
    visuel éteint, ou source hors allowlist : politique du format inchangée.
    """
    if not format_key:
        return False
    try:
        from .extraction import config as _xcfg
        from .extraction.validation import FMT_DOCX, FMT_IMAGE, FMT_PDF, FMT_PPTX

        sources = _xcfg.visual_analysis_sources()
        fmt = str(format_key)
        return ((fmt == FMT_IMAGE and "image" in sources)
                or (fmt == FMT_PDF and "pdf" in sources)
                or (fmt == FMT_DOCX and "docx" in sources)
                or (fmt == FMT_PPTX and "pptx" in sources))
    except Exception:
        return False


def _default_async() -> bool:
    """Valeur par défaut (sans override explicite).

    Un repli sur l'ancien nom ``RAG_ASYNC_INGEST`` vivait ici, présenté comme de la
    rétro-compatibilité. Il n'a jamais pu s'exécuter : ce nom n'était projeté dans
    aucun settings, et ``RAG_INGEST_ASYNC_DEFAULT`` l'est toujours. Le retirer ne
    supprime donc aucune configuration réelle — seulement l'illusion qu'un ancien
    nom fonctionnait encore.
    """
    return bool(get_registered_setting("RAG_INGEST_ASYNC_DEFAULT"))


def resolve_async(*, explicit=None, format_key=None) -> bool:
    """Cœur de la décision. ``explicit`` = valeur brute (str/bool/None) issue du
    query param, du header ou du body ; ``None`` => pas d'override.

    ``format_key`` = format RÉEL détecté (cf. validation.validate_and_classify), quand
    l'appelant le connaît. Sert à forcer l'async pour Excel, PPTX, DXF, IFC, DWG et les courriels.
    """
    # 1. OCR, Excel, PPTX, DXF ou IFC : non surchargeable (même si explicit == "false").
    if (ocr_forces_async() or excel_forces_async(format_key) or pptx_forces_async(format_key)
            or dxf_forces_async(format_key) or ifc_forces_async(format_key)
            or dwg_forces_async(format_key) or visual_forces_async(format_key)
            or email_forces_async(format_key) or str(format_key or "") == "audio"):
        # Audio : TOUJOURS asynchrone (transcription Gateway).
        return True
    # 2. Override explicite.
    if explicit is not None:
        return _boolish(explicit)
    # 3. Défaut serveur (+ fallback historique).
    return _default_async()


def should_async_from_request(request, *, body_value=None, format_key=None) -> bool:
    """Décision pour une requête DRF.

    ``body_value`` (optionnel) prime sur le query param et le header : utilisé
    par l'endpoint d'ingestion texte qui accepte ``{"async": ...}`` dans le body.
    """
    explicit = None
    if body_value is not None:
        explicit = body_value
    else:
        qp = request.query_params.get("async")
        if qp is not None:
            explicit = qp
        else:
            hdr = request.headers.get("x-rag-async")
            if hdr is not None:
                explicit = hdr
    return resolve_async(explicit=explicit, format_key=format_key)
