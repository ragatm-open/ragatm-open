/**
 * Contrat de l'aperçu de présentation (PPTX) — miroir typé de
 * `GET /api/v1/documents/<id>/presentation-preview/`.
 *
 * Distinct des contrats tabulaire et documentaire : une présentation est une
 * SUITE DE DIAPOSITIVES, chacune portant ses blocs et, séparément, ses notes.
 */

export type PresentationBlockType =
  | "title"
  | "paragraph"
  | "list"
  | "table"
  | "speaker_notes"
  | "unsupported";

export type BlockHighlight = { start: number; end: number };

export type PresentationBlock = {
  id: string;
  index: number;
  type: PresentationBlockType;
  text: string;
  /** Sous-plage citée, en coordonnées LOCALES au bloc. `null` = contexte. */
  highlight: BlockHighlight | null;
  truncated: boolean;
  full_length: number;
  warnings: string[];
  render_mode?: string;
  list_fidelity?: string;
};

export type SlideSummary = {
  number: number;
  title: string | null;
  has_citation: boolean;
  /** Absent = INCONNU (les unités de notes ne portent pas la clé), pas `false`. */
  has_images?: boolean;
  requires_visual_check?: boolean;
};

export type SlideView = {
  number: number;
  title: string | null;
  blocks: PresentationBlock[];
  speaker_notes: PresentationBlock[];
  warnings: string[];
  has_images?: boolean;
  requires_visual_check?: boolean;
};

export type PresentationPreview = {
  schema_version: number;
  document: { id: string; filename: string; sha256: string; preview_kind: string };
  source: {
    chunk_id: string;
    kind: string;
    slide_number: number;
    char_start: number;
    char_end: number;
    notes_included: boolean;
  } | null;
  versions: { extractor_version: string; chunking_version: string };
  presentation: {
    slide_count: number;
    indexed_slide_count: number;
    slides: SlideSummary[];
    inventory_truncated: boolean;
  };
  viewport: {
    active_slide: number;
    source_slide: number | null;
    first_slide: number;
    last_slide: number;
  };
  slides: SlideView[];
  fidelity: {
    simplified: boolean;
    extractor_version: string;
    chunking_version: string;
    warnings: string[];
    omitted: string[];
  };
  truncation: { truncated: boolean; reason: string | null };
};

/**
 * Clé i18n (namespace `documentViewer`) par code de refus. La traduction est
 * faite côté composant.
 *
 * `notes_configuration_mismatch` a la sienne : c'est le cas où le réglage des
 * notes a changé depuis l'indexation, donc où toutes les bases d'offset des
 * diapositives suivantes sont décalées. Compenser afficherait un surlignage
 * faux avec toutes les apparences d'un surlignage juste.
 */
export const PRESENTATION_PREVIEW_ERROR_KEYS: Record<string, string> = {
  chunk_stale: "pptxErrChunkStale",
  document_version_mismatch: "pptxErrVersionMismatch",
  notes_configuration_mismatch: "pptxErrNotesConfigMismatch",
  preview_unavailable: "pptxErrPreviewUnavailable",
  chunk_document_mismatch: "pptxErrChunkDocMismatch",
  invalid_chunk_offsets: "pptxErrInvalidChunkOffsets",
  slide_not_found: "pptxErrSlideNotFound",
  slide_not_previewable: "pptxErrSlideNotPreviewable",
  invalid_slide_range: "pptxErrInvalidSlideRange",
  not_found: "pptxErrNotFound",
  no_file_available: "pptxErrNoFile",
  unsupported_format: "pptxErrUnsupportedFormat",
  presentation_unreadable: "pptxErrPresentationUnreadable",
  encrypted_presentation: "pptxErrEncrypted",
  presentation_too_large: "pptxErrPresentationTooLarge",
  context_limit_exceeded: "pptxErrContextLimit",
  preview_timeout: "pptxErrTimeout",
  internal_error: "pptxErrInternal",
  network_error: "pptxErrNetwork",
};

/** Clé i18n du message d'erreur pour un `reason_code` (repli : erreur réseau). */
export function presentationPreviewErrorKey(code: string | null | undefined): string {
  return (
    PRESENTATION_PREVIEW_ERROR_KEYS[code || ""] ||
    PRESENTATION_PREVIEW_ERROR_KEYS.network_error
  );
}

/**
 * Découpe le texte d'un bloc en (avant, cité, après).
 *
 * Le surlignage est au CARACTÈRE : un chunk est découpé sans égard pour les
 * frontières de blocs, donc les blocs de bordure ne sont couverts que
 * partiellement. Marquer le bloc entier étendrait la citation.
 */
export function splitForHighlight(
  text: string,
  highlight: BlockHighlight | null
): { before: string; marked: string; after: string } {
  if (!highlight) return { before: text, marked: "", after: "" };
  const start = Math.max(0, Math.min(highlight.start, text.length));
  const end = Math.max(start, Math.min(highlight.end, text.length));
  return {
    before: text.slice(0, start),
    marked: text.slice(start, end),
    after: text.slice(end),
  };
}

/** Cette diapositive porte-t-elle un contenu visuel non affiché ? */
export function hasVisualContent(slide: {
  has_images?: boolean;
  requires_visual_check?: boolean;
}): boolean {
  // `undefined` signifie INCONNU, pas `false` : on ne l'affirme pas.
  return slide.has_images === true || slide.requires_visual_check === true;
}
