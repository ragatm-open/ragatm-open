/**
 * Contrat de l'aperçu documentaire (DOCX) — miroir typé de
 * `GET /api/v1/documents/<id>/document-preview/`.
 *
 * Distinct du contrat tabulaire : un document est une SUITE DE BLOCS, pas une
 * grille de cellules. Les deux endpoints partagent leurs permissions, leurs
 * bornes et leurs codes de refus — pas leur forme de sortie.
 */

export type DocumentBlockType =
  | "heading"
  | "paragraph"
  | "list"
  | "table"
  | "unsupported";

export type BlockHighlight = { start: number; end: number };

export type DocumentBlock = {
  id: string;
  index: number;
  type: DocumentBlockType;
  text: string;
  /** Sous-plage citée, en coordonnées LOCALES au bloc. `null` = bloc de contexte. */
  highlight: BlockHighlight | null;
  truncated: boolean;
  full_length: number;
  warnings: string[];
  /** heading uniquement. */
  level?: number;
  /** list uniquement — toujours `"flattened"` dans ce lot. */
  list_fidelity?: string;
  /** table uniquement — toujours `"preformatted"` dans ce lot. */
  render_mode?: string;
};

export type DocumentHeading = {
  id: string;
  block_index: number;
  level: number;
  text: string;
};

export type DocumentPreview = {
  schema_version: number;
  document: { id: string; filename: string; sha256: string; preview_kind: string };
  source: { chunk_id: string; char_start: number; char_end: number } | null;
  navigation: { headings: DocumentHeading[]; headings_truncated: boolean };
  viewport: {
    first_block: number;
    last_block: number;
    total_blocks: number;
    source_first_block: number | null;
    source_last_block: number | null;
  };
  blocks: DocumentBlock[];
  fidelity: {
    simplified: boolean;
    extractor_version: string;
    warnings: string[];
    omitted: string[];
  };
  truncation: { truncated: boolean; reason: string | null };
};

/**
 * Clé i18n (namespace `documentViewer`) par code de refus. La traduction est
 * faite côté composant.
 *
 * `chunk_stale` et `document_version_mismatch` ont leur message propre : ce sont
 * les cas où afficher un passage approchant serait pire que ne rien afficher,
 * puisqu'il aurait toutes les apparences d'une source vérifiée.
 */
export const DOCUMENT_PREVIEW_ERROR_KEYS: Record<string, string> = {
  chunk_stale: "docErrChunkStale",
  document_version_mismatch: "docErrVersionMismatch",
  preview_unavailable: "docErrPreviewUnavailable",
  chunk_document_mismatch: "docErrChunkDocMismatch",
  invalid_chunk_offsets: "docErrInvalidChunkOffsets",
  not_found: "docErrNotFound",
  no_file_available: "docErrNoFile",
  unsupported_format: "docErrUnsupportedFormat",
  document_unreadable: "docErrDocumentUnreadable",
  document_too_large: "docErrDocumentTooLarge",
  invalid_block_range: "docErrInvalidBlockRange",
  block_not_found: "docErrBlockNotFound",
  context_limit_exceeded: "docErrContextLimit",
  preview_timeout: "docErrTimeout",
  internal_error: "docErrInternal",
  network_error: "docErrNetwork",
};

/** Clé i18n du message d'erreur pour un `reason_code` (repli : erreur réseau). */
export function documentPreviewErrorKey(code: string | null | undefined): string {
  return DOCUMENT_PREVIEW_ERROR_KEYS[code || ""] || DOCUMENT_PREVIEW_ERROR_KEYS.network_error;
}

/** Niveau de titre borné pour l'AFFICHAGE. La donnée reste inchangée. */
export function displayHeadingLevel(level: number | undefined): number {
  if (!Number.isFinite(level as number)) return 1;
  return Math.min(6, Math.max(1, Math.trunc(level as number)));
}

/**
 * Découpe le texte d'un bloc en (avant, cité, après).
 *
 * Le surlignage est au CARACTÈRE : un chunk est découpé sans égard pour les
 * frontières de blocs, donc le premier et le dernier bloc d'une citation ne sont
 * couverts que partiellement. Marquer le bloc entier étendrait la citation à du
 * texte qu'elle ne porte pas.
 */
export function splitForHighlight(
  text: string,
  highlight: BlockHighlight | null
): { before: string; marked: string; after: string } {
  if (!highlight) return { before: text, marked: "", after: "" };
  // Bornes défensivement ramenées dans le texte RÉELLEMENT affiché : un bloc
  // tronqué à l'affichage peut voir son surlignage dépasser.
  const start = Math.max(0, Math.min(highlight.start, text.length));
  const end = Math.max(start, Math.min(highlight.end, text.length));
  return {
    before: text.slice(0, start),
    marked: text.slice(start, end),
    after: text.slice(end),
  };
}

/** Le surlignage déborde-t-il la partie affichée d'un bloc tronqué ? */
export function highlightExceedsDisplay(block: DocumentBlock): boolean {
  return Boolean(
    block.truncated && block.highlight && block.highlight.end > block.text.length
  );
}
