// frontend/src/app/documents/_lib/documentsQuery.ts
//
// Construction de la requête de LISTE des documents. Miroir strict de l'allowlist
// backend (`backend/api/v1/documents/listing.py`) : recherche, filtre de statut,
// tri et pagination sont décidés par le serveur, jamais recalculés sur la page
// chargée. Isoler cette construction ici permet aux tests de vérifier ce qui part
// réellement sur le réseau, pas seulement ce qui s'affiche.

import type { DocStatus } from "@/components/ui";

/** Onglets de la liste : « tous » + les quatre catégories de `toDocStatus`. */
export type FilterKey = "all" | DocStatus;

/** Ordre d'affichage des onglets (et clés des compteurs renvoyés par l'API). */
export const FILTER_KEYS: readonly FilterKey[] = ["all", "ready", "analyzing", "queued", "error"];

/** Colonnes triables — allowlist identique à `listing.ORDERING_FIELDS`. */
export type SortField = "filename" | "file_type" | "status" | "created_at";

export const SORT_FIELDS: readonly SortField[] = ["filename", "file_type", "status", "created_at"];

/** Le plus récent d'abord : un import qui vient d'être lancé reste visible. */
export const DEFAULT_SORT: SortField = "created_at";
export const DEFAULT_SORT_DESC = true;

/** Tailles de page proposées ; la première est le défaut (aligné sur le backend). */
export const PAGE_SIZES: readonly number[] = [25, 50, 100];
export const DEFAULT_PAGE_SIZE = 50;

export type DocumentsQuery = {
  ragSpaceId: string;
  limit: number;
  offset: number;
  filter: FilterKey;
  search: string;
  sortField: SortField;
  sortDesc: boolean;
};

export const INITIAL_QUERY: Omit<DocumentsQuery, "ragSpaceId"> = {
  limit: DEFAULT_PAGE_SIZE,
  offset: 0,
  filter: "all",
  search: "",
  sortField: DEFAULT_SORT,
  sortDesc: DEFAULT_SORT_DESC,
};

/** `("status", true)` -> `"-status"` (contrat `?ordering=` du backend). */
export function orderingParam(field: SortField, desc: boolean): string {
  return `${desc ? "-" : ""}${field}`;
}

/**
 * URL complète de la liste. `status=all` et la recherche vide ne sont pas envoyés :
 * une requête sans filtre doit rester lisible dans les journaux et le cache HTTP.
 */
export function buildDocumentsUrl(q: DocumentsQuery): string {
  const p = new URLSearchParams();
  p.set("rag_space_id", q.ragSpaceId);
  p.set("limit", String(q.limit));
  p.set("offset", String(q.offset));
  p.set("ordering", orderingParam(q.sortField, q.sortDesc));
  if (q.filter !== "all") p.set("status", q.filter);
  const search = q.search.trim();
  if (search) p.set("search", search);
  return `/api/v1/documents/?${p.toString()}`;
}

/**
 * Prochain état de tri au clic sur un en-tête : une nouvelle colonne démarre dans
 * son sens le plus utile (les dates du plus récent au plus ancien, le texte de A
 * à Z) ; recliquer la même colonne inverse.
 */
export function nextSort(
  current: { sortField: SortField; sortDesc: boolean },
  field: SortField
): { sortField: SortField; sortDesc: boolean } {
  if (current.sortField === field) return { sortField: field, sortDesc: !current.sortDesc };
  return { sortField: field, sortDesc: field === "created_at" };
}

/** Valeur `aria-sort` d'un en-tête de colonne. */
export function ariaSort(
  current: { sortField: SortField; sortDesc: boolean },
  field: SortField
): "ascending" | "descending" | "none" {
  if (current.sortField !== field) return "none";
  return current.sortDesc ? "descending" : "ascending";
}
