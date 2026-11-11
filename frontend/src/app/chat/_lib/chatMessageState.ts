// frontend/src/app/chat/_lib/chatMessageState.ts
/**
 * Transformations PURES de la liste de messages du chat.
 *
 * Ce module existe à cause d'un bug précis : les mises à jour du flux SSE
 * copiaient le tableau (`[...messages]`) puis MUTAIENT le message visé
 * (`last.text = last.text + delta`). La copie étant superficielle, l'objet muté
 * était celui de l'état React courant. React StrictMode rejoue les updaters pour
 * détecter exactement cette impureté : le second passage repartait de l'état de
 * base mais retrouvait un objet DÉJÀ muté, et la concaténation s'appliquait deux
 * fois — la réponse s'affichait en double jusqu'à l'arrivée de `done`.
 *
 * Règles tenues ici, et nulle part ailleurs :
 * - aucun message existant n'est modifié : celui qui change est RECONSTRUIT ;
 * - la cible est désignée par un identifiant local STABLE (`local_id`), jamais
 *   par « le dernier message » — un événement tardif ne peut donc plus écrire
 *   dans le message d'une autre demande ;
 * - quand rien ne change (cible inconnue, valeur identique), le tableau reçu est
 *   renvoyé tel quel : React court-circuite le rendu.
 */
import type { Msg, MsgKind, SourceV1 } from "./types";

/** Position du message assistant portant `localId`. -1 si inconnu. */
function indexOfAssistant(messages: Msg[], localId: string): number {
  if (!localId) return -1;
  return messages.findIndex((m) => m.local_id === localId && m.role === "assistant");
}

/** Remplace UN message par une version reconstruite. L'entrée n'est jamais touchée. */
function withMessageAt(messages: Msg[], index: number, next: Msg): Msg[] {
  const out = [...messages];
  out[index] = next;
  return out;
}

/**
 * Concatène un delta de streaming au texte du message visé.
 *
 * Seule opération CUMULATIVE du flux : c'est la seule dont un rejeu d'updater
 * corrompait le résultat. Elle doit rester pure au sens strict — appelée deux
 * fois depuis le même état de base, elle rend deux fois le même résultat.
 */
export function appendAssistantDelta(messages: Msg[], localId: string, delta: string): Msg[] {
  if (!delta) return messages;
  const i = indexOfAssistant(messages, localId);
  if (i < 0) return messages;
  const current = messages[i];
  return withMessageAt(messages, i, { ...current, text: (current.text || "") + delta });
}

/**
 * Écrase le texte par le snapshot canonique (`done.answer`).
 *
 * Un `done` SANS `answer` (aide, choix de corpus, garde Excel, listing) ne doit
 * rien effacer : l'appelant ne nous appelle simplement pas dans ce cas.
 */
export function replaceAssistantText(messages: Msg[], localId: string, text: string): Msg[] {
  const i = indexOfAssistant(messages, localId);
  if (i < 0) return messages;
  const current = messages[i];
  if (current.text === text) return messages;
  return withMessageAt(messages, i, { ...current, text });
}

/** Remplace la liste de sources (le backend en envoie l'intégralité, jamais un ajout). */
export function setAssistantSources(messages: Msg[], localId: string, sources: SourceV1[]): Msg[] {
  const i = indexOfAssistant(messages, localId);
  if (i < 0) return messages;
  return withMessageAt(messages, i, { ...messages[i], sources: [...sources] });
}

/** Nature de la réponse (`no_answer`, `fallback_general`…), décidée par le serveur. */
export function setAssistantKind(messages: Msg[], localId: string, kind: MsgKind): Msg[] {
  const i = indexOfAssistant(messages, localId);
  if (i < 0) return messages;
  const current = messages[i];
  if (current.kind === kind) return messages;
  return withMessageAt(messages, i, { ...current, kind });
}

/** Applique un patch partiel au message visé (log_id, suggestions, statut…). */
export function patchAssistant(messages: Msg[], localId: string, patch: Partial<Msg>): Msg[] {
  const i = indexOfAssistant(messages, localId);
  if (i < 0) return messages;
  return withMessageAt(messages, i, { ...messages[i], ...patch });
}

/**
 * Retire l'attente de choix de mode sur TOUT le fil.
 *
 * Le choix est fait : les boutons ne doivent plus jamais réapparaître sur un
 * message, même si la requête échoue à mi-chemin.
 */
export function clearAwaitingCorpusChoice(messages: Msg[]): Msg[] {
  if (!messages.some((m) => m.awaiting_corpus_choice)) return messages;
  return messages.map((m) => (m.awaiting_corpus_choice ? { ...m, awaiting_corpus_choice: false } : m));
}

/**
 * Identifiant local d'un message optimiste. Sert UNIQUEMENT à retrouver le
 * message côté client ; il n'est jamais envoyé au backend ni persisté.
 */
export function newLocalId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `loc-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
