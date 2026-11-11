/**
 * SpacesTab — boucle de statut `active-reindex-job` bornée.
 *
 * Incident staging du 08/10/2026 (99 espaces) : 1 GET par espace, relancé pour TOUS
 * les espaces à chaque nouvelle liste, l'ancienne boucle continuant d'émettre (le
 * drapeau d'annulation ne bloquait que l'état) et les 429 ignorés : 98 × 429 en 6 s,
 * seau DRF `user` (120/min) épuisé, puis /auth/me/ en 429 → renvoi à tort vers /login.
 */
import React, { useState } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act, configure } from "@testing-library/react";
import { ApiError } from "@/lib/apiError";

// Fixtures de 99-100 espaces : chaque carte est rendue. Sous la charge de la suite
// complète (workers parallèles), le délai par défaut (5 s / 1 s) ne suffit pas —
// même convention que DocumentsOfficeTempFiles (200 fichiers, 20 s).
vi.setConfig({ testTimeout: 20_000 });
configure({ asyncUtilTimeout: 10_000 });

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...a),
  // reindex.ts importe aussi apiFetchRaw (start/retry) : non sollicité ici.
  apiFetchRaw: vi.fn(),
}));
vi.mock("@/components/shell/WorkspaceContext", () => ({
  useWorkspace: () => ({
    upsertRagSpace: vi.fn(),
    refreshRagSpaces: vi.fn().mockResolvedValue([]),
    setRagSpaceId: vi.fn(),
    accessForRagSpace: () => ({ permissionLevel: null, spaceRole: null, isOwnerImplicit: false }),
  }),
}));

import { SpacesTab } from "@/app/workspace-admin/_components/SpacesTab";

// --- Fixtures ----------------------------------------------------------------
const space = (id: string, name = id) =>
  ({ id, name, llm_provider: "ollama", llm_model: "", embedding_provider: "ollama", embedding_model: "m" }) as any;
const spaces = (n: number) => Array.from({ length: n }, (_, i) => space(`s${String(i + 1).padStart(3, "0")}`));
const NEW = space("new-1", "Nouvel espace");

const activeJob = (ragSpaceId: string) => ({
  job_id: `job-${ragSpaceId}`,
  rag_space_id: ragSpaceId,
  status: "running",
  total: 10, pending: 5, queued: 0, running: 1, succeeded: 4, failed: 0, cancelled: 0, interrupted: 0,
  progress_percent: 40, parent_job_id: null, error_code: null,
  started_at: null, finished_at: null, created_at: null, updated_at: null,
});

const apiErr = (status: number, retryAfterMs: number | null = null) =>
  new ApiError(status, "", { detail: "x" }, `API ${status} : x`, retryAfterMs);

// --- Routeur d'API -------------------------------------------------------------
type StatusCall = { id: string; signal?: AbortSignal; at: number };
let statusCalls: StatusCall[] = [];
/** Réponse du statut d'un espace. Par défaut : aucun job actif, immédiatement. */
let statusResponder: (id: string, signal?: AbortSignal) => Promise<unknown>;
let serverList: any[] = [];
let createdOnServer = false;
let tick = 0;

const STATUS_RE = /^\/api\/v1\/rag-spaces\/([^/]+)\/active-reindex-job\/$/;

function route(url: string, init: RequestInit = {}) {
  const m = STATUS_RE.exec(url);
  if (m) {
    statusCalls.push({ id: m[1], signal: init.signal ?? undefined, at: tick++ });
    return statusResponder(m[1], init.signal ?? undefined);
  }
  if (url === "/api/v1/workspaces/1/ragspaces/" && init.method === "POST") {
    createdOnServer = true;
    return Promise.resolve(NEW);
  }
  if (url.startsWith("/api/v1/rag-spaces/?workspace_id=1")) {
    return Promise.resolve(createdOnServer ? [...serverList, NEW] : serverList);
  }
  if (url === "/api/v1/ai/model-catalog/") return Promise.resolve({ available: false });
  if (url === "/api/v1/capabilities/") return Promise.resolve({});
  if (url.startsWith("/api/v1/reindex-jobs/")) return Promise.resolve(activeJob("s001"));
  return Promise.resolve({});
}

/** Mode manuel : chaque requête de statut attend `release`, et honore l'annulation
 *  comme `fetch` (rejet AbortError). */
type Deferred = { id: string; resolve: (v: unknown) => void; signal?: AbortSignal };
let deferred: Deferred[] = [];
function manualStatus(id: string, signal?: AbortSignal) {
  return new Promise((resolve, reject) => {
    deferred.push({ id, resolve, signal });
    signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
  });
}
async function release(n: number, value: unknown = { active: false, job: null }) {
  for (let i = 0; i < n; i++) {
    await waitFor(() => expect(deferred.length).toBeGreaterThan(0));
    const d = deferred.shift()!;
    await act(async () => {
      d.resolve(value);
    });
  }
}
const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

const statusCount = () => statusCalls.length;
const postCount = () =>
  apiFetch.mock.calls.filter(([u, i]) => u === "/api/v1/workspaces/1/ragspaces/" && i?.method === "POST").length;
const countsById = () =>
  statusCalls.reduce<Record<string, number>>((acc, c) => ((acc[c.id] = (acc[c.id] ?? 0) + 1), acc), {});

/** Reproduit AdminContent : `onChanged` relit la liste et la repasse en prop. */
function Harness({ initial }: { initial: any[] }) {
  const [list, setList] = useState(initial);
  const onChanged = async () => {
    const rs = await apiFetch(`/api/v1/rag-spaces/?workspace_id=1`);
    setList(rs);
  };
  return <SpacesTab workspaceId={1} ragSpaces={list} loading={false} onChanged={onChanged} setErr={vi.fn()} />;
}

async function createViaModal() {
  fireEvent.click(screen.getAllByRole("button", { name: /Créer un espace/ })[0]);
  fireEvent.change(await screen.findByLabelText(/Nom de l'espace/), { target: { value: "Nouvel espace" } });
  const submit = screen.getByRole("button", { name: /Créer l'espace/ });
  await waitFor(() => expect(submit).not.toBeDisabled());
  fireEvent.click(submit);
  await waitFor(() => expect(screen.queryByRole("button", { name: /Créer l'espace/ })).not.toBeInTheDocument());
}

beforeEach(() => {
  vi.clearAllMocks();
  apiFetch.mockImplementation(route);
  statusCalls = [];
  deferred = [];
  tick = 0;
  createdOnServer = false;
  statusResponder = () => Promise.resolve({ active: false, job: null });
});

// --- W6 ---------------------------------------------------------------------
describe("W6 — annulation réelle de la boucle", () => {
  it("démontage en cours de boucle : la requête en vol est annulée et plus aucune ne part", async () => {
    statusResponder = manualStatus;
    const { unmount } = render(<SpacesTab workspaceId={1} ragSpaces={spaces(99)} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await release(3);
    await waitFor(() => expect(statusCount()).toBe(4));
    const inflight = statusCalls[3];

    unmount();
    expect(inflight.signal?.aborted).toBe(true);
    await flush();
    await flush();
    // CANCELLED_LOOP_RESIDUAL_REQUESTS = 0
    expect(statusCount()).toBe(4);
  });

  it("nouvelle liste en cours de boucle : l'ancien cycle s'arrête, aucun espace interrogé deux fois hormis l'annulé", async () => {
    statusResponder = manualStatus;
    const initial = spaces(99);
    const { rerender } = render(<SpacesTab workspaceId={1} ragSpaces={initial} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await release(10);
    await waitFor(() => expect(statusCount()).toBe(11));
    const inflight = statusCalls[10];

    // Changement de dépendance : même 99 espaces (nouvelle identité de tableau) + 1.
    statusResponder = () => Promise.resolve({ active: false, job: null });
    rerender(<SpacesTab workspaceId={1} ragSpaces={[...initial, NEW]} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    expect(inflight.signal?.aborted).toBe(true);

    // Le nouveau cycle reprend là où l'ancien s'est arrêté : 89 restants + 1 nouveau.
    await waitFor(() => expect(statusCount()).toBe(11 + 90));
    await flush();
    expect(statusCount()).toBe(101);
    const twice = Object.entries(countsById()).filter(([, n]) => n > 1).map(([id]) => id);
    expect(twice).toEqual([inflight.id]); // seule la requête annulée est rejouée
    // Aucune requête de l'ancien cycle après l'annulation : toutes les suivantes portent
    // un signal NON annulé (celui du nouveau cycle).
    expect(statusCalls.slice(11).every((c) => c.signal && !c.signal.aborted)).toBe(true);
  });

  it("une réponse arrivée APRÈS l'annulation ne met pas l'état à jour", async () => {
    // Réponse « active » qui ignore l'annulation (serveur lent) : elle ne doit pas
    // afficher de job pour un cycle abandonné.
    const late: Array<(v: unknown) => void> = [];
    statusResponder = (id) => new Promise((resolve) => late.push(resolve));
    const initial = [space("s001")];
    const { rerender } = render(<SpacesTab workspaceId={1} ragSpaces={initial} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await waitFor(() => expect(late).toHaveLength(1));

    statusResponder = () => Promise.resolve({ active: false, job: null });
    rerender(<SpacesTab workspaceId={1} ragSpaces={[...initial]} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await act(async () => {
      late[0]({ active: true, job: activeJob("s001") });
    });
    await flush();
    expect(screen.queryByRole("button", { name: /En cours/ })).not.toBeInTheDocument();
  });
});

// --- W7 ---------------------------------------------------------------------
describe("W7 — arrêt au premier 429", () => {
  it("espace 1 → 200, espace 2 → 429 : aucune requête pour les espaces 3..99", async () => {
    statusResponder = (id) =>
      id === "s002" ? Promise.reject(apiErr(429, 17_000)) : Promise.resolve({ active: false, job: null });
    render(<SpacesTab workspaceId={1} ragSpaces={spaces(99)} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId("reindex-status-rate-limited")).toBeInTheDocument());
    await flush();
    // REQUESTS_AFTER_FIRST_429 = 0
    expect(statusCalls.map((c) => c.id)).toEqual(["s001", "s002"]);
    // Pas de faux « aucune réindexation » : l'état inconnu est signalé.
    expect(screen.getByTestId("reindex-status-rate-limited")).toHaveTextContent(/98 espaces/);
    // La page Espaces reste utilisable.
    expect(screen.getAllByRole("button", { name: /Réindexer/ })).toHaveLength(99);
  });

  it("après un 429, une nouvelle liste ne relance rien avant Retry-After", async () => {
    statusResponder = (id) =>
      id === "s002" ? Promise.reject(apiErr(429, 17_000)) : Promise.resolve({ active: false, job: null });
    const initial = spaces(99);
    const { rerender } = render(<SpacesTab workspaceId={1} ragSpaces={initial} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await waitFor(() => expect(statusCount()).toBe(2));
    rerender(<SpacesTab workspaceId={1} ragSpaces={[...initial, NEW]} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await flush();
    expect(statusCount()).toBe(2);
    expect(screen.getByTestId("reindex-status-rate-limited")).toHaveTextContent(/99 espaces/);
  });

  it("erreurs non-429 (403/404/500/réseau) : politique inchangée — ignorées, la boucle continue", async () => {
    const errs: Record<string, unknown> = {
      s002: apiErr(403), s003: apiErr(404), s004: apiErr(500), s005: new TypeError("Failed to fetch"),
    };
    statusResponder = (id) => (errs[id] ? Promise.reject(errs[id]) : Promise.resolve({ active: false, job: null }));
    render(<SpacesTab workspaceId={1} ragSpaces={spaces(10)} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await waitFor(() => expect(statusCount()).toBe(10));
    expect(screen.queryByTestId("reindex-status-rate-limited")).not.toBeInTheDocument();
  });
});

// --- W8 / W9 ------------------------------------------------------------------
describe("W8/W9 — création d'espace", () => {
  it("W8 — 99 espaces déjà vérifiés, création → seul le nouvel espace est interrogé", async () => {
    serverList = spaces(99);
    render(<Harness initial={serverList} />);
    await waitFor(() => expect(statusCount()).toBe(99));
    await flush();

    await createViaModal();
    await waitFor(() => expect(screen.getByText("Nouvel espace")).toBeInTheDocument());
    await waitFor(() => expect(statusCount()).toBe(100));
    await flush();

    const after = statusCalls.slice(99).map((c) => c.id);
    // UNCHANGED_SPACES_REQUERIED_AFTER_CREATE = 0, NEW_SPACE_STATUS_REQUESTS = 1
    expect(after).toEqual(["new-1"]);
    expect(Object.values(countsById()).every((n) => n === 1)).toBe(true);
  });

  it("W9 — une action de création = exactement un POST", async () => {
    serverList = spaces(3);
    render(<Harness initial={serverList} />);
    await waitFor(() => expect(statusCount()).toBe(3));
    await createViaModal();
    await flush();
    expect(postCount()).toBe(1);
    expect(screen.getByText("Nouvel espace")).toBeInTheDocument();
  });

  it("W9 — POST en 429 : erreur affichée, aucun réessai implicite du POST", async () => {
    serverList = spaces(3);
    apiFetch.mockImplementation((url: string, init: RequestInit = {}) =>
      url === "/api/v1/workspaces/1/ragspaces/" && init.method === "POST"
        ? Promise.reject(apiErr(429, 5_000))
        : route(url, init)
    );
    render(<Harness initial={serverList} />);
    await waitFor(() => expect(statusCount()).toBe(3));
    fireEvent.click(screen.getAllByRole("button", { name: /Créer un espace/ })[0]);
    fireEvent.change(await screen.findByLabelText(/Nom de l'espace/), { target: { value: "X" } });
    const submit = screen.getByRole("button", { name: /Créer l'espace/ });
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);
    await waitFor(() => expect(screen.getByText(/API 429/)).toBeInTheDocument());
    await new Promise((r) => setTimeout(r, 50));
    expect(postCount()).toBe(1);
  });
});

// --- Non-régression -----------------------------------------------------------
describe("Non-régression Espaces", () => {
  it("un job actif découvert au chargement est toujours affiché et verrouille le bouton", async () => {
    statusResponder = (id) =>
      Promise.resolve(id === "s001" ? { active: true, job: activeJob("s001") } : { active: false, job: null });
    render(<SpacesTab workspaceId={1} ragSpaces={spaces(3)} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    const btn = await screen.findByRole("button", { name: /En cours/ });
    expect(btn).toBeDisabled();
    expect(screen.getAllByRole("button", { name: /^Réindexer$/ })).toHaveLength(2);
  });

  it("remontage (rechargement légitime de l'onglet) : tous les espaces sont revérifiés", async () => {
    const list = spaces(5);
    const first = render(<SpacesTab workspaceId={1} ragSpaces={list} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await waitFor(() => expect(statusCount()).toBe(5));
    first.unmount();
    render(<SpacesTab workspaceId={1} ragSpaces={list} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await waitFor(() => expect(statusCount()).toBe(10));
  });
});

// --- Mesures (§34) --------------------------------------------------------------
describe("Mesures de fan-out (fixture 100 espaces)", () => {
  it("compte les requêtes de statut", async () => {
    const m: Record<string, number> = {};

    // INITIAL_STATUS_REQUESTS : montage sur 100 espaces.
    const a = render(<SpacesTab workspaceId={1} ragSpaces={spaces(100)} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await waitFor(() => expect(statusCount()).toBeGreaterThanOrEqual(100));
    await flush();
    m.INITIAL_STATUS_REQUESTS = statusCount();
    a.unmount();

    // POST_CREATE_STATUS_REQUESTS : 99 vérifiés, puis création (liste de 100).
    statusCalls = [];
    serverList = spaces(99);
    const b = render(<Harness initial={serverList} />);
    await waitFor(() => expect(statusCount()).toBeGreaterThanOrEqual(99));
    await flush();
    const before = statusCount();
    await createViaModal();
    await waitFor(() => expect(screen.getByText("Nouvel espace")).toBeInTheDocument());
    await flush();
    await flush();
    m.POST_CREATE_STATUS_REQUESTS = statusCount() - before;
    b.unmount();

    // CANCELLED_LOOP_RESIDUAL_REQUESTS : requêtes émises après démontage en cours de boucle.
    statusCalls = [];
    createdOnServer = false;
    deferred = [];
    statusResponder = manualStatus;
    const c = render(<SpacesTab workspaceId={1} ragSpaces={spaces(100)} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await release(5);
    const atCancel = statusCount();
    c.unmount();
    while (deferred.length) await act(async () => deferred.shift()!.resolve({ active: false, job: null }));
    await flush();
    await flush();
    m.CANCELLED_LOOP_RESIDUAL_REQUESTS = statusCount() - atCancel;

    // REQUESTS_AFTER_FIRST_429 : 429 sur le 2e espace.
    statusCalls = [];
    statusResponder = (id) =>
      id === "s002" ? Promise.reject(apiErr(429, 17_000)) : Promise.resolve({ active: false, job: null });
    const d = render(<SpacesTab workspaceId={1} ragSpaces={spaces(100)} loading={false} onChanged={vi.fn()} setErr={vi.fn()} />);
    await waitFor(() => expect(statusCount()).toBeGreaterThanOrEqual(2));
    await flush();
    await flush();
    m.REQUESTS_AFTER_FIRST_429 = statusCount() - 2;
    d.unmount();

    console.log("FANOUT_METRICS " + JSON.stringify(m));
    expect(m).toEqual({
      INITIAL_STATUS_REQUESTS: 100,
      POST_CREATE_STATUS_REQUESTS: 1,
      CANCELLED_LOOP_RESIDUAL_REQUESTS: 0,
      REQUESTS_AFTER_FIRST_429: 0,
    });
  });
});
