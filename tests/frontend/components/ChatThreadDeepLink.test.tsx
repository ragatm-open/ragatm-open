/**
 * Page Assistant : lien direct `/chat?thread=<uuid>` et fils au-delà des 100 plus
 * récents. Le contenu passe toujours par `/messages/`.
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  params: new URLSearchParams(),
  openTarget: vi.fn(() => true),
  ragSpaceId: "rs-1",
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => state.params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@/app/chat/_components/SourcesPanel", async () => {
  const React = await import("react");
  return {
    SOURCES_PANEL_ID: "chat-sources",
    SourcesPanel: React.forwardRef<HTMLElement, any>(function SourcesPanel(_p, ref) {
      return <aside ref={ref as any} id="chat-sources" />;
    }),
  };
});

vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: any) => <div>{children}</div>,
  Topbar: ({ actions }: any) => <div>{actions}</div>,
  useWorkspace: () => ({
    ragSpace: { id: state.ragSpaceId, name: "Espace" },
    ragSpaceId: state.ragSpaceId,
    workspaceId: 1,
    openTarget: state.openTarget,
    currentStats: { documents: { total: 3 }, indexing: { in_progress: false } },
  }),
}));

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetch: (...args: any[]) => apiFetch(...args),
  listDocuments: vi.fn(async () => ({ items: [] })),
  summarizeSourceDocumentAwait: vi.fn(),
  summaryErrorKey: (s: string) => s,
  getRephrasingSuggestions: vi.fn(async () => ({ suggestions: [] })),
  cancelCorpusAnalysis: vi.fn(),
  getCorpusAnalysis: vi.fn(),
  getThreadAnalyses: vi.fn(async () => ({ analyses: [] })),
  isTerminalAnalysisStatus: () => true,
  getPreferences: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
  updateAnswerLanguage: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
}));
vi.mock("@/lib/auth", () => ({ getCsrfToken: () => null }));

import { ApiError } from "@/lib/apiError";
import ChatPage from "@/app/chat/page";
import { setStoredThreadIdForRagSpace } from "@/app/chat/_lib/storage";

const OLD = "0a0a0a0a-1111-4222-8333-444444444444";
const DENIED = "0b0b0b0b-1111-4222-8333-444444444444";
const OTHER_SPACE = "0c0c0c0c-1111-4222-8333-444444444444";
const PAGE1 = Array.from({ length: 100 }, (_, i) => ({
  id: `t${String(i).padStart(3, "0")}`,
  title: `Fil ${i}`,
  updated_at: new Date(Date.UTC(2026, 9, 30) - i * 60_000).toISOString(),
}));
const PAGE2 = [{ id: "t100", title: "Fil 100", updated_at: "2026-09-01T00:00:00Z" }];

function messagesCalls(): string[] {
  return apiFetch.mock.calls.map(([u]) => String(u)).filter((u) => u.endsWith("/messages/"));
}

function routeApi() {
  apiFetch.mockImplementation(async (url: string) => {
    if (url === "/api/v1/chat/threads/?rag_space_id=rs-1") return PAGE1;
    if (url.startsWith("/api/v1/chat/threads/?rag_space_id=rs-1&before=")) return PAGE2;
    if (url === `/api/v1/chat/threads/${OLD}/`)
      return { id: OLD, title: "Très ancienne conversation", rag_space_id: "rs-1", workspace_id: 1 };
    if (url === `/api/v1/chat/threads/${OTHER_SPACE}/`)
      return { id: OTHER_SPACE, title: "Ailleurs", rag_space_id: "rs-2", workspace_id: 2 };
    if (url === `/api/v1/chat/threads/${DENIED}/`) throw new ApiError(404, "Not Found", { detail: "not_found" }, "404");
    if (url.endsWith("/messages/")) return [];
    return [];
  });
}

describe("Page Assistant — lien direct et pagination des fils", () => {
  beforeEach(() => {
    localStorage.clear();
    apiFetch.mockReset();
    state.openTarget.mockReset();
    state.openTarget.mockReturnValue(true);
    state.ragSpaceId = "rs-1";
    window.history.replaceState(null, "", "/chat");
    routeApi();
  });

  it("ouvre un fil hors des 100 plus récents par le chemin /messages/ habituel", async () => {
    state.params = new URLSearchParams(`thread=${OLD}`);
    render(<ChatPage />);
    await waitFor(() => expect(messagesCalls()).toContain(`/api/v1/chat/threads/${OLD}/messages/`));
    expect(screen.getByRole("button", { name: /Très ancienne conversation/ })).toBeInTheDocument();
    expect(screen.queryByTestId("thread-link-notice")).not.toBeInTheDocument();
    // URL synchronisée sur le fil ouvert.
    await waitFor(() => expect(window.location.search).toBe(`?thread=${OLD}`));
  });

  it("fil refusé : bandeau, aucun contenu du fil demandé, sélection courante conservée", async () => {
    state.params = new URLSearchParams(`thread=${DENIED}`);
    render(<ChatPage />);
    await waitFor(() => expect(screen.getByTestId("thread-link-notice")).toBeInTheDocument());
    expect(screen.getByTestId("thread-link-notice")).toHaveTextContent(
      "Cette conversation n’existe pas ou ne vous est plus accessible."
    );
    expect(messagesCalls()).not.toContain(`/api/v1/chat/threads/${DENIED}/messages/`);
    await waitFor(() => expect(messagesCalls()).toContain("/api/v1/chat/threads/t000/messages/"));
  });

  it("identifiant invalide : bandeau sans aucun appel de résolution", async () => {
    state.params = new URLSearchParams("thread=pas-un-uuid");
    render(<ChatPage />);
    await waitFor(() => expect(screen.getByTestId("thread-link-notice")).toBeInTheDocument());
    expect(apiFetch.mock.calls.some(([u]) => String(u).includes("pas-un-uuid"))).toBe(false);
  });

  it("fil d'un autre espace : bascule de contexte via openTarget", async () => {
    state.params = new URLSearchParams(`thread=${OTHER_SPACE}`);
    render(<ChatPage />);
    await waitFor(() =>
      expect(state.openTarget).toHaveBeenCalledWith({ workspaceId: 2, ragSpaceId: "rs-2", threadId: OTHER_SPACE })
    );
  });

  it("« Afficher plus de conversations » charge la page suivante avec before=", async () => {
    state.params = new URLSearchParams();
    render(<ChatPage />);
    const menuButton = await screen.findByRole("button", { name: /Fil 0/ });
    fireEvent.click(menuButton);
    const more = await screen.findByRole("button", { name: "Afficher plus de conversations" });
    await act(async () => {
      fireEvent.click(more);
    });
    expect(apiFetch.mock.calls.map(([u]) => String(u))).toContain(
      "/api/v1/chat/threads/?rag_space_id=rs-1&before=t099"
    );
    await waitFor(() => expect(screen.getByRole("option", { name: /Fil 100/ })).toBeInTheDocument());
    // Dernière page incomplète : plus de bouton.
    expect(screen.queryByRole("button", { name: "Afficher plus de conversations" })).not.toBeInTheDocument();
  });

  it("réponse périmée de l'espace quitté : le fil du lien reste sélectionné", async () => {
    // Observé en staging réel : la liste des fils de l'espace INITIAL revenait après la
    // bascule du lien direct et sélectionnait un fil de l'ancien espace.
    const LINKED = "0d0d0d0d-1111-4222-8333-444444444444";
    let releaseOld: (v: unknown) => void = () => {};
    const oldList = new Promise((resolve) => { releaseOld = resolve; });
    apiFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v1/chat/threads/?rag_space_id=rs-0") return oldList;
      if (url === "/api/v1/chat/threads/?rag_space_id=rs-1") return [{ id: "t-new", title: "Récent", updated_at: "2026-10-30T00:00:00Z" }];
      if (url === `/api/v1/chat/threads/${LINKED}/`) return { id: LINKED, title: "Fil du lien", rag_space_id: "rs-1", workspace_id: 1 };
      if (url.endsWith("/messages/")) return [];
      return [];
    });
    state.ragSpaceId = "rs-0";
    state.params = new URLSearchParams(`thread=${LINKED}`);
    state.openTarget.mockImplementation(({ ragSpaceId, threadId }: any) => {
      setStoredThreadIdForRagSpace(ragSpaceId, threadId);
      state.ragSpaceId = ragSpaceId;
      return true;
    });
    const view = render(<ChatPage />);
    await waitFor(() => expect(state.openTarget).toHaveBeenCalled());
    view.rerender(<ChatPage />);
    await waitFor(() => expect(messagesCalls()).toContain(`/api/v1/chat/threads/${LINKED}/messages/`));
    await act(async () => {
      releaseOld([{ id: "z-old", title: "Ancien espace", updated_at: "2026-10-31T00:00:00Z" }]);
      await oldList;
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(messagesCalls()).not.toContain("/api/v1/chat/threads/z-old/messages/");
    expect(messagesCalls()[messagesCalls().length - 1]).toBe(`/api/v1/chat/threads/${LINKED}/messages/`);
    expect(screen.queryByRole("button", { name: /Ancien espace/ })).not.toBeInTheDocument();
    expect(window.location.search).toBe(`?thread=${LINKED}`);
  });
});
