/**
 * Chat streamé par la Gateway : fragments multiples et réponse TRONQUÉE.
 *
 * Deux comportements sont vérifiés du point de vue de l'utilisateur :
 *
 * 1. le vrai streaming affiche le texte au fil des fragments — et une seule
 *    fois (les mêmes garanties d'idempotence que le pseudo-stream, rejouées
 *    ici sous StrictMode puisque le nombre de fragments passe de 1 à N) ;
 * 2. une génération interrompue est ANNONCÉE : le texte reçu est conservé mais
 *    accompagné d'un avertissement. Sans lui, une réponse coupée en plein
 *    milieu se lit exactement comme une réponse terminée.
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/* -------------------------------------------------------------- mocks ---- */

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
vi.mock("@/lib/api", () => ({
  apiFetch: (...args: any[]) => apiFetch(...args),
  listDocuments: vi.fn(async () => ({ items: [] })),
  summarizeDocumentAwait: vi.fn(),
  summaryErrorKey: (s: string) => s,
  getRephrasingSuggestions: vi.fn(async () => []),
  cancelCorpusAnalysis: vi.fn(),
  getCorpusAnalysis: vi.fn(),
  getThreadAnalyses: vi.fn(async () => ({ analyses: [] })),
  isTerminalAnalysisStatus: () => true,
  getPreferences: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
  updateAnswerLanguage: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
}));

vi.mock("@/lib/auth", () => ({ getCsrfToken: () => null }));

import ChatPage from "@/app/chat/page";

/* ---------------------------------------------------------- trames SSE --- */

/** Réplique exacte de backend/api/v1/chat/views.py::_sse. */
function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

const PROSE = "La gouvernance est validée par le conseil.";
/**
 * Le modèle cite lui-même ses sources — dont une RÉFÉRENCE HORS BORNES ([9] alors que 3
 * sources sont transmises). Cette ligne est DANS le delta streamé.
 */
const TOKEN_TEXT = `${PROSE}\n\nSources: [1], [3], [9]`;
/** Snapshot final : le backend reconstruit la ligne depuis les citations VALIDES seules. */
const DONE_ANSWER = `${PROSE}\n\nSources: [1], [3]`;
/** Contrat des sources porté par `done` (cf. answer_contract côté backend). */
const DONE_CONTRACT = {
  retrieved_sources_count: 5,
  prompt_sources_count: 3,
  cited_source_ids: [1, 3],
  cited_sources_count: 2,
  answer_is_abstention: false,
};

const SOURCES = [
  { schema_version: 3, rank: 1, filename: "a.pdf", snippet: "…" },
  { schema_version: 3, rank: 2, filename: "b.pdf", snippet: "…" },
  { schema_version: 3, rank: 3, filename: "c.pdf", snippet: "…" },
];

const META = { request_id: "r-1", thread_id: "t-1", log_id: "log-1" };

/**
 * Flux contrôlé : chaque trame est libérée à la demande, ce qui permet
 * d'inspecter le DOM entre `token` et `done`.
 */
function controllableStream(frames: string[]) {
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
            // la dernière trame (`done`) attend l'autorisation du test
            if (i === frames.length - 1) await gate;
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

function occurrences(haystack: string, needle: string): number {
  let n = 0;
  let i = 0;
  while ((i = haystack.indexOf(needle, i)) !== -1) {
    n += 1;
    i += needle.length;
  }
  return n;
}

/** Nombre de bulles assistant portant du texte (une action « Copier » chacune). */
function assistantBubbles(): number {
  return screen.queryAllByRole("button", { name: /^Copier$/ }).length;
}

/**
 * Historique vide au chargement.
 *
 * Sans `canonical`, les rechargements suivants restent EN VOL : l'état observé
 * est alors celui que produit le flux SSE seul, sans que la réconciliation
 * serveur ne vienne le réécrire. C'est ce qu'il faut pour juger du rendu pendant
 * et juste après la génération. Un test dédié fournit `canonical` et vérifie la
 * réconciliation.
 */
function mockHistory(canonical?: any[]) {
  let calls = 0;
  apiFetch.mockImplementation(async (url: string) => {
    if (typeof url !== "string") return [];
    if (url.startsWith("/api/v1/chat/threads/?")) return [{ id: "t-1", title: "Fil" }];
    if (/\/messages\/$/.test(url)) {
      calls += 1;
      if (calls === 1) return [];
      if (canonical) return canonical;
      return new Promise(() => {});
    }
    return [];
  });
}

async function mountChat() {
  render(<ChatPage />, { reactStrictMode: true } as any);
  await screen.findByPlaceholderText(/Posez une question/i);
}

async function submit(question: string) {
  const composer = screen.getByPlaceholderText(/Posez une question/i);
  fireEvent.change(composer, { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: /Envoyer/i }));
}

const settle = (ms = 60) => act(async () => void (await new Promise((r) => setTimeout(r, ms))));

/* ---------------------------------------------------------------- tests -- */

const FRAGMENTS = ["La gouvernance ", "est validée ", "par le conseil."];

describe("Chat — streaming natif Gateway", () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  it("affiche les fragments au fil de l'eau, une seule fois chacun", async () => {
    mockHistory();
    const stream = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: SOURCES }),
      ...FRAGMENTS.map((delta) => sseFrame("token", { delta })),
      sseFrame("done", {
        ...META,
        status: "ok",
        partial: false,
        termination: "done",
        answer: PROSE,
        sources: SOURCES,
        sources_contract: DONE_CONTRACT,
      }),
    ]);
    vi.stubGlobal("fetch", vi.fn(async () => stream.response));

    await mountChat();
    await submit("Quelle est la gouvernance ?");
    await settle();

    const pendant = document.body.textContent || "";
    expect(occurrences(pendant, PROSE)).toBe(1);
    expect(assistantBubbles()).toBe(1);

    stream.release();
    await settle();

    const apres = document.body.textContent || "";
    expect(occurrences(apres, PROSE)).toBe(1);
    expect(apres).not.toMatch(/interrompue/i);
  });

  it("annonce une réponse interrompue au lieu de la présenter comme complète", async () => {
    mockHistory();
    const partielle = FRAGMENTS.slice(0, 2).join("");
    const stream = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: SOURCES }),
      ...FRAGMENTS.slice(0, 2).map((delta) => sseFrame("token", { delta })),
      sseFrame("done", {
        ...META,
        status: "partial",
        partial: true,
        termination: "stream_error",
        answer: partielle,
        sources: SOURCES,
        sources_contract: DONE_CONTRACT,
      }),
    ]);
    vi.stubGlobal("fetch", vi.fn(async () => stream.response));

    await mountChat();
    await submit("Quelle est la gouvernance ?");
    await settle();
    stream.release();
    await settle();

    const body = document.body.textContent || "";
    // Le texte déjà reçu n'est pas perdu (le rendu Markdown coupe l'espace final)…
    expect(body).toContain(partielle.trim());
    // …mais il est explicitement signalé comme incomplet.
    expect(screen.getByText(/interrompue avant la fin/i)).toBeTruthy();
  });
});
