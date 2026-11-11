"""Domaine ``ingestion`` — upload, extraction documentaire et OCR.

Toute limite atteinte doit produire un CODE explicite, jamais une troncature
silencieuse : les bornes ci-dessous sont des garde-fous de sécurité (zip-bomb,
décompression, temps worker), pas des réglages de confort.
"""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    # --- Limites d'upload ----------------------------------------------------
    D(
        name="UPLOAD_MAX_FILE_SIZE_MB",
        domain="ingestion",
        value_type="float",
        # Mio BINAIRES : `settings.base` fait `× 1024 × 1024` (50 -> 52 428 800 octets).
        # L'UI affiche « Mo » ; la convention produit est « calcul binaire, libellé Mo ».
        default=50.0,
        minimum=0.1,
        maximum=10000.0,
        description=(
            "Taille maximale d'un fichier uploadé. Aligne aussi le seuil de "
            "bufferisation mémoire de Django (DATA_UPLOAD_MAX_MEMORY_SIZE)."
        ),
        consumers=("api.v1.documents", "config.settings.base"),
    ),
    D(
        name="UPLOAD_PDF_MAX_PAGES",
        domain="ingestion",
        value_type="int",
        default=500,
        minimum=1,
        maximum=100000,
        description="Nombre maximal de pages d'un PDF accepté à l'upload.",
        consumers=("api.v1.documents",),
    ),
    D(
        name="UPLOAD_EXTRACTION_TIMEOUT_S",
        domain="ingestion",
        value_type="int",
        default=180,
        minimum=1,
        maximum=3600,
        description=(
            "Budget d'extraction de texte d'un PDF : évite qu'un fichier corrompu bloque "
            "un worker. ≥ budget OCR pour éviter les faux « timeout »."
        ),
        consumers=("apps.rag_core.services.ingest",),
    ),
    # --- Extraction : global -------------------------------------------------
    D(
        name="DOCUMENT_MAX_UPLOAD_SIZE_MB",
        domain="ingestion",
        value_type="float",
        default=50.0,
        default_expr="= UPLOAD_MAX_FILE_SIZE_MB (50)",
        minimum=0.1,
        maximum=10000.0,
        description="Taille maximale acceptée par le socle d'extraction multi-format.",
        consumers=("apps.rag_core.services.ingest.extraction",),
        default_is_dynamic=True,
    ),
    D(
        name="DOCUMENT_MAX_EXTRACTED_CHARS",
        domain="ingestion",
        value_type="int",
        default=5000000,
        minimum=1000,
        maximum=100000000,
        description="Volume maximal de texte extrait, tous formats confondus.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXTRACTION_TIMEOUT_SECONDS",
        domain="ingestion",
        value_type="int",
        default=180,
        default_expr="= UPLOAD_EXTRACTION_TIMEOUT_S (180)",
        minimum=1,
        maximum=3600,
        description="Budget global d'extraction, tous formats confondus.",
        consumers=("apps.rag_core.services.ingest.extraction",),
        default_is_dynamic=True,
    ),
    D(
        name="DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS",
        domain="ingestion",
        value_type="int",
        default=480,
        minimum=1,
        maximum=3600,
        description=(
            "Budget d'extraction de la stratégie de reprise « délai étendu ». Doit "
            "rester SOUS la limite douce Celery de la tâche qui l'exécute (marge de "
            "sécurité vérifiée par `manage.py check`) : au-delà, la tâche serait tuée "
            "avant la fin de l'extraction, ce qui produirait un échec moins lisible "
            "que le dépassement de délai qu'on cherchait à corriger."
        ),
        consumers=("apps.rag_core.services.ingest.retry_strategy",),
    ),
    D(
        name="DOCUMENT_PDF_MAX_PAGES",
        domain="ingestion",
        value_type="int",
        default=500,
        default_expr="= UPLOAD_PDF_MAX_PAGES (500)",
        minimum=1,
        maximum=100000,
        description="Nombre maximal de pages traitées par l'extracteur PDF.",
        consumers=("apps.rag_core.services.ingest.extraction",),
        default_is_dynamic=True,
    ),
    # --- PDF : fallback natif ------------------------------------------------
    D(
        name="DOCUMENT_PDF_NATIVE_FALLBACK_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=True,
        description=(
            "Fallback multi-moteur tenté quand pypdf n'extrait pas assez de texte. "
            "LOCAL, sans OCR ni réseau."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_NATIVE_FALLBACK_ENGINES",
        domain="ingestion",
        value_type="list",
        default=["pymupdf", "pdfminer", "pdfplumber"],
        description="Ordre d'essai des moteurs d'extraction PDF natifs.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_MIN_TOTAL_CHARS",
        domain="ingestion",
        value_type="int",
        default=200,
        minimum=0,
        maximum=1000000,
        description="En dessous de ce volume extrait, le PDF est considéré image-only.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- OCR -----------------------------------------------------------------
    D(
        name="DOCUMENT_OCR_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description="OCR des PDF image-only. Désactivé par défaut : aucun appel sans configuration.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_OCR_PROVIDER",
        domain="ingestion",
        value_type="str",
        default="tesseract",
        description="Moteur OCR. Seul `tesseract` (local) est implémenté.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_OCR_TIMEOUT_SECONDS",
        domain="ingestion",
        value_type="int",
        default=60,
        minimum=1,
        maximum=3600,
        description=(
            "Budget OCR par PAGE (total : OSD, texte et confiance se le partagent). "
            "Un document de 40 pages consomme 40 budgets a l'interieur du budget "
            "d'extraction du document."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_OCR_MAX_PAGES",
        domain="ingestion",
        value_type="int",
        default=50,
        minimum=1,
        maximum=10000,
        description="Nombre maximal de pages OCRisées par document.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_OCR_MIN_CHARS_PER_PAGE",
        domain="ingestion",
        value_type="int",
        default=24,
        minimum=0,
        maximum=100000,
        description="En dessous de ce volume par page, la page est jugée non textuelle.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_OCR_LANGUAGES",
        domain="ingestion",
        value_type="str",
        default="fra+eng",
        description="Langues Tesseract (syntaxe `fra+eng`).",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_OCR_RASTER_SCALE",
        domain="ingestion",
        value_type="float",
        default=2.0,
        minimum=0.5,
        maximum=10.0,
        description="Échelle de rastérisation PDF→image avant OCR (2 ≈ 144 DPI).",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_OCR_AUTO_ROTATE",
        domain="ingestion",
        value_type="bool",
        default=True,
        description="Correction d'orientation (OSD) avant OCR.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_OCR_COMPUTE_CONFIDENCE",
        domain="ingestion",
        value_type="bool",
        default=True,
        description=(
            "Calcule la confiance OCR via un SECOND passage Tesseract (image_to_data). "
            "Mesuré : 36 % du temps OCR médian pour une valeur qu'aucune décision ne lit. "
            "À 0, le texte indexé est strictement identique ; seuls disparaissent "
            "ocr_confidence, page_ocr_confidences et le warning ocr_low_confidence."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- Images --------------------------------------------------------------
    D(
        name="DOCUMENT_IMAGE_MAX_BYTES",
        domain="ingestion",
        value_type="int",
        default=0,
        minimum=0,
        description="Taille maximale d'une image. 0 => repli sur la limite d'upload globale.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_IMAGE_MAX_PIXELS",
        domain="ingestion",
        value_type="int",
        default=40000000,
        minimum=1,
        description="Garde anti décompression-bomb (nombre total de pixels).",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_IMAGE_PREPROCESS_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=True,
        description="Prétraitement d'image avant OCR (contraste, redressement).",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_IMAGE_INGEST_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=True,
        description=(
            "Porte d'ingestion image, INDÉPENDANTE de DOCUMENT_OCR_ENABLED : une image "
            "acceptée est OCRisée automatiquement. 0 => tout upload image est refusé."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_IMAGE_FORMATS",
        domain="ingestion",
        value_type="list",
        default=["jpeg", "png", "tiff", "webp"],
        description="Allowlist des formats image. GIF/BMP => `image_unsupported_format`.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- Analyse visuelle via l'AI Gateway -----------------
    D(
        name="DOCUMENT_VISUAL_ANALYSIS_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Analyse visuelle via l'AI Gateway (`POST /v1/vision/analyze`) des sources "
            "listées dans DOCUMENT_VISUAL_ANALYSIS_SOURCES. FERMÉE par défaut. Active : "
            "l'OCR reste exécuté ; la vision l'ENRICHIT, ou devient l'évidence PRIMARY "
            "d'une image sans texte. Ingestion d'image toujours asynchrone, routée vers la "
            "file dédiée `ingest_visual` : n'activer QUE si un worker "
            "`--queues=ingest_visual` tourne et après la qualification réelle du Gateway."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
        notes=(
            "source `image` (image autonome) ; source "
            "`pdf` (visuels INTÉGRÉS d'un PDF, sans rendu de page) ; "
            "source `docx` (visuels INTÉGRÉS d'un DOCX, sans rendu ; un DOCX n'étant pas "
            "paginé avant rendu, ces fragments sont publiés SANS numéro de page). "
            "source `pptx` (images raster INTÉGRÉES des "
            "diapositives, sans rendu ni vectoriel ; une diapositive n'est pas une page : "
            "fragments cités par `slide_number`, sans numéro de page). "
            "Éteindre la fonctionnalité ne retire jamais en silence une évidence visuelle "
            "publiée : la réindexation est refusée (`visual_analysis_disabled_would_degrade`) "
            "sauf opt-in explicite `drop_visual_evidence`."
        ),
    ),
    D(
        name="DOCUMENT_VISUAL_ANALYSIS_SOURCES",
        domain="ingestion",
        value_type="list",
        default=["image"],
        description=(
            "Natures de source soumises à l'analyse visuelle quand "
            "DOCUMENT_VISUAL_ANALYSIS_ENABLED=1. `image` (image autonome), `pdf`, `docx` "
            "et `pptx` (visuels INTÉGRÉS, rasters seulement) sont opérationnelles ; toute "
            "autre valeur est IGNORÉE. Le défaut n'active que `image` : ouvrir `pdf`, "
            "`docx` ou `pptx` route aussi ces documents vers la file `ingest_visual` et "
            "force leur ingestion en asynchrone. Chaque source a sa propre porte ; `pptx` "
            "exige en plus DOCUMENT_PPTX_ENABLED=1."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- Vision V2 orchestrée des crops — ÉTEINTE ---------------
    D(
        name="VISUAL_V2_ORCHESTRATION_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Orchestration Vision V2 des crops (`DocumentVersion → Page → PageRaster → Crop "
            "ragatm-crop/2 → VisualCropJob → POST /v1/vision/analyze` schéma 2). ÉTEINTE par "
            "défaut : à 0, aucune tâche, aucun appel réseau, rail V1 inchangé. Allumée, elle "
            "n'agit QUE sur les espaces de VISUAL_V2_RAGSPACE_ALLOWLIST, si `/health/vision` "
            "sert le schéma 2 avec une empreinte de AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.raster.orchestration",
                   "apps.rag_core.services.ingest.indexing"),
        notes=(
            "Ne suffit JAMAIS en production (`DJANGO_ENV=prod`) : il faut en plus "
            "VISUAL_V2_PRODUCTION_ALLOWED. Résultat interne seulement (`VisualCropJob."
            "result_raw`) : aucune citation, aucun champ `Chunk.meta`. Exige un worker "
            "`--queues=ingest_visual`."
        ),
    ),
    D(
        name="VISUAL_V2_RAGSPACE_ALLOWLIST",
        domain="ingestion",
        value_type="list",
        default=[],
        description=(
            "Identifiants des RagSpace soumis à l'orchestration Vision V2. VIDE = AUCUN "
            "espace, même drapeau allumé. Dans un espace listé (et préflight V2 conforme), "
            "le rail V1 DIFFÉRÉ ne traite plus les PDF : un seul rail visuel par document."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.raster.orchestration",),
    ),
    D(
        name="VISUAL_V2_PRODUCTION_ALLOWED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Garde DURE de production de l'orchestration Vision V2 : avec `DJANGO_ENV` = "
            "`prod`/`production`, l'orchestration est refusée tant que ce réglage dédié est "
            "faux, quel que soit VISUAL_V2_ORCHESTRATION_ENABLED."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.raster.orchestration",),
        notes="Production Gateway : V1 seulement — laisser à 0.",
    ),
    D(
        name="VISUAL_V2_RASTER_DPI",
        domain="ingestion",
        value_type="int",
        default=200,
        minimum=150,
        maximum=300,
        description=(
            "DPI UNIQUE du rendu de page de l'orchestration Vision V2 (`ragatm-pdfium/1`). "
            "Entre dans `params_sha256` donc dans l'identité du `PageRaster` : le changer "
            "crée d'autres rasters, crops et jobs. Valeurs supportées : 150, 200, 300 "
            "(toute autre valeur ferme l'orchestration)."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.raster.orchestration",),
    ),
    # --- Extraction structurée expérimentale ---
    D(
        name="STRUCTURED_VISUAL_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Extraction structurée EXPÉRIMENTALE d'un visuel déjà découvert (`POST "
            "/v1/vision/structured-extract`, contrat `structured-extract/1`, tâches `chart` / "
            "`table`). ÉTEINTE par défaut. Allumée, elle agit par la commande "
            "`structured_visual_extract` (visuel et tâche explicites) et, si "
            "STRUCTURED_VISUAL_AUTO_ENABLED, automatiquement après le `describe` ; toujours sur "
            "les seuls espaces de STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST. Avec `DJANGO_ENV` = "
            "`prod`/`production`, refusée sauf STRUCTURED_VISUAL_PRODUCTION_ALLOWED (sinon réglage "
            "ignoré, avertissement rag_core.W130) ; `DJANGO_ENV` inconnu : toujours refusée."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual.structured",),
        notes=(
            "Fragments ajoutés sous `meta.visual.structured`, `requires_visual_check` = vrai ; "
            "ni reportés à la réindexation, ni comptés comme évidence du rail `describe`."
        ),
    ),
    D(
        name="STRUCTURED_VISUAL_AUTO_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Extraction structurée AUTOMATIQUE : après l'enrichissement visuel différé d'une "
            "génération, chaque visuel est classé (CHART / TABLE / OTHER / UNKNOWN) depuis son "
            "évidence `describe`, puis extrait avec la tâche correspondante — aucune commande. "
            "Exige aussi STRUCTURED_VISUAL_ENABLED, l'allowlist d'espaces et un environnement "
            "admis (production : STRUCTURED_VISUAL_PRODUCTION_ALLOWED) ; une classe dont la tâche est "
            "exclue (STRUCTURED_VISUAL_ALLOWED_TASKS) est ignorée sans appel. ÉTEINTE par défaut."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual.structured_auto",),
        notes=(
            "Une réindexation régénère les extractions (nouvelle génération). GPU occupé : "
            "reprise persistée et bornée (Retry-After), jamais de sommeil dans un worker."
        ),
    ),
    D(
        name="STRUCTURED_VISUAL_PDF_MAX_ASSETS",
        domain="ingestion",
        value_type="int",
        default=0,
        minimum=0,
        maximum=32,
        description=(
            "Borne de visuels PDF admis par document pour les SEULS espaces de "
            "STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST (drapeau allumé, environnement admis). 0 = "
            "hériter de DOCUMENT_PDF_VISUAL_MAX_ASSETS. Ne change que le budget de la "
            "sélection : heuristiques, ordre, borne par page et classification inchangés."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual.structured",
                   "apps.rag_core.services.ingest.indexing",
                   "apps.rag_core.services.ingest.visual.enrichment"),
        notes=(
            "Plafonné à 32 = borne du rail visuel (MAX_ASSETS_PER_DOCUMENT). Chaque visuel "
            "admis coûte un appel Vision describe, et un appel structured-extract s'il est "
            "classé graphique/tableau."
        ),
    ),
    D(
        name="STRUCTURED_VISUAL_PDF_MAX_PER_PAGE",
        domain="ingestion",
        value_type="int",
        default=0,
        minimum=0,
        maximum=8,
        description=(
            "Borne de visuels PDF admis PAR PAGE pour les SEULS espaces de "
            "STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST (drapeau allumé, environnement admis). 0 = "
            "hériter de DOCUMENT_PDF_VISUAL_MAX_PER_PAGE (4). Agit pendant S4-prop (vivier au-"
            "delà du budget) et à l'admission ; la borne par document reste la seule borne globale."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual.structured",
                   "apps.rag_core.services.ingest.indexing",
                   "apps.rag_core.services.ingest.visual.enrichment"),
        notes="Planches à 6-7 graphiques par page.",
    ),
    D(
        name="STRUCTURED_VISUAL_PRODUCTION_ALLOWED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Garde DURE de production de l'extraction structurée (même modèle que "
            "VISUAL_V2_PRODUCTION_ALLOWED) : avec `DJANGO_ENV` = `prod`/`production`, toute "
            "extraction est refusée tant que ce réglage est faux. Vrai, il ne suffit JAMAIS seul : "
            "il faut aussi STRUCTURED_VISUAL_ENABLED et l'espace dans "
            "STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST ; les tâches restent plafonnées à `table`. "
            "Sans effet hors production ; n'ouvre jamais le shadow des courbes."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual.structured",
                   "apps.rag_core.checks_structured_visual"),
        notes="Blocage B1 de la readiness de production.",
    ),
    D(
        name="STRUCTURED_VISUAL_ALLOWED_TASKS",
        domain="ingestion",
        value_type="list",
        default=[],
        description=(
            "Tâches structurées admises (`chart`, `table`). VIDE = toutes hors production ; en "
            "production, plafond CODÉ à `table` qu'aucune valeur n'élargit. Une valeur inconnue "
            "est ignorée ; une liste sans valeur valide n'admet rien. Une tâche exclue n'est ni "
            "appelée, ni publiée, ni indexée (classe ignorée par le chemin automatique, commande "
            "refusée, publication refusée)."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual.structured",
                   "apps.rag_core.services.ingest.visual.structured_auto",
                   "apps.rag_core.checks_structured_visual"),
        notes=("Blocage B2 : publier les tableaux est une "
               "décision de périmètre, pas une certification de leurs valeurs."),
    ),
    D(
        name="STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST",
        domain="ingestion",
        value_type="list",
        default=[],
        description=(
            "Identifiants des RagSpace où l'extraction structurée expérimentale peut "
            "publier. VIDE = AUCUN espace, même drapeau allumé."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual.structured",),
    ),
    D(
        name="STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Numérisation des courbes en mode SHADOW : "
            "après l'extraction structurée d'un graphique, le prototype CPU figé du lot 13B "
            "calcule les extrema de chaque série depuis les pixels et l'OCR déjà obtenus. "
            "Résultats EN MÉMOIRE seulement (objet rendu à l'appelant, harnais d'audit) : ni "
            "fragment, ni index, ni prompt, ni API, ni base. ÉTEINTE par défaut ; exige aussi "
            "STRUCTURED_VISUAL_ENABLED, l'espace dans les deux allowlists et `DJANGO_ENV` hors "
            "`prod`/`production` (refus même drapeaux allumés)."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual.curve_shadow",),
        notes=(
            "Aucun appel Gateway ni GPU supplémentaire. Précision numérique NON qualifiée "
            "(couverture 41,7 % au lot 13B, aucune annotation humaine indépendante)."
        ),
    ),
    D(
        name="STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST",
        domain="ingestion",
        value_type="list",
        default=[],
        description=(
            "Identifiants des RagSpace où la numérisation SHADOW des courbes peut tourner, "
            "en plus de STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST. VIDE = AUCUN espace, même "
            "drapeau allumé."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual.curve_shadow",),
    ),
    # --- Visuels PDF : bornes génériques ------------------
    D(
        name="DOCUMENT_PDF_VISUAL_MAX_ASSETS",
        domain="ingestion",
        value_type="int",
        default=12,
        minimum=0,
        maximum=1000,
        description=(
            "Visuels intégrés ADMIS au plus par PDF, donc tentatives Gateway au plus par "
            "passage du rail (jamais un nombre garanti de réussites). Borne le coût, pas la "
            "découverte : au-delà, les visuels sont choisis sur TOUT le document (sélection "
            "S4-prop) et le reste est compté (`omitted_limit`) ; un PDF par ailleurs riche en "
            "texte n'échoue jamais."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VISUAL_MAX_PER_PAGE",
        domain="ingestion",
        value_type="int",
        default=4,
        minimum=0,
        maximum=1000,
        description="Visuels admis au plus par PAGE : une planche ne monopolise pas le budget.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VISUAL_MIN_SIDE_PX",
        domain="ingestion",
        value_type="int",
        default=96,
        minimum=0,
        maximum=10000,
        description="Côté minimal (px) d'un visuel admissible : écarte puces, filets et icônes.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VISUAL_MIN_PIXELS",
        domain="ingestion",
        value_type="int",
        default=20000,
        minimum=0,
        maximum=100_000_000,
        description="Aire minimale (px²) d'un visuel admissible.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VISUAL_MAX_PIXELS",
        domain="ingestion",
        value_type="int",
        default=2_000_000,
        minimum=1,
        maximum=100_000_000,
        description=(
            "Aire maximale (px²) envoyée au Gateway vision (contrat V1 : 2 Mpx). Au-delà, "
            "le visuel est RÉDUIT de façon déterministe, jamais écarté."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VISUAL_MAX_BYTES",
        domain="ingestion",
        value_type="int",
        default=8_000_000,
        minimum=1,
        maximum=100_000_000,
        description="Octets max d'un visuel PDF APRÈS normalisation PNG.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VISUAL_TABLE_OVERLAP_RATIO",
        domain="ingestion",
        value_type="float",
        default=0.6,
        minimum=0.0,
        maximum=1.0,
        description=(
            "Recouvrement au-delà duquel un visuel est réputé couvrir une région "
            "tabulaire déjà extraite nativement : l'évidence native fait foi, le visuel "
            "est écarté (aucun doublon appauvri)."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VISUAL_MAX_REPEATS",
        domain="ingestion",
        value_type="int",
        default=3,
        minimum=1,
        maximum=1000,
        description=(
            "Occurrences au-delà desquelles des octets identiques sont réputés DÉCORATIFS "
            "(logo, bandeau, filigrane) et écartés. Signal générique — répétition "
            "d'empreinte — jamais un nom de fichier ni un texte de marque."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- Régions vectorielles rendues -------------------
    D(
        name="DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Graphiques et schémas VECTORIELS des PDF : détection bornée des régions de tracés, rendu "
            "raster de la SEULE région retenue (`origin=rendered`), analyse par le rail visuel existant, "
            "budget visuel partagé avec les images intégrées. Exige aussi l'espace dans "
            "DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS et la source `pdf` de l'analyse visuelle. Éteint : "
            "chemin historique inchangé."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS",
        domain="ingestion",
        value_type="list",
        default=[],
        description=(
            "UUID COMPLETS des RagSpace où les régions vectorielles sont rendues. VIDE = AUCUN espace ; "
            "une entrée qui n'est pas un UUID complet invalide toute la liste."
        ),
        runtime_editable=False, max_scope="environment", restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VECTOR_REGION_RENDER_DPI",
        domain="ingestion",
        value_type="int",
        default=150,
        minimum=72,
        maximum=300,
        description=(
            "Résolution de rendu d'une région vectorielle ; réduite automatiquement sous "
            "DOCUMENT_PDF_VISUAL_MAX_PIXELS (jamais l'inverse)."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_VECTOR_REGION_MAX_CANDIDATES",
        domain="ingestion",
        value_type="int",
        default=24,
        minimum=0,
        maximum=200,
        description=(
            "Régions vectorielles candidates retenues par document AVANT les filtres de pixels et la "
            "sélection commune (le nombre d'appels Gateway reste borné par DOCUMENT_PDF_VISUAL_MAX_ASSETS)."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- Tableaux ------------------------------------------------------------
    D(
        name="DOCUMENT_TABLE_MAX_PER_DOC",
        domain="ingestion",
        value_type="int",
        default=50,
        minimum=0,
        maximum=10000,
        description="Nombre maximal de tableaux structurés extraits par document.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_TABLE_MAX_CHARS",
        domain="ingestion",
        value_type="int",
        default=100000,
        minimum=100,
        maximum=1000000,
        description=(
            "Volume maximal du rendu textuel d'UN tableau, avant fragmentation. "
            "Ce n'est pas une borne d'embeddings : celle-ci est le budget de "
            "découpage de l'espace, qui applique déjà le plafond du modèle gouverné."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_TABLE_MAX_FRAGMENTS",
        domain="ingestion",
        value_type="int",
        default=40,
        minimum=1,
        maximum=10000,
        description=(
            "Nombre maximal de fragments produits par UN tableau. Borne "
            "d'explosion exprimée dans l'unité qui compte depuis qu'un tableau "
            "peut donner plusieurs chunks."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_TABLE_OCR_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Reconstruction de tableaux depuis l'OCR (documents scannés). OPT-IN : "
            "fragile et coûteux ; les tableaux sont marqués `confidence=low`."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_GENERIC_TABLES_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Tableaux PDF génériques : reconstruit ligne, colonne et page source "
            "depuis la mise en page. OPT-IN ; sans lui l'indexation reste par page."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS",
        domain="ingestion",
        value_type="list",
        default=[],
        description=(
            "UUID COMPLETS des RagSpace où le rail tabulaire générique PDF s'applique quand "
            "DOCUMENT_PDF_GENERIC_TABLES_ENABLED est allumé. VIDE = AUCUN espace ; une entrée "
            "qui n'est pas un UUID complet invalide toute la liste (aucun espace) ; un espace de "
            "STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST est toujours exclu (rail visuel)."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- Rail hybride Document Vision (MinerU) — PROTOTYPE hors ligne, éteint ---
    D(
        name="DOCUMENT_MINERU_HYBRID_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Rail hybride Document Vision : MinerU en ENRICHISSEMENT sélectif, page "
            "par page (raster sans texte natif, tableaux, géométrie). L'extraction "
            "native garde l'autorité et n'est jamais remplacée. OPT-IN ; éteint, le "
            "chemin d'extraction est celui de la ligne de base."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="DOCUMENT_MINERU_SIDECAR_URL",
        domain="ingestion",
        value_type="str",
        default="http://127.0.0.1:16580/v1",
        description=(
            "Serveur de parsing MinerU LOCAL. Seule une boucle locale est acceptée : "
            "toute autre cible est refusée sans connexion, et mineru.net n'est jamais "
            "joignable."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="DOCUMENT_MINERU_PAGE_TIMEOUT_S",
        domain="ingestion",
        value_type="int",
        default=60,
        minimum=1,
        maximum=900,
        description=(
            "Budget MinerU par page. 60 s vient de la mesure : p50 = 5,5 s, "
            "max observé = 45,1 s. Dépassement : repli sur le chemin actuel."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="DOCUMENT_MINERU_DOCUMENT_TIMEOUT_S",
        domain="ingestion",
        value_type="int",
        default=300,
        minimum=1,
        maximum=3600,
        description=(
            "Budget MinerU par document. N'entame aucun budget Celery existant : le "
            "rail sélectif ne traite que les pages qui en ont besoin."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="DOCUMENT_MINERU_TIER",
        domain="ingestion",
        value_type="str",
        default="basic",
        description="Profil de modèles MinerU (ONNX CPU). Figé au prototype.",
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="DOCUMENT_MINERU_OUTPUT_FORMAT",
        domain="ingestion",
        value_type="str",
        default="middle_json",
        description=(
            "Format de sortie MinerU. `middle_json` est le SEUL qui porte la "
            "géométrie ; la sortie markdown la jette."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="DOCUMENT_MINERU_MAX_PAGES_PER_DOC",
        domain="ingestion",
        value_type="int",
        default=3,
        minimum=0,
        maximum=50,
        description=(
            "Plafond de pages routées vers MinerU pour UN document. Borne le coût du "
            "rail indépendamment du nombre de pages raster : au-delà, les pages "
            "restent sur le chemin actuel. Combiné au budget Celery restant, c'est le "
            "plus petit des deux qui s'applique."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="DOCUMENT_MINERU_SIDECAR_ALLOWED_HOSTS",
        domain="ingestion",
        value_type="str",
        default="",
        description=(
            "Hôtes acceptés pour le sidecar EN PLUS de la boucle locale, séparés par "
            "des virgules. Nécessaire quand le sidecar vit sur un pont Docker interne "
            "et n'est joignable que par son nom de conteneur. Déclarer un nom ne "
            "suffit pas : le client exige que toutes les adresses résolues soient "
            "privées, donc un nom pointant vers l'extérieur reste refusé."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="DOCUMENT_MINERU_MAX_INLINE_BYTES",
        domain="ingestion",
        value_type="int",
        default=33554432,
        minimum=1024,
        maximum=268435456,
        description=(
            "Plafond du document transmis EN LIGNE au sidecar Document Vision. "
            "Le document voyage dans la requête plutôt que par un volume partagé, "
            "ce qui évite de monter un répertoire documentaire dans le conteneur "
            "du moteur. 32 Mio vient de la mesure : sur 3443 documents stockés, le "
            "plus gros pèse 24,4 Mo et le p99 10,6 Mo, aucun ne dépasse ce plafond."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="MINERU_SERVICE_USE_SCOPE",
        domain="ingestion",
        value_type="str",
        default="unknown",
        choices=("personal_internal", "third_party_service", "unknown"),
        description=(
            "Périmètre d'usage DÉCLARÉ du déploiement, pour la porte licence "
            "MinerU. La licence déclenche son obligation d'attribution sur un "
            "fait précis — fournir un service en ligne à des tiers — et non sur "
            "l'installation du moteur. Ce réglage porte donc la déclaration "
            "humaine de ce fait ; le code ne tente jamais de le déduire. "
            "`unknown` (défaut) BLOQUE : une valeur absente ou non reconnue ne "
            "vaut pas « usage personnel »."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="MINERU_SERVICE_ATTRIBUTION_SATISFIED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Porte de service liée à la licence MinerU : un service en ligne fourni à "
            "des tiers doit indiquer clairement l'usage de MinerU, sous peine de "
            "résiliation automatique de la licence. Le prototype hors ligne n'est pas "
            "concerné ; toute activation hors prototype doit vérifier ce drapeau."
        ),
        consumers=("apps.rag_core.services.ingest.docvision",),
    ),
    D(
        name="DOCUMENT_TABLE_MAX_ROW_CHUNKS",
        domain="ingestion",
        value_type="int",
        default=2000,
        minimum=0,
        maximum=100000,
        description=(
            "Nombre maximal de fragments (ligne logique x page) émis par le rail "
            "tabulaire générique d'un document."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- DOCX ----------------------------------------------------------------
    D(
        name="DOCUMENT_DOCX_MAX_UNCOMPRESSED_SIZE_MB",
        domain="ingestion",
        value_type="float",
        default=200.0,
        minimum=1.0,
        maximum=10000.0,
        description="Taille décompressée maximale d'un DOCX (anti zip-bomb).",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_DOCX_MAX_ZIP_ENTRIES",
        domain="ingestion",
        value_type="int",
        default=2000,
        minimum=1,
        maximum=1000000,
        description="Nombre maximal d'entrées ZIP d'un DOCX.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_DOCX_MAX_COMPRESSION_RATIO",
        domain="ingestion",
        value_type="int",
        default=200,
        minimum=1,
        maximum=100000,
        description="Ratio de compression maximal toléré (anti zip-bomb).",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- CSV -----------------------------------------------------------------
    D(
        name="DOCUMENT_CSV_MAX_ROWS",
        domain="ingestion",
        value_type="int",
        default=100000,
        minimum=1,
        description="Nombre maximal de lignes lues dans un CSV.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_CSV_MAX_COLUMNS",
        domain="ingestion",
        value_type="int",
        default=256,
        minimum=1,
        description="Nombre maximal de colonnes lues dans un CSV.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_CSV_MAX_CELL_CHARS",
        domain="ingestion",
        value_type="int",
        default=10000,
        minimum=1,
        description="Taille maximale d'une cellule CSV.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_CSV_ROWS_PER_BLOCK",
        domain="ingestion",
        value_type="int",
        default=1,
        minimum=1,
        description="Nombre de lignes CSV regroupées par bloc de texte indexé.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- HTML / Markdown -----------------------------------------------------
    D(
        name="DOCUMENT_HTML_MAX_CHARS",
        domain="ingestion",
        value_type="int",
        default=2000000,
        minimum=1000,
        description="Volume maximal de texte extrait d'un document HTML.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_MARKDOWN_MAX_CHARS",
        domain="ingestion",
        value_type="int",
        default=2000000,
        minimum=1000,
        description="Volume maximal de texte extrait d'un document Markdown.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- Excel ---------------------------------------------------------------
    D(
        name="DOCUMENT_EXCEL_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=True,
        description="Ingestion des classeurs `.xlsx`.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_SHARED_STRINGS_MB",
        domain="ingestion",
        value_type="float",
        default=50.0,
        minimum=1.0,
        maximum=1000.0,
        description=(
            "LA borne mémoire du parsing Excel : openpyxl charge toute la table de "
            "chaînes partagées (RSS ≈ 45 Mo + 3,5 × taille). 50 Mo ⇒ ~220 Mo max."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_CELLS",
        domain="ingestion",
        value_type="int",
        default=1000000,
        minimum=1,
        description="Nombre maximal de cellules réelles parcourues.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_DECLARED_COLS",
        domain="ingestion",
        value_type="int",
        default=2048,
        minimum=1,
        description=(
            "Largeur déclarée au-delà de laquelle la dimension d'une feuille est jugée "
            "FANTÔME ; le classeur n'est pas refusé pour autant."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_SHEETS",
        domain="ingestion",
        value_type="int",
        default=100,
        minimum=1,
        description="Nombre maximal de feuilles traitées.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_COLUMNS",
        domain="ingestion",
        value_type="int",
        default=256,
        minimum=1,
        description="Nombre maximal de colonnes traitées par feuille.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_CELL_CHARS",
        domain="ingestion",
        value_type="int",
        default=2000,
        minimum=1,
        description="Taille maximale d'une cellule Excel.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_UNCOMPRESSED_MB",
        domain="ingestion",
        value_type="float",
        default=200.0,
        minimum=1.0,
        maximum=10000.0,
        description="Taille décompressée maximale d'un classeur (anti zip-bomb).",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_BLOCKS",
        domain="ingestion",
        value_type="int",
        default=200,
        minimum=1,
        description="Nombre maximal de blocs tabulaires détectés.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_CHUNKS",
        domain="ingestion",
        value_type="int",
        default=2000,
        minimum=1,
        description="Nombre maximal de chunks produits par un classeur.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_ROWS_PER_CHUNK",
        domain="ingestion",
        value_type="int",
        default=40,
        minimum=1,
        description="Nombre de lignes regroupées par chunk tabulaire.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_CHUNK_MAX_CHARS",
        domain="ingestion",
        value_type="int",
        default=4000,
        minimum=100,
        description="Taille maximale d'un chunk tabulaire.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_COLUMNS_MARKDOWN",
        domain="ingestion",
        value_type="int",
        default=20,
        minimum=1,
        description="Nombre de colonnes au-delà duquel le rendu Markdown est abandonné.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_MAX_HEADER_ROWS",
        domain="ingestion",
        value_type="int",
        default=4,
        minimum=1,
        maximum=50,
        description="Profondeur maximale d'un en-tête multi-lignes.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY",
        domain="ingestion",
        value_type="enum",
        choices=("none", "hidden_only", "all"),
        default="hidden_only",
        description=(
            "Politique des feuilles masquées. `hidden` = commodité de présentation "
            "(indexée) ; `veryHidden` = intention délibérée (exclue)."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS",
        domain="ingestion",
        value_type="bool",
        default=False,
        description="Ancien interrupteur des feuilles masquées (à 1, équivaut à `all`).",
        deprecated=True,
        replacement="DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- PowerPoint ----------------------------------------------------------
    D(
        name="DOCUMENT_PPTX_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=True,
        description=(
            "Ingestion des présentations `.pptx`. `.pptm`/`.ppt`/`.odp` restent refusés "
            "par CONTENU ; aucune macro n'est exécutée."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_MAX_UNCOMPRESSED_SIZE_MB",
        domain="ingestion",
        value_type="float",
        default=200.0,
        minimum=1.0,
        maximum=10000.0,
        description="Taille décompressée maximale d'un `.pptx` (anti zip-bomb).",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_MAX_ZIP_ENTRIES",
        domain="ingestion",
        value_type="int",
        default=5000,
        minimum=1,
        description="Nombre maximal d'entrées ZIP (un deck illustré porte une entrée par média).",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_MAX_COMPRESSION_RATIO",
        domain="ingestion",
        value_type="int",
        default=200,
        minimum=1,
        description="Ratio de compression maximal toléré pour un `.pptx`.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_MAX_SLIDES",
        domain="ingestion",
        value_type="int",
        default=500,
        minimum=1,
        description="Nombre maximal de diapositives traitées.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_MAX_SHAPES_PER_SLIDE",
        domain="ingestion",
        value_type="int",
        default=500,
        minimum=1,
        description="Nombre maximal de formes traitées par diapositive.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_MAX_TABLE_CELLS",
        domain="ingestion",
        value_type="int",
        default=50000,
        minimum=1,
        description="Nombre maximal de cellules de tableau traitées dans un deck.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_MAX_CHARS",
        domain="ingestion",
        value_type="int",
        default=2000000,
        minimum=1000,
        description="Volume maximal de texte extrait d'un deck.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_GROUP_MAX_DEPTH",
        domain="ingestion",
        value_type="int",
        default=10,
        minimum=1,
        maximum=100,
        description="Profondeur maximale de groupes de formes explorée.",
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_INCLUDE_NOTES",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Notes du présentateur : OFF par défaut (commentaire de l'orateur, pas le "
            "contenu publié). Activées => chunks SÉPARÉS `kind=pptx_notes`."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_PPTX_INCLUDE_ALT_TEXT",
        domain="ingestion",
        value_type="bool",
        default=True,
        description=(
            "Texte alternatif des images : souvent la seule description d'un schéma. "
            "Rendu marqué « [Image : …] », jamais confondu avec le texte de diapositive."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_OFFICE_CHART_DATA_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Graphiques NATIFS Office (DOCX, PPTX) : indexe les catégories et valeurs ENREGISTRÉES dans le "
            "fichier (cache du graphique), en tableau catégories × séries, à la place de la seule légende. "
            "Aucun appel Gateway, aucune estimation visuelle. OPT-IN ; "
            "lu à l'extraction : à poser dans backend, worker, worker_reindex, worker_visual."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- XML générique (.xml) — qualification interne ----------------------------
    D(
        name="DOCUMENT_XML_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Ingestion des documents XML génériques `.xml` (defusedxml, unités structurées, "
            "citations sans page). FERMÉE par défaut : lot neuf. À 0, un `.xml` reconnu "
            "est refusé (`xml_ingestion_disabled`) — il n'est plus indexé comme texte brut."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_XML_MAX_DEPTH",
        domain="ingestion",
        value_type="int",
        default=64,
        minimum=1,
        maximum=1000,
        description=(
            "Profondeur d'imbrication maximale d'un XML (racine = 1). Au-delà : refus "
            "`xml_complexity_limit`, analyse interrompue immédiatement."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_XML_MAX_NODES",
        domain="ingestion",
        value_type="int",
        default=200000,
        minimum=1,
        description=(
            "Nombre maximal d'éléments d'un XML. Au-delà : refus `xml_complexity_limit`, "
            "analyse interrompue immédiatement (lecture en flux)."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- DXF (.dxf) — qualification interne ---------------------------------------
    D(
        name="DOCUMENT_DXF_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Ingestion documentaire des dessins DXF `.dxf` (ezdxf, lecture stricte, sans "
            "rendu) : textes, blocs et attributs, cotes, calques, layouts, XREF déclarées. "
            "FERMÉE par défaut : lot neuf. À 0, un `.dxf` reconnu est refusé "
            "(`dxf_ingestion_disabled`). Toujours asynchrone."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_DXF_MAX_ENTITIES",
        domain="ingestion",
        value_type="int",
        default=250000,
        minimum=1,
        description=(
            "Nombre maximal d'entités d'un DXF (layouts + définitions de blocs). Au-delà : "
            "refus `dxf_complexity_limit` avant toute extraction sémantique."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- IFC (.ifc) — qualification interne ---------------------------------------
    D(
        name="DOCUMENT_IFC_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Ingestion sémantique des maquettes IFC `.ifc` (IfcOpenShell, sans géométrie) : "
            "projet, hiérarchie spatiale, éléments, propriétés, quantités, matériaux, "
            "systèmes, classifications. FERMÉE par défaut. Toujours asynchrone, routée vers "
            "la file dédiée `ingest_ifc` : n'activer QUE si un worker `--queues=ingest_ifc` "
            "tourne. À 0, un `.ifc` reconnu est refusé (`ifc_ingestion_disabled`)."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_IFC_MAX_ENTITIES",
        domain="ingestion",
        value_type="int",
        default=250000,
        minimum=1,
        description=(
            "Nombre maximal d'entités STEP d'une maquette IFC (pré-scan textuel puis compte "
            "du modèle). Au-delà : refus `ifc_complexity_limit` avant tout chargement."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- DWG (.dwg) — qualification interne ---------------------------------------
    D(
        name="DOCUMENT_DWG_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Ingestion documentaire des dessins AutoCAD `.dwg` : conversion LOCALE par GNU "
            "LibreDWG 0.14 (`dwg2dxf`, sous-processus) vers un DXF ASCII temporaire, puis "
            "extraction par le DxfExtractor (textes, MTEXT, blocs, ATTRIB, cotes, calques, "
            "layouts, XREF déclarées). FERMÉE par défaut. Toujours asynchrone, routée vers la "
            "file dédiée `ingest_dwg` : n'activer QUE si un worker `--queues=ingest_dwg` "
            "portant LibreDWG 0.14 tourne, et seulement après la revue de conformité GPLv3+ "
            "et la qualification d'un DWG de consultation réel. À 0, un `.dwg` reconnu est "
            "refusé (`dwg_ingestion_disabled`)."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_DWG_CONVERTER_PATH",
        domain="ingestion",
        value_type="str",
        default="/usr/local/bin/dwg2dxf",
        description=(
            "Chemin absolu du convertisseur `dwg2dxf` (GNU LibreDWG 0.14) dans l'image du "
            "worker DWG. Valeur de CONFIGURATION SERVEUR uniquement : jamais issue d'une "
            "requête. Une version différente de 0.14 est refusée "
            "(`dwg_dependency_unavailable`) : le writer DXF de 0.13.3 corrompt les MTEXT longs."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_DWG_CONVERSION_TIMEOUT_SECONDS",
        domain="ingestion",
        value_type="int",
        default=60,
        minimum=1,
        description=(
            "Temps maximal d'une conversion DWG→DXF. Au-delà, le groupe de processus du "
            "convertisseur est tué et l'ingestion échoue (`dwg_conversion_timeout`, non "
            "rejouable). Les conversions mesurées en qualification restent sous la seconde."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_DWG_MAX_INTERMEDIATE_DXF_MB",
        domain="ingestion",
        value_type="int",
        default=128,
        minimum=1,
        description=(
            "Plafond du DXF intermédiaire produit par la conversion (le DXF pèse jusqu'à "
            "~4,6x le DWG). Vérifié par `stat` AVANT toute lecture ; au-delà : refus "
            "`dwg_complexity_limit`. Ne modifie pas la limite d'upload de la source."
        ),
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- Messagerie Outlook (.msg) -------------------------------------------
    # Déclarées et documentées dans `.env.example` ; consommées par le lot
    # d'ingestion `.msg`, absent de cette branche. `config_audit` les signale
    # comme NON CONSOMMÉES tant que le code ne les lit pas — c'est le constat
    # attendu, pas une anomalie du registre.
    D(
        name="DOCUMENT_MSG_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Messages Outlook `.msg` — NON PRIS EN CHARGE par RAGatm Open (parseur tiers GPL "
            "écarté). Le drapeau est conservé pour la compatibilité des réglages mais n'ouvre "
            "rien : à 0 un `.msg` est refusé (`msg_ingestion_disabled`), à 1 il est refusé "
            "(`msg_unsupported_in_open_edition`)."
        ),
        source=".env.example (lot ingestion .msg)",
        consumers=("apps.rag_core.services.ingest.extraction (lot .msg)",),
    ),
    D(
        name="DOCUMENT_MSG_MAX_FILE_SIZE_MB",
        domain="ingestion",
        value_type="float",
        default=25.0,
        minimum=0.1,
        maximum=10000.0,
        description=(
            "Plafond propre au `.msg`, en plus de la limite d'upload globale. Corpus "
            "de référence observé : 0,9 à 2,5 Mo."
        ),
        source=".env.example (lot ingestion .msg)",
        consumers=("apps.rag_core.services.ingest.extraction (lot .msg)",),
    ),
    D(
        name="DOCUMENT_MSG_MAX_BODY_CHARS",
        domain="ingestion",
        value_type="int",
        default=2000000,
        minimum=1000,
        description="Borne d'extraction du corps. Dépassement = troncature + statut partiel.",
        source=".env.example (lot ingestion .msg)",
        consumers=("apps.rag_core.services.ingest.extraction (lot .msg)",),
    ),
    D(
        name="DOCUMENT_MSG_MAX_RECIPIENTS",
        domain="ingestion",
        value_type="int",
        default=500,
        minimum=1,
        description=(
            "Destinataires conservés PAR CHAMP dans les métadonnées. Jamais injectés "
            "dans le texte indexé ni dans les embeddings."
        ),
        source=".env.example (lot ingestion .msg)",
        consumers=("apps.rag_core.services.ingest.extraction (lot .msg)",),
    ),
    D(
        name="DOCUMENT_MSG_MAX_ATTACHMENTS_INVENTORIED",
        domain="ingestion",
        value_type="int",
        default=100,
        minimum=1,
        description=(
            "Pièces jointes DÉCRITES dans l'inventaire ; le compte total reste exact "
            "au-delà."
        ),
        source=".env.example (lot ingestion .msg)",
        consumers=("apps.rag_core.services.ingest.extraction (lot .msg)",),
    ),
    # --- Messagerie : EML et pièces jointes ------------------
    D(
        name="DOCUMENT_EML_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Ingestion des courriels MIME `.eml` par un extracteur dédié (bibliothèque standard "
            "`email`). FERMÉE par défaut ; à 0, un `.eml` reconnu est refusé (`eml_ingestion_disabled`)."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EML_MAX_FILE_SIZE_MB",
        domain="ingestion",
        value_type="float",
        default=25.0,
        minimum=0.1,
        maximum=10000.0,
        description="Plafond propre au `.eml`, en plus de la limite d'upload globale (aligné sur le `.msg`).",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EMAIL_RAGSPACE_IDS",
        domain="ingestion",
        value_type="list",
        default=[],
        description=(
            "UUID COMPLETS des RagSpace qui ADMETTENT les courriels (.eml) au dépôt, et où leurs "
            "pièces jointes peuvent être ingérées. VIDE = AUCUN espace ; une entrée qui n'est pas un UUID "
            "complet invalide toute la liste. S'ajoute à la porte de format "
            "DOCUMENT_EML_ENABLED : aucune combinaison de drapeaux n'ouvre tous les espaces."
        ),
        runtime_editable=False, max_scope="environment", restart_required=True,
        consumers=("api.v1.documents", "apps.rag_core.services.ingest"),
    ),
    # --- Audio (.mp3 .m4a .wav .flac .ogg .webm) -----------------
    D(
        name="DOCUMENT_AUDIO_INGEST_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Ingestion des fichiers AUDIO : transcription intégrale horodatée par l'ASR d'AI Gateway "
            "(`POST /v1/audio/transcriptions`), seule représentation indexée. FERMÉE par défaut. "
            "N'activer que si un worker `--queues=ingest_audio` portant ffmpeg/ffprobe tourne. Le dépôt "
            "est borné aux espaces de DOCUMENT_AUDIO_RAGSPACE_IDS, sauf DOCUMENT_AUDIO_GLOBAL_ENABLED=1. À 0 : dépôt audio refusé "
            "(`audio_ingestion_disabled`) ; les audios déjà transcrits restent indexés et réindexables."
        ),
        restart_required=True,
        consumers=("api.v1.documents", "apps.rag_core.services.ingest"),
    ),
    D(
        name="DOCUMENT_AUDIO_RAGSPACE_IDS",
        domain="ingestion",
        value_type="list",
        default=[],
        description=(
            "UUID COMPLETS des RagSpace qui ADMETTENT le dépôt audio. VIDE = AUCUN espace ; une entrée "
            "qui n'est pas un UUID complet invalide toute la liste. S'ajoute à DOCUMENT_AUDIO_INGEST_ENABLED."
        ),
        runtime_editable=False, max_scope="environment", restart_required=True,
        consumers=("api.v1.documents", "apps.rag_core.services.ingest"),
    ),
    D(
        name="DOCUMENT_AUDIO_GLOBAL_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Disponibilité GÉNÉRALE du dépôt audio : sous DOCUMENT_AUDIO_INGEST_ENABLED=1, "
            "tout RagSpace l'admet, y compris les espaces créés ensuite ; DOCUMENT_AUDIO_RAGSPACE_IDS ne "
            "restreint plus. Aucun droit n'est élargi (droit d'écriture sur l'espace, quotas, plafond audio "
            "et validation toujours exigés). À 0 : seule la liste DOCUMENT_AUDIO_RAGSPACE_IDS s'applique."
        ),
        restart_required=True,
        consumers=("api.v1.documents", "apps.rag_core.services.ingest"),
    ),
    D(
        name="DOCUMENT_AUDIO_MAX_UPLOAD_BYTES",
        domain="ingestion",
        value_type="int",
        default=600000000,
        minimum=1,
        maximum=2000000000,
        description=(
            "Taille maximale d'un fichier AUDIO au dépôt, en OCTETS décimaux (600000000 = 600 Mo). "
            "Propre à l'audio : la limite générale des autres formats est inchangée. Le chemin "
            "`/api/v1/documents/upload-audio/` n'accepte QUE l'audio ; le proxy frontal doit lui "
            "autoriser un corps de cette taille."
        ),
        restart_required=True,
        consumers=("api.v1.documents", "apps.rag_core.services.ingest"),
    ),
    D(
        name="DOCUMENT_AUDIO_MAX_DURATION_SECONDS",
        domain="ingestion",
        value_type="int",
        default=43200,
        minimum=60,
        maximum=172800,
        description=(
            "Durée maximale d'un enregistrement (12 h par défaut), mesurée par ffprobe AVANT toute "
            "transcription. Distincte de la limite d'un job Gateway (105 min) : un enregistrement "
            "plus long est découpé en parties traitées séquentiellement. Au-delà : `audio_too_long`."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest",),
    ),
    D(
        name="AUDIO_ASR_MODEL_LOGICAL",
        domain="ingestion",
        value_type="str",
        default="asr-whisper-turbo",
        description=(
            "Modèle ASR LOGIQUE demandé à AI Gateway (convention un autre client interne). Une transcription "
            "n'est réutilisée à la réindexation que pour le même modèle logique."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest",),
    ),
    D(
        name="AUDIO_ASR_PART_MAX_SECONDS",
        domain="ingestion",
        value_type="int",
        default=5400,
        minimum=60,
        maximum=6000,
        description=(
            "Durée maximale d'une PARTIE soumise au Gateway (90 min ; limite Gateway 105 min, "
            "marge 15 min). La coupe est placée sur un silence des 10 dernières minutes de la "
            "fenêtre quand il en existe un."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest",),
    ),
    D(
        name="AUDIO_ASR_PART_MAX_BYTES",
        domain="ingestion",
        value_type="int",
        default=201326592,
        minimum=1048576,
        maximum=255852544,
        description=(
            "Taille maximale d'une partie préparée (FLAC 16 kHz mono) soumise au Gateway : "
            "192 Mio, sous la limite Gateway de 256 Mio (marge pour le multipart). Une partie "
            "plus grosse est redécoupée."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest",),
    ),
    D(
        name="AUDIO_ASR_POLL_INTERVAL_SECONDS",
        domain="ingestion",
        value_type="int",
        default=10,
        minimum=2,
        maximum=120,
        description="Intervalle entre deux relectures d'un job ASR (`wait_ms=0`) : pas de sondage en rafale.",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest",),
    ),
    D(
        name="AUDIO_ASR_JOB_DEADLINE_SECONDS",
        domain="ingestion",
        value_type="int",
        default=4800,
        minimum=300,
        maximum=21600,
        description=(
            "Délai maximal d'UN job ASR (soumission → résultat). Au-delà : job supprimé côté Gateway "
            "et document en échec `asr_timeout` (convention un autre client interne : 4800 s)."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest",),
    ),
    D(
        name="AUDIO_SPOOL_DIR",
        domain="ingestion",
        value_type="str",
        default="/tmp/ragatm-audio",
        description=(
            "Répertoire de travail TEMPORAIRE du worker audio (parties préparées). Nettoyé en "
            "succès, en échec et à la suppression ; jamais compté dans les quotas."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest",),
    ),
    D(
        name="DOCUMENT_EMAIL_GLOBAL_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Disponibilité GÉNÉRALE des courriels (.eml) : tout RagSpace les admet au dépôt, "
            "et leurs pièces jointes y sont ingérées si DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED=1. "
            "Aucun droit n'est élargi (droit d'écriture sur l'espace, quotas et validation toujours "
            "exigés). À 0 : seule la liste DOCUMENT_EMAIL_RAGSPACE_IDS s'applique."
        ),
        restart_required=True,
        consumers=("api.v1.documents", "apps.rag_core.services.ingest"),
    ),
    D(
        name="DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=False,
        description=(
            "Ingestion du CONTENU des pièces jointes (PDF, DOCX, PPTX, XLSX, CSV, texte, HTML, images, "
            "courriels imbriqués) par les extracteurs existants, dans les espaces de "
            "DOCUMENT_EMAIL_RAGSPACE_IDS. À 0 : inventaire seulement (comportement historique)."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EMAIL_MAX_ATTACHMENTS",
        domain="ingestion",
        value_type="int",
        default=20,
        minimum=1,
        maximum=200,
        description=(
            "Pièces jointes TRAITÉES au plus par courriel, imbriquées comprises (au-delà : SKIPPED_LIMIT). "
            "L'inventaire reste borné par DOCUMENT_MSG_MAX_ATTACHMENTS_INVENTORIED."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EMAIL_MAX_ATTACHMENT_SIZE_MB",
        domain="ingestion",
        value_type="float",
        default=20.0,
        minimum=0.1,
        maximum=10000.0,
        description="Taille décodée maximale d'UNE pièce jointe traitée (au-delà : SKIPPED_LIMIT).",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EMAIL_MAX_TOTAL_DECODED_MB",
        domain="ingestion",
        value_type="float",
        default=50.0,
        minimum=0.1,
        maximum=10000.0,
        description=(
            "Volume décodé CUMULÉ des pièces jointes traitées d'un courriel, imbriquées comprises "
            "(budget partagé, jamais remis à zéro par l'imbrication)."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    D(
        name="DOCUMENT_EMAIL_MAX_NESTING_DEPTH",
        domain="ingestion",
        value_type="int",
        default=2,
        minimum=0,
        maximum=5,
        description=(
            "Profondeur maximale des courriels imbriqués (message transféré en pièce jointe). 0 = aucun "
            "courriel imbriqué n'est ouvert ; les cycles sont détectés indépendamment."
        ),
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.extraction",),
    ),
    # --- Ingestion asynchrone ------------------------------------------------
    D(
        name="RAG_INGEST_ASYNC_DEFAULT",
        domain="ingestion",
        value_type="bool",
        default=False,
        description="Ingestion asynchrone par défaut sur les endpoints d'upload.",
        consumers=("api.v1.documents",),
    ),
    # --- Découpage en fragments (chunking) ------------------------------------
    #
    # Ces trois-là étaient lus par `getattr(settings, nom, défaut)` dans le
    # pipeline d'ingestion RÉEL sans qu'aucun settings ne les projette : la taille
    # de chunk, qui conditionne tout le retrieval en aval, n'était pas configurable.
    D(
        name="RAG_CHUNK_SIZE_CHARS",
        domain="ingestion",
        value_type="int",
        default=2500,
        minimum=200,
        maximum=100000,
        description=(
            "Taille cible d'un fragment indexé, en caractères. Elle conditionne tout "
            "le retrieval en aval : la changer sur un corpus déjà indexé rend les "
            "anciens fragments incomparables aux nouveaux."
        ),
        consumers=(
            "apps.rag_core.services.ingest.pipeline",
            "apps.rag_core.services.ingest.indexing",
            "apps.corpus.services.ingest",
        ),
        notes="Un changement impose une réindexation complète pour rester cohérent.",
    ),
    # --- Chunking des espaces GOUVERNÉS (lot 3, révisé le 22/08/2026) ---------
    #
    # SENS RÉVISÉ. Ces deux réglages étaient le plafond de TOUT espace `ai_gateway`.
    # Ils sont désormais le **repli conservateur** appliqué quand le modèle gouverné
    # est INCONNU de la table de capacités (`GOVERNED_MODEL_MAX_INPUT_CHARS`).
    #
    # Pourquoi : la contrainte n'a jamais été celle du provider, mais celle du MODÈLE
    # — le commit d'origine le disait déjà. Le profil `fr-en-retrieval` (Granite,
    # fenêtre 512 tokens) est déclaré `on_overflow: reject` à 1 200 CARACTÈRES ; un
    # fragment plus long n'est pas tronqué, il fait ÉCHOUER son document. Tant que
    # `ai_gateway` n'exposait que ce modèle, le provider était un proxy exact.
    #
    # `legacy-768` a rompu l'équivalence (plafond 6 000). Garder la borne au provider
    # redécoupait alors un corpus sans raison technique — et transformait une
    # migration de PROVENANCE d'embedding en migration de STRUCTURE documentaire,
    # à hauteur de ×2,3 fragments. Le budget est donc `min(RAG_CHUNK_SIZE_CHARS,
    # plafond du modèle)`, ces réglages ne servant que de repli.
    #
    # Vérification de la déclaration : `manage.py check_embedding_capabilities`.
    D(
        name="RAG_GOVERNED_CHUNK_SIZE_CHARS",
        domain="ingestion",
        value_type="int",
        default=1200,
        minimum=200,
        maximum=100000,
        description=(
            "Repli conservateur, en CARACTÈRES, pour un RagSpace gouverné dont le "
            "modèle est absent de `GOVERNED_MODEL_MAX_INPUT_CHARS`. Les modèles "
            "déclarés utilisent leur propre plafond, lisible sur "
            "`/health/embeddings` de la Gateway."
        ),
        consumers=(
            "apps.rag_core.services.ingest.pipeline",
            "apps.rag_core.services.ingest.indexing",
        ),
        notes=(
            "Le dépasser ne dégrade pas la qualité : la Gateway REFUSE le texte et "
            "l'indexation du document échoue. À vérifier par "
            "`audit_embedding_chunk_lengths` avant toute réindexation. Depuis le "
            "22/08/2026 ce réglage n'est plus le plafond de tous les espaces "
            "gouvernés — seulement celui des modèles non déclarés."
        ),
    ),
    D(
        name="RAG_GOVERNED_CHUNK_OVERLAP_CHARS",
        domain="ingestion",
        value_type="int",
        default=180,
        minimum=0,
        maximum=50000,
        description=(
            "Recouvrement entre fragments pour un RagSpace gouverné. Ramené sous la "
            "taille de fragment si nécessaire (un recouvrement ≥ taille ne "
            "progresserait plus dans le texte)."
        ),
        consumers=(
            "apps.rag_core.services.ingest.pipeline",
            "apps.rag_core.services.ingest.indexing",
        ),
        notes=(
            "15 % de la taille, comme le couple historique (250/2500 = 10 %) mais un "
            "peu plus généreux : à fragment plus court, une phrase coupée a "
            "proportionnellement plus de chances de tomber sur une frontière."
        ),
    ),
    D(
        name="RAG_CHUNK_OVERLAP_CHARS",
        domain="ingestion",
        value_type="int",
        default=250,
        minimum=0,
        maximum=50000,
        description=(
            "Recouvrement entre deux fragments consécutifs. Il évite qu'une phrase "
            "coupée à la frontière ne devienne introuvable dans les deux fragments."
        ),
        consumers=(
            "apps.rag_core.services.ingest.pipeline",
            "apps.rag_core.services.ingest.indexing",
            "apps.corpus.services.ingest",
        ),
    ),
    D(
        name="RAG_CHUNKING_VERSION",
        domain="ingestion",
        value_type="str",
        default="",
        description=(
            "Étiquette de version du découpage, reportée dans les rapports de "
            "benchmark. Vide = non renseignée ; elle ne pilote AUCUN comportement."
        ),
        consumers=("apps.rag_core.management.commands.rag_benchmark",),
    ),
    D(
        name="PREVIEW_CACHE_ENABLED",
        domain="ingestion",
        value_type="bool",
        default=True,
        description=(
            "Cache des aperçus de documents. À 0, chaque consultation reconstruit "
            "l'aperçu : correct mais coûteux sur les gros fichiers."
        ),
        consumers=("apps.rag_core.services.preview.config",),
    ),
)
