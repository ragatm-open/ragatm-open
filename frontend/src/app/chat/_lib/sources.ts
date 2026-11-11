// Extraction tolérante des champs d'une source — supporte les deux formats
// renvoyés par le backend : SourceV1 (audit) et RetrievalChunk (stream).
//
// C'est la SEULE porte d'entrée vers les métadonnées d'une source. Aucun composant ne
// doit lire `source.meta` directement : les anciens messages n'en ont pas, les formats
// diffèrent selon le chemin (sync / SSE / historique), et un cast local finirait par
// diverger d'un composant à l'autre.
//
// Invariants :
// - une feuille n'est JAMAIS dérivée d'un numéro de page ;
// - une plage n'est JAMAIS devinée depuis le snippet ni depuis le nom du fichier ;
// - le texte du chunk n'est JAMAIS analysé pour retrouver une cellule.
import type { SourceLocator, SourceMeta, SourceType, SourceV1 } from "./types";

export type AnySource = SourceV1 & {
  text?: string | null;
  source?: {
    filename?: string;
    page_number?: number;
    label?: string;
    document_id?: string;
    source_type?: string;
    meta?: SourceMeta | null;
    corpus_id?: string;
    corpus_name?: string;
    business_domain_id?: string;
    business_domain_name?: string;
  } | null;
};

/** Chaîne non vide, ou null. Tolère `undefined`, `null`, nombres, objets. */
function str(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const s = v.trim();
  return s ? s : null;
}

/** Métadonnées de citation, quel que soit le chemin (v3, stream, ancien contrat). */
export function srcMeta(s: AnySource | null | undefined): SourceMeta {
  if (!s) return {};
  const m = s.meta ?? s.source?.meta;
  return m && typeof m === "object" ? (m as SourceMeta) : {};
}

/** `excel_table` | `excel_narrative` | `pptx_slide` | `pptx_notes` | `table` (P3) | null. */
export function srcKind(s: AnySource | null | undefined): string | null {
  return str(srcMeta(s).kind);
}

/** La source vient-elle d'un classeur ? */
export function srcIsExcel(s: AnySource | null | undefined): boolean {
  return (srcKind(s) ?? "").startsWith("excel_");
}

/** La source vient-elle d'une présentation ? */
export function srcIsPptx(s: AnySource | null | undefined): boolean {
  return (srcKind(s) ?? "").startsWith("pptx_");
}

/** Nom de la feuille — uniquement depuis `meta.sheet_name`. Jamais déduit. */
export function srcSheet(s: AnySource | null | undefined): string | null {
  return str(srcMeta(s).sheet_name);
}

/** La feuille citée est-elle MASQUÉE dans le classeur ?
 *
 * Elle est bien indexée — elle fait partie du fichier — mais elle n'apparaît pas dans
 * les onglets de l'utilisateur. Le taire reviendrait à exposer sans le dire.
 */
export function srcSheetHidden(s: AnySource | null | undefined): boolean {
  const m = srcMeta(s);
  if (typeof m.sheet_hidden === "boolean") return m.sheet_hidden;
  const state = str(m.sheet_state);
  return state === "hidden" || state === "veryHidden";
}

/** Plage de cellules (`B8:F17`) — uniquement depuis `meta.cell_range`. Jamais devinée. */
export function srcCellRange(s: AnySource | null | undefined): string | null {
  return str(srcMeta(s).cell_range);
}

/** Titre du tableau, quand l'auteur du classeur en a mis un au-dessus. */
export function srcTitle(s: AnySource | null | undefined): string | null {
  return str(srcMeta(s).title);
}

export function srcDoc(s: AnySource | null | undefined): string {
  return str(s?.filename) || str(s?.source?.filename) || str(s?.source?.label) || "Document";
}

/**
 * Type de source : "domain_corpus" (socle métier), "workspace_document" (privé) ou
 * "unknown".
 *
 * 🔴 Le repli historique renvoyait `workspace_document` pour TOUTE valeur non reconnue,
 * y compris absente. Une source dont la provenance avait été perdue en amont était donc
 * présentée comme un document privé du workspace — une affirmation fausse sur l'origine
 * d'une information citée, et invisible. On ne devine plus : on le dit.
 *
 * Les sources des anciennes conversations, elles, portent bien `source_type` (le backend
 * le pose depuis la v2 du contrat) ; celles qui ne le portent pas basculent en `unknown`,
 * ce qui reste affichable — cf. `srcBadgeLabel`.
 */
export function srcType(s: AnySource | null | undefined): SourceType {
  const t = (s?.source_type || s?.source?.source_type || "").toString();
  if (t === "domain_corpus") return "domain_corpus";
  if (t === "workspace_document") return "workspace_document";
  return "unknown";
}

export function srcCorpusName(s: AnySource | null | undefined): string | null {
  return str(s?.corpus_name) || str(s?.source?.corpus_name);
}

export function srcDomainName(s: AnySource | null | undefined): string | null {
  return str(s?.business_domain_name) || str(s?.source?.business_domain_name);
}

/**
 * Libellé de badge : "Corpus <domaine>" / "Corpus métier" / "Workspace" /
 * "Provenance inconnue".
 *
 * Les libellés localisés vivent dans le catalogue i18n (`chat.workspaceBadge`,
 * `chat.unknownSourceBadge`) et sont appliqués par le composant ; cette fonction reste
 * la source de vérité du CHOIX, pour rester testable sans contexte React.
 */
export function srcBadgeLabel(s: AnySource | null | undefined): string {
  const t = srcType(s);
  if (t === "domain_corpus") {
    const dom = srcDomainName(s);
    return dom ? `Corpus ${dom}` : "Corpus métier";
  }
  if (t === "unknown") return "Provenance inconnue";
  return "Workspace";
}

/**
 * Numéro de page — **PDF uniquement**, et uniquement depuis `page_number`.
 *
 * Un classeur a `page_number: null` : aucune pastille « p. N » ne doit apparaître, et
 * surtout aucune page ne doit être FABRIQUÉE depuis un index de feuille. Les valeurs
 * < 1 sont écartées : les pages sont numérotées à partir de 1, un « Page 0 » serait un
 * artefact, pas une information.
 */
export function srcPage(s: AnySource | null | undefined): number | null {
  const p = s?.page_number ?? s?.source?.page_number ?? null;
  if (p == null) return null;
  const n = Number(p);
  return Number.isFinite(n) && n >= 1 ? n : null;
}

/**
 * Numéro de diapositive — **présentations uniquement**, et uniquement depuis
 * `meta.slide_number`.
 *
 * Symétrique exact de `srcPage` : une diapositive n'est PAS une page, et une page n'est
 * pas une diapositive. Aucun des deux ne doit être fabriqué depuis l'autre — c'est
 * précisément ce qui ferait afficher « p. 4 » pour un `.pptx` (qui n'a pas de pages) et
 * ouvrirait un lien viewer vers une page inexistante.
 *
 * Les numéros sont HUMAINS, donc >= 1 : un « diapositive 0 » serait un artefact.
 */
export function srcSlide(s: AnySource | null | undefined): number | null {
  const v = srcMeta(s).slide_number;
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) && n >= 1 ? n : null;
}

/**
 * La source est-elle l'évidence VISUELLE d'une diapositive (image intégrée analysée)
 * plutôt que la diapositive native ?
 *
 * Un fragment natif de présentation porte toujours `kind` = `pptx_slide`/`pptx_notes` ;
 * un fragment visuel porte `slide_number` SANS ce `kind` — il n'est jamais maquillé en
 * diapositive native. C'est la seule autre provenance qui cite une diapositive.
 */
export function srcIsSlideVisual(s: AnySource | null | undefined): boolean {
  return srcSlide(s) != null && !srcIsPptx(s);
}

/** Titre de la diapositive citée, quand elle en porte un. */
export function srcSlideTitle(s: AnySource | null | undefined): string | null {
  return str(srcMeta(s).slide_title);
}

/**
 * La source est-elle les NOTES du présentateur plutôt que la diapositive elle-même ?
 *
 * La distinction doit rester visible : une note est un commentaire de l'orateur, pas le
 * contenu publié. Citer l'un pour l'autre serait une erreur de provenance.
 */
export function srcIsSpeakerNotes(s: AnySource | null | undefined): boolean {
  return srcKind(s) === "pptx_notes";
}

/**
 * Repère de provenance GÉNÉRIQUE (`meta.locator`) — documents
 * structurés sans pages (XML, DXF, IFC…).
 *
 * Lecture tolérante : seules les chaînes non vides de `type`, `path`, `identifier` sont
 * conservées ; tout le reste (null, chaîne, nombre, objet vide, champs malformés) donne
 * `null`. Un locator n'est NI une page, NI une diapositive, NI une feuille : il ne
 * fabrique jamais l'un de ces repères, et réciproquement.
 */
export function srcLocator(s: AnySource | null | undefined): SourceLocator | null {
  const raw = srcMeta(s).locator;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const out: SourceLocator = {};
  const type = str(r.type);
  const path = str(r.path);
  const identifier = str(r.identifier);
  if (type) out.type = type;
  if (path) out.path = path;
  if (identifier) out.identifier = identifier;
  return path || identifier || type ? out : null;
}

/**
 * Libellé humain du repère : « chemin · identifiant », le chemin seul, ou l'identifiant
 * seul. `type` seul ne donne AUCUN libellé (c'est une nature technique, pas un repère
 * lisible). Rendu comme texte par React — jamais interprété.
 */
export function srcLocatorLabel(s: AnySource | null | undefined): string | null {
  const loc = srcLocator(s);
  if (!loc) return null;
  if (loc.path && loc.identifier) return `${loc.path} · ${loc.identifier}`;
  return loc.path ?? loc.identifier ?? null;
}

/**
 * Passage de TRANSCRIPTION AUDIO cité : intervalle lisible
 * (`locator.identifier`, ex. « 00:12:35 → 00:13:48 ») et précision de l'horodatage.
 *
 * Reconnu uniquement par `locator.type === "time_range"` : jamais déduit du nom de
 * fichier ni du snippet. `approximate` est vrai quand le backend n'a qu'un horodatage
 * à la PARTIE (`timestamp_precision === "part"`). Il n'existe pas de lecteur audio en
 * V1 : ce repère est un texte, jamais un lien vers un instant de l'enregistrement.
 */
export function srcAudioTimeRange(
  s: AnySource | null | undefined
): { range: string; approximate: boolean } | null {
  const loc = srcLocator(s);
  if (!loc || loc.type !== "time_range" || !loc.identifier) return null;
  return { range: loc.identifier, approximate: str(srcMeta(s).timestamp_precision) === "part" };
}

/**
 * Pièce jointe citée : nom et position dans l'arbre des pièces, quand
 * la source est le CONTENU d'une pièce jointe de courriel. `null` pour le corps du
 * courriel et pour tout autre document : rien n'est jamais déduit du nom de fichier.
 */
export function srcEmailAttachment(s: AnySource | null | undefined): { filename: string; path: string | null } | null {
  const m = srcMeta(s);
  if (m.email_part !== "attachment") return null;
  const filename = str(m.email_attachment_filename);
  if (!filename) return null;
  return { filename, path: str(m.email_attachment_path) };
}

/** Objet du courriel PARENT d'une source issue d'un courriel (corps ou pièce jointe). */
export function srcEmailSubject(s: AnySource | null | undefined): string | null {
  const m = srcMeta(s);
  if (m.email_part !== "attachment" && m.email_part !== "body") return null;
  return str(m.email_subject);
}

export function srcSnippet(s: AnySource | null | undefined): string {
  return (s?.snippet || s?.text || "").trim();
}

export function srcDocId(s: AnySource | null | undefined): string | null {
  const v = s?.document_id ?? s?.source?.document_id ?? null;
  if (typeof v === "number") return String(v);
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/**
 * Référence de source DISCRIMINÉE — le seul objet qu'une action doit recevoir.
 *
 * Un `document_id` nu ne suffit plus : les documents privés et ceux du corpus
 * métier vivent dans deux espaces d'UUID disjoints, et l'endpoint à appeler
 * dépend de la provenance, pas de la forme de l'identifiant. Transmettre l'UUID
 * seul est exactement ce qui envoyait un identifiant de corpus vers l'API privée.
 *
 * `unknown` est conservé comme variante à part entière : les sources antérieures
 * à la v2 du contrat n'ont pas de `source_type` et doivent continuer à emprunter
 * le chemin privé — retirer leurs actions punirait l'historique sans rien
 * sécuriser (`/doc/<id>` et l'API privée vérifient déjà l'accès).
 */
export type SourceReference =
  | { sourceType: "workspace_document"; documentId: string; pageNumber?: number | null }
  | {
      sourceType: "domain_corpus";
      documentId: string;
      pageNumber?: number | null;
      corpusId?: string | null;
    }
  | { sourceType: "unknown"; documentId: string; pageNumber?: number | null };

export function srcCorpusId(s: AnySource | null | undefined): string | null {
  return str(s?.corpus_id) || str(s?.source?.corpus_id);
}

/**
 * Construit la référence discriminée d'une source, ou `null` si elle n'est reliée
 * à aucun document (rien à résumer, aucune action à proposer).
 */
export function srcReference(s: AnySource | null | undefined): SourceReference | null {
  const documentId = srcDocId(s);
  if (!documentId) return null;
  const pageNumber = srcPage(s);
  const type = srcType(s);
  if (type === "domain_corpus") {
    return { sourceType: "domain_corpus", documentId, pageNumber, corpusId: srcCorpusId(s) };
  }
  if (type === "workspace_document") {
    return { sourceType: "workspace_document", documentId, pageNumber };
  }
  return { sourceType: "unknown", documentId, pageNumber };
}

export function srcChunkId(s: AnySource | null | undefined): string | null {
  const v = s?.chunk_id ?? null;
  if (typeof v === "number") return String(v);
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/**
 * Lien vers le viewer de document existant (page PDF + extrait surligné).
 *
 * Pour un classeur, `srcPage()` renvoie `null` : **aucun `page=` n'est ajouté**. Le
 * lien ouvre le document, avec `chunk_id` et `snippet` pour le surlignage — pas de
 * faux ancrage vers une page qui n'existe pas. (L'aperçu feuille/plage est P1.1.)
 */
export function srcViewerHref(s: AnySource | null | undefined): string | null {
  // Les documents de corpus métier n'ont pas de viewer privé (/doc/...) : ils ne sont
  // pas exposés par l'API documents du workspace.
  //
  // Une provenance INCONNUE, elle, conserve son lien. C'est délibéré : le contrat porte
  // `source_type` depuis sa v2, donc une source qui n'en a pas vient d'un message
  // ANCIEN — et c'est un document privé. Retirer le lien punirait l'historique sans rien
  // sécuriser : `/doc/<id>` vérifie lui-même l'accès au workspace. Le badge, en
  // revanche, ne doit pas affirmer une origine qu'on n'a pas — d'où `unknown` là, et le
  // lien ici.
  if (srcType(s) === "domain_corpus") return null;
  // Évidence visuelle d'une diapositive : l'aperçu de présentation rejoue des positions
  // de TEXTE natif, qu'un fragment visuel n'a pas (refus `slide_not_previewable`). Pas de
  // lien plutôt qu'un lien qui mène à une erreur ; la pastille « Diapositive N » reste.
  if (srcIsSlideVisual(s)) return null;
  const docId = srcDocId(s);
  if (!docId) return null;
  const qs = new URLSearchParams();
  // Une page de PIÈCE JOINTE n'est pas une page du courriel : aucun ancrage `page=` vers
  // le message parent (qui n'a pas de pages). Le lien ouvre le courriel, sous ses droits.
  const page = srcEmailAttachment(s) ? null : srcPage(s);
  if (page != null) qs.set("page", String(page));
  const chunk = srcChunkId(s);
  if (chunk) qs.set("chunk_id", chunk);
  const sn = srcSnippet(s);
  if (sn) qs.set("snippet", sn.length > 800 ? sn.slice(0, 800) + "…" : sn);
  qs.set("back", "/chat");
  const q = qs.toString();
  return q ? `/doc/${encodeURIComponent(docId)}?${q}` : `/doc/${encodeURIComponent(docId)}`;
}
