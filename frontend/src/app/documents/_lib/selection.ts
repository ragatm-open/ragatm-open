// frontend/src/app/documents/_lib/selection.ts
//
// Sélection de documents pour une reprise en masse.
//
// La liste étant paginée côté serveur, « tout sélectionner » ne peut PAS se
// réduire à cocher les lignes visibles : les 50 lignes d'une page ne sont pas les
// 184 documents en erreur de l'espace. Deux modes coexistent donc, et l'interface
// dit lequel est actif :
//
//   - `explicit`    : l'utilisateur a coché des lignes ; on connaît leurs ids.
//   - `allMatching` : « tous les résultats du filtre », exprimé au serveur par le
//                     filtre lui-même — moins d'éventuelles exclusions. Le
//                     navigateur n'a jamais à transporter des centaines d'ids
//                     pour dire « tout ».

import type { RetrySelection } from "@/lib/reindex";

export type SelectionState =
  | { mode: "none" }
  | { mode: "explicit"; ids: Set<string> }
  | { mode: "allMatching"; excludedIds: Set<string> };

export const EMPTY_SELECTION: SelectionState = { mode: "none" };

/** Nombre de documents réellement sélectionnés (`totalMatching` = total du filtre). */
export function selectedCount(sel: SelectionState, totalMatching: number): number {
  if (sel.mode === "none") return 0;
  if (sel.mode === "explicit") return sel.ids.size;
  return Math.max(0, totalMatching - sel.excludedIds.size);
}

export function isSelected(sel: SelectionState, id: string): boolean {
  if (sel.mode === "none") return false;
  if (sel.mode === "explicit") return sel.ids.has(id);
  return !sel.excludedIds.has(id);
}

export function hasSelection(sel: SelectionState, totalMatching: number): boolean {
  return selectedCount(sel, totalMatching) > 0;
}

/** Coche / décoche UNE ligne, dans le mode courant (jamais par position). */
export function toggleOne(sel: SelectionState, id: string): SelectionState {
  if (sel.mode === "allMatching") {
    // Décocher une ligne d'une sélection globale ne la fait PAS retomber en mode
    // explicite : on retire ce document du périmètre, et le serveur l'exclura.
    const excludedIds = new Set(sel.excludedIds);
    if (excludedIds.has(id)) excludedIds.delete(id);
    else excludedIds.add(id);
    return { mode: "allMatching", excludedIds };
  }
  const ids = new Set(sel.mode === "explicit" ? sel.ids : []);
  if (ids.has(id)) ids.delete(id);
  else ids.add(id);
  return ids.size === 0 ? EMPTY_SELECTION : { mode: "explicit", ids };
}

/** Coche/décoche toutes les lignes de la PAGE (jamais tout le filtre). */
export function togglePage(sel: SelectionState, pageIds: string[]): SelectionState {
  const allOnPageSelected = pageIds.length > 0 && pageIds.every((id) => isSelected(sel, id));
  if (sel.mode === "allMatching") {
    const excludedIds = new Set(sel.excludedIds);
    pageIds.forEach((id) => (allOnPageSelected ? excludedIds.add(id) : excludedIds.delete(id)));
    return { mode: "allMatching", excludedIds };
  }
  const ids = new Set(sel.mode === "explicit" ? sel.ids : []);
  pageIds.forEach((id) => (allOnPageSelected ? ids.delete(id) : ids.add(id)));
  return ids.size === 0 ? EMPTY_SELECTION : { mode: "explicit", ids };
}

/** Étend la sélection à TOUS les résultats du filtre courant. */
export function selectAllMatching(): SelectionState {
  return { mode: "allMatching", excludedIds: new Set() };
}

/** Retire un document disparu (supprimé, ou sorti du filtre après traitement). */
export function forgetDocument(sel: SelectionState, id: string): SelectionState {
  if (sel.mode !== "explicit") return sel;
  if (!sel.ids.has(id)) return sel;
  const ids = new Set(sel.ids);
  ids.delete(id);
  return ids.size === 0 ? EMPTY_SELECTION : { mode: "explicit", ids };
}

/**
 * Réconcilie une sélection explicite avec les documents encore présents dans le
 * filtre. Utilisé après un job : un document redevenu prêt quitte l'onglet
 * Erreurs, le garder coché n'aurait plus de sens.
 */
export function reconcile(sel: SelectionState, stillMatchingIds: Set<string>): SelectionState {
  if (sel.mode !== "explicit") return sel;
  const ids = new Set([...sel.ids].filter((id) => stillMatchingIds.has(id)));
  return ids.size === 0 ? EMPTY_SELECTION : { mode: "explicit", ids };
}

/** Corps `selection` de l'API de reprise. `null` si rien n'est sélectionné. */
export function toRetrySelection(sel: SelectionState, search: string): RetrySelection | null {
  if (sel.mode === "explicit") {
    if (sel.ids.size === 0) return null;
    return { mode: "explicit", document_ids: [...sel.ids] };
  }
  if (sel.mode === "allMatching") {
    return {
      mode: "filter",
      search: search.trim(),
      excluded_document_ids: [...sel.excludedIds],
    };
  }
  return null;
}
