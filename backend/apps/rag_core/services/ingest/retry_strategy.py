# backend/apps/rag_core/services/ingest/retry_strategy.py
from __future__ import annotations

"""Stratégies de reprise d'un document en échec — décision DÉTERMINISTE et unique.

Pourquoi ce module
------------------
Jusqu'ici, reprendre un document en erreur rejouait **exactement** le pipeline qui
venait d'échouer. Sur les deux causes les plus fréquentes observées en production —
un PDF scanné sans texte natif, et une extraction dépassant le délai — cela ne
pouvait produire que le même échec. L'interface proposait donc une action dont on
savait qu'elle ne changerait rien.

Ce module décide **quoi faire différemment**, à partir du code d'erreur persisté :

- pas de LLM, pas d'heuristique floue : une table `error_code -> stratégie` ;
- pas de duplication frontend/backend : le backend est la seule source de vérité,
  le frontend affiche la recommandation qu'il reçoit ;
- pas de parsing d'un message humain : on lit `meta.extraction.error_code`, avec un
  repli borné sur le préfixe de `error_message` pour les documents indexés AVANT
  que tous les chemins ne persistent un code structuré ;
- une erreur appartient à SA tentative : le code lu doit être celui de la tentative
  COURANTE (cf. `document_error_code`), jamais celui d'une tentative antérieure.

Ce que ce module ne fait PAS : lire un fichier, appeler le pipeline, écrire en base.
Il calcule une décision, et rien d'autre — c'est ce qui le rend testable à l'unité.
"""

import os
import re
from dataclasses import dataclass

from django.conf import settings

from .extraction import mime as M

# --------------------------------------------------------------------------- #
# Stratégies (allowlist)
# --------------------------------------------------------------------------- #
STRATEGY_STANDARD = "standard"
STRATEGY_FORCE_OCR = "force_ocr"
STRATEGY_EXTENDED_TIMEOUT = "extended_timeout"
STRATEGY_AUTO = "auto"

#: Stratégies acceptées en ENTRÉE d'API (allowlist stricte).
REQUESTABLE_STRATEGIES: frozenset[str] = frozenset({
    STRATEGY_STANDARD, STRATEGY_FORCE_OCR, STRATEGY_EXTENDED_TIMEOUT, STRATEGY_AUTO,
})

#: Stratégies EFFECTIVES (ce qu'un item exécute réellement). `auto` n'en est pas
#: une : c'est une demande de résolution, jamais un traitement.
EFFECTIVE_STRATEGIES: frozenset[str] = frozenset({
    STRATEGY_STANDARD, STRATEGY_FORCE_OCR, STRATEGY_EXTENDED_TIMEOUT,
})

DEFAULT_STRATEGY = STRATEGY_STANDARD


# --------------------------------------------------------------------------- #
# Retryabilité
# --------------------------------------------------------------------------- #
#: Une reprise à l'identique a une chance réelle d'aboutir (échec transitoire).
RETRYABLE = "retryable"
#: Une reprise n'a de sens qu'en CHANGEANT de méthode (OCR, délai étendu).
RETRYABLE_WITH_STRATEGY = "retryable_with_strategy"
#: Aucune reprise automatique ne peut aboutir : le fichier lui-même est en cause.
NON_RETRYABLE = "non_retryable"
#: Le pipeline a fait ce qu'il pouvait ; seul un autre fichier source aiderait.
REPLACE_SOURCE_RECOMMENDED = "replace_source_recommended"

RETRYABILITIES: frozenset[str] = frozenset({
    RETRYABLE, RETRYABLE_WITH_STRATEGY, NON_RETRYABLE, REPLACE_SOURCE_RECOMMENDED,
})


# --------------------------------------------------------------------------- #
# Table de décision
# --------------------------------------------------------------------------- #
#: `error_code` -> stratégie qui change RÉELLEMENT quelque chose.
#: N'y figurent que les codes pour lesquels une autre méthode existe.
STRATEGY_BY_ERROR_CODE: dict[str, str] = {
    # Aucun texte natif exploitable : seul un OCR du rendu peut récupérer le contenu.
    "pdf_scanned_requires_ocr": STRATEGY_FORCE_OCR,
    # Polices sans table Unicode : aucun moteur natif ne peut lire ce PDF.
    "pdf_text_unrecoverable": STRATEGY_FORCE_OCR,
    # Le budget d'extraction a été dépassé : le même budget le sera de nouveau.
    "extraction_timeout": STRATEGY_EXTENDED_TIMEOUT,
    "pdf_extraction_timeout": STRATEGY_EXTENDED_TIMEOUT,
}

#: Codes dont l'échec est DÉFINITIF pour ce fichier : format refusé, archive
#: corrompue, protection, limite de ressources structurelle. Rejouer ne changera
#: rien tant que le fichier est celui-là.
NON_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset({
    # Collision de contenu : un document ACTIF porte déjà ce sha256 dans l'espace
    # (contrainte uniq_rag_document_space_sha256_not_failed). Rejouer reproduirait
    # la collision ; la seule issue est le document actif lui-même.
    "duplicate_content",
    # Refus de sécurité / format
    "unsupported_file_type", "office_temporary_file", "archive_not_supported",
    "macro_enabled_document_refused", "macro_enabled_workbook_refused",
    "macro_enabled_presentation_refused", "executable_refused", "unsafe_xml",
    "legacy_office_format", "unsupported_spreadsheet_format",
    "unsupported_presentation_format", "unsupported_office_format",
    "image_unsupported_format",
    # Protection / chiffrement
    "encrypted_pptx", "encrypted_pdf", "password_protected",
    # Fichier illisible
    "corrupted_file", "docx_corrupted", "docx_missing_document_xml",
    "docx_xml_parse_error", "excel_corrupted", "excel_missing_workbook_xml",
    "excel_xml_parse_error", "invalid_pptx_archive", "image_corrupted",
    "msg_parse_failed", "csv_parse_error", "empty_file",
    # Limites structurelles du fichier
    "file_too_large", "image_too_large", "excel_too_many_cells",
    "excel_too_many_sheets", "excel_shared_strings_too_large",
    "excel_uncompressed_too_large", "pptx_limit_exceeded",
    "pptx_uncompressed_too_large", "pptx_too_many_entries", "pptx_compression_ratio",
    "csv_too_many_rows", "csv_too_many_columns", "csv_cell_too_long",
    "docx_uncompressed_too_large", "docx_compression_ratio", "docx_too_many_entries",
    # Contrat structuré partagé : complexité structurelle dépassée,
    # détecteur de signature en échec fermé — définitifs pour CE fichier.
    "extraction_complexity_exceeded", "structured_detection_failed",
    # XML générique : mal formé ou trop complexe — définitifs pour
    # CE fichier (`unsafe_xml` figure déjà dans la famille sécurité ci-dessus).
    "xml_malformed", "xml_complexity_limit",
    # DXF : invalide, version non lue, trop d'entités — définitifs.
    "dxf_malformed", "dxf_unsupported_version", "dxf_complexity_limit",
    # IFC : invalide, schéma non supporté, trop d'entités — définitifs.
    "ifc_malformed", "ifc_unsupported_schema", "ifc_complexity_limit",
    # DWG : source/conversion déterministes — rejouer ne change rien,
    # et un dessin pathologique ne doit pas consommer le worker en boucle.
    "dwg_malformed", "dwg_unsupported_version", "dwg_conversion_failed",
    "dwg_conversion_timeout", "dwg_complexity_limit",
    # Fichier source absent : il n'y a rien à ré-extraire.
    "no_storage_file_for_reindex", "not_found", "rag_space_deleted",
})

#: Codes où le pipeline a épuisé ce qu'il sait faire sur CE fichier. Une reprise
#: est techniquement possible mais reproduirait l'échec : ce qu'il faut, c'est un
#: meilleur fichier source (un scan plus lisible, un export natif).
REPLACE_SOURCE_ERROR_CODES: frozenset[str] = frozenset({
    # Le document DÉCLARE une source, mais le fichier est introuvable dans le
    # stockage. Le pipeline est sain, le fichier n'existe plus : aucune reprise à
    # l'identique ne peut aboutir, seule une source fournie à nouveau le peut.
    # À ne pas confondre avec `no_storage_file_for_reindex` (aucune source n'a
    # jamais été conservée), qui reste NON_RETRYABLE.
    "source_file_missing",
    # L'OCR a bien tourné et n'a rien produit d'exploitable. Reproposer « OCR »
    # serait promettre exactement ce qui vient d'échouer.
    "ocr_no_text",
    # Aucun texte, tous chemins confondus.
    "empty_document", "empty_text",
    # Chunks impossibles à construire depuis le texte extrait.
    "no_valid_chunks",
    # Analyse visuelle RÉUSSIE sans évidence exploitable (inconnu, décoratif, vide) et
    # aucun autre rail utile : rejouer reproduirait le même résultat.
    "visual_no_usable_evidence",
})

#: Codes dont la cause est ENVIRONNEMENTALE : le document est sain, c'est le
#: service qui manquait. Une reprise à l'identique est le bon geste — une fois le
#: service rétabli.
ENVIRONMENTAL_ERROR_CODES: frozenset[str] = frozenset({
    "transient_ingest_error", "embedding_provider_unavailable", "embedding_timeout",
    "embeddings_unavailable", "provider_unavailable", "enqueue_failed",
    "reindex_enqueue_unavailable", "storage_file_save_failed",
    # OCR/PPTX/Excel indisponibles par déploiement ou par flag : réparable côté
    # serveur, pas côté fichier.
    "ocr_engine_missing", "ocr_pdf_rasterizer_missing", "ocr_unavailable",
    "ocr_no_provider", "ocr_provider_not_implemented", "ocr_disabled_for_image",
    "pptx_library_missing", "pptx_disabled", "excel_disabled",
    "msg_dependency_unavailable", "msg_ingestion_disabled", "image_ingest_disabled",
    "xml_ingestion_disabled", "xml_dependency_unavailable",
    "dxf_ingestion_disabled", "dxf_dependency_unavailable",
    "ifc_ingestion_disabled", "ifc_dependency_unavailable",
    "dwg_ingestion_disabled", "dwg_dependency_unavailable",
    "excel_not_supported_here", "pptx_not_supported_here", "msg_file_too_large",
    "reindex_interrupted", "internal_error", "unexpected_error",
    # Analyse visuelle : Gateway en échec ou désactivé, réindexation
    # refusée pour ne pas dégrader un index publié — le document est sain.
    "visual_analysis_failed", "visual_reindex_degraded", "visual_analysis_disabled_would_degrade",
})


# --------------------------------------------------------------------------- #
# Éligibilité OCR
# --------------------------------------------------------------------------- #
#: Types sur lesquels un OCR a un sens : une page rendue en image. Forcer l'OCR sur
#: un DOCX ou un CSV ne produirait rien — le texte y est déjà du texte.
OCR_ELIGIBLE_MIMES: frozenset[str] = frozenset({M.PDF, *M.IMAGE_MIMES})

OCR_ELIGIBLE_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif",
})


def supports_ocr(*, content_type: str | None, filename: str | None) -> bool:
    """True si l'OCR est techniquement applicable à ce document.

    `content_type` (détecté par CONTENU à l'ingestion) prime ; l'extension n'est
    consultée que s'il est absent ou inconnu — même doctrine que partout ailleurs
    dans le dépôt.
    """
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in OCR_ELIGIBLE_MIMES:
        return True
    if ctype and ctype != M.OCTET:
        # Type connu ET non éligible : on ne va pas chercher un avis contraire
        # dans l'extension, ce serait exactement la confiance interdite.
        return False
    ext = os.path.splitext((filename or "").strip())[1].lower()
    return ext in OCR_ELIGIBLE_EXTENSIONS


# --------------------------------------------------------------------------- #
# Lecture du code d'erreur persisté
# --------------------------------------------------------------------------- #
#: `error_message` a la forme « code: détail » sur les chemins d'extraction, et le
#: code nu sur les autres. Le repli ne lit QUE ce préfixe, jamais la prose.
_LEADING_CODE = re.compile(r"^([a-z][a-z0-9_]{2,63})\b")


def known_error_codes() -> frozenset[str]:
    """Vocabulaire GOUVERNÉ des codes : l'union des tables de décision de ce module.

    Aucune taxonomie parallèle : un code est « connu » parce qu'une table le classe.
    """
    return (frozenset(STRATEGY_BY_ERROR_CODE) | NON_RETRYABLE_ERROR_CODES
            | REPLACE_SOURCE_ERROR_CODES | ENVIRONMENTAL_ERROR_CODES)


def _meta_error_code(doc) -> str:
    meta = getattr(doc, "meta", None)
    if not isinstance(meta, dict):
        return ""
    ext = meta.get("extraction")
    if not isinstance(ext, dict):
        return ""
    code = ext.get("error_code")
    return code.strip() if isinstance(code, str) else ""


def _message_error_code(doc) -> str:
    msg = str(getattr(doc, "error_message", "") or "").strip()
    m = _LEADING_CODE.match(msg)
    return m.group(1) if m else ""


def document_error_code(doc) -> str:
    """Code d'erreur structuré de la tentative COURANTE d'un document en échec (`""` si inconnu).

    Deux sources, deux autorités :

    - ``meta.extraction.error_code`` : autorité de STRUCTURE. Depuis le lot
      STALE-ERROR-METADATA il est retiré au début de chaque tentative
      (`attempts.ingesting_update_fields`) : s'il est présent, c'est que le règlement
      de la tentative courante l'a écrit ;
    - ``error_message`` : autorité de TEMPORALITÉ. Vidé à INGESTING et réécrit par
      TOUT règlement terminal (couche d'indexation, tâche, vues, reaper), il décrit
      toujours la tentative courante. On n'en lit que le PRÉFIXE (`code: détail` ou
      code nu), jamais la prose.

    Règle : le code meta est retenu, SAUF si le préfixe du message est lui aussi un
    code connu des tables et qu'il diffère — aucun règlement n'écrit deux codes connus
    différents au même instant, donc le code meta vient alors d'un règlement antérieur
    (ligne héritée, antérieure au reset) et le message, toujours courant, prime. Un
    message en prose (`GatewayEmbeddingsError`, exception technique → `internal_error`)
    ne contredit jamais un code meta frais. Sans code meta, le repli historique sur le
    préfixe du message couvre les règlements qui n'écrivent pas de code (fichier source
    absent, enfilement, reaper, erreur technique de la tâche).
    """
    meta_code = _meta_error_code(doc)
    msg_code = _message_error_code(doc)
    if meta_code and msg_code and msg_code != meta_code and msg_code in known_error_codes():
        return msg_code
    return meta_code or msg_code


# --------------------------------------------------------------------------- #
# Décision
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RetryDecision:
    """Ce qui a été demandé, ce qui sera fait, et pourquoi."""

    requested: str
    effective: str
    retryability: str
    reason_code: str
    force_ocr: bool
    timeout_seconds: int | None
    #: `auto` n'a pas trouvé le code dans la table : repli sur `standard`, dit
    #: explicitement plutôt que présenté comme un choix éclairé.
    auto_fallback: bool = False
    #: Raison structurée quand aucun traitement ne sera lancé.
    skip_reason: str = ""

    @property
    def is_actionable(self) -> bool:
        """False quand aucune reprise ne doit être tentée."""
        return not self.skip_reason

    def as_meta(self) -> dict:
        """Trace persistable (aucun contenu documentaire)."""
        meta = {
            "requested_strategy": self.requested,
            "error_code": self.reason_code,
            "retryability": self.retryability,
        }
        if self.auto_fallback:
            meta["auto_fallback"] = True
        if self.timeout_seconds is not None:
            meta["timeout_seconds"] = self.timeout_seconds
        if self.skip_reason:
            meta["skip_reason"] = self.skip_reason
        return meta


@dataclass(frozen=True)
class ReindexOptions:
    """Ce que la stratégie change CONCRÈTEMENT dans le pipeline.

    Transporté explicitement de bout en bout : jamais de settings mutés, jamais de
    variable globale ni de thread-local. Une tâche Celery en vol ne peut donc pas
    voir sa stratégie changer sous elle.
    """

    force_ocr: bool = False
    extraction_timeout_seconds: int | None = None

    @classmethod
    def for_strategy(cls, effective: str) -> "ReindexOptions":
        if effective == STRATEGY_FORCE_OCR:
            return cls(force_ocr=True)
        if effective == STRATEGY_EXTENDED_TIMEOUT:
            return cls(extraction_timeout_seconds=extended_timeout_seconds())
        return cls()


#: Raisons structurées d'un saut, exposées telles quelles par l'API.
SKIP_NON_RETRYABLE = "non_retryable"
SKIP_REPLACE_SOURCE = "replace_source_recommended"
SKIP_STRATEGY_NOT_SUPPORTED = "strategy_not_supported"
SKIP_MISSING_FILE = "missing_file"


def extended_timeout_seconds() -> int:
    """Budget d'extraction de la stratégie « délai étendu » (secondes).

    Valeur de CONFIGURATION, jamais fournie par le client : un délai libre serait
    un levier pour occuper un worker aussi longtemps qu'on veut.
    """
    return int(getattr(settings, "DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS", 480))


def classify_retryability(doc) -> tuple[str, str, str | None]:
    """(retryabilité, code d'erreur, stratégie recommandée ou None).

    Fonction PURE : elle ne lit que les champs déjà chargés du document.
    """
    code = document_error_code(doc)

    if code in NON_RETRYABLE_ERROR_CODES:
        return NON_RETRYABLE, code, None
    if code in REPLACE_SOURCE_ERROR_CODES:
        return REPLACE_SOURCE_RECOMMENDED, code, None

    strategy = STRATEGY_BY_ERROR_CODE.get(code)
    if strategy == STRATEGY_FORCE_OCR:
        # Un PDF qui appelle l'OCR mais dont le type ne le supporte pas ne peut pas
        # être repris utilement — cas théorique, tranché explicitement.
        if not supports_ocr(
            content_type=getattr(doc, "content_type", ""),
            filename=getattr(doc, "filename", ""),
        ):
            return REPLACE_SOURCE_RECOMMENDED, code, None
        return RETRYABLE_WITH_STRATEGY, code, STRATEGY_FORCE_OCR
    if strategy is not None:
        return RETRYABLE_WITH_STRATEGY, code, strategy

    if code in ENVIRONMENTAL_ERROR_CODES:
        return RETRYABLE, code, STRATEGY_STANDARD

    # Code absent ou inconnu : une reprise standard reste raisonnable, mais on ne
    # prétend pas savoir. `auto_fallback` le dira au niveau de la décision.
    return RETRYABLE, code, STRATEGY_STANDARD


def resolve_retry_strategy(doc, requested: str = STRATEGY_AUTO) -> RetryDecision:
    """Décide la stratégie EFFECTIVE d'un document pour une demande donnée.

    `auto` consulte la table ; une stratégie explicite est respectée **sauf**
    incompatibilité technique (OCR sur un format qui n'en relève pas) ou échec
    définitif — dans ces cas l'item est sauté avec une raison structurée plutôt
    que lancé pour rien.
    """
    requested = (requested or STRATEGY_AUTO).strip().lower()
    if requested not in REQUESTABLE_STRATEGIES:
        requested = STRATEGY_AUTO

    retryability, code, recommended = classify_retryability(doc)

    if retryability == NON_RETRYABLE:
        return RetryDecision(
            requested=requested, effective=STRATEGY_STANDARD, retryability=retryability,
            reason_code=code, force_ocr=False, timeout_seconds=None,
            skip_reason=SKIP_NON_RETRYABLE,
        )
    if retryability == REPLACE_SOURCE_RECOMMENDED:
        return RetryDecision(
            requested=requested, effective=STRATEGY_STANDARD, retryability=retryability,
            reason_code=code, force_ocr=False, timeout_seconds=None,
            skip_reason=SKIP_REPLACE_SOURCE,
        )

    if requested == STRATEGY_AUTO:
        effective = recommended or STRATEGY_STANDARD
        auto_fallback = code not in STRATEGY_BY_ERROR_CODE and code not in ENVIRONMENTAL_ERROR_CODES
    else:
        effective = requested
        auto_fallback = False

    if effective == STRATEGY_FORCE_OCR and not supports_ocr(
        content_type=getattr(doc, "content_type", ""),
        filename=getattr(doc, "filename", ""),
    ):
        return RetryDecision(
            requested=requested, effective=STRATEGY_STANDARD, retryability=retryability,
            reason_code=code, force_ocr=False, timeout_seconds=None,
            skip_reason=SKIP_STRATEGY_NOT_SUPPORTED,
        )

    options = ReindexOptions.for_strategy(effective)
    return RetryDecision(
        requested=requested,
        effective=effective,
        retryability=retryability,
        reason_code=code,
        force_ocr=options.force_ocr,
        timeout_seconds=options.extraction_timeout_seconds,
        auto_fallback=auto_fallback,
    )
