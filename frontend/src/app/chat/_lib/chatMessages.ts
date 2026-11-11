// frontend/src/app/chat/_lib/chatMessages.ts
/**
 * Passage historique serveur -> messages affichés, et réconciliation avec les
 * messages optimistes.
 *
 * Extrait dans un module pur (aucun React, aucun fetch) parce que c'est ici que
 * vivait le bug : un résumé injecté seulement dans l'état local disparaissait au
 * premier rechargement du fil. La règle est désormais explicite et testable —
 * le serveur est la source de vérité, l'optimisme n'est qu'un affichage
 * transitoire réconcilié par identité.
 */
import type { Msg, MsgCategory, MsgStatus, SourceV1 } from "./types";

/** Forme « fil de fer » d'un message tel que le renvoie l'API d'historique. */
export type WireChatMessage = {
  id?: string;
  role: "user" | "assistant";
  content: string;
  created_at?: string;
  sources?: SourceV1[] | null;
  request_id?: string | null;
  rag_query_log_id?: string | null;
  analysis_id?: string | null;
  kind?: string | null;
  status?: string | null;
  document_id?: string | null;
  summary_id?: string | null;
  awaiting_corpus_choice?: boolean;
  global_analysis_available?: boolean;
};

const KNOWN_CATEGORIES: MsgCategory[] = ["document_summary"];
const KNOWN_STATUSES: MsgStatus[] = ["pending", "completed", "failed"];

/**
 * Ne retient qu'une valeur connue. Un `kind` inconnu (backend plus récent que
 * le frontend) est ramené à `null`, ce qui dégrade vers le rendu standard
 * plutôt que de produire un message vide ou un crash.
 */
function asCategory(v: string | null | undefined): MsgCategory | null {
  return v && (KNOWN_CATEGORIES as string[]).includes(v) ? (v as MsgCategory) : null;
}

function asStatus(v: string | null | undefined): MsgStatus | null {
  return v && (KNOWN_STATUSES as string[]).includes(v) ? (v as MsgStatus) : null;
}

/** Convertit un message serveur en message affichable. Aucune perte de champ. */
export function mapServerMessage(m: WireChatMessage): Msg {
  return {
    id: m.id,
    request_id: m.request_id ?? null,
    role: m.role === "assistant" ? "assistant" : "user",
    text: m.content || "",
    sources: (m.sources as SourceV1[] | undefined) || undefined,
    created_at: m.created_at,
    log_id: m.rag_query_log_id ?? null,
    // Sans cet identifiant, la carte d'analyse n'a plus de message auquel
    // s'accrocher après un rechargement : elle disparaîtrait du fil.
    analysis_id: m.analysis_id ?? null,
    category: asCategory(m.kind),
    status: asStatus(m.status),
    document_id: m.document_id ?? null,
    summary_id: m.summary_id ?? null,
    // Le serveur RECALCULE si un choix est encore en attente. Un choix consommé
    // ne revient jamais, un vieux message n'en porte jamais.
    awaiting_corpus_choice: !!m.awaiting_corpus_choice,
    global_analysis_available: !!m.global_analysis_available,
  };
}

export function mapServerMessages(list: WireChatMessage[] | null | undefined): Msg[] {
  return (list || []).map(mapServerMessage);
}

/** Identité de réconciliation : jamais le texte, jamais l'index. */
function identity(m: Msg): string | null {
  if (m.id) return `id:${m.id}`;
  if (m.request_id) return `rq:${m.request_id}:${m.role}`;
  return null;
}

/**
 * Fusionne l'état local avec l'historique serveur.
 *
 * Règles :
 * - le serveur fait autorité : quand les deux versions existent, la sienne gagne ;
 * - un message local identifiable (par `id` ou `request_id`) déjà présent côté
 *   serveur n'est pas dupliqué — c'est ce qui évite le doublon quand la réponse
 *   du POST et le rechargement de l'historique se croisent ;
 * - un message local encore inconnu du serveur (POST en vol) est CONSERVÉ, en
 *   queue, pour ne pas faire clignoter l'affichage ;
 * - un message local non identifiable est abandonné : il n'a jamais été persisté,
 *   donc l'afficher plus longtemps serait mentir sur son sort.
 */
export function reconcileChatMessages(local: Msg[], server: Msg[]): Msg[] {
  const seen = new Set<string>();
  for (const m of server) {
    const key = identity(m);
    if (key) seen.add(key);
  }

  // Un même message peut être connu côté serveur par son id et côté local par son
  // seul request_id : on indexe aussi par request_id pour ne pas le dédoubler.
  const serverByRequest = new Set<string>();
  for (const m of server) {
    if (m.request_id) serverByRequest.add(`${m.request_id}:${m.role}`);
  }

  const pendingLocal = local.filter((m) => {
    const key = identity(m);
    if (!key) return false; // jamais persisté -> pas de survie artificielle
    if (seen.has(key)) return false;
    if (m.request_id && serverByRequest.has(`${m.request_id}:${m.role}`)) return false;
    return true;
  });

  return [...server, ...pendingLocal];
}

/**
 * Clé React stable. L'index de tableau recycle les nœuds au mauvais message.
 *
 * Priorité : identifiant serveur > corrélation de requête > identifiant local
 * optimiste. L'index ne sert plus que de dernier recours (messages purement
 * décoratifs, sans aucune identité).
 */
export function messageKey(m: Msg, index: number): string {
  if (m.id) return m.id;
  if (m.request_id) return `${m.request_id}:${m.role}`;
  if (m.local_id) return `local:${m.local_id}`;
  return `idx:${index}`;
}
