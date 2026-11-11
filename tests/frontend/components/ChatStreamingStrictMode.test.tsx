/**
 * Flux de chat de bout en bout (hors LLM/DB), page RÉELLE montée en React
 * StrictMode, trames SSE identiques à celles du backend.
 *
 * Pourquoi StrictMode est indispensable ici : c'est lui qui rejoue les updaters
 * et révélait le défaut. Un test équivalent hors StrictMode passait alors même
 * que l'utilisateur voyait la réponse en double pendant plusieurs secondes —
 * jusqu'à l'arrivée de `done`, qui écrasait le texte et masquait tout.
 *
 * On coupe donc volontairement le flux APRÈS `token` pour observer l'état que
 * l'utilisateur a réellement sous les yeux pendant la génération.
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

describe("Chat — flux streamé sous React StrictMode", () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  it("n'affiche le texte streamé QU'UNE FOIS avant l'arrivée de `done`", async () => {
    mockHistory();
    const stream = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: SOURCES }),
      sseFrame("token", { delta: TOKEN_TEXT }),
      sseFrame("done", {
        ...META,
        status: "ok",
        answer: DONE_ANSWER,
        sources: SOURCES,
        sources_contract: DONE_CONTRACT,
      }),
    ]);
    vi.stubGlobal("fetch", vi.fn(async () => stream.response));

    await mountChat();
    await submit("Quelle est la gouvernance ?");
    await settle();

    // État réellement sous les yeux de l'utilisateur pendant la génération.
    const body = document.body.textContent || "";
    expect(occurrences(body, PROSE)).toBe(1);
    expect(occurrences(body, "Sources: [1], [3]")).toBe(1);
    expect(assistantBubbles()).toBe(1);
    // Le bloc de citations est un composant distinct : une seule occurrence. Le contrat
    // n'arrive qu'avec `done` : pendant la génération, l'affichage retombe sur les [n] du
    // texte — 2 citations valides sur 3 sources transmises, jamais « 3 sources citées ».
    expect(screen.getAllByText(/2 sources citées/)).toHaveLength(1);

    stream.release();
    await settle();
  });

  it("`done.answer` remplace le texte streamé (jamais de concaténation)", async () => {
    mockHistory();
    const stream = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: SOURCES }),
      sseFrame("token", { delta: TOKEN_TEXT }),
      sseFrame("done", {
        ...META,
        status: "ok",
        answer: DONE_ANSWER,
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
    expect(occurrences(body, PROSE)).toBe(1);
    // la ligne du modèle a cédé la place à celle du backend, purgée de la référence
    // hallucinée [9]
    expect(occurrences(body, "Sources: [1], [3]")).toBe(1);
    expect(occurrences(body, "[9]")).toBe(0);
    expect(assistantBubbles()).toBe(1);
    // Le badge suit désormais le CONTRAT serveur, pas le nombre de sources retrouvées.
    expect(screen.getAllByText(/2 sources citées/)).toHaveLength(1);
  });

  it("un `done` SANS `answer` conserve le texte streamé en un seul exemplaire", async () => {
    // Chemins aide, choix de corpus, garde Excel, listing : le backend clôt le
    // flux sans snapshot. Rien ne doit être effacé ni dupliqué.
    mockHistory();
    const stream = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: [] }),
      sseFrame("token", { delta: TOKEN_TEXT }),
      sseFrame("done", { ...META, status: "ok" }),
    ]);
    vi.stubGlobal("fetch", vi.fn(async () => stream.response));

    await mountChat();
    await submit("/help");
    await settle();
    stream.release();
    await settle();

    const body = document.body.textContent || "";
    expect(occurrences(body, PROSE)).toBe(1);
    expect(occurrences(body, "Sources: [1], [3]")).toBe(1);
    expect(assistantBubbles()).toBe(1);
  });

  it("ne duplique pas le message après réconciliation avec l'historique serveur", async () => {
    mockHistory([
      { id: "m-1", role: "user", content: "Quelle est la gouvernance ?" },
      { id: "m-2", role: "assistant", content: DONE_ANSWER, sources: SOURCES, rag_query_log_id: "log-1" },
    ]);
    const stream = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: SOURCES }),
      sseFrame("token", { delta: TOKEN_TEXT }),
      sseFrame("done", {
        ...META,
        status: "ok",
        answer: DONE_ANSWER,
        sources: SOURCES,
        sources_contract: DONE_CONTRACT,
      }),
    ]);
    vi.stubGlobal("fetch", vi.fn(async () => stream.response));

    await mountChat();
    await submit("Quelle est la gouvernance ?");
    await settle();
    stream.release();
    await settle(120);

    const body = document.body.textContent || "";
    expect(occurrences(body, PROSE)).toBe(1);
    expect(occurrences(body, "Quelle est la gouvernance ?")).toBe(1);
    expect(assistantBubbles()).toBe(1);
  });
});

describe("Chat — relecture d'un fil (contrat non persisté)", () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  /**
   * Limite ASSUMÉE : `ChatMessage` ne persiste pas `sources_contract`. Après rechargement,
   * l'affichage retombe sur `_lib/citations`. Ce test verrouille le seul point qui compte :
   * une ancienne abstention — qui porte encore la ligne « Sources: [1], [2], [3] » écrite par
   * l'ancien backend — ne doit PLUS afficher de badge de citation.
   */
  it("une abstention relue depuis l'historique n'affiche pas de badge de citation", async () => {
    apiFetch.mockImplementation(async (url: string) => {
      if (typeof url !== "string") return [];
      if (url.startsWith("/api/v1/chat/threads/?")) return [{ id: "t-1", title: "Fil" }];
      if (/\/messages\/$/.test(url)) {
        return [
          { id: "m-1", role: "user", content: "Quelles différences entre Tessaly et Valteria ?" },
          {
            id: "m-2",
            role: "assistant",
            content: "Je ne sais pas.\n\nSources: [1], [2], [3]",
            sources: SOURCES,
            rag_query_log_id: "log-1",
          },
        ];
      }
      return [];
    });

    await mountChat();
    await settle();

    expect(screen.getByText(/3 sources consultées/)).toBeTruthy();
    expect(screen.queryByText(/sources citées/)).toBeNull();
  });

  it("une réponse citée relue depuis l'historique garde son badge", async () => {
    apiFetch.mockImplementation(async (url: string) => {
      if (typeof url !== "string") return [];
      if (url.startsWith("/api/v1/chat/threads/?")) return [{ id: "t-1", title: "Fil" }];
      if (/\/messages\/$/.test(url)) {
        return [
          { id: "m-1", role: "user", content: "Quelle est la gouvernance ?" },
          { id: "m-2", role: "assistant", content: DONE_ANSWER, sources: SOURCES, rag_query_log_id: "log-1" },
        ];
      }
      return [];
    });

    await mountChat();
    await settle();

    expect(screen.getByText(/2 sources citées/)).toBeTruthy();
  });
});

describe("Chat — verrou de soumission", () => {
  beforeEach(() => {
    apiFetch.mockReset();
  });

  it("deux soumissions dans le même tick ne produisent qu'un seul appel de génération", async () => {
    mockHistory();
    const stream = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: SOURCES }),
      sseFrame("token", { delta: TOKEN_TEXT }),
      sseFrame("done", {
        ...META,
        status: "ok",
        answer: DONE_ANSWER,
        sources: SOURCES,
        sources_contract: DONE_CONTRACT,
      }),
    ]);
    const fetchSpy = vi.fn(async () => stream.response);
    vi.stubGlobal("fetch", fetchSpy);

    await mountChat();
    const composer = screen.getByPlaceholderText(/Posez une question/i);
    fireEvent.change(composer, { target: { value: "Question ?" } });
    const send = screen.getByRole("button", { name: /Envoyer/i });

    // Avant le rendu suivant : `loading` vaut encore false pour les deux appels.
    fireEvent.click(send);
    fireEvent.click(send);
    await settle();

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(occurrences(document.body.textContent || "", "Question ?")).toBe(1);
    expect(assistantBubbles()).toBe(1);

    stream.release();
    await settle();
  });

  it("le verrou est libéré après la fin du flux : une seconde question part", async () => {
    mockHistory();
    const first = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: [] }),
      sseFrame("token", { delta: "Première réponse." }),
      sseFrame("done", { ...META, status: "ok", answer: "Première réponse." }),
    ]);
    const second = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: [] }),
      sseFrame("token", { delta: "Seconde réponse." }),
      sseFrame("done", { ...META, status: "ok", answer: "Seconde réponse." }),
    ]);
    const fetchSpy = vi
      .fn<[], Promise<Response>>()
      .mockResolvedValueOnce(first.response)
      .mockResolvedValueOnce(second.response);
    vi.stubGlobal("fetch", fetchSpy);

    await mountChat();
    await submit("Q1");
    first.release();
    await settle();

    await submit("Q2");
    second.release();
    await settle();

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(assistantBubbles()).toBe(2));
    const body = document.body.textContent || "";
    expect(occurrences(body, "Première réponse.")).toBe(1);
    expect(occurrences(body, "Seconde réponse.")).toBe(1);
  });

  it("le verrou est libéré après une erreur réseau", async () => {
    mockHistory();
    const ok = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: [] }),
      sseFrame("token", { delta: "Réponse après échec." }),
      sseFrame("done", { ...META, status: "ok", answer: "Réponse après échec." }),
    ]);
    const fetchSpy = vi
      .fn<[], Promise<Response>>()
      .mockRejectedValueOnce(new Error("réseau indisponible"))
      .mockResolvedValueOnce(ok.response);
    vi.stubGlobal("fetch", fetchSpy);

    await mountChat();
    await submit("Q1");
    await settle();

    await submit("Q2");
    ok.release();
    await settle();

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(occurrences(document.body.textContent || "", "Réponse après échec.")).toBe(1);
  });

  it("le verrou est libéré après un arrêt de la génération", async () => {
    mockHistory();
    const stream = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: [] }),
      sseFrame("token", { delta: "Réponse interrompue." }),
      sseFrame("done", { ...META, status: "ok", answer: "Réponse interrompue." }),
    ]);
    const next = controllableStream([
      sseFrame("meta", META),
      sseFrame("sources", { sources: [] }),
      sseFrame("token", { delta: "Réponse suivante." }),
      sseFrame("done", { ...META, status: "ok", answer: "Réponse suivante." }),
    ]);
    const fetchSpy = vi
      .fn<[], Promise<Response>>()
      .mockResolvedValueOnce(stream.response)
      .mockResolvedValueOnce(next.response);
    vi.stubGlobal("fetch", fetchSpy);

    await mountChat();
    await submit("Q1");
    await settle();

    fireEvent.click(screen.getByRole("button", { name: /Arrêter/i }));
    stream.release(); // le flux se termine côté serveur, le client a coupé
    await settle();

    await submit("Q2");
    next.release();
    await settle();

    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});
