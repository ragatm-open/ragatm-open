/**
 * Contrat d'un appel de garde d'authentification (lib/authProbe.ts).
 *
 * Incident staging du 08/10/2026 : un 429 de /auth/me/ renvoyait vers /login alors
 * que la session serveur était valide. Seul un 401 signifie « non connecté » ; un 429
 * attend `Retry-After` puis réessaie UNE fois.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ApiError } from "@/lib/apiError";
import {
  guardedCall,
  rateLimitWaitMs,
  RATE_LIMIT_FALLBACK_WAIT_MS,
  RATE_LIMIT_MAX_WAIT_MS,
} from "@/lib/authProbe";

const err = (status: number, retryAfterMs: number | null = null) =>
  new ApiError(status, "", { detail: "x" }, `API ${status} : x`, retryAfterMs);

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("rateLimitWaitMs", () => {
  it("respecte le délai du serveur", () => {
    expect(rateLimitWaitMs(17_000)).toBe(17_000);
  });
  it("se rabat sur un délai borné et documenté si l'en-tête manque ou est inexploitable", () => {
    expect(rateLimitWaitMs(null)).toBe(RATE_LIMIT_FALLBACK_WAIT_MS);
    expect(rateLimitWaitMs(undefined)).toBe(RATE_LIMIT_FALLBACK_WAIT_MS);
    expect(rateLimitWaitMs(0)).toBe(RATE_LIMIT_FALLBACK_WAIT_MS);
    expect(rateLimitWaitMs(Number.NaN)).toBe(RATE_LIMIT_FALLBACK_WAIT_MS);
  });
  it("plafonne l'attente à la fenêtre du throttle", () => {
    expect(rateLimitWaitMs(3_600_000)).toBe(RATE_LIMIT_MAX_WAIT_MS);
  });
});

describe("guardedCall — classement", () => {
  it.each([
    [401, "unauthenticated"],
    [403, "unavailable"],
    [500, "unavailable"],
    [503, "unavailable"],
  ])("HTTP %i → %s, sans réessai", async (status, kind) => {
    const call = vi.fn().mockRejectedValue(err(status));
    const r = await guardedCall(call);
    expect(r.kind).toBe(kind);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it("erreur réseau (non ApiError) → unavailable, status null, sans réessai", async () => {
    const call = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const r = await guardedCall(call);
    expect(r).toEqual({ kind: "unavailable", status: null });
    expect(call).toHaveBeenCalledTimes(1);
  });

  it("succès → ok", async () => {
    const r = await guardedCall(vi.fn().mockResolvedValue({ id: "u1" }));
    expect(r).toEqual({ kind: "ok", value: { id: "u1" } });
  });
});

describe("guardedCall — 429", () => {
  it("attend Retry-After avant l'unique réessai (jamais immédiat)", async () => {
    const call = vi.fn().mockRejectedValueOnce(err(429, 2_000)).mockResolvedValueOnce("me");
    const p = guardedCall(call);
    await vi.advanceTimersByTimeAsync(1_999);
    expect(call).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await expect(p).resolves.toEqual({ kind: "ok", value: "me" });
    expect(call).toHaveBeenCalledTimes(2);
  });

  it("deux 429 consécutifs : un seul réessai, résultat rate_limited avec le dernier délai", async () => {
    const call = vi.fn().mockRejectedValueOnce(err(429, 2_000)).mockRejectedValueOnce(err(429, 9_000));
    const p = guardedCall(call);
    await vi.advanceTimersByTimeAsync(2_000);
    await expect(p).resolves.toEqual({ kind: "rate_limited", retryAfterMs: 9_000 });
    await vi.advanceTimersByTimeAsync(120_000);
    expect(call).toHaveBeenCalledTimes(2);
  });

  it("429 puis 401 : la session est réellement invalide", async () => {
    const call = vi.fn().mockRejectedValueOnce(err(429, 1_000)).mockRejectedValueOnce(err(401));
    const p = guardedCall(call);
    await vi.advanceTimersByTimeAsync(1_000);
    await expect(p).resolves.toEqual({ kind: "unauthenticated" });
  });

  it("sans Retry-After : attente de repli, pas de réessai immédiat", async () => {
    const call = vi.fn().mockRejectedValueOnce(err(429, null)).mockResolvedValueOnce("me");
    const p = guardedCall(call);
    await vi.advanceTimersByTimeAsync(RATE_LIMIT_FALLBACK_WAIT_MS - 1);
    expect(call).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await expect(p).resolves.toEqual({ kind: "ok", value: "me" });
  });

  it("annulation pendant l'attente : aucun réessai, résultat aborted", async () => {
    const ctrl = new AbortController();
    const call = vi.fn().mockRejectedValueOnce(err(429, 5_000)).mockResolvedValueOnce("me");
    const p = guardedCall(call, ctrl.signal);
    await vi.advanceTimersByTimeAsync(1_000);
    ctrl.abort();
    await expect(p).resolves.toEqual({ kind: "aborted" });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(call).toHaveBeenCalledTimes(1);
  });
});
