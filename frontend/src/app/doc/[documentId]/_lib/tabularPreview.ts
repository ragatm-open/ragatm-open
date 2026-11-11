/**
 * Contrat de l'aperçu tabulaire — miroir typé de
 * `GET /api/v1/documents/<id>/tabular-preview/`.
 *
 * Le `reason_code` est la partie la plus importante du contrat : c'est lui qui
 * permet à l'interface de dire *pourquoi* elle n'affiche pas la source, au lieu
 * de rendre un tableau vide. Un tableau vide et « je n'ai pas pu lire » sont deux
 * choses différentes, et les confondre recréerait la zone blanche d'origine.
 */

export type PreviewCell = {
  column_index: number;
  column_letter: string;
  value: string;
  highlighted: boolean;
  flags: string[];
  /** Longueur canonique quand `value` est tronquée pour l'affichage. */
  full_length?: number | null;
};

export type PreviewRow = { row_number: number; cells: PreviewCell[] };

export type PreviewSheet = {
  index: number;
  name: string;
  state: "visible" | "hidden" | string;
  has_citation: boolean;
};

export type TabularPreview = {
  schema_version: number;
  document: { id: string; filename: string; sha256: string };
  workbook: { sheet_count: number; sheets: PreviewSheet[] };
  viewport: {
    sheet_index: number;
    sheet_name: string;
    sheet_state: string;
    requested_ranges: string[];
    display_range: string;
    row_start: number;
    row_end: number;
    column_start: string;
    column_end: string;
  };
  columns: { index: number; letter: string; header: string }[];
  rows: PreviewRow[];
  fidelity: { simplified: boolean; omitted: string[]; warnings: string[] };
  truncation: { truncated: boolean; reason: string | null };
};

export type PreviewErrorCode =
  | "not_found"
  | "no_file_available"
  | "chunk_document_mismatch"
  | "chunk_stale"
  | "document_version_mismatch"
  | "unsupported_format"
  | "chunk_not_tabular"
  | "sheet_not_found"
  | "sheet_not_previewable"
  | "sheet_reference_conflict"
  | "chunk_location_invalid"
  | "invalid_cell_range"
  | "range_out_of_bounds"
  | "context_limit_exceeded"
  | "preview_timeout"
  | "workbook_unreadable"
  | "internal_error"
  | "network_error";

/**
 * Clé i18n (namespace `documentViewer`) destinée à l'utilisateur pour chaque
 * refus. La traduction est faite côté composant ; ce module ne fait que router
 * le `reason_code` vers sa clé.
 *
 * `chunk_stale` mérite son message dédié : c'est le cas où retomber
 * silencieusement sur la première feuille afficherait une source FAUSSE avec
 * l'apparence d'une source vraie.
 */
export const PREVIEW_ERROR_MESSAGE_KEYS: Record<PreviewErrorCode, string> = {
  chunk_stale: "tabularErrChunkStale",
  document_version_mismatch: "tabularErrVersionMismatch",
  chunk_document_mismatch: "tabularErrChunkDocMismatch",
  not_found: "tabularErrNotFound",
  no_file_available: "tabularErrNoFile",
  unsupported_format: "tabularErrUnsupportedFormat",
  chunk_not_tabular: "tabularErrChunkNotTabular",
  sheet_not_found: "tabularErrSheetNotFound",
  sheet_not_previewable: "tabularErrSheetNotPreviewable",
  sheet_reference_conflict: "tabularErrSheetConflict",
  chunk_location_invalid: "tabularErrLocationInvalid",
  invalid_cell_range: "tabularErrInvalidRange",
  range_out_of_bounds: "tabularErrRangeOutOfBounds",
  context_limit_exceeded: "tabularErrContextLimit",
  preview_timeout: "tabularErrTimeout",
  workbook_unreadable: "tabularErrWorkbookUnreadable",
  internal_error: "tabularErrInternal",
  network_error: "tabularErrNetwork",
};

/** Clé i18n du message d'erreur pour un `reason_code` (repli : erreur réseau). */
export function previewErrorMessageKey(code: string | null | undefined): string {
  const key = (code || "") as PreviewErrorCode;
  return PREVIEW_ERROR_MESSAGE_KEYS[key] || PREVIEW_ERROR_MESSAGE_KEYS.network_error;
}

/** Clé i18n d'un drapeau de cellule, pour l'infobulle et les lecteurs d'écran. */
export const CELL_FLAG_LABEL_KEYS: Record<string, string> = {
  error_cell: "cellFlagErrorCell",
  uncached_formula: "cellFlagUncachedFormula",
  truncated: "cellFlagTruncated",
  neutralized: "cellFlagNeutralized",
  duration_unsupported: "cellFlagDurationUnsupported",
};

/**
 * Clé i18n par drapeau connu ; pour un drapeau inconnu, renvoie le drapeau brut
 * (le composant l'affiche tel quel via `t.has`).
 */
export function cellFlagLabelKeys(flags: string[] | undefined): string[] {
  return (flags || []).map((f) => CELL_FLAG_LABEL_KEYS[f] || f);
}
