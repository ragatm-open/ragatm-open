/**
 * Contrat de redirection /login du chargement de la coquille (WorkspaceProvider).
 *
 * Incident staging du 08/10/2026 : 429 sur /api/v1/auth/me/ → `catch {}` →
 * router.replace("/login"), alors que la session serveur était intacte (0 × 401,
 * 0 × 403, aucun logout). Contrat : LOGIN_REDIRECT_ON_401_ONLY.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { ApiError } from "@/lib/apiError";

const apiFetch = vi.fn();
const bootstrap = vi.fn().mockResolvedValue(undefined);
vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...a),
  bootstrap: (...a: any[]) => bootstrap(...a),
}));
const replace = vi.fn();
const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push }),
}));

import { WorkspaceProvider, useWorkspace } from "@/components/shell/WorkspaceContext";
import { AuthIssueNotice } from "@/components/shell/AuthIssueNotice";
import Home from "@/app/page";

const ME = { id: "u1", email: "u@x.com", username: "u" };
const WS = { id: 1, name: "W1", role: "admin" };

const apiErr = (status: number, retryAfterMs: number | null = null) =>
  new ApiError(status, "", { detail: "x" }, `API ${status} : x`, retryAfterMs);

/** Réponses successives de /auth/me/ (une par appel) ; au-delà : succès. */
let meQueue: Array<() => Promise<unknown>> = [];

function route(url: string, init?: RequestInit) {
  if (url === "/api/v1/auth/me/") {
    const next = meQueue.shift();
    return next ? next() : Promise.resolve(ME);
  }
  if (url === "/api/v1/auth/logout/") return Promise.resolve(null);
  if (url === "/api/v1/workspaces/") return Promise.resolve([WS]);
  if (url.startsWith("/api/v1/rag-spaces/?")) return Promise.resolve([]);
  return Promise.resolve({});
}

const meCalls = () => apiFetch.mock.calls.filter(([u]) => u === "/api/v1/auth/me/").length;
const logoutCalls = () => apiFetch.mock.calls.filter(([u]) => u === "/api/v1/auth/logout/").length;
const loginRedirects = () => replace.mock.calls.filter(([p]) => p === "/login").length;

function Probe() {
  const { me, loading, authIssue, workspaceId } = useWorkspace();
  return (
    <div>
      <div data-testid="me">{me ? me.id : "none"}</div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="issue">{authIssue ? authIssue.kind : "none"}</div>
      <div data-testid="ws">{workspaceId ?? "none"}</div>
    </div>
  );
}

function renderShell() {
  return render(
    <WorkspaceProvider>
      <AuthIssueNotice />
      <Probe />
    </WorkspaceProvider>
  );
}

/** Fait avancer les timers simulés ET vide les microtâches, dans `act`. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  apiFetch.mockReset();
  apiFetch.mockImplementation(route);
  replace.mockReset();
  push.mockReset();
  meQueue = [];
});
afterEach(() => {
  vi.useRealTimers();
});

describe("WorkspaceProvider — /auth/me/", () => {
  it("W1 — 401 : redirection /login", async () => {
    meQueue = [() => Promise.reject(apiErr(401))];
    renderShell();
    await advance(0);
    expect(loginRedirects()).toBe(1);
    expect(meCalls()).toBe(1);
    expect(screen.getByTestId("issue")).toHaveTextContent("none");
  });

  it("W2 — 429 (Retry-After 2 s) puis 200 : un réessai après 2 s, pas de /login, session conservée", async () => {
    meQueue = [() => Promise.reject(apiErr(429, 2_000)), () => Promise.resolve(ME)];
    renderShell();
    await advance(0);
    expect(meCalls()).toBe(1);
    // Pendant l'attente : ni /login, ni état « anonyme » affiché comme définitif.
    expect(loginRedirects()).toBe(0);
    expect(screen.getByTestId("loading")).toHaveTextContent("true");

    await advance(1_999);
    expect(meCalls()).toBe(1); // jamais avant l'échéance
    await advance(1);

    expect(meCalls()).toBe(2); // retry count = 1
    expect(screen.getByTestId("me")).toHaveTextContent("u1");
    expect(screen.getByTestId("ws")).toHaveTextContent("1");
    expect(screen.getByTestId("issue")).toHaveTextContent("none");
    expect(loginRedirects()).toBe(0);
    expect(logoutCalls()).toBe(0);
  });

  it("W3 — 429 puis 429 : un seul réessai, pas de /login, état transitoire exposé", async () => {
    meQueue = [() => Promise.reject(apiErr(429, 2_000)), () => Promise.reject(apiErr(429, 17_000))];
    renderShell();
    await advance(2_000);
    expect(meCalls()).toBe(2);

    expect(screen.getByTestId("issue")).toHaveTextContent("rate_limited");
    expect(screen.getByTestId("loading")).toHaveTextContent("false");
    expect(screen.getByTestId("auth-issue-notice")).toHaveTextContent(/environ 17 secondes/);
    expect(screen.getByTestId("auth-issue-notice")).toHaveTextContent(/session est conservée/);

    // Aucune boucle : rien ne repart tout seul.
    await advance(300_000);
    expect(meCalls()).toBe(2);
    expect(loginRedirects()).toBe(0);
    expect(logoutCalls()).toBe(0);
  });

  it("W4 — 500 : pas de /login, pas de logout, pas de réessai automatique", async () => {
    meQueue = [() => Promise.reject(apiErr(500))];
    renderShell();
    await advance(0);
    await advance(120_000);
    expect(loginRedirects()).toBe(0);
    expect(logoutCalls()).toBe(0);
    expect(meCalls()).toBe(1);
    expect(screen.getByTestId("issue")).toHaveTextContent("unavailable");
    expect(screen.getByTestId("auth-issue-notice")).toBeInTheDocument();
  });

  it("W5 — erreur réseau : pas de /login, pas de logout", async () => {
    meQueue = [() => Promise.reject(new TypeError("Failed to fetch"))];
    renderShell();
    await advance(0);
    expect(loginRedirects()).toBe(0);
    expect(logoutCalls()).toBe(0);
    expect(screen.getByTestId("issue")).toHaveTextContent("unavailable");
  });

  it("403 : pas traité comme une déconnexion", async () => {
    meQueue = [() => Promise.reject(apiErr(403))];
    renderShell();
    await advance(0);
    expect(loginRedirects()).toBe(0);
    expect(screen.getByTestId("issue")).toHaveTextContent("unavailable");
  });

  it("429 puis 401 : la session est réellement invalide → /login", async () => {
    meQueue = [() => Promise.reject(apiErr(429, 1_000)), () => Promise.reject(apiErr(401))];
    renderShell();
    await advance(1_000);
    expect(loginRedirects()).toBe(1);
  });

  it("« Réessayer » relance UNE tentative, qui peut réussir", async () => {
    meQueue = [() => Promise.reject(apiErr(500))];
    renderShell();
    await advance(0);
    expect(meCalls()).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: /Réessayer/ }));
    await advance(0);
    expect(meCalls()).toBe(2);
    expect(screen.getByTestId("me")).toHaveTextContent("u1");
    expect(screen.queryByTestId("auth-issue-notice")).not.toBeInTheDocument();
  });

  it("démontage pendant l'attente du 429 : aucun réessai", async () => {
    meQueue = [() => Promise.reject(apiErr(429, 5_000))];
    const { unmount } = renderShell();
    await advance(1_000);
    unmount();
    await advance(10_000);
    expect(meCalls()).toBe(1);
    expect(loginRedirects()).toBe(0);
  });
});

describe("WorkspaceProvider — /workspaces/ au chargement", () => {
  it("429 persistant sur /workspaces/ : pas de /login", async () => {
    apiFetch.mockImplementation((url: string, init?: RequestInit) =>
      url === "/api/v1/workspaces/" ? Promise.reject(apiErr(429, 1_000)) : route(url, init)
    );
    renderShell();
    await advance(1_000);
    expect(loginRedirects()).toBe(0);
    expect(apiFetch.mock.calls.filter(([u]) => u === "/api/v1/workspaces/")).toHaveLength(2);
    expect(screen.getByTestId("issue")).toHaveTextContent("rate_limited");
  });

  it("401 sur /workspaces/ : /login", async () => {
    apiFetch.mockImplementation((url: string, init?: RequestInit) =>
      url === "/api/v1/workspaces/" ? Promise.reject(apiErr(401)) : route(url, init)
    );
    renderShell();
    await advance(0);
    expect(loginRedirects()).toBe(1);
  });
});

describe("Page racine /", () => {
  it.each([
    [401, "/login"],
    [429, "/home"],
    [500, "/home"],
  ])("/auth/me/ %i → %s", async (status, target) => {
    meQueue = [() => Promise.reject(apiErr(status))];
    render(<Home />);
    await advance(0);
    expect(replace).toHaveBeenCalledWith(target);
  });

  it("/auth/me/ 200 → /home", async () => {
    render(<Home />);
    await advance(0);
    expect(replace).toHaveBeenCalledWith("/home");
  });
});
