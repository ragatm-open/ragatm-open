/**
 * Suivi de l'ingestion — BUDGET RÉSEAU de la page Documents.
 *
 * Défaut constaté en staging le 21/09/2026 : liste + stats toutes les 2,5 s tant qu'un
 * document attendait, onglet caché compris. 48 req/min par onglet ; à trois onglets le
 * `UserRateThrottle` DRF (120 req/min PAR UTILISATEUR, tous onglets confondus) répondait
 * 429 et la page affichait « API 429 ». Ces tests comptent les requêtes réellement émises
 * sur une horloge simulée : c'est le trafic qui est vérifié, pas un message masqué.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";

import {
  POLL_ACTIVE_MAX_MS,
  POLL_ACTIVE_MIN_MS,
  countsSignature,
  nextPollDelay,
} from "@/app/documents/_lib/documentsPolling";

const calls: { url: string; at: number }[] = [];
let counts = { all: 10, ready: 6, analyzing: 1, queued: 3, error: 0 };
let listError: any = null;

const apiFetch = vi.fn(async (url: string) => {
  calls.push({ url, at: Date.now() });
  if (url.startsWith("/api/v1/documents/?")) {
    if (listError) {
      const e = listError;
      listError = null;
      throw e;
    }
    return { items: [], total: counts.all, counts: { ...counts } };
  }
  return {};
});
const refreshStats = vi.fn(() => {
  calls.push({ url: "STATS", at: Date.now() });
});

vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...(a as [string])),
  apiUploadForm: vi.fn(),
  apiFetchBlobUrl: vi.fn(),
  deleteDocument: vi.fn(),
  getCapabilities: vi.fn().mockResolvedValue({ embeddings: { provider: "ollama" } }),
  getWorkspaceQuota: vi.fn().mockResolvedValue({
    max_document_count: 5000, used_document_count: 10, max_document_bytes: 1e12,
    used_document_bytes: 1, count_usage_percent: 0, size_usage_percent: 0,
    is_exceeded: false, can_upload: true,
  }),
  summarizeDocumentAwait: vi.fn(),
  summaryErrorKey: vi.fn(),
}));

vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Topbar: () => <div />,
  useWorkspace: () => ({
    ragSpace: { id: "SP1", name: "AO", embedding_provider: "ollama" },
    ragSpaceId: "SP1",
    ragSpaceAccess: { permissionLevel: "write", spaceRole: null, isOwnerImplicit: false },
    canWriteCurrent: true,
    canManageCurrent: true,
    refreshStats,
    workspaceId: 1,
  }),
}));

import DocumentsPage from "@/app/documents/page";

const listCalls = () => calls.filter((c) => c.url.startsWith("/api/v1/documents/?"));
const trackedCalls = () => calls.filter((c) => c.url.startsWith("/api/v1/documents/?") || c.url === "STATS");

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

async function mount() {
  render(<DocumentsPage />);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function elapse(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  calls.length = 0;
  apiFetch.mockClear();
  refreshStats.mockClear();
  counts = { all: 10, ready: 6, analyzing: 1, queued: 3, error: 0 };
  listError = null;
  setVisibility("visible");
});

afterEach(() => {
  vi.useRealTimers();
});

describe("cadence (fonction pure)", () => {
  it("rien en cours ou onglet caché : aucun suivi", () => {
    expect(nextPollDelay({ inFlight: false, hidden: false, previousMs: null, changed: true })).toBeNull();
    expect(nextPollDelay({ inFlight: true, hidden: true, previousMs: 5000, changed: true })).toBeNull();
  });
  it("changement : cadence rapide ; immobilité : doublement borné", () => {
    expect(nextPollDelay({ inFlight: true, hidden: false, previousMs: 20000, changed: true })).toBe(POLL_ACTIVE_MIN_MS);
    expect(nextPollDelay({ inFlight: true, hidden: false, previousMs: 5000, changed: false })).toBe(10000);
    expect(nextPollDelay({ inFlight: true, hidden: false, previousMs: 20000, changed: false })).toBe(POLL_ACTIVE_MAX_MS);
    expect(nextPollDelay({ inFlight: true, hidden: false, previousMs: POLL_ACTIVE_MAX_MS, changed: false })).toBe(POLL_ACTIVE_MAX_MS);
  });
  it("429 : jamais avant l'échéance du serveur", () => {
    expect(nextPollDelay({ inFlight: true, hidden: false, previousMs: 5000, changed: false, rateLimited: true, retryAfterMs: 18000 })).toBe(18000);
    expect(nextPollDelay({ inFlight: true, hidden: false, previousMs: 5000, changed: false, rateLimited: true, retryAfterMs: null })).toBeGreaterThanOrEqual(30000);
  });
  it("empreinte des compteurs indépendante de l'ordre des clés", () => {
    expect(countsSignature({ a: 1, b: 2 })).toBe(countsSignature({ b: 2, a: 1 }));
    expect(countsSignature({ a: 1 })).not.toBe(countsSignature({ a: 2 }));
  });
});

describe("page Documents — trafic réel sur horloge simulée", () => {
  it("ingestion en cours, rien ne bouge : bien sous 120 req/min même à trois onglets", async () => {
    await mount();
    const start = Date.now();
    await elapse(60_000);
    const perMinute = trackedCalls().filter((c) => c.at > start).length;
    // Ancien comportement : 24 listes + 24 stats = 48/min/onglet.
    expect(perMinute).toBeLessThanOrEqual(8);
    expect(perMinute * 3).toBeLessThan(120);
  });

  it("au pire (compteurs qui changent à chaque tick) : ≤ 24 req/min par onglet", async () => {
    await mount();
    const start = Date.now();
    for (let i = 0; i < 12; i++) {
      counts = { ...counts, ready: counts.ready + 1, all: counts.all + 1 };
      await elapse(POLL_ACTIVE_MIN_MS);
    }
    const perMinute = trackedCalls().filter((c) => c.at > start).length;
    expect(perMinute).toBeLessThanOrEqual(24);
    expect(perMinute * 3).toBeLessThan(120);
  });

  it("onglet caché : aucune requête ; au retour : un rafraîchissement puis reprise", async () => {
    await mount();
    await elapse(POLL_ACTIVE_MIN_MS);
    setVisibility("hidden");
    await elapse(0);
    const before = listCalls().length;
    await elapse(5 * 60_000);
    expect(listCalls().length).toBe(before);
    setVisibility("visible");
    await elapse(0);
    expect(listCalls().length).toBe(before + 1);
    await elapse(POLL_ACTIVE_MIN_MS);
    expect(listCalls().length).toBe(before + 2);
  });

  it("aucun document en cours : aucun suivi", async () => {
    counts = { all: 10, ready: 10, analyzing: 0, queued: 0, error: 0 };
    await mount();
    const after = listCalls().length;
    await elapse(5 * 60_000);
    expect(listCalls().length).toBe(after);
  });

  it("HTTP 429 : le suivi respecte Retry-After au lieu d'insister", async () => {
    await mount();
    listError = Object.assign(new Error("API 429 Too Many Requests"), { status: 429, retryAfterMs: 18_000 });
    await elapse(POLL_ACTIVE_MIN_MS); // ce tick reçoit le 429
    const at429 = listCalls().length;
    await elapse(17_000);
    expect(listCalls().length).toBe(at429);
    await elapse(1_500);
    expect(listCalls().length).toBe(at429 + 1);
  });
});
