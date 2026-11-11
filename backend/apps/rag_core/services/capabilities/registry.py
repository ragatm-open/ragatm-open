"""Registre DÉCLARATIF des capacités documentaires.

Le registre DÉCRIT une capacité (ce que RagATM sait faire, où, avec quel service, ce qu'il
produit). Il ne dit JAMAIS si elle est active : c'est le rôle de ``resolver``, qui lit les
accesseurs et les résolveurs de portée existants. Aucune valeur d'environnement observée
(staging, production) n'est recopiée ici.

Pour enregistrer une nouvelle capacité : ajouter un ``CapabilitySpec`` (ou un format), en
référençant une porte (``gate``) et, si besoin, une portée (``scope``) connues du résolveur,
les flags lus (``flags``, noms du registre de configuration) et les rôles qui les consomment.
``tests/tests_root/unit/capabilities/test_capability_registry.py`` refuse un extracteur, un
détecteur, un flag de capacité ou une porte non classés.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------- #
# Vocabulaire
# --------------------------------------------------------------------------- #
#: Rôles d'exécution (nom de nœud Celery ``<rôle>@hôte`` ou processus web).
ROLES = ("backend", "worker", "worker_reindex", "worker_visual", "worker_audio", "worker_ifc",
         "worker_dwg", "worker_analytics", "worker_maintenance")
EXTRACT = ("worker", "worker_reindex")                  # extraction standard et réindexation
UPLOAD = ("backend",) + EXTRACT                         # validation à l'upload ET à l'extraction
VISUAL = ("backend", "worker", "worker_reindex", "worker_visual")   # routage + découverte + Vision

#: Colonnes de la matrice principale (libellés FR côté frontend).
MATRIX_COLUMNS = ("text", "ocr", "images", "vision", "tables", "charts", "audio")
CATEGORIES = MATRIX_COLUMNS + ("upload", "email", "structured", "common")

CALL_DIRECT = "DIRECT_GATEWAY"
CALL_INDIRECT = "INDIRECT_GATEWAY"
CALL_LOCAL = "LOCAL_ONLY"
CALL_OTHER = "OTHER_PROVIDER"
CALL_PER_SPACE = "DEPENDS_ON_RAGSPACE"
CALL_TYPES = (CALL_DIRECT, CALL_INDIRECT, CALL_LOCAL, CALL_OTHER, CALL_PER_SPACE)

INDEXED = "INDEXED"
INTERNAL_ONLY = "INTERNAL_ONLY"
NOT_PRODUCED = "NONE"
INDEX_STATES = (INDEXED, INTERNAL_ONLY, NOT_PRODUCED)

#: Services AI Gateway sondés (``gateway_health``) — un service déclaré doit y figurer.
GATEWAY_SERVICES = ("vision", "structured_extract", "asr", "embeddings", "rerank")

#: Statuts de qualification (donnée datée, avec la référence d'audit qui l'établit).
QUALIFICATION = ("REAL_DOCUMENT_QUALIFIED", "SYNTHETIC_QUALIFIED", "PARTIAL", "EXPERIMENTAL",
                 "NOT_QUALIFIED", "NOT_MEASURED", "NOT_APPLICABLE")


@dataclass(frozen=True)
class GatewayDependency:
    service: str                      # clé de GATEWAY_SERVICES
    route: str                        # route Gateway (chemin seul, jamais d'hôte)
    model_setting: Optional[str]      # réglage qui porte le modèle logique, si un seul
    call_type: str = CALL_DIRECT
    stage: str = "ingestion"          # ingestion | enrichissement différé | question


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    format_id: str
    category: str
    label_fr: str
    description_fr: str
    content_type: str
    gate: str                         # porte connue de resolver.GATES
    executing_service: str
    consumers: tuple
    output_type: str
    output_indexed: str
    implemented: str = "YES"          # YES | PARTIAL | NO
    flags: tuple = ()
    scope: Optional[str] = None       # portée connue de resolver.SCOPES
    trigger: str = "always"           # "always" ou condition de contenu (texte)
    gateway: Optional[GatewayDependency] = None
    citation: str = "—"
    numeric_evidence_class: str = "n/a"
    qualification_status: str = "NOT_MEASURED"
    qualification_evidence: str = ""
    limitations: tuple = ()


@dataclass(frozen=True)
class FormatSpec:
    format_id: str
    label_fr: str
    family: str
    extensions: tuple
    gate: str                         # porte d'acceptation (resolver.GATES) ou "never"
    registry_key: Optional[str] = None   # clé de extraction._REGISTRY, si extracteur dédié
    extractor: str = "—"
    executing_service: str = "worker"
    matrix: bool = False              # ligne de la matrice principale
    refusal_code: str = ""
    max_bytes_setting: Optional[str] = "UPLOAD_MAX_FILE_SIZE_MB"
    notes: tuple = ()


VISION = GatewayDependency("vision", "/v1/vision/analyze (tâche describe)", "AI_GATEWAY_VISION_MODEL",
                           stage="enrichissement différé")
VISION_INLINE = GatewayDependency("vision", "/v1/vision/analyze (tâche describe)", "AI_GATEWAY_VISION_MODEL",
                                  stage="ingestion (avant READY)")
VISION_V2 = GatewayDependency("vision", "/v1/vision/capacity + /v1/vision/analyze (V2)", "AI_GATEWAY_VISION_MODEL",
                              stage="enrichissement différé")
STRUCTX = GatewayDependency("structured_extract", "/v1/vision/structured-extract", None, stage="enrichissement différé")
ASR = GatewayDependency("asr", "/v1/audio/transcriptions", "AUDIO_ASR_MODEL_LOGICAL", stage="ingestion (worker_audio)")
EMBEDDINGS = GatewayDependency("embeddings", "/v1/embeddings", None, call_type=CALL_PER_SPACE, stage="indexation et question")
RERANK = GatewayDependency("rerank", "/v1/rerank", "RAG_RERANKER_MODEL_LOGICAL", stage="question (après la recherche)")

# --------------------------------------------------------------------------- #
# Formats (54 entrées d'extension)
# --------------------------------------------------------------------------- #
FORMATS: tuple = (
    FormatSpec("pdf", "PDF", "PDF", (".pdf",), "always", "pdf", "PdfExtractor", matrix=True,
               max_bytes_setting="UPLOAD_MAX_FILE_SIZE_MB", notes=("≤ DOCUMENT_PDF_MAX_PAGES pages",)),
    FormatSpec("docx", "Word (DOCX)", "Office", (".docx", ".dotx"), "always", "docx", "DocxExtractor", matrix=True),
    FormatSpec("pptx", "PowerPoint (PPTX)", "Office", (".pptx",), "pptx", "pptx", "PptxExtractor", matrix=True),
    FormatSpec("xlsx", "Excel (XLSX)", "Office", (".xlsx",), "excel", "excel", "ExcelExtractor", matrix=True,
               notes=("refusé dans le corpus métier partagé",)),
    FormatSpec("csv", "CSV / TSV", "Texte tabulaire", (".csv", ".tsv"), "always", "csv", "CsvExtractor"),
    FormatSpec("txt", "Texte brut", "Texte", (".txt",), "always", "txt", "TextExtractor"),
    FormatSpec("markdown", "Markdown", "Texte", (".md", ".markdown"), "always", "markdown", "MarkdownExtractor"),
    FormatSpec("html", "HTML", "Texte", (".html", ".htm"), "always", "html", "HtmlExtractor"),
    FormatSpec("image", "Images (JPEG, PNG, TIFF, WEBP)", "Image autonome", (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"),
               "image", "image", "ImageExtractor", executing_service="worker_visual", matrix=True,
               max_bytes_setting="DOCUMENT_IMAGE_MAX_BYTES", notes=("sous-formats : DOCUMENT_IMAGE_FORMATS",)),
    FormatSpec("image_optin", "Images GIF / BMP", "Image autonome", (".gif", ".bmp"), "image_optin", "image", "ImageExtractor",
               executing_service="worker_visual", refusal_code="image_unsupported_format",
               notes=("détectées ; acceptées seulement si ajoutées à DOCUMENT_IMAGE_FORMATS",)),
    FormatSpec("eml", "Courriel MIME (EML)", "Courriel", (".eml",), "eml", "eml", "EmlExtractor", matrix=True,
               max_bytes_setting="DOCUMENT_EML_MAX_FILE_SIZE_MB"),
    FormatSpec("audio", "Audio", "Audio", (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".webm"), "audio", "audio",
               "AudioTranscriptExtractor (transcription persistée)", executing_service="worker_audio", matrix=True,
               max_bytes_setting="DOCUMENT_AUDIO_MAX_UPLOAD_BYTES",
               notes=("route dédiée /api/v1/documents/upload-audio/", "audio seul : une piste vidéo est refusée")),
    FormatSpec("xml", "XML", "Structuré", (".xml",), "xml", "xml", "XmlExtractor"),
    FormatSpec("dxf", "CAO DXF", "CAO / BIM", (".dxf",), "dxf", "dxf", "DxfExtractor"),
    FormatSpec("dwg", "CAO DWG", "CAO / BIM", (".dwg",), "dwg", "dwg", "DwgExtractor (LibreDWG → DXF)", executing_service="worker_dwg"),
    FormatSpec("ifc", "BIM IFC", "CAO / BIM", (".ifc",), "ifc", "ifc", "IfcExtractor (ifcopenshell)", executing_service="worker_ifc"),
    FormatSpec("text_fallback", "RTF, SVG, JSON", "Texte brut implicite", (".rtf", ".svg", ".json"), "text_fallback", None,
               "TextExtractor (texte brut, sans extracteur dédié)",
               notes=("accepté comme texte brut par heuristique : le balisage est indexé tel quel",
                      "ne pas considérer comme un format pris en charge")),
    FormatSpec("macro", "Office avec macros", "Refusé", (".docm", ".xlsm", ".pptm"), "never", refusal_code="macro_enabled_*_refused"),
    FormatSpec("legacy_office", "Office ancien (OLE2)", "Refusé", (".doc", ".xls", ".ppt"), "never", refusal_code="legacy_office_format"),
    FormatSpec("opendocument", "OpenDocument", "Refusé", (".odt", ".ods", ".odp"), "never", refusal_code="unsupported_*_format"),
    FormatSpec("archive", "Archives", "Refusé", (".zip", ".7z", ".rar"), "never", refusal_code="archive_not_supported"),
    FormatSpec("image_unsupported", "Images HEIC / AVIF", "Refusé", (".heic", ".avif"), "never", refusal_code="unsupported_file_type"),
    FormatSpec("video", "Vidéo", "Refusé", (".mp4", ".mov", ".mkv"), "never", refusal_code="unsupported_file_type"),
    FormatSpec("opus", "Opus nu", "Refusé", (".opus",), "never", refusal_code="unsupported_audio"),
    FormatSpec("executable", "Exécutables (ELF, PE, Mach-O, script)", "Refusé", (".exe",), "never", refusal_code="executable_refused"),
)

# --------------------------------------------------------------------------- #
# Capacités
# --------------------------------------------------------------------------- #
_NATIVE_TEXT = dict(category="text", content_type="texte natif", output_type="texte découpé en fragments",
                    output_indexed=INDEXED, numeric_evidence_class="native_text")


def _native(fmt, label, description, *, gate="always", executing="worker", consumers=EXTRACT, citation="document",
            numeric="native_text", qualification="REAL_DOCUMENT_QUALIFIED", evidence="qualification interne — documents réels",
            limitations=(), flags=()):
    return CapabilitySpec(f"{fmt}.text", fmt, "text", label, description, "texte natif", gate, executing, consumers,
                          "texte découpé en fragments", INDEXED, flags=flags, citation=citation, numeric_evidence_class=numeric,
                          qualification_status=qualification, qualification_evidence=evidence, limitations=limitations)


def _upload(fmt, gate, flags, *, scope=None, consumers=UPLOAD, description="", limitations=()):
    return CapabilitySpec(f"{fmt}.upload", fmt, "upload", "Acceptation à l'import",
                          description or "Le fichier est accepté si son contenu est reconnu (jamais sur l'extension seule).",
                          "fichier", gate, "backend", consumers, "document créé", NOT_PRODUCED, flags=flags, scope=scope,
                          qualification_status="REAL_DOCUMENT_QUALIFIED", qualification_evidence="qualification interne — documents réels",
                          limitations=limitations)


def _inline_vision(fmt, citation, discovery, evidence_limits):
    return (
        CapabilitySpec(f"{fmt}.inline_image.detection", fmt, "images", "Détection et extraction des images intégrées",
                       "Les images raster intégrées (photographies, captures, illustrations) sont repérées et extraites "
                       "pour être décrites. Elles ne sont PAS lues par OCR.",
                       "image raster intégrée", f"visual_{fmt}", "worker", VISUAL, "image candidate (interne)", INTERNAL_ONLY,
                       flags=("DOCUMENT_VISUAL_ANALYSIS_ENABLED", "DOCUMENT_VISUAL_ANALYSIS_SOURCES"),
                       trigger=discovery, numeric_evidence_class="n/a", qualification_status="PARTIAL",
                       qualification_evidence="qualification interne — visuels bureautiques", limitations=evidence_limits),
        CapabilitySpec(f"{fmt}.inline_image.ocr", fmt, "ocr", "OCR des images intégrées",
                       "Aucun OCR n'est appliqué aux images intégrées : seul le texte des pages sans texte (PDF) est OCRisé.",
                       "image raster intégrée", "never", "worker", EXTRACT, "—", NOT_PRODUCED, implemented="NO",
                       qualification_status="NOT_APPLICABLE"),
        CapabilitySpec(f"{fmt}.inline_image.vision", fmt, "vision", "Description des images intégrées (Vision)",
                       "Chaque image éligible est décrite par AI Gateway (tâche describe) après la mise à disposition du "
                       "document ; la description devient un fragment recherchable.",
                       "image raster intégrée", f"visual_{fmt}", "worker_visual", VISUAL, "description textuelle", INDEXED,
                       flags=("DOCUMENT_VISUAL_ANALYSIS_ENABLED", "DOCUMENT_VISUAL_ANALYSIS_SOURCES", "AI_GATEWAY_VISION_MODEL"),
                       trigger=discovery, gateway=VISION, citation=citation, numeric_evidence_class="ocr_other",
                       qualification_status="PARTIAL", qualification_evidence="qualification interne — partielle (canari)",
                       limitations=("une description n'est pas une extraction de valeurs",
                                    "aucune relation structurée de schéma (tâche diagram non qualifiée)")),
    )


CAPABILITIES: tuple = (
    # ---------------------------------------------------------------- PDF
    _upload("pdf", "always", ()),
    _native("pdf", "Texte natif", "Texte de chaque page (pypdf, puis moteurs de repli si la qualité est insuffisante).",
            citation="page", flags=("DOCUMENT_PDF_NATIVE_FALLBACK_ENABLED",)),
    CapabilitySpec("pdf.ocr", "pdf", "ocr", "OCR des pages sans texte",
                   "Les pages scannées ou sans texte exploitable sont lues par Tesseract, localement.",
                   "page scannée", "ocr", "worker", EXTRACT, "texte OCR", INDEXED, flags=("DOCUMENT_OCR_ENABLED",),
                   trigger="page sans texte natif exploitable", citation="page", numeric_evidence_class="ocr_other",
                   qualification_status="REAL_DOCUMENT_QUALIFIED", qualification_evidence="qualification interne — documents réels",
                   limitations=("Tesseract local (jamais AI Gateway)", "≤ DOCUMENT_OCR_MAX_PAGES pages")),
    *_inline_vision("pdf", "page + localisation de l'image",
                    "image ≥ 96 px de côté, hors tableau natif, bornes par page et par document",
                    ("images recouvrant un tableau natif écartées", "répétitions et images uniformes filtrées")),
    CapabilitySpec("pdf.vector_figures", "pdf", "vision", "Figures vectorielles (rendu puis description)",
                   "Les régions dessinées en vectoriel (schémas, courbes) sont rendues en image puis décrites par Vision.",
                   "figure vectorielle", "pdf_vector", "worker_visual", VISUAL, "description textuelle", INDEXED,
                   flags=("DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED", "DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS"),
                   scope="pdf_vector", trigger="région vectorielle détectée et rail visuel PDF actif", gateway=VISION,
                   citation="page + région", numeric_evidence_class="ocr_other", qualification_status="SYNTHETIC_QUALIFIED",
                   qualification_evidence="qualification interne — canari ; extraction visuelle : NO-GO métier",
                   limitations=("hors espaces listés : seule la couche texte est indexée",)),
    CapabilitySpec("pdf.tables.native_specialized", "pdf", "tables", "Tableaux natifs reconnus",
                   "Tableaux de types reconnus extraits du texte natif (pdfplumber).", "tableau natif", "always", "worker",
                   EXTRACT, "fragment de tableau", INDEXED, trigger="type de tableau reconnu sur page à texte natif",
                   citation="page", numeric_evidence_class="native_table_structured",
                   qualification_status="REAL_DOCUMENT_QUALIFIED", qualification_evidence="qualification interne — tableaux natifs"),
    CapabilitySpec("pdf.tables.native_generic", "pdf", "tables", "Tableaux natifs génériques",
                   "Toute grille native découpée ligne par ligne avec ses cellules.", "tableau natif", "pdf_generic_tables",
                   "worker", EXTRACT, "fragments par ligne", INDEXED,
                   flags=("DOCUMENT_PDF_GENERIC_TABLES_ENABLED", "DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS"),
                   scope="pdf_generic_tables", trigger="grille native détectée", citation="page + cellules",
                   numeric_evidence_class="native_table_structured", qualification_status="PARTIAL",
                   qualification_evidence="qualification interne — requalification des tableaux natifs",
                   limitations=("jamais superposé au rail StructX (espaces exclus)",)),
    CapabilitySpec("pdf.tables.ocr", "pdf", "tables", "Tableaux reconstruits depuis l'OCR",
                   "Reconstruction de tableaux sur des pages OCRisées.", "tableau scanné", "table_ocr", "worker", EXTRACT,
                   "tableau OCR", INDEXED, flags=("DOCUMENT_TABLE_OCR_ENABLED",), trigger="OCR exécuté sur la page",
                   citation="page", numeric_evidence_class="ocr_table_unverified", qualification_status="NOT_QUALIFIED"),
    CapabilitySpec("pdf.charts", "pdf", "charts", "Graphiques PDF",
                   "Pas de lecture native des graphiques PDF : ils ne sont exploités que par la description Vision "
                   "(et StructX dans les espaces listés).", "graphique raster ou vectoriel", "visual_pdf", "worker_visual",
                   VISUAL, "description textuelle", INDEXED, flags=("DOCUMENT_VISUAL_ANALYSIS_ENABLED",),
                   trigger="graphique présent comme image ou région vectorielle", gateway=VISION, citation="page",
                   numeric_evidence_class="ocr_other", qualification_status="PARTIAL",
                   limitations=("aucune valeur de série native",)),
    CapabilitySpec("pdf.mineru", "pdf", "tables", "Rail hybride MinerU (sidecar local)",
                   "Tableaux et texte de pages raster via un service local MinerU.", "page raster", "mineru", "worker",
                   EXTRACT, "tableaux importés", INDEXED, flags=("DOCUMENT_MINERU_HYBRID_ENABLED",),
                   trigger="page raster ou tableau détecté", citation="page", numeric_evidence_class="n/a",
                   qualification_status="NOT_QUALIFIED", limitations=("service local, pas AI Gateway",)),
    # ---------------------------------------------------------------- DOCX
    _upload("docx", "always", ()),
    _native("docx", "Texte du corps", "Paragraphes, titres, listes et tableaux du corps du document.",
            limitations=("en-têtes, pieds de page, notes, commentaires et zones de texte non extraits",)),
    *_inline_vision("docx", "localisation structurelle (pas de numéro de page)",
                    "image embarquée dans le corps (relations embarquées)",
                    ("images liées, SmartArt, formes et EMF/WMF/SVG ignorés",)),
    CapabilitySpec("docx.tables", "docx", "tables", "Tableaux Word", "Tableaux aplatis « cellule | cellule » dans le texte.",
                   "tableau Office", "always", "worker", EXTRACT, "texte", INDEXED, trigger="tableau présent",
                   citation="document", numeric_evidence_class="native_text", qualification_status="PARTIAL",
                   limitations=("pas de fragment de tableau structuré",)),
    CapabilitySpec("docx.office_charts", "docx", "charts", "Graphiques Office natifs",
                   "Les séries en cache des graphiques Office sont lues et rendues comme un tableau dans le texte.",
                   "graphique Office natif", "office_charts", "worker", EXTRACT, "table de séries", INDEXED,
                   flags=("DOCUMENT_OFFICE_CHART_DATA_ENABLED",), trigger="graphique chartN.xml présent",
                   citation="document", numeric_evidence_class="native_text", qualification_status="SYNTHETIC_QUALIFIED",
                   qualification_evidence="RAGATM-23B2", limitations=("graphiques chartex (cx:) non lus",)),
    # ---------------------------------------------------------------- PPTX
    _upload("pptx", "pptx", ("DOCUMENT_PPTX_ENABLED",)),
    _native("pptx", "Texte des diapositives", "Une unité par diapositive : titre, texte, tableaux, texte alternatif signifiant.",
            gate="pptx", citation="numéro et titre de diapositive", numeric="unknown",
            flags=("DOCUMENT_PPTX_ENABLED", "DOCUMENT_PPTX_INCLUDE_ALT_TEXT")),
    CapabilitySpec("pptx.notes", "pptx", "text", "Notes du présentateur", "Inclusion des notes de diapositive.",
                   "notes", "pptx_notes", "worker", EXTRACT, "texte", INDEXED, flags=("DOCUMENT_PPTX_INCLUDE_NOTES",),
                   trigger="notes présentes", citation="diapositive", numeric_evidence_class="unknown",
                   qualification_status="NOT_MEASURED"),
    *_inline_vision("pptx", "numéro de diapositive", "image embarquée dans /ppt/media/",
                    ("images liées et SVG/EMF/WMF ignorés",)),
    CapabilitySpec("pptx.tables", "pptx", "tables", "Tableaux PowerPoint", "Tableaux rendus en Markdown dans l'unité de diapositive.",
                   "tableau Office", "pptx", "worker", EXTRACT, "texte", INDEXED, trigger="tableau présent",
                   citation="diapositive", numeric_evidence_class="unknown", qualification_status="PARTIAL"),
    CapabilitySpec("pptx.office_charts", "pptx", "charts", "Graphiques Office natifs",
                   "Les séries en cache des graphiques sont lues et rendues comme un tableau.", "graphique Office natif",
                   "office_charts", "worker", EXTRACT, "table de séries", INDEXED,
                   flags=("DOCUMENT_OFFICE_CHART_DATA_ENABLED",), trigger="graphique présent", citation="diapositive",
                   numeric_evidence_class="unknown", qualification_status="SYNTHETIC_QUALIFIED",
                   qualification_evidence="RAGATM-23B2"),
    # ---------------------------------------------------------------- XLSX
    _upload("xlsx", "excel", ("DOCUMENT_EXCEL_ENABLED",)),
    CapabilitySpec("xlsx.cells", "xlsx", "tables", "Cellules et plages",
                   "Valeurs des cellules (valeurs en cache ; formules jamais évaluées), découpées par plages.",
                   "cellules", "excel", "worker", EXTRACT, "fragments de plages", INDEXED, flags=("DOCUMENT_EXCEL_ENABLED",),
                   citation="feuille + plage de cellules", numeric_evidence_class="unknown",
                   qualification_status="REAL_DOCUMENT_QUALIFIED", qualification_evidence="qualification interne — documents réels",
                   limitations=("formule sans valeur en cache : marquée à recalculer",)),
    CapabilitySpec("xlsx.text", "xlsx", "text", "Texte des cellules", "Le texte des cellules est indexé avec les plages.",
                   "texte", "excel", "worker", EXTRACT, "fragments de plages", INDEXED, flags=("DOCUMENT_EXCEL_ENABLED",),
                   citation="feuille + plage", numeric_evidence_class="unknown", qualification_status="REAL_DOCUMENT_QUALIFIED"),
    CapabilitySpec("xlsx.images", "xlsx", "images", "Images intégrées", "Les images d'un classeur sont comptées puis ignorées.",
                   "image intégrée", "never", "worker", EXTRACT, "avertissement", NOT_PRODUCED, implemented="NO",
                   qualification_status="NOT_APPLICABLE", limitations=("avertissement excel_visual_content_ignored",)),
    CapabilitySpec("xlsx.charts", "xlsx", "charts", "Graphiques Excel",
                   "Les graphiques et feuilles graphiques d'un classeur sont ignorés (le réglage des graphiques Office "
                   "ne s'applique pas à Excel).", "graphique Excel", "never", "worker", EXTRACT, "—", NOT_PRODUCED,
                   implemented="NO", qualification_status="NOT_APPLICABLE"),
    CapabilitySpec("xlsx.tabular_query", "xlsx", "tables", "Calcul tabulaire à la question",
                   "Moteur de requêtes calculées sur les feuilles, exécuté au moment de la question (pas à l'import).",
                   "cellules", "tabular_query", "backend", ("backend",), "réponse calculée", NOT_PRODUCED,
                   flags=("TABULAR_QUERY_ENABLED",), trigger="question tabulaire", qualification_status="NOT_MEASURED",
                   limitations=("hors ingestion",)),
    # ---------------------------------------------------------------- Images autonomes
    _upload("image", "image", ("DOCUMENT_IMAGE_INGEST_ENABLED", "DOCUMENT_IMAGE_FORMATS")),
    _upload("image_optin", "image_optin", ("DOCUMENT_IMAGE_INGEST_ENABLED", "DOCUMENT_IMAGE_FORMATS"),
            description="GIF et BMP sont reconnus mais refusés tant qu'ils ne figurent pas dans DOCUMENT_IMAGE_FORMATS."),
    CapabilitySpec("image.ocr", "image", "ocr", "OCR de l'image", "Le texte de l'image est lu par Tesseract, localement.",
                   "image", "image", "worker_visual", ("backend", "worker_visual"), "texte OCR", INDEXED,
                   flags=("DOCUMENT_IMAGE_INGEST_ENABLED",), citation="image", numeric_evidence_class="ocr_other",
                   qualification_status="REAL_DOCUMENT_QUALIFIED", limitations=("Tesseract local",)),
    CapabilitySpec("image.vision", "image", "vision", "Description de l'image (Vision)",
                   "L'image est décrite par AI Gateway avant la mise à disposition ; source principale si l'OCR est vide.",
                   "image", "visual_image", "worker_visual", VISUAL, "description textuelle", INDEXED,
                   flags=("DOCUMENT_VISUAL_ANALYSIS_ENABLED", "DOCUMENT_VISUAL_ANALYSIS_SOURCES", "AI_GATEWAY_VISION_MODEL"),
                   gateway=VISION_INLINE, citation="image", numeric_evidence_class="ocr_other", qualification_status="PARTIAL"),
    CapabilitySpec("image.charts", "image", "charts", "Graphique dans une image", "Décrit par Vision ; aucune série native.",
                   "graphique raster", "visual_image", "worker_visual", VISUAL, "description textuelle", INDEXED,
                   flags=("DOCUMENT_VISUAL_ANALYSIS_ENABLED",), trigger="graphique présent", gateway=VISION_INLINE,
                   citation="image", numeric_evidence_class="ocr_other", qualification_status="PARTIAL"),
    # ---------------------------------------------------------------- StructX / Vision V2 (documents visuels)
    CapabilitySpec("visual.structx.table", "visual", "tables", "Tableaux dans les visuels (StructX)",
                   "Après la description, les tableaux repérés dans une image ou un visuel sont structurés en cellules.",
                   "tableau dans une image ou un scan", "structx_table", "worker_visual", ("worker_visual",),
                   "cellules structurées", INDEXED,
                   flags=("STRUCTURED_VISUAL_ENABLED", "STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST", "STRUCTURED_VISUAL_ALLOWED_TASKS",
                          "STRUCTURED_VISUAL_PRODUCTION_ALLOWED", "STRUCTURED_VISUAL_AUTO_ENABLED"),
                   scope="structx", trigger="visuel décrit classé tableau", gateway=STRUCTX, citation="page / localisation",
                   numeric_evidence_class="structured_unqualified", qualification_status="PARTIAL",
                   qualification_evidence="sonde Gateway structured-extract (protocole QP-22A)",
                   limitations=("probant seulement si la qualification de l'extraction est acceptée (structured_accepted)",
                                "non reporté à la réindexation")),
    CapabilitySpec("visual.structx.chart", "visual", "charts", "Séries de graphiques (StructX)",
                   "Extraction de séries depuis un graphique en image.", "graphique raster", "structx_chart", "worker_visual",
                   ("worker_visual",), "séries structurées", INDEXED,
                   flags=("STRUCTURED_VISUAL_ENABLED", "STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST", "STRUCTURED_VISUAL_ALLOWED_TASKS",
                          "STRUCTURED_VISUAL_PRODUCTION_ALLOWED"),
                   scope="structx", trigger="visuel décrit classé graphique", gateway=STRUCTX, citation="page / localisation",
                   numeric_evidence_class="structured_unqualified", qualification_status="NOT_QUALIFIED",
                   qualification_evidence="sonde Gateway structured-extract",
                   limitations=("plafond de production : tâche table seulement",)),
    CapabilitySpec("visual.curve_shadow", "visual", "charts", "Numérisation de courbes (audit fantôme)",
                   "Analyse CPU des courbes en mode fantôme : rien n'est publié dans l'index.", "courbe", "curve_shadow",
                   "worker_visual", ("worker_visual",), "rapport d'audit", INTERNAL_ONLY,
                   flags=("STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED", "STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST"),
                   scope="curve_shadow", trigger="graphique de courbes", qualification_status="EXPERIMENTAL",
                   qualification_evidence="qualification interne — numérisation de courbes (shadow)"),
    CapabilitySpec("visual.vision_v2", "visual", "vision", "Vision V2 (régions recadrées)",
                   "Analyse expérimentale de régions recadrées ; les résultats restent internes et ne sont jamais indexés.",
                   "région de page", "vision_v2", "worker_visual", ("worker_visual",), "résultat interne", INTERNAL_ONLY,
                   flags=("VISUAL_V2_ORCHESTRATION_ENABLED", "VISUAL_V2_RAGSPACE_ALLOWLIST", "VISUAL_V2_PRODUCTION_ALLOWED",
                          "AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS"),
                   scope="vision_v2", trigger="politique d'espace activée", gateway=VISION_V2,
                   qualification_status="EXPERIMENTAL", limitations=("aucun fragment produit",)),
    CapabilitySpec("visual.technical_diagram", "visual", "charts", "Analyse des schémas techniques",
                   "Les schémas, organigrammes et plans ne reçoivent qu'une description : la Gateway refuse les tâches "
                   "diagram et technical_drawing.", "schéma", "never", "worker_visual", ("worker_visual",), "—", NOT_PRODUCED,
                   implemented="PARTIAL", qualification_status="NOT_QUALIFIED",
                   limitations=("aucune relation structurée",)),
    # ---------------------------------------------------------------- Courriels
    _upload("eml", "eml", ("DOCUMENT_EML_ENABLED", "DOCUMENT_EMAIL_GLOBAL_ENABLED", "DOCUMENT_EMAIL_RAGSPACE_IDS"),
            scope="email", description="Accepté si le format est activé et l'espace admis (mode général ou liste)."),
    *(c for fmt in ("eml",) for c in (
        CapabilitySpec(f"{fmt}.body", fmt, "text", "Corps du courriel", "Corps texte et HTML nettoyés, signatures séparées.",
                       "corps", fmt, "worker", EXTRACT, "sections", INDEXED, flags=(f"DOCUMENT_{fmt.upper()}_ENABLED",),
                       scope="email", citation="objet + partie corps", numeric_evidence_class="unknown",
                       qualification_status="SYNTHETIC_QUALIFIED", qualification_evidence="qualification interne — courriels (synthétique)",
                       limitations=("contenu encadré comme non fiable si RAG_EMAIL_UNTRUSTED_FRAMING_ENABLED",)),
        CapabilitySpec(f"{fmt}.attachments", fmt, "email", "Pièces jointes et messages imbriqués",
                       "Chaque pièce jointe est extraite par l'extracteur de SON format ; les messages imbriqués sont parcourus.",
                       "pièce jointe", "email_attachments", "worker", EXTRACT, "fragments de la pièce", INDEXED,
                       flags=("DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED",), scope="email",
                       trigger="pièce jointe présente (bornes de nombre, de taille et de profondeur)",
                       citation="courriel + pièce jointe", numeric_evidence_class="classe du format de la pièce",
                       qualification_status="SYNTHETIC_QUALIFIED", qualification_evidence="qualification interne — courriels (synthétique)",
                       limitations=("IFC/DWG refusés en pièce jointe", "aucune Vision sur les pièces jointes")),
        CapabilitySpec(f"{fmt}.inline_image.ocr", fmt, "ocr", "OCR des images du courriel",
                       "Les images intégrées (CID, logos, photographies) sont traitées comme des pièces jointes image : "
                       "OCR local seulement.", "image intégrée au courriel", "email_attachments", "worker", EXTRACT,
                       "texte OCR", INDEXED, flags=("DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED",), scope="email",
                       trigger="image présente ; sans texte → aucun contenu", citation="courriel + pièce jointe",
                       numeric_evidence_class="ocr_other", qualification_status="SYNTHETIC_QUALIFIED"),
        CapabilitySpec(f"{fmt}.inline_image.vision", fmt, "vision", "Description Vision des images du courriel",
                       "Les images de courriel ne sont jamais envoyées à Vision.", "image intégrée au courriel", "never",
                       "worker", EXTRACT, "—", NOT_PRODUCED, implemented="NO", qualification_status="NOT_APPLICABLE"),
    )),
    # ---------------------------------------------------------------- Audio
    _upload("audio", "audio", ("DOCUMENT_AUDIO_INGEST_ENABLED", "DOCUMENT_AUDIO_GLOBAL_ENABLED", "DOCUMENT_AUDIO_RAGSPACE_IDS",
                               "DOCUMENT_AUDIO_MAX_UPLOAD_BYTES"),
            scope="audio", consumers=("backend", "worker_audio"),
            description="Route dédiée ; accepté si l'audio est activé et l'espace admis (mode général ou liste)."),
    CapabilitySpec("audio.asr", "audio", "audio", "Transcription horodatée (ASR)",
                   "L'audio est préparé (FLAC 16 kHz, parties bornées) puis transcrit par AI Gateway ; la transcription "
                   "est conservée et indexée avec ses plages temporelles.", "parole", "audio", "worker_audio",
                   ("backend", "worker_audio", "worker"), "transcription horodatée", INDEXED,
                   flags=("DOCUMENT_AUDIO_INGEST_ENABLED", "DOCUMENT_AUDIO_GLOBAL_ENABLED", "AUDIO_ASR_MODEL_LOGICAL"),
                   scope="audio", gateway=ASR, citation="plage HH:MM:SS → HH:MM:SS", numeric_evidence_class="asr_transcript",
                   qualification_status="SYNTHETIC_QUALIFIED", qualification_evidence="qualification interne — audio (synthétique)",
                   limitations=("qualité sur enregistrements réels non mesurée", "un seul job de transcription à la fois",
                                "aucune synthèse longue", "aucune analyse visuelle")),
    CapabilitySpec("audio.text", "audio", "text", "Texte transcrit", "Unités de transcription (≤ 1000 caractères, ≤ 120 s).",
                   "transcription", "audio", "worker", ("worker",), "unités horodatées", INDEXED,
                   flags=("DOCUMENT_AUDIO_INGEST_ENABLED",), scope="audio", citation="plage temporelle",
                   numeric_evidence_class="asr_transcript", qualification_status="SYNTHETIC_QUALIFIED"),
    # ---------------------------------------------------------------- Formats structurés / texte
    *(c for fmt, flag, worker in (("xml", "DOCUMENT_XML_ENABLED", "worker"), ("dxf", "DOCUMENT_DXF_ENABLED", "worker"),
                                  ("dwg", "DOCUMENT_DWG_ENABLED", "worker_dwg"), ("ifc", "DOCUMENT_IFC_ENABLED", "worker_ifc"))
      for c in (_upload(fmt, fmt, (flag,), consumers=("backend",) + ((worker,) if worker != "worker" else EXTRACT)),
                CapabilitySpec(f"{fmt}.units", fmt, "structured", "Unités structurées",
                               "Éléments, entités ou attributs extraits avec leur localisation.", "unités", fmt, worker,
                               ((worker,) if worker != "worker" else EXTRACT), "unités", INDEXED, flags=(flag,),
                               citation="localisation structurelle", numeric_evidence_class="unknown",
                               qualification_status="SYNTHETIC_QUALIFIED", qualification_evidence="qualification interne — formats structurés",
                               limitations=("aucun rendu graphique, aucun OCR, aucune Vision",)))),
    *(c for fmt, label in (("csv", "Lignes CSV/TSV"), ("txt", "Texte brut"), ("markdown", "Markdown"), ("html", "Texte HTML"))
      for c in (_upload(fmt, "always", ()), _native(fmt, label, "Extraction locale du texte.", qualification="SYNTHETIC_QUALIFIED",
                                                     limitations=(("images ignorées",) if fmt == "html" else ())))),
    _upload("text_fallback", "text_fallback", (), description="Accepté comme texte brut par heuristique, sans extracteur dédié.",
            limitations=("balisage indexé tel quel",)),
    # ---------------------------------------------------------------- Commun
    CapabilitySpec("common.chunking", "common", "common", "Découpage en fragments", "Découpage déterministe des sorties indexées.",
                   "toutes sorties", "always", "worker", EXTRACT, "fragments", INDEXED, qualification_status="REAL_DOCUMENT_QUALIFIED"),
    CapabilitySpec("common.embeddings", "common", "common", "Vectorisation (embeddings)",
                   "Fournisseur et modèle propres à CHAQUE RagSpace (AI Gateway ou fournisseur direct).", "fragments",
                   "always", "worker", EXTRACT, "vecteurs", INDEXED,
                   flags=("AI_GATEWAY_EMBEDDINGS_ENABLED", "DEFAULT_RAGSPACE_EMBEDDING_PROVIDER", "DEFAULT_RAGSPACE_EMBEDDING_MODEL"),
                   gateway=EMBEDDINGS, qualification_status="REAL_DOCUMENT_QUALIFIED",
                   limitations=("le défaut ne concerne que les nouveaux espaces",)),
    CapabilitySpec("common.rerank", "common", "common", "Reclassement des résultats",
                   "Reclassement des passages retrouvés, au moment de la question.", "passages", "reranker", "backend",
                   ("backend",), "classement", NOT_PRODUCED, flags=("RAG_RERANKER_ENABLED", "RAG_RERANKER_MODEL_LOGICAL"),
                   gateway=RERANK, qualification_status="PARTIAL", limitations=("intervient à la question, pas à l'import",)),
    CapabilitySpec("common.citations", "common", "common", "Citations", "Sources normalisées avec la localisation propre au format.",
                   "sources", "always", "backend", ("backend",), "citations", NOT_PRODUCED,
                   qualification_status="REAL_DOCUMENT_QUALIFIED"),
)

#: Flags de capacité volontairement NON référencés (bornes, réglages fins ou hors périmètre),
#: pour que le test de couverture distingue un oubli d'un choix.
EXCLUDED_FLAGS = frozenset({
    "DOCUMENT_IMAGE_PREPROCESS_ENABLED", "DOCUMENT_OCR_AUTO_ROTATE", "DOCUMENT_OCR_COMPUTE_CONFIDENCE",
    "AI_GATEWAY_ENABLED", "AI_GATEWAY_STREAMING_ENABLED", "AI_GATEWAY_FALLBACK_ENABLED", "RAG_RERANK_ENABLED",
    "TABULAR_LLM_PLANNER_ENABLED", "TABULAR_NARRATION_ENABLED", "DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS",
    "RAG_EMAIL_UNTRUSTED_FRAMING_ENABLED", "DOCUMENT_AUDIO_MAX_DURATION_SECONDS",
    # Édition Open : le format Outlook `.msg` n'existe plus (parseur GPL écarté) ; le flag
    # est conservé pour la compatibilité des réglages mais ne peut rien ouvrir.
    "DOCUMENT_MSG_ENABLED",
})


def capabilities() -> tuple:
    return CAPABILITIES


def formats() -> tuple:
    return FORMATS


def referenced_flags() -> frozenset:
    """Tous les réglages lus pour résoudre une capacité (base de l'empreinte des workers)."""
    out = set()
    for c in CAPABILITIES:
        out.update(c.flags)
        if c.gateway and c.gateway.model_setting:
            out.add(c.gateway.model_setting)
    return frozenset(out)


def consumers_by_flag() -> dict:
    """Rôles qui CONSOMMENT réellement chaque flag (union des capacités qui le lisent)."""
    out: dict = {}
    for c in CAPABILITIES:
        names = set(c.flags) | ({c.gateway.model_setting} if c.gateway and c.gateway.model_setting else set())
        for name in names:
            out.setdefault(name, set()).update(c.consumers)
    return {k: frozenset(v) for k, v in out.items()}
