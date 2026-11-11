import type { RagSpaceListItem } from "./WorkspaceContext";

/**
 * Ajout ou remplacement IDEMPOTENT d'un espace dans une liste (clé = `id`).
 *
 * - si l'id existe déjà : remplace (merge superficiel) — jamais de doublon,
 *   utilisé aussi bien pour une création que pour une édition ;
 * - sinon : ajoute en fin de liste.
 *
 * Fonction pure (pas d'effet de bord) pour rester trivialement testable et
 * partagée par le contexte (source unique de la liste des espaces).
 */
export function upsertRagSpaceInList(
  list: RagSpaceListItem[],
  space: RagSpaceListItem
): RagSpaceListItem[] {
  const exists = list.some((r) => r.id === space.id);
  return exists
    ? list.map((r) => (r.id === space.id ? { ...r, ...space } : r))
    : [...list, space];
}
