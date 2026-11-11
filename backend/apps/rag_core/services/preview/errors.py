# backend/apps/rag_core/services/preview/errors.py
from __future__ import annotations

"""Refus TYPÉS de l'aperçu tabulaire — un code stable par raison de ne pas afficher.

Doctrine reprise du moteur tabulaire : **aucune erreur n'est convertie en résultat
vide**. Une grille sans lignes et « je n'ai pas pu lire » sont deux réponses
différentes, et les confondre est le défaut le plus grave ici — c'est exactement la
zone blanche que ce lot existe pour supprimer, déplacée d'un cran.

Chaque code est un endroit où l'aperçu a choisi de **ne pas deviner**. En
particulier, jamais de repli sur « la première feuille » ni sur « une plage
approchante » : une source mal localisée est pire qu'une source non affichée, parce
qu'elle est vérifiable en apparence.
"""

# --- Document / accès ------------------------------------------------------
#: Document inexistant OU hors périmètre — indiscernables (anti-énumération).
ERR_NOT_FOUND = "not_found"
#: Le document existe mais son fichier source n'est pas dans le stockage.
ERR_NO_FILE = "no_file_available"
#: Format non prévisualisable par le visualiseur tabulaire (CSV inclus, cf. doc).
ERR_UNSUPPORTED_FORMAT = "unsupported_format"

# --- Chunk / version -------------------------------------------------------
#: `chunk_id` inconnu : le document a été réindexé, les chunks ont été recréés.
ERR_CHUNK_STALE = "chunk_stale"
#: Le chunk existe mais appartient à un autre document (ou un autre RagSpace).
ERR_CHUNK_DOCUMENT_MISMATCH = "chunk_document_mismatch"
#: Le chunk n'est pas une source tabulaire (PDF, PPTX, e-mail…).
ERR_CHUNK_NOT_TABULAR = "chunk_not_tabular"
#: Le chunk ne porte pas de localisation exploitable (méta absente ou malformée).
ERR_CHUNK_LOCATION_INVALID = "chunk_location_invalid"
#: La version de fichier attendue par l'appelant ne correspond plus.
ERR_DOCUMENT_VERSION_MISMATCH = "document_version_mismatch"

# --- Feuille / plage -------------------------------------------------------
ERR_SHEET_NOT_FOUND = "sheet_not_found"
#: Feuille non indexée (`veryHidden`, feuille-graphique) : hors périmètre d'aperçu.
ERR_SHEET_NOT_PREVIEWABLE = "sheet_not_previewable"
#: Le nom fourni contredit l'index fourni — on refuse plutôt que d'arbitrer.
ERR_SHEET_REFERENCE_CONFLICT = "sheet_reference_conflict"
ERR_INVALID_CELL_RANGE = "invalid_cell_range"
ERR_RANGE_OUT_OF_BOUNDS = "range_out_of_bounds"

# --- Documents structurés (DOCX) -------------------------------------------
#: Le chunk ne porte pas d'offsets exploitables (`char_start`/`char_end`).
ERR_INVALID_CHUNK_OFFSETS = "invalid_chunk_offsets"
#: Fenêtre de blocs incohérente (bornes inversées, négatives…).
ERR_INVALID_BLOCK_RANGE = "invalid_block_range"
#: Bloc demandé hors du document.
ERR_BLOCK_NOT_FOUND = "block_not_found"
#: Archive ou XML invalide — l'équivalent DOCX de `workbook_unreadable`.
ERR_DOCUMENT_UNREADABLE = "document_unreadable"
#: Le document dépasse les bornes de l'aperçu (blocs ou caractères).
ERR_DOCUMENT_TOO_LARGE = "document_too_large"
#: L'extraction n'est plus compatible avec ce qui a été indexé : la structure
#: reconstruite ne correspond pas au texte, ou l'extracteur a changé de version.
ERR_PREVIEW_UNAVAILABLE = "preview_unavailable"

# --- Présentations (PPTX) --------------------------------------------------
#: Diapositive inconnue de la présentation.
ERR_SLIDE_NOT_FOUND = "slide_not_found"
#: Unité incompatible (ni diapositive, ni notes exploitables).
ERR_SLIDE_NOT_PREVIEWABLE = "slide_not_previewable"
#: Fenêtre de diapositives incohérente.
ERR_INVALID_SLIDE_RANGE = "invalid_slide_range"
#: Le réglage des notes diffère de celui utilisé à l'ingestion. Compenser serait
#: pire que refuser : les bases d'offset de TOUTES les diapositives suivantes
#: sont décalées, donc le surlignage serait faux sans que rien ne le signale.
ERR_NOTES_CONFIGURATION_MISMATCH = "notes_configuration_mismatch"
#: Archive ou XML de présentation invalide.
ERR_PRESENTATION_UNREADABLE = "presentation_unreadable"
#: Présentation au-delà des bornes de l'aperçu.
ERR_PRESENTATION_TOO_LARGE = "presentation_too_large"
#: Présentation protégée par chiffrement DRM/IRM.
ERR_ENCRYPTED_PRESENTATION = "encrypted_presentation"

# --- Bornes / ressources ---------------------------------------------------
ERR_CONTEXT_LIMIT_EXCEEDED = "context_limit_exceeded"
ERR_PREVIEW_TIMEOUT = "preview_timeout"
ERR_WORKBOOK_UNREADABLE = "workbook_unreadable"


class PreviewError(Exception):
    """Refus typé. `code` est stable et fait partie du contrat d'API."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class PreviewNotFound(PreviewError):
    """404 — inexistant ou inaccessible, sans distinction."""


class PreviewPermissionDenied(PreviewError):
    """Refus d'accès qui ne doit PAS confirmer l'existence de la ressource."""


class PreviewUnsupported(PreviewError):
    """Reconnu, mais hors périmètre du visualiseur tabulaire."""


class PreviewStale(PreviewError):
    """La référence pointe une version qui n'existe plus."""


class PreviewInvalidRequest(PreviewError):
    """Paramètres mal formés ou incohérents entre eux."""


class PreviewLimitExceeded(PreviewError):
    """Une borne de ressources a été atteinte."""


class PreviewTimeout(PreviewError):
    """Le budget de lecture a été dépassé — la lecture est INTERROMPUE."""


class PreviewUnreadable(PreviewError):
    """Classeur corrompu ou illisible. Jamais un 500."""
