// frontend/src/lib/navigationPrefs.ts
//
// Favoris et espaces récents, stockés DANS LE NAVIGATEUR (localStorage), par
// utilisateur : `ragatm_nav_favorites:<userId>` et `ragatm_nav_recents:<userId>`.
// Un poste partagé ne mélange donc jamais deux comptes.
//
// Seuls des IDENTIFIANTS d'espaces sont stockés — jamais un nom, un document ou un
// message. L'affichage croise toujours ces ids avec la liste renvoyée par le
// backend (`visibleIds`) : un espace supprimé ou révoqué disparaît sans action, un
// renommage s'affiche automatiquement. Aucune synchronisation entre appareils.

export const FAVORITES_MAX = 50;
export const RECENTS_MAX = 8;

const FAV_PREFIX = "ragatm_nav_favorites:";
const REC_PREFIX = "ragatm_nav_recents:";

export function favoritesKey(userId: string | number): string {
  return FAV_PREFIX + String(userId);
}

export function recentsKey(userId: string | number): string {
  return REC_PREFIX + String(userId);
}

function readIds(key: string, max: number): string[] {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const out: string[] = [];
    for (const v of parsed) {
      if (typeof v !== "string") continue;
      const id = v.trim();
      if (id && !out.includes(id)) out.push(id);
      if (out.length >= max) break;
    }
    return out;
  } catch {
    return [];
  }
}

function writeIds(key: string, ids: string[]) {
  try {
    localStorage.setItem(key, JSON.stringify(ids));
  } catch {
    // stockage indisponible (navigation privée, quota) : préférence non persistée
  }
}

function hasUser(userId: string | number | null | undefined): userId is string | number {
  return userId !== null && userId !== undefined && String(userId).trim() !== "";
}

export function getFavorites(userId: string | number | null | undefined): string[] {
  return hasUser(userId) ? readIds(favoritesKey(userId), FAVORITES_MAX) : [];
}

export function getRecents(userId: string | number | null | undefined): string[] {
  return hasUser(userId) ? readIds(recentsKey(userId), RECENTS_MAX) : [];
}

/**
 * Ajoute ou retire un favori. Rend la nouvelle liste. Au plafond (`FAVORITES_MAX`),
 * un AJOUT est refusé (liste inchangée) plutôt que d'évincer un favori en silence.
 */
export function toggleFavorite(userId: string | number | null | undefined, spaceId: string): string[] {
  if (!hasUser(userId) || !spaceId) return getFavorites(userId);
  const cur = getFavorites(userId);
  let next: string[];
  if (cur.includes(spaceId)) next = cur.filter((x) => x !== spaceId);
  else if (cur.length >= FAVORITES_MAX) return cur;
  else next = [...cur, spaceId];
  writeIds(favoritesKey(userId), next);
  return next;
}

/** Place `spaceId` en tête des récents (dédoublonné, `RECENTS_MAX` au plus). */
export function pushRecent(userId: string | number | null | undefined, spaceId: string): string[] {
  if (!hasUser(userId) || !spaceId) return getRecents(userId);
  const next = [spaceId, ...getRecents(userId).filter((x) => x !== spaceId)].slice(0, RECENTS_MAX);
  writeIds(recentsKey(userId), next);
  return next;
}

/** Ids stockés ∩ espaces visibles, dans l'ordre stocké. */
export function visibleIds(ids: string[], visible: { id: string }[]): string[] {
  const allowed = new Set(visible.map((v) => v.id));
  return ids.filter((id) => allowed.has(id));
}
