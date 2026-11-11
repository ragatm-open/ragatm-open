/**
 * Brouillon du composer : isolation du rendu et contrat fonctionnel
 *
 *
 * Défaut corrigé : le texte saisi était un état de la page de chat. Chaque
 * caractère rerendait toute la page — chaque message de l'historique, chaque
 * rendu Markdown, le panneau Sources. Sur un fil de 450 messages, une frappe
 * coûtait ~250 ms de main thread.
 *
 * Instrumentation de TEST uniquement : `SummaryMessageBody` (rendu une fois par
 * message assistant à chaque rendu de `Message`) et `SourcesPanel` sont remplacés
 * par des doublures qui comptent leurs rendus. Aucun code de production n'est
 * instrumenté.
 *
 * Les tests de contrat (envoi, vidage, changement de fil…) décrivent le
 * comportement HISTORIQUE : ils passent à l'identique avant et après la correction.
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/* -------------------------------------------------------------- mocks ---- */

const renders = { historyBody: 0, sourcesPanel: 0 };

vi.mock("@/app/chat/_components/SummaryMessageBody", () => ({
  SummaryMessageBody: ({ m }: any) => {
    renders.historyBody += 1;
    return <div data-testid="history-body">{m.text}</div>;
  },
}));

vi.mock("@/app/chat/_components/SourcesPanel", async () => {
  const React = await import("react");
  return {
    SOURCES_PANEL_ID: "chat-sources",
    SourcesPanel: React.forwardRef<HTMLElement, any>(function SourcesPanel(_props, ref) {
      renders.sourcesPanel += 1;
      return <aside ref={ref as any} id="chat-sources" />;
    }),
  };
});

vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: any) => <div>{children}</div>,
  Topbar: ({ actions }: any) => <div>{actions}</div>,
  useWorkspace: () => ({
    ragSpace: { id: "rs-1", name: "Espace" },
    ragSpaceId: "rs-1",
    currentStats: { documents: { total: 3 }, indexing: { in_progress: false } },
  }),
}));

const apiFetch = vi.fn();
const getRephrasingSuggestions = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetch: (...args: any[]) => apiFetch(...args),
  listDocuments: vi.fn(async () => ({ items: [] })),
  summarizeSourceDocumentAwait: vi.fn(),
  summaryErrorKey: (s: string) => s,
  getRephrasingSuggestions: (...args: any[]) => getRephrasingSuggestions(...args),
  cancelCorpusAnalysis: vi.fn(),
  getCorpusAnalysis: vi.fn(),
  getThreadAnalyses: vi.fn(async () => ({ analyses: [] })),
  isTerminalAnalysisStatus: () => true,
  getPreferences: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
  updateAnswerLanguage: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
}));

vi.mock("@/lib/auth", () => ({ getCsrfToken: () => null }));

import ChatPage from "@/app/chat/page";

/* ------------------------------------------------------------ fixtures --- */

const SOURCES = [{ schema_version: 3, rank: 1, filename: "a.pdf", snippet: "extrait", document_id: "d1" }];
const HISTORY: Record<string, any[]> = {
  "t-a": [
    { id: "a-u1", role: "user", content: "Question A" },
    { id: "a-a1", role: "assistant", content: "Réponse A [1].\n\nSources: [1]", sources: SOURCES },
  ],
  "t-b": [
    { id: "b-u1", role: "user", content: "Question B" },
    { id: "b-a1", role: "assistant", content: "Réponse B" },
  ],
};

function mockApi() {
  apiFetch.mockImplementation(async (url: string, init?: RequestInit) => {
    if (typeof url !== "string") return [];
    if (url.startsWith("/api/v1/chat/threads/?")) {
      return [
        { id: "t-a", title: "Fil A" },
        { id: "t-b", title: "Fil B" },
      ];
    }
    if (url === "/api/v1/chat/threads/" && init?.method === "POST") return { id: "t-new", title: null };
    const m = url.match(/\/chat\/threads\/([^/]+)\/messages\/$/);
    if (m) return HISTORY[m[1]] ?? [];
    return [];
  });
}

function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** Réponse SSE ; `hold` laisse la dernière trame en attente (flux en cours). */
function sseResponse(frames: string[], hold = false) {
  const enc = new TextEncoder();
  let i = 0;
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  return {
    release,
    response: {
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => frames.join(""),
      body: {
        getReader: () => ({
          read: async () => {
            if (i >= frames.length) return { value: undefined, done: true };
            if (hold && i === frames.length - 1) await gate;
            const value = enc.encode(frames[i]);
            i += 1;
            return { value, done: false };
          },
          cancel: async () => {},
          releaseLock: () => {},
        }),
      },
    } as unknown as Response,
  };
}

const META = { request_id: "r-1", thread_id: "t-a", log_id: "log-1" };
const settle = (ms = 40) => act(async () => void (await new Promise((r) => setTimeout(r, ms))));

const composer = () => screen.getByLabelText("Votre question") as HTMLTextAreaElement;
const sendButton = () => screen.getByRole("button", { name: /Envoyer/ });

function type(text: string) {
  // Une frappe = un événement `change` avec la valeur cumulée, comme le navigateur.
  let acc = composer().value;
  for (const ch of text) {
    acc += ch;
    fireEvent.change(composer(), { target: { value: acc } });
  }
}

/** Corps JSON des requêtes de chat effectivement émises. */
function chatBodies(fetchSpy: ReturnType<typeof vi.fn>) {
  return fetchSpy.mock.calls
    .filter(([url]) => String(url).includes("/api/v1/chat/stream/"))
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

async function mountChat() {
  render(<ChatPage />);
  await screen.findByText("Réponse A [1].\n\nSources: [1]", { normalizer: (s) => s });
  await settle();
}

/* ---------------------------------------------------------------- tests -- */

describe("Chat — isolation du brouillon", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    getRephrasingSuggestions.mockReset();
    renders.historyBody = 0;
    renders.sourcesPanel = 0;
    mockApi();
    vi.stubGlobal("fetch", vi.fn());
  });

  it("taper 20 caractères ne rerend ni l'historique ni le panneau Sources", async () => {
    await mountChat();
    const before = { ...renders };
    expect(before.historyBody).toBeGreaterThan(0); // la sentinelle est bien rendue

    type("abcdefghij klmnopqrs");

    expect(composer().value).toBe("abcdefghij klmnopqrs");
    expect(renders.historyBody).toBe(before.historyBody);
    expect(renders.sourcesPanel).toBe(before.sourcesPanel);
  });

  it("aucune requête n'est émise pendant la frappe", async () => {
    await mountChat();
    const fetchSpy = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    const apiCalls = apiFetch.mock.calls.length;
    type("une question sans envoi");
    await settle();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(apiFetch.mock.calls.length).toBe(apiCalls);
  });
});

describe("Chat — contrat du brouillon (comportement historique)", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    getRephrasingSuggestions.mockReset();
    mockApi();
  });

  it("le clic Envoyer transmet exactement le texte visible, une seule fois, puis vide le composer", async () => {
    const stream = sseResponse([sseFrame("meta", META), sseFrame("done", { ...META, status: "ok", answer: "Ok." })]);
    const fetchSpy = vi.fn(async () => stream.response);
    vi.stubGlobal("fetch", fetchSpy);
    await mountChat();

    type("abc");
    fireEvent.click(sendButton());
    await settle();

    const bodies = chatBodies(fetchSpy);
    expect(bodies).toHaveLength(1);
    expect(bodies[0].question).toBe("abc");
    expect(bodies[0].thread_id).toBe("t-a");
    expect(composer().value).toBe("");
  });

  it("Entrée envoie (texte rogné), Maj+Entrée n'envoie pas", async () => {
    const stream = sseResponse([sseFrame("meta", META), sseFrame("done", { ...META, status: "ok", answer: "Ok." })]);
    const fetchSpy = vi.fn(async () => stream.response);
    vi.stubGlobal("fetch", fetchSpy);
    await mountChat();

    type("  ligne 1");
    fireEvent.keyDown(composer(), { key: "Enter", shiftKey: true });
    await settle();
    expect(chatBodies(fetchSpy)).toHaveLength(0);
    expect(composer().value).toBe("  ligne 1");

    fireEvent.keyDown(composer(), { key: "Enter" });
    await settle();
    const bodies = chatBodies(fetchSpy);
    expect(bodies).toHaveLength(1);
    expect(bodies[0].question).toBe("ligne 1");
    expect(composer().value).toBe("");
  });

  it("un brouillon vide ou fait d'espaces n'est jamais envoyé et reste en place", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    await mountChat();

    type("   ");
    expect(sendButton()).toBeDisabled();
    fireEvent.keyDown(composer(), { key: "Enter" });
    await settle();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(composer().value).toBe("   ");
  });

  it("pendant une génération : Entrée ne part pas et le nouveau brouillon est conservé", async () => {
    const stream = sseResponse(
      [sseFrame("meta", META), sseFrame("token", { delta: "Rép" }), sseFrame("done", { ...META, status: "ok", answer: "Réponse." })],
      true,
    );
    const fetchSpy = vi.fn(async () => stream.response);
    vi.stubGlobal("fetch", fetchSpy);
    await mountChat();

    type("première");
    fireEvent.click(sendButton());
    await settle();
    expect(composer().value).toBe("");
    expect(screen.getByRole("button", { name: /Arrêter/ })).toBeInTheDocument();

    type("seconde");
    expect(sendButton()).toBeDisabled();
    fireEvent.keyDown(composer(), { key: "Enter" });
    await settle();
    expect(chatBodies(fetchSpy)).toHaveLength(1);
    expect(composer().value).toBe("seconde");

    stream.release();
    await settle();
    expect(screen.queryByRole("button", { name: /Arrêter/ })).toBeNull();
    expect(composer().value).toBe("seconde");
    expect(sendButton()).not.toBeDisabled();
  });

  it("échec réseau : le brouillon a déjà été vidé à l'envoi et l'erreur est affichée", async () => {
    const fetchSpy = vi.fn(async () => {
      throw new Error("réseau indisponible");
    });
    vi.stubGlobal("fetch", fetchSpy);
    await mountChat();

    type("question perdue");
    fireEvent.click(sendButton());
    await settle();
    expect(chatBodies(fetchSpy)).toHaveLength(1);
    expect(composer().value).toBe("");
    expect(await screen.findByText(/réseau indisponible/)).toBeInTheDocument();
  });

  it("un rerendu de la page pour une autre raison conserve le brouillon", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await mountChat();

    type("brouillon en cours");
    // Clic sur une citation : état de la page (surbrillance), sans rapport avec le brouillon.
    fireEvent.click(screen.getByRole("button", { name: "Mettre en évidence la source 1" }));
    await settle();
    expect(composer().value).toBe("brouillon en cours");
  });

  it("changement de fil : le brouillon n'est ni vidé ni envoyé (composer partagé, comme avant)", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    await mountChat();

    type("brouillon du fil A");
    fireEvent.click(screen.getByRole("button", { name: /Fil A/ }));
    fireEvent.click(await screen.findByRole("option", { name: /Fil B/ }));
    await screen.findByText("Réponse B");
    await settle();

    expect(screen.queryByText("Question A")).toBeNull();
    expect(composer().value).toBe("brouillon du fil A");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("nouvelle conversation : le brouillon est conservé, rien n'est envoyé", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    await mountChat();

    type("à reprendre");
    fireEvent.click(screen.getByRole("button", { name: /Nouvelle conversation/ }));
    await waitFor(() => expect(screen.queryByText("Question A")).toBeNull());
    expect(composer().value).toBe("à reprendre");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("commande de résumé interceptée : le composer est vidé et aucun chat n'est envoyé", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    await mountChat();

    type("résume la source 7");
    fireEvent.click(sendButton());
    await settle();
    expect(composer().value).toBe("");
    expect(chatBodies(fetchSpy)).toHaveLength(0);
    expect(screen.getByText("résume la source 7")).toBeInTheDocument();
  });

  it("clic sur une suggestion de reformulation : recopiée dans le composer, sans envoi", async () => {
    const stream = sseResponse([
      sseFrame("meta", META),
      sseFrame("done", { ...META, status: "no_answer" }),
    ]);
    const fetchSpy = vi.fn(async () => stream.response);
    vi.stubGlobal("fetch", fetchSpy);
    getRephrasingSuggestions.mockResolvedValue({ status: "ready", suggestions: ["Reformulation proposée"] });
    await mountChat();
    // Rechargements suivants laissés en vol (comme ChatStreamingStrictMode) : sans
    // serveur réel, la réconciliation effacerait l'échange non persisté.
    const base = apiFetch.getMockImplementation()!;
    apiFetch.mockImplementation(async (url: string, init?: RequestInit) =>
      /\/messages\/$/.test(String(url)) ? new Promise(() => {}) : base(url, init),
    );

    type("question introuvable");
    fireEvent.click(sendButton());
    // Désactivée tant que la génération n'est pas close (`!canAsk`) ; requêtée à
    // chaque tour : le message peut être remonté par la réconciliation du fil.
    const enabledSuggestion = () => screen.getByRole("button", { name: /Reformulation proposée/ });
    await waitFor(() => expect(enabledSuggestion()).not.toBeDisabled());
    await settle();
    fireEvent.click(enabledSuggestion());
    await settle();

    expect(composer().value).toBe("Reformulation proposée");
    expect(chatBodies(fetchSpy)).toHaveLength(1);
  });
});
