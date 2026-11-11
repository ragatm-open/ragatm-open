// Détection et résolution des commandes de résumé en langage naturel dans le chat.
// Tout est pur (aucun effet, aucune dépendance React) pour être testable unitairement.
import type { AnySource, SourceReference } from "./sources";
import { srcDoc, srcReference } from "./sources";

export type SummaryRef =
  | { kind: "source"; n: number }
  | { kind: "deictic" } // « ce document », « cet article »…
  | { kind: "filename"; name: string };

export type SummaryIntent = { isSummary: boolean; ref: SummaryRef | null };

export type ResolveResult =
  // `ref` porte la PROVENANCE, pas seulement l'identifiant : une commande en
  // langage naturel qui vise une source de corpus métier doit emprunter le même
  // endpoint que le bouton du panneau, sinon les deux chemins divergent.
  | { status: "resolved"; ref: SourceReference; filename: string; scope: "document" | "page"; page?: number }
  | { status: "need_clarification"; message: string }
  | { status: "lookup_docs"; name: string } // à résoudre via la liste des documents de l'espace
  | { status: "fallthrough" }; // pas une commande de résumé exploitable → chat normal

export type DocLite = { id: string; filename: string; status?: string };

// Noms qui désignent un document résumable.
const DOC_NOUNS = "document|article|pdf|fichier|texte|rapport|doc|note|compte[- ]?rendu";
// Cibles explicitement NON documentaires : on laisse le chat normal répondre.
const DENY_NOUNS = "espace|conversation|discussion|echange|chat|thread|fil|historique";

function stripAccents(s: string): string {
  return s.normalize("NFD").replace(/[̀-ͯ]/g, "");
}
function norm(s: string): string {
  return stripAccents((s || "").toLowerCase()).trim();
}
function stem(filename: string): string {
  return norm(filename).replace(/\.[a-z0-9]+$/i, "");
}

/** Analyse une saisie utilisateur : est-ce une demande de résumé, et de quoi ? */
export function parseSummaryIntent(text: string): SummaryIntent {
  const t = norm(text);

  const hasTrigger = /\b(resum|synthes|synthetis)/.test(t) || /points?\s+cles?/.test(t);
  if (!hasTrigger) return { isSummary: false, ref: null };

  // La demande vise explicitement autre chose qu'un document (espace, conversation…).
  if (new RegExp(`\\b(${DENY_NOUNS})\\b`).test(t)) return { isSummary: false, ref: null };

  // « la source 2 », « source n°2 », « source numero 2 »
  const mSrc = t.match(/\bsources?\s+(?:numero\s+|n[°o]?\s*)?(\d{1,2})\b/);
  if (mSrc) return { isSummary: true, ref: { kind: "source", n: parseInt(mSrc[1], 10) } };

  // Un nom de fichier explicite (avec extension) — on garde la casse d'origine.
  const mFile = text.match(/([^\s"«»']+\.(?:pdf|docx?|txt|md|markdown|csv|html?|pptx?|xlsx?))/i);
  if (mFile) return { isSummary: true, ref: { kind: "filename", name: mFile[1] } };

  // « ce document », « cet article », « le rapport », « ce PDF »…
  if (
    new RegExp(`\\b(ce|cet|cette|le|la|du|des|les|mon|notre|votre)\\s+(${DOC_NOUNS})\\b`).test(t) ||
    new RegExp(`\\b(${DOC_NOUNS})\\b`).test(t)
  ) {
    return { isSummary: true, ref: { kind: "deictic" } };
  }

  // Déclencheur présent mais cible implicite : on tentera une résolution déictique.
  return { isSummary: true, ref: null };
}

function nameMatches(query: string, filename: string): boolean {
  const q = norm(query);
  const f = norm(filename);
  if (!q || !f) return false;
  if (f.includes(q) || q.includes(f)) return true;
  const fs = stem(filename);
  const qs = stem(query);
  return !!fs && (fs.includes(qs) || qs.includes(fs));
}

/**
 * Documents DISTINCTS cités, dédupliqués sur `(provenance, identifiant)`.
 *
 * La provenance entre dans la clé parce que les deux espaces d'UUID sont
 * disjoints : deux sources de familles différentes ne doivent jamais s'effondrer
 * l'une sur l'autre, même dans le cas théorique d'une collision.
 */
function distinctByDoc(sources: AnySource[]): { ref: SourceReference; name: string }[] {
  const seen = new Set<string>();
  const out: { ref: SourceReference; name: string }[] = [];
  for (const s of sources) {
    const ref = srcReference(s);
    if (!ref) continue;
    const key = `${ref.sourceType}:${ref.documentId}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ ref, name: srcDoc(s) });
  }
  return out;
}

const clar = (message: string): ResolveResult => ({ status: "need_clarification", message });

/**
 * Résout la cible d'un résumé à partir des sources de la dernière réponse.
 * Pour un nom de fichier absent des sources, renvoie `lookup_docs` afin que
 * l'appelant interroge la liste des documents de l'espace.
 */
export function resolveSummaryTargetFromSources(ref: SummaryRef | null, sources: AnySource[]): ResolveResult {
  if (ref && ref.kind === "source") {
    if (sources.length === 0)
      return clar("Aucune source récente : posez d'abord une question, puis demandez le résumé.");
    if (ref.n < 1 || ref.n > sources.length)
      return clar(`La dernière réponse ne contient que ${sources.length} source(s).`);
    const s = sources[ref.n - 1];
    const sourceRef = srcReference(s);
    if (!sourceRef) return clar("Cette source n'est pas reliée à un document résumable.");
    return { status: "resolved", ref: sourceRef, filename: srcDoc(s), scope: "document" };
  }

  if (ref && ref.kind === "filename") {
    const matches = distinctByDoc(sources.filter((s) => nameMatches(ref.name, srcDoc(s))));
    if (matches.length === 1)
      return { status: "resolved", ref: matches[0].ref, filename: matches[0].name, scope: "document" };
    if (matches.length > 1)
      return clar(
        `Plusieurs documents cités correspondent à « ${ref.name} » : ${matches
          .map((m) => m.name)
          .join(", ")}. Précisez lequel.`
      );
    return { status: "lookup_docs", name: ref.name };
  }

  // Déictique (« ce document ») ou cible implicite.
  const distinct = distinctByDoc(sources);
  if (distinct.length === 0)
    return clar("Précisez le document à résumer (par son nom), ou posez d'abord une question pour afficher des sources.");
  if (distinct.length === 1)
    return { status: "resolved", ref: distinct[0].ref, filename: distinct[0].name, scope: "document" };
  return clar(
    "Plusieurs documents sont cités dans la dernière réponse. Précisez lequel résumer (ex. « résume la source 2 »)."
  );
}

/** Résout un nom de fichier contre la liste des documents de l'espace (READY only). */
export function matchDocumentsByName(
  name: string,
  docs: DocLite[]
):
  | { status: "resolved"; documentId: string; filename: string }
  | { status: "need_clarification"; candidates: string[] }
  | { status: "not_found" } {
  const ready = docs.filter((d) => (d.status || "READY").toUpperCase() === "READY");
  const matches = ready.filter((d) => nameMatches(name, d.filename));

  const seen = new Set<string>();
  const distinct = matches.filter((d) => (seen.has(d.id) ? false : (seen.add(d.id), true)));

  if (distinct.length === 1) return { status: "resolved", documentId: distinct[0].id, filename: distinct[0].filename };
  if (distinct.length > 1) return { status: "need_clarification", candidates: distinct.map((d) => d.filename) };
  return { status: "not_found" };
}
