import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// --- Mocks des frontières ---------------------------------------------------
const apiFetch = vi.fn();
const bootstrap = vi.fn().mockResolvedValue(undefined);
vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...a),
  bootstrap: (...a: any[]) => bootstrap(...a),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

import { WorkspaceProvider, useWorkspace, SpaceSwitcher, type RagSpaceListItem } from "@/components/shell";

const ME = { id: "u1", email: "u@x.com", username: "u" };
const WS = { id: 1, name: "W1", role: "owner" };
const A: RagSpaceListItem = { id: "A", name: "Alpha", llm_provider: "ollama", embedding_provider: "ollama" };
const CREATED: RagSpaceListItem = {
  id: "B",
  name: "Beta",
  llm_provider: "ollama",
  llm_model: "llama3.1:8b",
  embedding_provider: "ollama",
  embedding_model: "nomic-embed-text",
};

// Contrôle de la réponse du listing des espaces (initial + revalidation).
let listResponse: RagSpaceListItem[] = [A];
let listShouldThrow = false;

function routeApi(url: string) {
  if (url.startsWith("/api/v1/auth/me/")) return Promise.resolve(ME);
  if (url === "/api/v1/workspaces/") return Promise.resolve([WS]);
  if (url.startsWith("/api/v1/rag-spaces/") && url.includes("workspace_id")) {
    if (listShouldThrow) return Promise.reject(new Error("network"));
    return Promise.resolve(listResponse);
  }
  if (url.includes("/stats/")) return Promise.resolve(null);
  return Promise.resolve({});
}

/** Reproduit EXACTEMENT la séquence de page.tsx::createSpace, mais sans rendre
 *  toute la page admin : upsert immédiat + sélection + revalidation silencieuse. */
function Harness() {
  const { ragSpaces, ragSpaceId, upsertRagSpace, setRagSpaceId, refreshRagSpaces } = useWorkspace();
  return (
    <div>
      <div data-testid="ids">{ragSpaces.map((s) => s.id).join(",")}</div>
      <div data-testid="sel">{ragSpaceId}</div>
      <button
        onClick={() => {
          // "created" = objet réellement renvoyé par le POST de création.
          upsertRagSpace(CREATED);
          setRagSpaceId(CREATED.id);
          void refreshRagSpaces();
        }}
      >
        create
      </button>
    </div>
  );
}

async function renderLoaded() {
  const utils = render(
    <WorkspaceProvider>
      <SpaceSwitcher />
      <Harness />
    </WorkspaceProvider>
  );
  // Attendre le chargement initial (liste = [A], sélection = A).
  await waitFor(() => expect(screen.getByTestId("ids")).toHaveTextContent("A"));
  await waitFor(() => expect(screen.getByTestId("sel")).toHaveTextContent("A"));
  return utils;
}

describe("WorkspaceContext — création d'espace met à jour la source partagée", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    apiFetch.mockImplementation(routeApi);
    listResponse = [A];
    listShouldThrow = false;
  });

  it("ajoute le nouvel espace, le sélectionne et l'affiche dans le sélecteur (sans reload)", async () => {
    const reloadSpy = vi.fn();
    Object.defineProperty(window, "location", {
      value: { ...window.location, reload: reloadSpy },
      writable: true,
    });
    // Après création, le backend renvoie la liste complète (A + B).
    listResponse = [A, CREATED];

    await renderLoaded();
    fireEvent.click(screen.getByText("create"));

    // Source partagée mise à jour + sélection auto.
    await waitFor(() => expect(screen.getByTestId("ids")).toHaveTextContent("A,B"));
    expect(screen.getByTestId("sel")).toHaveTextContent("B");
    // Le sélecteur (sidebar) affiche immédiatement le nouvel espace sélectionné.
    await waitFor(() => expect(screen.getByRole("button", { name: /Beta/ })).toBeInTheDocument());
    // Aucun refresh navigateur.
    expect(reloadSpy).not.toHaveBeenCalled();
  });

  it("ne crée pas de doublon quand la revalidation renvoie le même espace", async () => {
    listResponse = [A, CREATED]; // le refetch renvoie B (déjà ajouté en optimiste)
    await renderLoaded();
    fireEvent.click(screen.getByText("create"));
    await waitFor(() => expect(screen.getByTestId("ids")).toHaveTextContent("A,B"));
    // B n'apparaît qu'une seule fois.
    const ids = screen.getByTestId("ids").textContent!.split(",");
    expect(ids.filter((x) => x === "B")).toHaveLength(1);
  });

  it("conserve l'espace créé et sélectionné si la revalidation échoue", async () => {
    await renderLoaded();
    listShouldThrow = true; // le refetch post-création échoue
    fireEvent.click(screen.getByText("create"));
    // L'espace optimiste reste présent + sélectionné (pas d'écrasement de la liste).
    await waitFor(() => expect(screen.getByTestId("sel")).toHaveTextContent("B"));
    expect(screen.getByTestId("ids")).toHaveTextContent("A,B");
  });
});

/* ------------------------------------------------------------------------- */
/* openTarget */
/* ------------------------------------------------------------------------- */
describe("WorkspaceContext — openTarget cross-workspace", () => {
  const W2 = { id: 2, name: "W2", role: "member" };
  const W1_SPACES = [{ id: "A1", name: "A1" }, { id: "A2", name: "A2" }];
  const W2_SPACES = [{ id: "B1", name: "B1" }, { id: "B2", name: "B2" }];

  function routeTwo(url: string) {
    if (url.startsWith("/api/v1/auth/me/")) return Promise.resolve(ME);
    if (url === "/api/v1/workspaces/") return Promise.resolve([WS, W2]);
    if (url === "/api/v1/rag-spaces/?workspace_id=1") return Promise.resolve(W1_SPACES);
    if (url === "/api/v1/rag-spaces/?workspace_id=2") return Promise.resolve(W2_SPACES);
    if (url.includes("/stats/")) return Promise.resolve(null);
    return Promise.resolve({});
  }

  const seen: string[] = [];
  function Probe() {
    const { workspaceId, ragSpaceId, ragSpaces, openTarget } = useWorkspace();
    React.useEffect(() => {
      seen.push(`${workspaceId}:${ragSpaceId}:${ragSpaces.map((s) => s.id).join("|")}`);
    });
    return (
      <div>
        <div data-testid="ws">{String(workspaceId)}</div>
        <div data-testid="rs">{ragSpaceId}</div>
        <button onClick={() => openTarget({ workspaceId: 2, ragSpaceId: "B2", threadId: "t-42" })}>go</button>
        <button onClick={() => openTarget({ workspaceId: 99, ragSpaceId: "X" })}>bad</button>
      </div>
    );
  }

  beforeEach(() => {
    localStorage.clear();
    seen.length = 0;
    apiFetch.mockReset();
    apiFetch.mockImplementation(routeTwo);
  });

  it("sélectionne le bon espace du workspace cible, sans état intermédiaire mêlé", async () => {
    render(
      <WorkspaceProvider>
        <Probe />
      </WorkspaceProvider>
    );
    await waitFor(() => expect(screen.getByTestId("rs")).toHaveTextContent("A1"));
    fireEvent.click(screen.getByText("go"));
    await waitFor(() => expect(screen.getByTestId("rs")).toHaveTextContent("B2"));
    expect(screen.getByTestId("ws")).toHaveTextContent("2");
    // Fil pré-mémorisé pour l'espace cible (lu ensuite par la page chat).
    expect(JSON.parse(localStorage.getItem("ragatm_thread_by_ragspace")!)).toEqual({ B2: "t-42" });
    // Jamais le workspace 2 avec un espace ou une liste du workspace 1, jamais B1.
    for (const s of seen) {
      const [ws, rs, list] = s.split(":");
      if (ws === "2") {
        expect(rs === "" || rs === "B2").toBe(true);
        expect(list === "" || list === "B1|B2").toBe(true);
      }
    }
  });

  it("refuse un workspace qui n'est pas dans la liste active", async () => {
    render(
      <WorkspaceProvider>
        <Probe />
      </WorkspaceProvider>
    );
    await waitFor(() => expect(screen.getByTestId("rs")).toHaveTextContent("A1"));
    fireEvent.click(screen.getByText("bad"));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByTestId("ws")).toHaveTextContent("1");
    expect(screen.getByTestId("rs")).toHaveTextContent("A1");
  });
});
