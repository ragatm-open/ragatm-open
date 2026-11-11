// frontend/src/lib/authProbe.ts
//
// Classement des erreurs d'un appel de GARDE d'authentification (`/auth/me/`,
// `/workspaces/` au chargement de la coquille), avec UN SEUL réessai sur 429.
//
// Incident staging du 08/10/2026 : un `catch {}` générique renvoyait vers /login
// sur un 429 de `/auth/me/` alors que la session serveur était intacte. Le seau DRF
// `user` (120/min) est commun à toute l'API : un rate-limit dit « attendez », jamais
// « vous n'êtes plus connecté ».
//
// Contrat :
//   401         → `unauthenticated` (SEUL cas qui autorise la redirection /login)
//   429         → attente `Retry-After` (borne ci-dessous), puis UN réessai
//   403, 5xx    → `unavailable` (aucun réessai, aucune redirection)
//   réseau      → `unavailable` (status null)
//   annulation  → `aborted` (l'appelant ne touche plus à son état)

import { isApiError } from "./apiError";

/** Attente sur 429 quand le serveur ne donne pas de `Retry-After` exploitable. */
export const RATE_LIMIT_FALLBACK_WAIT_MS = 5_000;

/**
 * Plafond d'attente avant le réessai. La fenêtre du throttle DRF `user` est d'une
 * minute : un délai plus long n'est pas un délai de throttle, on ne bloque pas la
 * coquille au-delà.
 */
export const RATE_LIMIT_MAX_WAIT_MS = 60_000;

export type GuardResult<T> =
  | { kind: "ok"; value: T }
  | { kind: "unauthenticated" }
  | { kind: "rate_limited"; retryAfterMs: number }
  | { kind: "unavailable"; status: number | null }
  | { kind: "aborted" };

function isAbortError(e: unknown): boolean {
  return typeof e === "object" && e !== null && (e as { name?: string }).name === "AbortError";
}

/** Délai d'attente effectif d'un 429 : celui du serveur, borné ; sinon le repli. */
export function rateLimitWaitMs(retryAfterMs: number | null | undefined): number {
  if (retryAfterMs == null || !Number.isFinite(retryAfterMs) || retryAfterMs <= 0) {
    return RATE_LIMIT_FALLBACK_WAIT_MS;
  }
  return Math.min(retryAfterMs, RATE_LIMIT_MAX_WAIT_MS);
}

/** Attente annulable. Résout `false` si le signal a été annulé. */
function sleep(ms: number, signal?: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve(false);
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve(true);
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      resolve(false);
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

async function attempt<T>(call: () => Promise<T>, signal?: AbortSignal): Promise<GuardResult<T>> {
  try {
    const value = await call();
    return signal?.aborted ? { kind: "aborted" } : { kind: "ok", value };
  } catch (e) {
    if (signal?.aborted || isAbortError(e)) return { kind: "aborted" };
    if (!isApiError(e)) return { kind: "unavailable", status: null };
    if (e.status === 401) return { kind: "unauthenticated" };
    if (e.status === 429) return { kind: "rate_limited", retryAfterMs: rateLimitWaitMs(e.retryAfterMs) };
    return { kind: "unavailable", status: e.status };
  }
}

/**
 * Exécute un appel de garde. Sur 429 : attend le délai annoncé puis réessaie UNE
 * fois. Un second 429 est rendu tel quel — jamais de boucle, jamais de logout.
 */
export async function guardedCall<T>(
  call: () => Promise<T>,
  signal?: AbortSignal,
): Promise<GuardResult<T>> {
  const first = await attempt(call, signal);
  if (first.kind !== "rate_limited") return first;
  if (!(await sleep(first.retryAfterMs, signal))) return { kind: "aborted" };
  return attempt(call, signal);
}
