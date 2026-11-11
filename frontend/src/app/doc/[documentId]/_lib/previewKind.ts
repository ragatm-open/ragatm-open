/**
 * Type de visualiseur à employer pour un document.
 *
 * La décision CANONIQUE vient du backend (`preview_kind`, posé par
 * `api/v1/documents/preview_kind.py` à partir du type MIME détecté PAR CONTENU
 * à l'ingestion). Ce module ne fait que :
 *
 *  1. typer la valeur reçue et rejeter toute valeur inconnue ;
 *  2. fournir un repli DÉFENSIF si le champ est absent — cas d'un backend plus
 *     ancien que ce frontend, ou d'une réponse tronquée.
 *
 * Le repli est volontairement plus pauvre que la table backend : il n'a pas accès
 * au type détecté par contenu, seulement à ce que la réponse porte. Il ne doit
 * jamais devenir la voie normale, sans quoi les deux tables divergeraient au
 * premier format ajouté.
 */

export type PreviewKind =
  | "pdf"
  | "spreadsheet"
  | "document"
  | "presentation"
  | "text"
  | "unsupported";

const KNOWN: readonly PreviewKind[] = [
  "pdf",
  "spreadsheet",
  "document",
  "presentation",
  "text",
  "unsupported",
];

/** Types MIME dont le rendu est maîtrisé. Miroir réduit de la table backend. */
const BY_MIME: Record<string, PreviewKind> = {
  "application/pdf": "pdf",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation":
    "presentation",
  "text/plain": "text",
  "text/markdown": "text",
};

const BY_EXTENSION: Record<string, PreviewKind> = {
  pdf: "pdf",
  xlsx: "spreadsheet",
  docx: "document",
  pptx: "presentation",
  txt: "text",
  md: "text",
  markdown: "text",
};

function extensionOf(filename: string): string {
  const base = (filename || "").trim();
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

/**
 * Repli local, utilisé UNIQUEMENT quand le backend n'a pas fourni `preview_kind`.
 * `content_type` prime ; l'extension ne sert que s'il est absent ou non reconnu —
 * jamais pour contredire un type connu.
 */
export function inferPreviewKind(opts: {
  contentType?: string | null;
  filename?: string | null;
}): PreviewKind {
  const ctype = (opts.contentType || "").split(";")[0].trim().toLowerCase();
  const byMime = BY_MIME[ctype];
  if (byMime) return byMime;

  // Type renseigné mais inconnu : on sait qu'on ne sait pas le rendre. On ne va pas
  // chercher un avis contraire dans l'extension.
  if (ctype && ctype !== "application/octet-stream") return "unsupported";

  return BY_EXTENSION[extensionOf(opts.filename || "")] || "unsupported";
}

/**
 * Normalise la valeur renvoyée par le backend, avec repli si elle est absente ou
 * inconnue de ce frontend.
 */
export function resolvePreviewKind(doc: {
  preview_kind?: string | null;
  content_type?: string | null;
  filename?: string | null;
}): PreviewKind {
  const raw = (doc.preview_kind || "").trim() as PreviewKind;
  if (KNOWN.includes(raw)) return raw;
  return inferPreviewKind({ contentType: doc.content_type, filename: doc.filename });
}

/**
 * Extension en majuscules, pour les états explicites (jamais « FIC »).
 * Chaîne vide si l'extension est inconnue : le libellé « format inconnu » est
 * une chaîne d'IHM et relève de l'i18n côté composant.
 */
export function documentKindLabel(filename?: string | null): string {
  const ext = extensionOf(filename || "");
  return ext ? ext.toUpperCase() : "";
}
