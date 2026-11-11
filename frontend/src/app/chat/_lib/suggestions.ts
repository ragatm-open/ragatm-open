// frontend/src/app/chat/_lib/suggestions.ts
//
// Helpers PURS pour les suggestions de reformulation (cas no_answer). Isolés de
// page.tsx pour être testables sans monter tout le chat.

/**
 * Extrait une liste de suggestions non vides depuis le payload d'un événement
 * SSE (``done`` ou ``rephrasing_suggestions``). Tolérant : renvoie [] si le champ
 * est absent, n'est pas un tableau, ou ne contient aucune chaîne exploitable.
 * Borne dure à 3 (contrat API).
 */
export function extractSuggestions(data: any): string[] {
  const raw = data?.suggested_rephrasings;
  if (!Array.isArray(raw)) return [];
  return raw.filter((s: unknown): s is string => typeof s === "string" && s.trim().length > 0).slice(0, 3);
}
