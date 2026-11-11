// frontend/src/lib/navigationSearch.ts
//
// Recherche de navigation.
//
// - Espaces : filtrage et classement 100 % locaux, sur la liste que le backend
//   renvoie déjà (aucun appel réseau).
// - Conversations : client de `GET /api/v1/chat/threads/search/`. Le serveur est
//   l'autorité (propriété du fil + accès à l'espace) ; ce module ne filtre rien.
// - Ouverture d'un fil par son id : `GET /api/v1/chat/threads/<uuid>/`.
//
// Un 429 de la recherche est une ERREUR DE RECHERCHE, jamais une perte de session :
// aucun appel ici ne passe par la garde d'authentification (`authProbe`), et aucun
// ne réessaie de lui-même. L'appelant affiche un état « trop de recherches ».

import { apiFetch } from "./api";

/** Pliage pour la comparaison : décomposition NFD, retrait des marques combinantes,
 *  minuscules. `Dépenses` et `DEPENSES` deviennent tous deux `depenses`. */
export function foldText(s: string | null | undefined): string {
  return (s || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

/** Nombre maximal d'espaces rendus par un classement (le reste est tronqué). */
export const SPACE_RESULTS_MAX = 50;

const WORD_CHAR = /[\p{L}\p{N}]/u;

/**
 * Rang de correspondance d'un nom plié avec une requête pliée :
 * 0 = égalité, 1 = préfixe, 2 = début de mot, 3 = sous-chaîne ; `null` = aucune.
 */
export function matchRank(foldedName: string, foldedQuery: string): number | null {
  if (!foldedQuery) return null;
  if (foldedName === foldedQuery) return 0;
  if (foldedName.startsWith(foldedQuery)) return 1;
  let from = 0;
  let first = -1;
  for (;;) {
    const at = foldedName.indexOf(foldedQuery, from);
    if (at < 0) break;
    if (first < 0) first = at;
    if (at > 0 && !WORD_CHAR.test(foldedName[at - 1])) return 2;
    from = at + 1;
  }
  return first >= 0 ? 3 : null;
}

export type RankableSpace = { id: string; name: string };

/**
 * Classement local des espaces pour une requête : clé de tri
 * `(rang_correspondance, favori ? 0 : 1, rang_récence, nom_plié, id)`.
 * Requête vide → aucun résultat (les sections Favoris/Récents/Tous s'en chargent).
 */
export function rankSpaces<T extends RankableSpace>(
  spaces: T[],
  query: string,
  prefs: { favorites?: string[]; recents?: string[] } = {},
  limit: number = SPACE_RESULTS_MAX
): T[] {
  const q = foldText(query).trim();
  if (!q) return [];
  const fav = new Set(prefs.favorites ?? []);
  const recents = prefs.recents ?? [];
  const recency = (id: string) => {
    const i = recents.indexOf(id);
    return i < 0 ? Number.MAX_SAFE_INTEGER : i;
  };
  const scored: { s: T; rank: number; fav: number; rec: number; name: string }[] = [];
  for (const s of spaces) {
    const name = foldText(s.name);
    const rank = matchRank(name, q);
    if (rank === null) continue;
    scored.push({ s, rank, fav: fav.has(s.id) ? 0 : 1, rec: recency(s.id), name });
  }
  scored.sort(
    (a, b) =>
      a.rank - b.rank ||
      a.fav - b.fav ||
      a.rec - b.rec ||
      (a.name < b.name ? -1 : a.name > b.name ? 1 : 0) ||
      (a.s.id < b.s.id ? -1 : a.s.id > b.s.id ? 1 : 0)
  );
  return scored.slice(0, Math.max(0, limit)).map((x) => x.s);
}

/* ------------------------------------------------------------------ */
/* Conversations                                                       */
/* ------------------------------------------------------------------ */

/** Longueur minimale d'une requête envoyée au serveur (en dessous : aucun appel). */
export const THREAD_QUERY_MIN = 2;
/** Longueur maximale acceptée par le serveur. */
export const THREAD_QUERY_MAX = 200;
/** Délai de frappe avant d'interroger le serveur. */
export const THREAD_SEARCH_DEBOUNCE_MS = 250;

export type ThreadSearchResult = {
  id: string;
  title: string | null;
  rag_space_id: string;
  rag_space_name: string;
  workspace_id: number;
  workspace_name: string;
  updated_at: string;
  match: "title" | "content";
  /** Texte BRUT (jamais du HTML) : à afficher comme texte. */
  snippet: string | null;
};

export type ThreadSearchResponse = { results: ThreadSearchResult[]; next: string | null };

export type ThreadSearchOptions = {
  signal?: AbortSignal;
  cursor?: string | null;
  scope?: "all" | "title";
  ragSpaceId?: string | null;
  workspaceId?: number | null;
  limit?: number;
};

/** Recherche de conversations de l'utilisateur courant (serveur = autorité). */
export function searchThreads(q: string, opts: ThreadSearchOptions = {}): Promise<ThreadSearchResponse> {
  const p = new URLSearchParams();
  p.set("q", q.trim());
  if (opts.scope) p.set("scope", opts.scope);
  if (opts.ragSpaceId) p.set("rag_space_id", opts.ragSpaceId);
  if (opts.workspaceId !== undefined && opts.workspaceId !== null) p.set("workspace_id", String(opts.workspaceId));
  if (opts.limit) p.set("limit", String(opts.limit));
  if (opts.cursor) p.set("cursor", opts.cursor);
  return apiFetch<ThreadSearchResponse>(`/api/v1/chat/threads/search/?${p.toString()}`, { signal: opts.signal });
}

export type ResolvedThread = {
  id: string;
  title: string | null;
  rag_space_id: string;
  workspace_id: number;
  created_at?: string;
  updated_at?: string;
};

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** `true` si `s` a la forme d'un UUID (vérification locale avant tout appel). */
export function isUuid(s: string | null | undefined): boolean {
  return UUID_RE.test((s || "").trim());
}

/** Résout le contexte (espace, workspace) d'un fil — 404/403 si inaccessible. */
export function resolveThread(id: string, init: RequestInit = {}): Promise<ResolvedThread> {
  return apiFetch<ResolvedThread>(`/api/v1/chat/threads/${encodeURIComponent(id)}/`, init);
}

/** Lien direct canonique vers une conversation. */
export function threadHref(id: string): string {
  return `/chat?thread=${encodeURIComponent(id)}`;
}
