/**
 * Cadence de suivi de l'ingestion sur la page Documents.
 *
 * Le défaut corrigé : la page relisait la liste ET les stats toutes les 2,5 s tant
 * qu'un document était en attente, onglet visible ou non. Deux requêtes par tick,
 * soit 48 req/min par onglet : à trois onglets, 144 req/min dépassaient le plafond
 * `UserRateThrottle` de 120 req/min par utilisateur (commun à tous les onglets), et
 * la page s'affichait « API 429 ». La vague d'import du 21/09/2026 attendait pourtant
 * une heure en file : relire toutes les 2,5 s n'apportait rien à l'utilisateur.
 *
 * Contrat (fonction pure, sans horloge ni React — testable seule) :
 *   - rien en cours       → aucun suivi (`null`) ;
 *   - onglet caché        → aucun suivi ; un rafraîchissement unique au retour ;
 *   - quelque chose bouge → retour à la cadence rapide `POLL_ACTIVE_MIN_MS` ;
 *   - rien ne bouge       → l'intervalle double jusqu'à `POLL_ACTIVE_MAX_MS` ;
 *   - HTTP 429 reçu       → jamais avant l'échéance donnée par le serveur.
 *
 * Budget au pire (cadence rapide soutenue, liste + stats à chaque tick) :
 * 60/5 × 2 = 24 req/min par onglet VISIBLE — un onglet caché ne coûte rien.
 */

/** Cadence après un changement observé (ou à la reprise). */
export const POLL_ACTIVE_MIN_MS = 5_000;
/** Plafond de l'intervalle quand l'état ne change plus. */
export const POLL_ACTIVE_MAX_MS = 30_000;
/** Attente minimale sur 429 quand le serveur ne donne pas d'échéance. */
export const POLL_RATE_LIMITED_FALLBACK_MS = 30_000;

export type PollInput = {
  /** Au moins un document en file ou en analyse dans l'espace. */
  inFlight: boolean;
  /** `document.visibilityState !== "visible"`. */
  hidden: boolean;
  /** Intervalle du tick précédent (`null` au premier tick). */
  previousMs: number | null;
  /** L'état observé a changé depuis le tick précédent. */
  changed: boolean;
  /** Échéance serveur d'un 429 (ms), `undefined` si le tick n'a pas été limité. */
  retryAfterMs?: number | null;
  rateLimited?: boolean;
};

/** Délai avant le prochain tick, ou `null` : ne plus suivre (pour l'instant). */
export function nextPollDelay(input: PollInput): number | null {
  if (!input.inFlight || input.hidden) return null;
  if (input.rateLimited) {
    const wait = input.retryAfterMs && input.retryAfterMs > 0 ? input.retryAfterMs : POLL_RATE_LIMITED_FALLBACK_MS;
    return Math.max(wait, POLL_ACTIVE_MIN_MS);
  }
  if (input.changed || input.previousMs === null) return POLL_ACTIVE_MIN_MS;
  return Math.min(POLL_ACTIVE_MAX_MS, Math.max(POLL_ACTIVE_MIN_MS, input.previousMs * 2));
}

/** Empreinte des compteurs : les stats ne sont relues que si elle change. */
export function countsSignature(counts: { [k: string]: number | undefined } | null | undefined): string {
  if (!counts) return "";
  return Object.keys(counts)
    .sort()
    .map((k) => `${k}:${counts[k] ?? 0}`)
    .join("|");
}
